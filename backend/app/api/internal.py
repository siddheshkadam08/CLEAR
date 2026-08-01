"""Internal stage endpoints - the Python half of the worker contract (§1.2).

The queue is a **logic-free dispatch layer**. A BullMQ worker receives a message,
POSTs it here, and translates the response into a BullMQ outcome. Every decision -
which stage runs, whether a failure is retryable, how long to back off, what runs
next - is made in Python and reported back in the response body.

That split is what keeps the queue swappable. The dispatcher could be BullMQ, arq,
Celery or a bare Redis loop; as long as it can POST a message and read
``should_retry`` and ``retry_delay_ms`` off the reply, nothing else changes.

These routes are **not** part of the public API:

* They are mounted under ``/internal`` and guarded by a shared secret compared in
  constant time. In Kubernetes they are additionally unreachable from outside the
  namespace.
* They are excluded from the public OpenAPI schema, so they do not appear in
  customer-facing documentation as if they were callable.

A stage failure is a 200 with ``status: failed``, not an HTTP error. The dispatcher
must be able to distinguish "the stage ran and failed" - where Python has already
recorded the failure and decided about retrying - from "the call itself did not get
through", which is the only case the dispatcher retries on its own initiative.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, status
from pydantic import Field

from app.core.deps import DbSession, InternalCallerDep, require_internal_caller
from app.core.enums import JobPriority, PipelineStage
from app.core.logging import get_logger
from app.schemas.common import BaseSchema, ResponseSchema

logger = get_logger(__name__)

router = APIRouter(
    prefix="/internal",
    tags=["Internal"],
    # Hidden from the public schema: these are cluster-internal, and publishing them
    # would invite calls that the shared secret would then have to reject.
    include_in_schema=False,
    # The shared-secret check is applied at the router, so a route added here later
    # cannot be left unguarded by forgetting the dependency in its signature.
    dependencies=[Depends(require_internal_caller)],
)


class StageRunRequest(BaseSchema):
    """The queue message, as the dispatcher forwards it.

    Deliberately identifiers only - never document content. Messages stay small,
    safe to log, and a re-delivered message always reads current state from the
    database rather than acting on a stale snapshot.
    """

    job_id: uuid.UUID
    contract_id: uuid.UUID
    project_id: uuid.UUID
    stage: PipelineStage
    attempt: int = Field(default=1, ge=1, le=100)
    priority: JobPriority = JobPriority.NORMAL
    #: W3C traceparent, so a stage span minutes later joins the upload's trace.
    trace: dict[str, str] = Field(default_factory=dict)
    continue_pipeline: bool = True
    options: dict[str, Any] = Field(default_factory=dict)
    #: Echoed back by the dispatcher, which keys its deduplication on it.
    #:
    #: The message round-trips: this service enqueues it, the dispatcher stores
    #: it, and the dispatcher POSTs it back here. So a field added to
    #: `StageMessage` has to be accepted here too - this schema forbids extras,
    #: and the 422 it returns looks to the dispatcher like a message it should
    #: retry and then dead-letter, rather than a contract mismatch between two
    #: halves of the same system.
    dispatch_id: str | None = Field(default=None, max_length=64)


class StageRunResponse(ResponseSchema):
    """What the dispatcher acts on.

    ``should_retry`` and ``retry_delay_ms`` are the whole contract: Python has
    already classified the error and recorded it, and the dispatcher only has to
    honour the decision.
    """

    job_id: uuid.UUID
    stage: PipelineStage
    status: str
    attempt: int
    duration_ms: int
    next_stage: PipelineStage | None = None
    artifact_ref: str | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    should_retry: bool = False
    retry_delay_ms: int = 0


@router.post(
    "/stages/{stage}/run",
    response_model=StageRunResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute one pipeline stage",
)
async def run_stage_endpoint(
    stage: PipelineStage,
    payload: StageRunRequest,
    caller: InternalCallerDep,
    db: DbSession,
) -> StageRunResponse:
    """Run one stage and report the outcome.

    Returns 200 even when the stage fails. The dispatcher needs to tell a stage
    failure - already recorded, retry decision already made - apart from a transport
    failure, and an HTTP error code would conflate the two.
    """
    from app.core.errors import ValidationError
    from app.orchestrator.queue import StageMessage
    from app.orchestrator.runner import run_stage

    # `BaseSchema` sets `use_enum_values`, so `payload.stage` arrives as a plain
    # string. Comparing by value rather than identity is therefore mandatory: `is
    # not` between a str and an enum member is *always* true, which would reject
    # every valid message.
    body_stage = PipelineStage(payload.stage)

    if body_stage is not stage:
        # The path and the body disagree. Trusting either silently could run the
        # wrong stage against a contract, so it is rejected outright.
        logger.warning(
            "stage_mismatch",
            path_stage=stage.value,
            body_stage=body_stage.value,
            job_id=str(payload.job_id),
        )
        raise ValidationError(
            f"The path stage '{stage.value}' does not match the message stage '{body_stage.value}'."
        )

    message = StageMessage(
        job_id=payload.job_id,
        contract_id=payload.contract_id,
        project_id=payload.project_id,
        stage=body_stage,
        attempt=payload.attempt,
        priority=JobPriority(payload.priority),
        trace=payload.trace,
        continue_pipeline=payload.continue_pipeline,
        options=payload.options,
        # Carried through so a stage this one dispatches next inherits nothing
        # from it - each onward dispatch mints its own id - while a redelivery
        # of *this* message keeps the id it was queued under.
        **({"dispatch_id": payload.dispatch_id} if payload.dispatch_id else {}),
    )

    outcome = await run_stage(message)
    return StageRunResponse(**outcome.to_dict())


@router.get("/stages", summary="Stages this worker serves")
async def list_worker_stages(caller: InternalCallerDep) -> dict[str, Any]:
    """Which stages this process can actually run.

    A worker pool is specialised by ``WORKER_ROLE``, and the dispatcher uses this to
    avoid routing an AI stage to a parser-only pool. A stage whose module failed to
    import is reported as unavailable rather than silently accepted and then failed.
    """
    from app.core.config import get_settings
    from app.core.enums import STAGE_ORDER
    from app.orchestrator.stages.base import registered_stages, stage_load_errors

    settings = get_settings()
    available = {s.value for s in registered_stages()}
    served = {s.value for s in stages_for_role(settings.worker_role)}

    return {
        "worker_role": settings.worker_role,
        "serves": [s.value for s in STAGE_ORDER if s.value in served & available],
        "registered": sorted(available),
        "unavailable": stage_load_errors(),
    }


@router.get("/health", summary="Worker liveness")
async def worker_health(caller: InternalCallerDep) -> dict[str, Any]:
    """Can this worker take work?

    Gated on the database and the stage handlers - a worker that cannot reach
    Postgres or has no handler for its role would accept a message and then fail it,
    which is worse than not accepting it.
    """
    from app.core.config import get_settings
    from app.db.session import database_healthy
    from app.orchestrator.stages.base import registered_stages

    settings = get_settings()
    db_ok = await database_healthy()
    served = stages_for_role(settings.worker_role)
    available = set(registered_stages())
    ready = db_ok and bool(set(served) & available)

    return {
        "status": "ok" if ready else "unavailable",
        "worker_role": settings.worker_role,
        "database": db_ok,
        "stages_ready": [s.value for s in served if s in available],
    }


#: Stage groupings a worker pool can specialise in. Parsing is IO- and CPU-heavy;
#: the AI stages are latency-bound on a provider, so they scale on different curves
#: and are worth separating.
_ROLE_STAGES: dict[str, tuple[PipelineStage, ...]] = {
    "parser": (
        PipelineStage.VALIDATION,
        PipelineStage.PARSER,
        PipelineStage.ENRICHMENT,
        PipelineStage.CLASSIFICATION,
        PipelineStage.CHUNKING,
    ),
    "ai": (
        PipelineStage.DOCPIPELINE,
        PipelineStage.EXTRACTION,
        PipelineStage.AI_EXTRACTION,
        PipelineStage.EMBEDDING,
        PipelineStage.INDEXING,
    ),
}


def stages_for_role(role: str) -> tuple[PipelineStage, ...]:
    """Stages a worker with this role serves.

    ``all`` - the default and the single-container path - serves everything.
    """
    from app.core.enums import STAGE_ORDER

    if role in {"all", "", None}:
        return tuple(STAGE_ORDER)
    return _ROLE_STAGES.get(role, tuple(STAGE_ORDER))


__all__ = ["StageRunRequest", "StageRunResponse", "router", "stages_for_role"]

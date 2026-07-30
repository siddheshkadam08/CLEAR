"""Processing job endpoints.

What a user watches while a contract processes, and what an operator uses when it
does not. Two routers:

* ``/contracts/{contract_id}/jobs`` - the job history for one contract.
* ``/jobs`` - the queue view and single-job operations.

Contract- and job-scoped routes resolve the project from the row and apply the same
membership check as the project-scoped routes, so an id leaks nothing about a
project the caller cannot see.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import (
    AccessScope,
    AccessScopeDep,
    ContractContextDep,
    CurrentUserDep,
    DbSession,
    PaginationDep,
    RequestInfoDep,
    require_system_admin,
    resolve_scope_for_project,
)
from app.core.enums import AuditAction, JobPriority, JobState, Permission, PipelineStage
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.schemas.common import MessageResponse, Paginated
from app.schemas.job import (
    JobListItem,
    JobResponse,
    PipelineHealthResponse,
    QueueStatsResponse,
    ReprocessRequest,
    StageRunResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/jobs", tags=["Processing"])
contract_jobs_router = APIRouter(prefix="/contracts/{contract_id}/jobs", tags=["Processing"])

#: States a job can be retried from. A running job is not retryable: the retry would
#: race the in-flight worker and put two writers on one contract.
_RETRYABLE_STATES = frozenset({JobState.FAILED, JobState.CANCELLED})

#: States that mean the job is over.
_TERMINAL_STATES = frozenset({JobState.READY, JobState.FAILED, JobState.CANCELLED})


# =============================================================================
# Contract-scoped
# =============================================================================
@contract_jobs_router.get(
    "",
    response_model=list[JobResponse],
    summary="Job history for a contract",
)
async def list_contract_jobs(ref: ContractContextDep, db: DbSession) -> list[JobResponse]:
    """Every processing job for one contract, newest first."""
    ref.require(Permission.JOB_READ)

    from app.repositories.processing import JobStageRunRepository, ProcessingJobRepository

    repository = ProcessingJobRepository(db)
    jobs = await repository.list_all(
        contract_id=ref.contract_id,
        project_id=ref.project_id,
        sort_by="created_at",
        sort_dir="desc",
    )
    runs = JobStageRunRepository(db)
    return [_job_response(job, stages=list(await runs.runs_for_job(job.id))) for job in jobs]


@contract_jobs_router.post(
    "/reprocess",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Reprocess a contract from a stage",
)
async def reprocess_contract(
    payload: ReprocessRequest,
    ref: ContractContextDep,
    db: DbSession,
    info: RequestInfoDep,
) -> JobResponse:
    """Re-run the pipeline from a stage.

    Stages before ``from_stage`` keep their checkpoints, so re-extracting a contract
    does not re-parse or re-OCR it - the difference between seconds and minutes on a
    long agreement (§25).
    """
    ref.require(Permission.JOB_CONTROL)
    # Re-coerced from the wire value: `use_enum_values` means these arrive as plain
    # strings, and the queue message and the runner both expect real enum members.
    from_stage = PipelineStage(payload.from_stage)
    job = await _enqueue_reprocess(
        db,
        contract_id=ref.contract_id,
        project_id=ref.project_id,
        from_stage=from_stage,
        priority=JobPriority(payload.priority),
        options={**payload.options, "force": payload.force},
        actor=ref.user,
        info=info,
    )
    logger.info(
        "contract_reprocess_requested",
        contract_id=str(ref.contract_id),
        job_id=str(job.id),
        from_stage=from_stage.value,
        force=payload.force,
        user_id=str(ref.user.id),
    )
    return _job_response(job)


# =============================================================================
# Job-scoped
# =============================================================================
@router.get("", response_model=Paginated[JobListItem], summary="Processing queue")
async def list_jobs(
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
    pagination: PaginationDep,
    project_id: Annotated[uuid.UUID | None, Query(description="Narrow to one project")] = None,
    state: Annotated[list[JobState] | None, Query(description="Filter by state")] = None,
    contract_id: Annotated[uuid.UUID | None, Query()] = None,
    failed_only: Annotated[bool, Query(description="Only failed jobs")] = False,
) -> Paginated[JobListItem]:
    """Jobs across the caller's accessible projects."""
    project_ids = await resolve_scope_for_project(project_id, scope)
    if not project_ids:
        # No accessible project is an empty page, not an error.
        return Paginated.build(items=[], page=pagination.page, size=pagination.size, total=0)

    from app.repositories.processing import ProcessingJobRepository

    repository = ProcessingJobRepository(db)
    stmt = repository.filtered_query(
        project_ids, state=state, contract_id=contract_id, failed_only=failed_only
    )
    items, total = await repository.paginate(
        stmt, page=pagination.page, size=pagination.size, sort_by="created_at", sort_dir="desc"
    )
    return Paginated.build(
        items=[_job_list_item(job) for job in items],
        page=pagination.page,
        size=pagination.size,
        total=total,
    )


@router.get("/{job_id}", response_model=JobResponse, summary="Job detail")
async def get_job(
    job_id: uuid.UUID,
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
) -> JobResponse:
    """One job with its full stage history."""
    from app.repositories.processing import ProcessingJobRepository

    job = await ProcessingJobRepository(db).get_with_runs(job_id)
    if job is None or not scope.contains(job.project_id):
        # 404 rather than 403: confirming an id exists elsewhere is itself a leak.
        raise NotFoundError("Job", job_id)
    return _job_response(job, stages=list(getattr(job, "stage_runs", None) or []))


@router.get(
    "/{job_id}/stages",
    response_model=list[StageRunResponse],
    summary="Stage runs for a job",
)
async def list_stage_runs(
    job_id: uuid.UUID,
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
) -> list[StageRunResponse]:
    """Every attempt at every stage, in order.

    Append-only: a retry adds a row rather than overwriting the failure, so the
    history of what went wrong survives the fix.
    """
    job = await _load_job(db, job_id, scope)

    from app.repositories.processing import JobStageRunRepository

    runs = await JobStageRunRepository(db).runs_for_job(job.id)
    return [_stage_response(run) for run in runs]


@router.post(
    "/{job_id}/retry",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry a failed job",
)
async def retry_job(
    job_id: uuid.UUID,
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
    info: RequestInfoDep,
) -> JobResponse:
    """Retry from the stage that failed.

    Completed stages keep their checkpoints, so this resumes rather than restarts.
    """
    job = await _load_job(db, job_id, scope)

    if job.state not in _RETRYABLE_STATES:
        raise ConflictError(
            f"This job is {job.state.value} and cannot be retried. Only failed or "
            "cancelled jobs can be retried."
        )

    resumed = await _enqueue_reprocess(
        db,
        contract_id=job.contract_id,
        project_id=job.project_id,
        from_stage=job.current_stage or PipelineStage.VALIDATION,
        priority=job.priority,
        options={"force": True},
        actor=user,
        info=info,
    )
    logger.info(
        "job_retry_requested",
        job_id=str(job_id),
        new_job_id=str(resumed.id),
        from_stage=(job.current_stage or PipelineStage.VALIDATION).value,
        user_id=str(user.id),
    )
    return _job_response(resumed)


@router.post("/{job_id}/cancel", response_model=MessageResponse, summary="Cancel a job")
async def cancel_job(
    job_id: uuid.UUID,
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
    info: RequestInfoDep,
) -> MessageResponse:
    """Cancel a queued or running job.

    The stage currently executing finishes - a worker cannot be interrupted safely
    mid-write - but nothing further is dispatched.
    """
    job = await _load_job(db, job_id, scope)

    if job.state in _TERMINAL_STATES:
        raise ConflictError(f"This job has already finished ({job.state.value}).")

    from app.repositories.processing import ProcessingJobRepository
    from app.services.audit import AuditService

    await ProcessingJobRepository(db).mark_cancelled(job.id)
    await AuditService(db).record(
        action=AuditAction.JOB_CANCEL,
        entity_type="processing_job",
        entity_id=job.id,
        project_id=job.project_id,
        user_id=user.id,
        user_email=user.email,
        ip=info.ip,
        user_agent=info.user_agent,
        route=info.route,
    )
    logger.info("job_cancelled", job_id=str(job_id), user_id=str(user.id))
    return MessageResponse(
        message="The job was cancelled. Any stage already running will finish, but no "
        "further stages will be dispatched."
    )


# =============================================================================
# Pipeline health (admin)
# =============================================================================
@router.get(
    "/-/health",
    response_model=PipelineHealthResponse,
    summary="Pipeline health",
    dependencies=[Depends(require_system_admin)],
)
async def pipeline_health(db: DbSession) -> PipelineHealthResponse:
    """Which stages have a handler, and how deep the queues are.

    A stage whose module failed to import silently truncates every job at that
    point, so it is reported here rather than discovered from a stalled queue.
    """
    from app.core.enums import STAGE_ORDER
    from app.orchestrator.queue import get_queue_client
    from app.orchestrator.stages.base import registered_stages, stage_load_errors
    from app.repositories.processing import ProcessingJobRepository

    available = {stage.value for stage in registered_stages()}
    errors = stage_load_errors()

    queue = get_queue_client()
    try:
        stats = await queue.stats()
        dead_letter = await queue.dlq_size()
    except Exception as exc:  # noqa: BLE001 - health must answer even when the queue is down
        logger.warning("queue_stats_unavailable", error=str(exc))
        stats, dead_letter = [], 0
    finally:
        await queue.close()

    repository = ProcessingJobRepository(db)
    return PipelineHealthResponse(
        registered_stages=[s.value for s in STAGE_ORDER if s.value in available],
        unavailable_stages=[s.value for s in STAGE_ORDER if s.value not in available],
        import_errors=errors,
        queues=[
            QueueStatsResponse(
                queue=entry.queue,
                waiting=entry.waiting,
                active=entry.active,
                completed=entry.completed,
                failed=entry.failed,
                delayed=entry.delayed,
            )
            for entry in stats
        ],
        dead_letter_count=dead_letter,
        in_flight=await repository.count_in_flight(),
        stalled=len(await repository.find_stalled()),
    )


# =============================================================================
# Helpers
# =============================================================================
async def _load_job(db: Any, job_id: uuid.UUID, scope: AccessScope) -> Any:
    """Load a job the caller may see, or 404."""
    from app.repositories.processing import ProcessingJobRepository

    job = await ProcessingJobRepository(db).get(job_id)
    if job is None or not scope.contains(job.project_id):
        raise NotFoundError("Job", job_id)
    return job


async def _enqueue_reprocess(
    db: Any,
    *,
    contract_id: uuid.UUID,
    project_id: uuid.UUID,
    from_stage: PipelineStage,
    priority: JobPriority,
    options: dict[str, Any],
    actor: Any,
    info: Any,
) -> Any:
    """Create a job and queue it from a stage.

    A new job row rather than resurrecting the old one: the previous attempt's stage
    history is evidence of what went wrong and must survive the retry.
    """
    from app.orchestrator.queue import StageMessage, get_queue_client
    from app.repositories.processing import ProcessingJobRepository
    from app.services.audit import AuditService

    repository = ProcessingJobRepository(db)
    job = await repository.create_job(
        contract_id=contract_id,
        project_id=project_id,
        priority=priority,
        triggered_by=actor.id,
        is_reprocess=True,
        resume_from_stage=from_stage,
    )

    await AuditService(db).record(
        action=AuditAction.JOB_RETRY,
        entity_type="processing_job",
        entity_id=job.id,
        project_id=project_id,
        user_id=actor.id,
        user_email=actor.email,
        ip=info.ip,
        user_agent=info.user_agent,
        route=info.route,
        after={"from_stage": from_stage.value, "options": options},
    )

    queue = get_queue_client()
    try:
        await queue.enqueue(
            StageMessage(
                job_id=job.id,
                contract_id=contract_id,
                project_id=project_id,
                stage=from_stage,
                priority=priority,
                options=options,
            )
        )
    finally:
        await queue.close()
    return job


def _job_response(job: Any, *, stages: list[Any] | None = None) -> JobResponse:
    return JobResponse(
        id=job.id,
        contract_id=job.contract_id,
        project_id=job.project_id,
        state=job.state,
        priority=job.priority,
        current_stage=job.current_stage,
        progress=job.progress or 0,
        retry_count=job.retry_count or 0,
        max_retries=job.max_retries or 3,
        error=job.error or None,
        profile_id=job.profile_id,
        profile_version=job.profile_version,
        created_at=job.created_at,
        updated_at=getattr(job, "updated_at", None),
        started_at=getattr(job, "started_at", None),
        finished_at=getattr(job, "finished_at", None),
        heartbeat_at=getattr(job, "heartbeat_at", None),
        duration_ms=getattr(job, "duration_ms", None),
        is_retryable=job.state in _RETRYABLE_STATES,
        stages=[_stage_response(run) for run in (stages or [])],
    )


def _job_list_item(job: Any) -> JobListItem:
    error = job.error if isinstance(job.error, dict) else {}
    return JobListItem(
        id=job.id,
        contract_id=job.contract_id,
        contract_title=getattr(getattr(job, "contract", None), "title", None),
        project_id=job.project_id,
        state=job.state,
        current_stage=job.current_stage,
        progress=job.progress or 0,
        priority=job.priority,
        retry_count=job.retry_count or 0,
        created_at=job.created_at,
        finished_at=getattr(job, "finished_at", None),
        error_message=error.get("message"),
    )


def _stage_response(run: Any) -> StageRunResponse:
    stats = run.stats if isinstance(run.stats, dict) else {}
    status_value = run.status.value if hasattr(run.status, "value") else str(run.status)
    return StageRunResponse(
        id=run.id,
        stage=run.stage,
        status=run.status,
        attempt=run.attempt,
        started_at=getattr(run, "started_at", None),
        finished_at=getattr(run, "finished_at", None),
        duration_ms=getattr(run, "duration_ms", None),
        # A skipped stage reused a valid checkpoint. Distinguishing that from "ran"
        # is what makes a two-second reprocess explainable rather than suspicious.
        reused_checkpoint=status_value == "skipped",
        error=run.error or None,
        stats=stats,
        warnings=list(getattr(run, "warnings", None) or []),
    )


__all__ = ["contract_jobs_router", "router"]

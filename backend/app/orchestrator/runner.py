"""Stage runner - executes one stage and advances the pipeline.

This is what the queue ultimately invokes (via ``POST /internal/stages/{stage}/run``)
and it owns the transactional discipline that makes the frozen rules hold:

* **Checkpoint after each success.** Artifact, status, timings and versions are
  persisted before the next stage is dispatched, so a crash between stages resumes
  rather than restarts.
* **Cleanup before re-run.** A handler's ``cleanup`` removes its prior output first,
  so a retry replaces rows instead of duplicating them.
* **Dispatch after commit.** The next stage is enqueued only once this stage's
  transaction has committed. Enqueuing inside the transaction would let a fast
  worker start before the checkpoint it depends on is visible.
* **Retry the failed stage only.** A transient failure re-queues the same stage with
  a backoff; completed stages are never re-run.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core import metrics
from app.core.config import get_settings
from app.core.enums import (
    ContractStatus,
    JobState,
    PipelineStage,
    StageStatus,
)
from app.core.errors import AppError, PipelineError
from app.core.logging import bind_context, get_logger
from app.core.telemetry import continued_span, set_span_attributes
from app.db.session import session_scope
from app.orchestrator.queue import StageMessage, get_queue_client
from app.orchestrator.stages.base import StageContext, StageResult, get_stage_handler
from app.orchestrator.workflow import ExecutionPlan, WorkflowEngine
from app.repositories.contract import ContractRepository
from app.repositories.processing import (
    DocumentArtifactRepository,
    JobStageRunRepository,
    ProcessingJobRepository,
)
from app.storage import StorageKey, get_storage

logger = get_logger(__name__)

#: Identifies which worker handled a stage, for debugging a bad pod.
WORKER_ID = f"{os.environ.get('HOSTNAME', 'local')}:{os.getpid()}"


@dataclass(slots=True)
class StageOutcome:
    """What the runner reports back to the caller (and thus to the queue)."""

    job_id: uuid.UUID
    stage: PipelineStage
    status: StageStatus
    attempt: int
    duration_ms: int
    next_stage: PipelineStage | None = None
    artifact_ref: str | None = None
    stats: dict[str, Any] = None  # type: ignore[assignment]
    warnings: list[str] = None  # type: ignore[assignment]
    error: dict[str, Any] | None = None
    #: True when the queue should retry this message.
    should_retry: bool = False
    retry_delay_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": str(self.job_id),
            "stage": self.stage.value,
            "status": self.status.value,
            "attempt": self.attempt,
            "duration_ms": self.duration_ms,
            "next_stage": self.next_stage.value if self.next_stage else None,
            "artifact_ref": self.artifact_ref,
            "stats": self.stats or {},
            "warnings": self.warnings or [],
            "error": self.error,
            "should_retry": self.should_retry,
            "retry_delay_ms": self.retry_delay_ms,
        }


async def run_stage(message: StageMessage) -> StageOutcome:
    """Execute one stage for one job.

    Called by the internal stage endpoint. Never raises for a *stage* failure - the
    outcome carries the error and whether to retry, so the dispatcher stays a thin
    shim with no error-classification logic of its own.
    """
    settings = get_settings()
    started = time.perf_counter()

    bind_context(
        job_id=str(message.job_id),
        contract_id=str(message.contract_id),
        project_id=str(message.project_id),
        stage=message.stage.value,
    )

    with continued_span(
        f"stage.{message.stage.value}",
        message.trace,
        **{
            "cip.job_id": str(message.job_id),
            "cip.contract_id": str(message.contract_id),
            "cip.project_id": str(message.project_id),
            "cip.stage": message.stage.value,
            "cip.attempt": message.attempt,
        },
    ):
        metrics.stage_started_total.labels(stage=message.stage.value).inc()

        # --- phase 1: run the stage inside its own transaction ---------------
        outcome = await _execute(message, settings.queue.max_attempts)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        outcome.duration_ms = elapsed_ms
        metrics.stage_duration_seconds.labels(stage=message.stage.value).observe(elapsed_ms / 1000)
        metrics.stage_completed_total.labels(
            stage=message.stage.value,
            outcome="succeeded"
            if outcome.status is StageStatus.SUCCEEDED
            else ("skipped" if outcome.status is StageStatus.SKIPPED else "failed"),
        ).inc()

        set_span_attributes(**{"cip.stage_status": outcome.status.value})

        # --- phase 2: dispatch the next stage, after the commit --------------
        # Outside the transaction on purpose: a worker must not be able to start the
        # next stage before this one's checkpoint is durable.
        if outcome.next_stage is not None and message.continue_pipeline:
            await _dispatch_next(message, outcome)

        if outcome.should_retry:
            await _schedule_retry(message, outcome)

        return outcome


async def _execute(message: StageMessage, max_attempts: int) -> StageOutcome:
    """Run the handler and persist its checkpoint in one transaction."""
    async with session_scope() as db:
        jobs = ProcessingJobRepository(db)
        runs = JobStageRunRepository(db)
        artifacts = DocumentArtifactRepository(db)
        contracts = ContractRepository(db)
        storage = get_storage()

        job = await jobs.get(message.job_id)
        if job is None:
            logger.error("stage_job_missing", job_id=str(message.job_id))
            return StageOutcome(
                job_id=message.job_id,
                stage=message.stage,
                status=StageStatus.FAILED,
                attempt=message.attempt,
                duration_ms=0,
                error={
                    "code": "job_not_found",
                    "message": "Job no longer exists.",
                    "retryable": False,
                },
            )

        # A cancelled or paused job stops here rather than being processed further.
        if job.state in {JobState.CANCELLED, JobState.PAUSED}:
            logger.info(
                "stage_skipped_job_not_active",
                job_id=str(job.id),
                state=str(job.state),
            )
            return StageOutcome(
                job_id=job.id,
                stage=message.stage,
                status=StageStatus.CANCELLED,
                attempt=message.attempt,
                duration_ms=0,
            )

        contract = await contracts.get(message.contract_id)
        if contract is None:
            return StageOutcome(
                job_id=job.id,
                stage=message.stage,
                status=StageStatus.FAILED,
                attempt=message.attempt,
                duration_ms=0,
                error={
                    "code": "contract_not_found",
                    "message": "Contract no longer exists.",
                    "retryable": False,
                },
            )

        profile = await _load_profile(db, job)
        workflow = WorkflowEngine(db)

        # --- plan (built once per job, then reused) --------------------------
        plan = await _resolve_plan(
            workflow=workflow, jobs=jobs, job=job, contract=contract, profile=profile
        )

        planned = plan.for_stage(message.stage)

        # --- checkpoint reuse -----------------------------------------------
        if planned is not None and not planned.should_run:
            checkpoint = await runs.checkpoint(contract.id, message.stage)
            if checkpoint is not None:
                await runs.record_skipped(
                    job_id=job.id,
                    contract_id=contract.id,
                    project_id=contract.project_id,
                    stage=message.stage,
                    checkpoint=checkpoint,
                )
                metrics.stage_checkpoint_reuse_total.labels(stage=message.stage.value).inc()
                logger.info(
                    "stage_skipped_checkpoint_current",
                    stage=message.stage.value,
                    reason=planned.reason,
                )
                next_stage = workflow.next_after(plan, message.stage)
                await _finalise_if_last(
                    jobs=jobs,
                    contracts=contracts,
                    job=job,
                    contract=contract,
                    stage=message.stage,
                    next_stage=next_stage,
                )
                return StageOutcome(
                    job_id=job.id,
                    stage=message.stage,
                    status=StageStatus.SKIPPED,
                    attempt=message.attempt,
                    duration_ms=0,
                    next_stage=next_stage,
                    artifact_ref=checkpoint.artifact_ref,
                    stats={"reused": True},
                )

        # --- prepare ---------------------------------------------------------
        handler = get_stage_handler(message.stage)
        await jobs.mark_stage_started(job.id, message.stage, worker_id=WORKER_ID)
        await jobs.update_progress(job.id, workflow.progress_for(message.stage, completed=False))

        run = await runs.start_run(
            job_id=job.id,
            contract_id=contract.id,
            project_id=contract.project_id,
            stage=message.stage,
            attempt=message.attempt,
            worker_id=WORKER_ID,
            versions=handler.versions_for(
                StageContext(
                    db=db,
                    storage=storage,
                    job=job,
                    contract=contract,
                    stage=message.stage,
                    attempt=message.attempt,
                    profile=profile,
                    options=message.options,
                )
            ),
        )

        async def report(percent: int, note: str | None = None) -> None:
            await jobs.update_progress(job.id, percent)
            if note:
                logger.info("stage_progress", stage=message.stage.value, percent=percent, note=note)

        ctx = StageContext(
            db=db,
            storage=storage,
            job=job,
            contract=contract,
            stage=message.stage,
            attempt=message.attempt,
            profile=profile,
            options=message.options,
            progress_callback=report,
        )

        # --- execute ---------------------------------------------------------
        try:
            # Idempotency: clear this stage's prior output before writing new output.
            await handler.cleanup(ctx)
            result = await handler.run(ctx)
        except AppError as exc:
            return await _handle_failure(
                db=db,
                jobs=jobs,
                runs=runs,
                contracts=contracts,
                job=job,
                contract=contract,
                run=run,
                message=message,
                exc=exc,
                retryable=exc.retryable and handler.retryable,
                max_attempts=max_attempts,
            )
        except Exception as exc:
            logger.exception("stage_unexpected_error", stage=message.stage.value)
            return await _handle_failure(
                db=db,
                jobs=jobs,
                runs=runs,
                contracts=contracts,
                job=job,
                contract=contract,
                run=run,
                message=message,
                exc=exc,
                retryable=handler.retryable,
                max_attempts=max_attempts,
            )

        # --- persist artifacts ----------------------------------------------
        primary_ref = await _persist_artifacts(
            artifacts=artifacts,
            storage=storage,
            job=job,
            contract=contract,
            stage=message.stage,
            result=result,
            versions=run.versions,
        )

        await runs.finish_run(
            run,
            status=StageStatus.SUCCEEDED,
            artifact_ref=primary_ref,
            stats=result.stats,
        )
        if result.stats:
            await jobs.accumulate_metrics(job.id, result.stats)

        # Context the classifier (and others) hand forward to later stages.
        if result.context_updates:
            merged = dict(job.execution_plan or {})
            merged.setdefault("context", {}).update(result.context_updates)
            job.execution_plan = merged
            await db.flush()
            await _apply_context_updates(db, jobs, job, contract, result.context_updates)

        await jobs.update_progress(job.id, workflow.progress_for(message.stage, completed=True))

        # --- halt (a deliberate, successful stop) ----------------------------
        if result.halt:
            logger.info("pipeline_halted", stage=message.stage.value, reason=result.halt_reason)
            await jobs.mark_failed(
                job.id,
                stage=message.stage,
                error={
                    "code": "halted",
                    "message": result.halt_reason or "Processing stopped.",
                    "retryable": False,
                    "stage": message.stage.value,
                },
            )
            await contracts.mark_processed(contract, status=ContractStatus.FAILED)
            return StageOutcome(
                job_id=job.id,
                stage=message.stage,
                status=StageStatus.SUCCEEDED,
                attempt=message.attempt,
                duration_ms=0,
                artifact_ref=primary_ref,
                stats=result.stats,
                warnings=result.warnings,
            )

        # --- advance ---------------------------------------------------------
        # Re-plan when this stage produced context that changes the plan - the
        # profile it selected is a version key for every stage after it, and the
        # plan was built before one existed.
        #
        # This tested `CLASSIFICATION`, which is retired and never dispatched, so
        # the re-plan simply stopped happening. `DOCPIPELINE` is what resolves the
        # profile now (`_resolve_profile`, emitted as `profile_id` in
        # `context_updates`), so it is the stage the plan has to be rebuilt after.
        if message.stage is PipelineStage.DOCPIPELINE and result.context_updates:
            profile = await _load_profile(db, job)
            plan = await workflow.plan(job=job, contract=contract, profile=profile)
            job.execution_plan = {**(job.execution_plan or {}), **plan.to_dict()}
            await db.flush()

        next_stage = workflow.next_after(plan, message.stage)
        await _finalise_if_last(
            jobs=jobs,
            contracts=contracts,
            job=job,
            contract=contract,
            stage=message.stage,
            next_stage=next_stage,
        )

        logger.info(
            "stage_succeeded",
            stage=message.stage.value,
            attempt=message.attempt,
            next_stage=next_stage.value if next_stage else None,
            stats=result.stats,
        )

        return StageOutcome(
            job_id=job.id,
            stage=message.stage,
            status=StageStatus.SUCCEEDED,
            attempt=message.attempt,
            duration_ms=0,
            next_stage=next_stage,
            artifact_ref=primary_ref,
            stats=result.stats,
            warnings=result.warnings,
        )


# =============================================================================
# Helpers
# =============================================================================
async def _resolve_plan(
    *,
    workflow: WorkflowEngine,
    jobs: ProcessingJobRepository,
    job: Any,
    contract: Any,
    profile: Any,
) -> ExecutionPlan:
    """Return the job's plan, building and persisting it on first use.

    Persisting it means every stage of a run shares one decision set, so a
    mid-pipeline version bump cannot change the plan underneath a running job.
    """
    stored = (job.execution_plan or {}).get("stages")
    if stored:
        from app.orchestrator.workflow import PlannedStage

        plan = ExecutionPlan(
            job_id=job.id,
            contract_id=contract.id,
            project_id=contract.project_id,
            profile_key=(job.execution_plan or {}).get("profile_key"),
            profile_version=(job.execution_plan or {}).get("profile_version"),
            branches=list((job.execution_plan or {}).get("branches") or []),
            extensions=list((job.execution_plan or {}).get("extensions") or []),
        )
        for item in stored:
            plan.stages.append(
                PlannedStage(
                    stage=PipelineStage(item["stage"]),
                    action=item.get("action", "run"),
                    reason=item.get("reason"),
                    checkpoint_id=uuid.UUID(item["checkpoint_id"])
                    if item.get("checkpoint_id")
                    else None,
                    versions=item.get("versions") or {},
                )
            )
        return plan

    plan = await workflow.plan(
        job=job,
        contract=contract,
        profile=profile,
        from_stage=job.resume_from_stage,
    )
    job.execution_plan = {**(job.execution_plan or {}), **plan.to_dict()}
    await jobs.db.flush()
    return plan


async def _load_profile(db: Any, job: Any) -> Any:
    if job.profile_id is None:
        return None
    from app.models.profile import DocumentProfile

    return await db.get(DocumentProfile, job.profile_id)


async def _persist_artifacts(
    *,
    artifacts: DocumentArtifactRepository,
    storage: Any,
    job: Any,
    contract: Any,
    stage: PipelineStage,
    result: StageResult,
    versions: dict[str, Any],
) -> str | None:
    """Write each artifact to object storage and record its pointer.

    Large payloads never enter Postgres (§7.3); the row holds path, checksum and
    versions so an incremental check is one indexed lookup.
    """
    primary_ref: str | None = None

    for index, artifact in enumerate(result.artifacts):
        existing = await artifacts.current(contract.id, artifact.kind)
        generation = (existing.generation + 1) if existing is not None else 1
        key = StorageKey.artifact(contract.project_id, contract.id, artifact.kind, generation)

        upload = await storage.put_json(
            key,
            artifact.payload,
            metadata={
                "contract_id": str(contract.id),
                "kind": artifact.kind.value,
                "stage": stage.value,
            },
        )

        await artifacts.supersede(
            contract_id=contract.id,
            project_id=contract.project_id,
            job_id=job.id,
            kind=artifact.kind,
            storage_path=key,
            checksum=upload.checksum or "",
            size_bytes=upload.size,
            versions=versions,
            summary=artifact.summary,
        )

        if index == 0:
            primary_ref = key

    return primary_ref


async def _apply_context_updates(
    db: Any,
    jobs: ProcessingJobRepository,
    job: Any,
    contract: Any,
    updates: dict[str, Any],
) -> None:
    """Promote selected stage output onto the job and contract rows.

    Classification returns a profile; pinning it on both rows is what makes a
    contract permanently linked to the profile version that processed it (§11).
    """
    profile_id = updates.get("profile_id")
    if profile_id:
        job.profile_id = uuid.UUID(str(profile_id))
        contract.profile_id = uuid.UUID(str(profile_id))
    if updates.get("profile_version"):
        job.profile_version = str(updates["profile_version"])
        contract.profile_version = str(updates["profile_version"])
    if updates.get("agreement_type"):
        contract.agreement_type = str(updates["agreement_type"])
    if updates.get("agreement_subtype"):
        contract.agreement_subtype = str(updates["agreement_subtype"])
    if updates.get("classification_confidence") is not None:
        contract.classification_confidence = updates["classification_confidence"]
    if updates.get("page_count"):
        contract.page_count = int(updates["page_count"])
    if updates.get("language"):
        contract.language = str(updates["language"])
    if updates.get("title") and not contract.title:
        contract.title = str(updates["title"])[:512]
    await db.flush()


async def _finalise_if_last(
    *,
    jobs: ProcessingJobRepository,
    contracts: ContractRepository,
    job: Any,
    contract: Any,
    stage: PipelineStage,
    next_stage: PipelineStage | None,
) -> None:
    """Mark the job READY when no stage remains."""
    if next_stage is not None:
        return

    await jobs.mark_ready(job.id)
    await contracts.mark_processed(
        contract,
        status=ContractStatus.NEEDS_REVIEW if contract.needs_review else ContractStatus.READY,
    )
    metrics.jobs_completed_total.inc()

    # Everything derived from this contract just changed, so cached dashboards,
    # searches and answers must be evicted.
    from app.core.cache import invalidate_project_cache

    await invalidate_project_cache(contract.project_id)

    from app.repositories.project import ProjectRepository

    await ProjectRepository(jobs.db).recount_contracts(contract.project_id)

    logger.info(
        "job_ready",
        job_id=str(job.id),
        contract_id=str(contract.id),
        final_stage=stage.value,
    )


async def _handle_failure(
    *,
    db: Any,
    jobs: ProcessingJobRepository,
    runs: JobStageRunRepository,
    contracts: ContractRepository,
    job: Any,
    contract: Any,
    run: Any,
    message: StageMessage,
    exc: Exception,
    retryable: bool,
    max_attempts: int,
) -> StageOutcome:
    """Record a stage failure and decide whether to retry.

    Retry the *failed stage only*; completed stages keep their checkpoints, so a
    retry resumes rather than restarts (§10.1).
    """
    settings = get_settings()

    error: dict[str, Any] = {
        "stage": message.stage.value,
        "attempt": message.attempt,
        "retryable": retryable,
        "exception": type(exc).__name__,
    }
    if isinstance(exc, AppError):
        # A deliberate failure carries a code and a message written for the user.
        error["code"] = exc.code
        error["message"] = exc.message
        if exc.details:
            error["details"] = exc.details
    else:
        # An unexpected exception's text can carry internals - a DSN, a file path -
        # and this record is surfaced on the job, so it is not echoed back. The full
        # traceback is logged separately for an operator.
        error["code"] = "internal_error"
        error["message"] = "An unexpected error occurred."
    if isinstance(exc, PipelineError) and exc.stage:
        error["stage"] = exc.stage

    # Recording the failure must not itself be able to fail silently.
    #
    # Whatever went wrong may have left the session with a poisoned transaction -
    # a constraint violation, or Postgres terminating the connection outright.
    # Writing the failure record on that session then raises
    # `PendingRollbackError`, and *that* propagates instead of the original
    # exception: the stage's real cause is replaced by a generic database error.
    # The one record that would explain the failure is the one thing the failure
    # stops from being written.
    #
    # So: try the normal path, and if it fails, roll back and write a fresh row.
    # A rollback cannot come first - `start_run` only flushed, so the whole
    # stage including its own run row is uncommitted, and rolling back would
    # discard the record we are trying to complete.
    try:
        await runs.finish_run(run, status=StageStatus.FAILED, error=error)
    except Exception as record_error:  # noqa: BLE001 - diagnostics must survive
        logger.warning(
            "stage_failure_record_retrying",
            stage=message.stage.value,
            attempt=message.attempt,
            error=str(record_error)[:200],
        )
        error.setdefault("details", {})["record_error"] = str(record_error)[:300]
        try:
            await runs.db.rollback()
            await runs.create(
                job_id=job.id,
                contract_id=message.contract_id,
                project_id=message.project_id,
                stage=message.stage,
                status=StageStatus.FAILED,
                attempt=message.attempt,
                worker_id=WORKER_ID,
                error=error,
                started_at=run.started_at,
                finished_at=datetime.now(UTC),
            )
            await runs.db.commit()
        except Exception as retry_error:  # noqa: BLE001
            # The connection is gone, not just the transaction. Log the original
            # cause so it is not lost along with it.
            logger.error(
                "stage_failure_not_recorded",
                stage=message.stage.value,
                attempt=message.attempt,
                original_error=repr(exc)[:400],
                record_error=str(retry_error)[:300],
            )

    can_retry = retryable and message.attempt < max_attempts
    if can_retry:
        metrics.stage_retry_total.labels(stage=message.stage.value).inc()

    if can_retry:
        await jobs.mark_retrying(job.id, stage=message.stage, attempt=message.attempt)
        # Exponential backoff: a provider rate limit or a database blip needs time,
        # and hammering it immediately makes the outage worse.
        delay = settings.queue.backoff_ms * (2 ** (message.attempt - 1))
        logger.warning(
            "stage_failed_retrying",
            stage=message.stage.value,
            attempt=message.attempt,
            max_attempts=max_attempts,
            delay_ms=delay,
            error_code=error["code"],
        )
        return StageOutcome(
            job_id=job.id,
            stage=message.stage,
            status=StageStatus.FAILED,
            attempt=message.attempt,
            duration_ms=0,
            error=error,
            should_retry=True,
            retry_delay_ms=delay,
        )

    await jobs.mark_failed(job.id, stage=message.stage, error=error)
    await contracts.mark_processed(contract, status=ContractStatus.FAILED)
    metrics.jobs_failed_total.labels(stage=message.stage.value).inc()

    logger.error(
        "stage_failed_terminal",
        stage=message.stage.value,
        attempt=message.attempt,
        error_code=error["code"],
        retryable=retryable,
    )

    # An unrecoverable ingestion failure is operationally actionable, so raise it as
    # an alert rather than leaving it only in the job row.
    await _raise_failure_alert(db, contract, message.stage, error)

    return StageOutcome(
        job_id=job.id,
        stage=message.stage,
        status=StageStatus.FAILED,
        attempt=message.attempt,
        duration_ms=0,
        error=error,
        should_retry=False,
    )


async def _raise_failure_alert(
    db: Any, contract: Any, stage: PipelineStage, error: dict[str, Any]
) -> None:
    try:
        from app.services.alerts import AlertService

        await AlertService(db).raise_processing_failure(contract=contract, stage=stage, error=error)
    except Exception as exc:  # noqa: BLE001 - alerting must not mask the failure
        logger.warning("failure_alert_not_raised", error=str(exc))


async def _dispatch_next(message: StageMessage, outcome: StageOutcome) -> None:
    """Queue the next stage. Runs after the transaction has committed."""
    if outcome.next_stage is None:
        return
    try:
        await get_queue_client().enqueue(
            StageMessage(
                job_id=message.job_id,
                contract_id=message.contract_id,
                project_id=message.project_id,
                stage=outcome.next_stage,
                priority=message.priority,
                trace=message.trace,
                continue_pipeline=True,
                options=message.options,
            )
        )
    except Exception as exc:  # noqa: BLE001
        # The checkpoint is durable, so the pipeline is resumable by hand; surface
        # the break rather than silently stalling.
        logger.error(
            "next_stage_dispatch_failed",
            job_id=str(message.job_id),
            next_stage=outcome.next_stage.value,
            error=str(exc),
        )
        async with session_scope() as db:
            await ProcessingJobRepository(db).mark_failed(
                message.job_id,
                stage=outcome.next_stage,
                error={
                    "code": "queue_error",
                    "message": "Could not queue the next processing stage. Retry the job.",
                    "retryable": True,
                    "stage": outcome.next_stage.value,
                },
            )


async def _schedule_retry(message: StageMessage, outcome: StageOutcome) -> None:
    """Re-queue the failed stage with backoff."""
    try:
        await get_queue_client().enqueue(
            StageMessage(
                job_id=message.job_id,
                contract_id=message.contract_id,
                project_id=message.project_id,
                stage=message.stage,
                attempt=message.attempt + 1,
                priority=message.priority,
                trace=message.trace,
                continue_pipeline=message.continue_pipeline,
                options=message.options,
            ),
            delay_ms=outcome.retry_delay_ms,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("retry_dispatch_failed", job_id=str(message.job_id), error=str(exc))
        async with session_scope() as db:
            await ProcessingJobRepository(db).mark_failed(
                message.job_id,
                stage=message.stage,
                error={
                    "code": "queue_error",
                    "message": "Could not re-queue the stage for retry.",
                    "retryable": True,
                    "stage": message.stage.value,
                },
            )


__all__ = ["WORKER_ID", "StageOutcome", "run_stage"]

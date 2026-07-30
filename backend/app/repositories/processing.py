"""Repositories for processing jobs, stage checkpoints and artifacts.

The checkpoint logic lives here because it is the mechanism behind two frozen
rules (§10.1):

* **Retry only the failed stage.** :meth:`JobStageRunRepository.checkpoint` finds
  the latest *successful* run of a stage, so a resume never re-runs completed work.
* **Idempotency.** :meth:`DocumentArtifactRepository.supersede` marks the previous
  artifact generation stale and inserts a new current one, so re-running a stage
  replaces its output rather than accumulating duplicates.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, func, or_, select, update

from app.core.enums import (
    STAGE_TO_STATE,
    ArtifactKind,
    JobPriority,
    JobState,
    PipelineStage,
    StageStatus,
)
from app.core.logging import get_logger
from app.core.versions import ComponentVersions
from app.models.processing import DocumentArtifact, JobStageRun, ProcessingJob
from app.repositories.base import ProjectScopedRepository, affected_rows

logger = get_logger(__name__)


class ProcessingJobRepository(ProjectScopedRepository[ProcessingJob]):
    model = ProcessingJob
    sortable_fields = frozenset({"created_at", "updated_at", "state", "priority", "progress"})
    default_order_by = "created_at"

    # ------------------------------------------------------------------ create
    async def create_job(
        self,
        *,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        priority: JobPriority = JobPriority.NORMAL,
        triggered_by: uuid.UUID | None = None,
        trace_context: dict[str, str] | None = None,
        is_reprocess: bool = False,
        resume_from_stage: PipelineStage | None = None,
    ) -> ProcessingJob:
        """Create a queued job. One job per contract per processing run (§10.1)."""
        return await self.create(
            contract_id=contract_id,
            project_id=project_id,
            state=JobState.QUEUED,
            priority=priority,
            progress=0,
            triggered_by=triggered_by,
            trace_context=trace_context or {},
            is_reprocess=is_reprocess,
            resume_from_stage=resume_from_stage,
            queued_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------------ lookup
    async def get_with_runs(self, job_id: uuid.UUID) -> ProcessingJob | None:
        """Load a job with its stage runs - what the Workflow Engine plans against."""
        from sqlalchemy.orm import selectinload

        stmt = (
            select(ProcessingJob)
            .where(ProcessingJob.id == job_id)
            .options(selectinload(ProcessingJob.stage_runs))
        )
        return (await self.db.execute(stmt)).unique().scalar_one_or_none()

    async def active_for_contract(self, contract_id: uuid.UUID) -> ProcessingJob | None:
        """A non-terminal job for this contract, if one exists.

        Guards against queuing a second pipeline for a document already in flight,
        which would race two writers over the same derived rows.
        """
        stmt = (
            select(ProcessingJob)
            .where(
                ProcessingJob.contract_id == contract_id,
                ProcessingJob.state.notin_([JobState.READY, JobState.FAILED, JobState.CANCELLED]),
            )
            .order_by(ProcessingJob.created_at.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def filtered_query(
        self,
        project_ids: Sequence[uuid.UUID],
        *,
        state: list[JobState] | None = None,
        stage: PipelineStage | None = None,
        contract_id: uuid.UUID | None = None,
        failed_only: bool = False,
    ) -> Select[tuple[ProcessingJob]]:
        stmt = self.query().where(ProcessingJob.project_id.in_(list(project_ids)))
        if state:
            stmt = stmt.where(ProcessingJob.state.in_(state))
        if stage:
            stmt = stmt.where(ProcessingJob.current_stage == stage)
        if contract_id:
            stmt = stmt.where(ProcessingJob.contract_id == contract_id)
        if failed_only:
            stmt = stmt.where(ProcessingJob.state == JobState.FAILED)
        return stmt

    async def state_counts(self, project_ids: Sequence[uuid.UUID]) -> dict[str, int]:
        if not project_ids:
            return {}
        stmt = (
            select(ProcessingJob.state, func.count(ProcessingJob.id))
            .where(ProcessingJob.project_id.in_(list(project_ids)))
            .group_by(ProcessingJob.state)
        )
        return {str(state): int(count) for state, count in await self.db.execute(stmt)}

    # ---------------------------------------------------------------- transitions
    async def mark_stage_started(
        self,
        job_id: uuid.UUID,
        stage: PipelineStage,
        *,
        worker_id: str | None = None,
    ) -> ProcessingJob | None:
        """Move the job into the state that corresponds to ``stage``."""
        job = await self.get(job_id)
        if job is None:
            return None

        job.state = STAGE_TO_STATE[stage]
        job.current_stage = stage
        job.heartbeat_at = datetime.now(UTC)
        job.worker_id = worker_id
        if job.started_at is None:
            job.started_at = datetime.now(UTC)
        await self.db.flush()
        return job

    async def update_progress(self, job_id: uuid.UUID, progress: int) -> None:
        """Set progress and refresh the heartbeat.

        The heartbeat is what lets the scheduler distinguish a long parse from a
        dead worker, so it is updated on every progress report.
        """
        await self.bulk_update(
            ProcessingJob.id == job_id,
            progress=max(0, min(100, progress)),
            heartbeat_at=datetime.now(UTC),
        )

    async def heartbeat(self, job_id: uuid.UUID) -> None:
        await self.bulk_update(ProcessingJob.id == job_id, heartbeat_at=datetime.now(UTC))

    async def mark_ready(self, job_id: uuid.UUID) -> None:
        await self.bulk_update(
            ProcessingJob.id == job_id,
            state=JobState.READY,
            current_stage=None,
            progress=100,
            error=None,
            finished_at=datetime.now(UTC),
        )

    async def mark_failed(
        self,
        job_id: uuid.UUID,
        *,
        stage: PipelineStage | None,
        error: dict[str, Any],
    ) -> None:
        """Terminal failure. ``resume_from_stage`` records where a retry should start."""
        await self.bulk_update(
            ProcessingJob.id == job_id,
            state=JobState.FAILED,
            current_stage=stage,
            resume_from_stage=stage,
            error=error,
            finished_at=datetime.now(UTC),
        )

    async def mark_retrying(self, job_id: uuid.UUID, *, stage: PipelineStage, attempt: int) -> None:
        await self.bulk_update(
            ProcessingJob.id == job_id,
            state=JobState.RETRYING,
            current_stage=stage,
            retry_count=attempt,
            heartbeat_at=datetime.now(UTC),
        )

    async def mark_cancelled(self, job_id: uuid.UUID) -> None:
        await self.bulk_update(
            ProcessingJob.id == job_id,
            state=JobState.CANCELLED,
            finished_at=datetime.now(UTC),
        )

    async def mark_paused(self, job_id: uuid.UUID) -> None:
        await self.bulk_update(ProcessingJob.id == job_id, state=JobState.PAUSED)

    async def mark_queued(
        self, job_id: uuid.UUID, *, resume_from_stage: PipelineStage | None = None
    ) -> None:
        """Return a job to the queue, clearing the previous failure."""
        await self.bulk_update(
            ProcessingJob.id == job_id,
            state=JobState.QUEUED,
            error=None,
            resume_from_stage=resume_from_stage,
            queued_at=datetime.now(UTC),
            finished_at=None,
        )

    async def accumulate_metrics(self, job_id: uuid.UUID, values: dict[str, Any]) -> None:
        """Merge stage metrics into the job's running totals."""
        job = await self.get(job_id)
        if job is None:
            return
        merged = dict(job.metrics or {})
        for key, value in values.items():
            if isinstance(value, int | float) and isinstance(merged.get(key), int | float):
                merged[key] = merged[key] + value
            else:
                merged[key] = value
        job.metrics = merged
        await self.db.flush()

    # ------------------------------------------------------------------- sweep
    async def find_stalled(self, *, timeout_minutes: int = 45) -> Sequence[ProcessingJob]:
        """Jobs whose worker stopped reporting.

        A crashed worker leaves a job in a running state forever; the scheduler
        reclaims these so the contract is not silently stuck.
        """
        cutoff = datetime.now(UTC) - timedelta(minutes=timeout_minutes)
        stmt = select(ProcessingJob).where(
            ProcessingJob.state.in_(
                [
                    JobState.VALIDATING,
                    JobState.PARSING,
                    JobState.ENRICHING,
                    JobState.CLASSIFYING,
                    JobState.CHUNKING,
                    JobState.AI_EXTRACTION,
                    JobState.EMBEDDING,
                    JobState.INDEXING,
                ]
            ),
            or_(
                ProcessingJob.heartbeat_at < cutoff,
                and_(
                    ProcessingJob.heartbeat_at.is_(None),
                    ProcessingJob.updated_at < cutoff,
                ),
            ),
        )
        return (await self.db.execute(stmt)).scalars().all()

    async def count_in_flight(self) -> int:
        stmt = select(func.count(ProcessingJob.id)).where(
            ProcessingJob.state.notin_([JobState.READY, JobState.FAILED, JobState.CANCELLED])
        )
        return int((await self.db.execute(stmt)).scalar() or 0)


class JobStageRunRepository(ProjectScopedRepository[JobStageRun]):
    """Stage attempts. Append-only: a retry inserts, it never mutates history."""

    model = JobStageRun
    default_order_by = "created_at"

    async def next_attempt(self, job_id: uuid.UUID, stage: PipelineStage) -> int:
        current = (
            await self.db.execute(
                select(func.coalesce(func.max(JobStageRun.attempt), 0)).where(
                    JobStageRun.job_id == job_id, JobStageRun.stage == stage
                )
            )
        ).scalar() or 0
        return int(current) + 1

    async def start_run(
        self,
        *,
        job_id: uuid.UUID,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        stage: PipelineStage,
        attempt: int | None = None,
        worker_id: str | None = None,
        queue_job_id: str | None = None,
        versions: ComponentVersions | dict[str, Any] | None = None,
    ) -> JobStageRun:
        resolved_attempt = attempt or await self.next_attempt(job_id, stage)
        payload = (
            versions.model_dump(exclude_none=True)
            if isinstance(versions, ComponentVersions)
            else (versions or {})
        )
        return await self.create(
            job_id=job_id,
            contract_id=contract_id,
            project_id=project_id,
            stage=stage,
            status=StageStatus.RUNNING,
            attempt=resolved_attempt,
            worker_id=worker_id,
            queue_job_id=queue_job_id,
            versions=payload,
            started_at=datetime.now(UTC),
        )

    async def finish_run(
        self,
        run: JobStageRun,
        *,
        status: StageStatus,
        artifact_ref: str | None = None,
        stats: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> JobStageRun:
        run.status = status
        run.artifact_ref = artifact_ref or run.artifact_ref
        run.stats = stats or run.stats
        run.error = error
        run.finished_at = datetime.now(UTC)
        if run.started_at is not None:
            run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
        await self.db.flush()
        return run

    async def checkpoint(self, contract_id: uuid.UUID, stage: PipelineStage) -> JobStageRun | None:
        """The latest successful run of ``stage`` for this contract.

        Keyed by contract rather than job on purpose: a reprocess creates a new job
        but must still be able to reuse the parse from the previous one - that is
        what makes "regenerate only affected stages" possible across runs (§10.1).
        """
        stmt = (
            select(JobStageRun)
            .where(
                JobStageRun.contract_id == contract_id,
                JobStageRun.stage == stage,
                JobStageRun.status == StageStatus.SUCCEEDED,
            )
            .order_by(JobStageRun.finished_at.desc().nullslast(), JobStageRun.attempt.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def checkpoints(self, contract_id: uuid.UUID) -> dict[PipelineStage, JobStageRun]:
        """Every stage's checkpoint for a contract, in one query."""
        stmt = (
            select(JobStageRun)
            .where(
                JobStageRun.contract_id == contract_id,
                JobStageRun.status == StageStatus.SUCCEEDED,
            )
            .order_by(JobStageRun.attempt.asc())
        )
        result: dict[PipelineStage, JobStageRun] = {}
        for run in (await self.db.execute(stmt)).scalars().all():
            # Later attempts overwrite earlier ones, leaving the newest per stage.
            result[run.stage] = run
        return result

    async def record_skipped(
        self,
        *,
        job_id: uuid.UUID,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        stage: PipelineStage,
        checkpoint: JobStageRun,
    ) -> JobStageRun:
        """Record that a stage was skipped because its checkpoint was reused.

        Written rather than silently omitted so the stage timeline shows *why* a
        stage took no time on a reprocess.
        """
        return await self.create(
            job_id=job_id,
            contract_id=contract_id,
            project_id=project_id,
            stage=stage,
            status=StageStatus.SKIPPED,
            attempt=await self.next_attempt(job_id, stage),
            artifact_ref=checkpoint.artifact_ref,
            versions=checkpoint.versions,
            stats={"reused_run_id": str(checkpoint.id)},
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            duration_ms=0,
        )

    async def runs_for_job(self, job_id: uuid.UUID) -> Sequence[JobStageRun]:
        stmt = (
            select(JobStageRun)
            .where(JobStageRun.job_id == job_id)
            .order_by(JobStageRun.created_at.asc())
        )
        return (await self.db.execute(stmt)).scalars().all()

    async def stage_durations(self, project_ids: Sequence[uuid.UUID]) -> dict[str, float]:
        """Mean successful duration per stage, in milliseconds."""
        if not project_ids:
            return {}
        stmt = (
            select(JobStageRun.stage, func.avg(JobStageRun.duration_ms))
            .where(
                JobStageRun.project_id.in_(list(project_ids)),
                JobStageRun.status == StageStatus.SUCCEEDED,
                JobStageRun.duration_ms.isnot(None),
            )
            .group_by(JobStageRun.stage)
        )
        return {str(stage): float(avg or 0) for stage, avg in await self.db.execute(stmt)}


class DocumentArtifactRepository(ProjectScopedRepository[DocumentArtifact]):
    """Artifact pointers. One current artifact per (contract, kind)."""

    model = DocumentArtifact
    default_order_by = "created_at"

    async def current(self, contract_id: uuid.UUID, kind: ArtifactKind) -> DocumentArtifact | None:
        stmt = select(DocumentArtifact).where(
            DocumentArtifact.contract_id == contract_id,
            DocumentArtifact.kind == kind,
            DocumentArtifact.is_current.is_(True),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def current_map(self, contract_id: uuid.UUID) -> dict[ArtifactKind, DocumentArtifact]:
        stmt = select(DocumentArtifact).where(
            DocumentArtifact.contract_id == contract_id,
            DocumentArtifact.is_current.is_(True),
        )
        return {row.kind: row for row in (await self.db.execute(stmt)).scalars().all()}

    async def supersede(
        self,
        *,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        job_id: uuid.UUID | None,
        kind: ArtifactKind,
        storage_path: str,
        checksum: str,
        size_bytes: int | None = None,
        versions: ComponentVersions | dict[str, Any] | None = None,
        summary: dict[str, Any] | None = None,
    ) -> DocumentArtifact:
        """Record a new artifact generation, retiring the previous one.

        Supersede rather than overwrite: the partial unique index guarantees one
        current row per kind, while earlier generations stay queryable so an
        extraction produced last month remains auditable (§25).
        """
        existing = await self.current(contract_id, kind)
        generation = (existing.generation + 1) if existing is not None else 1

        if existing is not None:
            existing.is_current = False
            # Flush before inserting the replacement, or the partial unique index
            # would see two current rows within the same statement batch.
            await self.db.flush()

        payload = (
            versions.model_dump(exclude_none=True)
            if isinstance(versions, ComponentVersions)
            else (versions or {})
        )

        return await self.create(
            contract_id=contract_id,
            project_id=project_id,
            job_id=job_id,
            kind=kind,
            storage_path=storage_path,
            checksum=checksum,
            size_bytes=size_bytes,
            versions=payload,
            summary=summary or {},
            generation=generation,
            is_current=True,
        )

    async def invalidate_from_stage(self, contract_id: uuid.UUID, stage: PipelineStage) -> int:
        """Retire artifacts produced by ``stage`` and everything downstream.

        Called when a reprocess starts: leaving a stale ``chunks.json`` current while
        re-parsing would let a later stage read output from a document version that
        no longer exists.
        """
        from app.core.enums import STAGE_ARTIFACTS, stages_from

        kinds: list[ArtifactKind] = []
        for downstream in stages_from(stage):
            kinds.extend(STAGE_ARTIFACTS.get(downstream, ()))
        if not kinds:
            return 0

        result = await self.db.execute(
            update(DocumentArtifact)
            .where(
                DocumentArtifact.contract_id == contract_id,
                DocumentArtifact.kind.in_(kinds),
                DocumentArtifact.is_current.is_(True),
            )
            .values(is_current=False)
        )
        await self.db.flush()
        count = affected_rows(result)
        if count:
            logger.info(
                "artifacts_invalidated",
                contract_id=str(contract_id),
                from_stage=stage.value,
                count=count,
            )
        return count


__all__ = [
    "DocumentArtifactRepository",
    "JobStageRunRepository",
    "ProcessingJobRepository",
]

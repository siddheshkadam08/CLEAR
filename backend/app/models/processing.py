"""Processing job, per-stage checkpoints and artifact pointers.

The orchestrator's durable state lives here:

* :class:`ProcessingJob` - one row per contract, holding the state machine.
* :class:`JobStageRun` - one row per stage *attempt*. The latest successful run
  for a stage is that stage's **checkpoint**: it records the artifact reference
  and the exact component versions used, which is what lets the Workflow Engine
  resume mid-pipeline and skip stages whose inputs and versions are unchanged.
* :class:`DocumentArtifact` - the pointer to a stage output in object storage.
  Large payloads (CDM, chunks) never enter Postgres; the row holds path,
  checksum and versions so incremental checks are a single indexed lookup.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    ArtifactKind,
    JobPriority,
    JobState,
    PipelineStage,
    StageStatus,
)
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import pg_enum

if TYPE_CHECKING:
    from app.models.contract import Contract


class ProcessingJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One processing job per contract (§10.1).

    ``state`` is the single source of truth for pipeline progress; transitions are
    validated by the orchestrator, never written ad hoc.
    """

    __tablename__ = "processing_jobs"

    contract_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    state: Mapped[JobState] = mapped_column(
        pg_enum(JobState, "job_state"),
        nullable=False,
        default=JobState.QUEUED,
        server_default=JobState.QUEUED.value,
        index=True,
    )
    priority: Mapped[JobPriority] = mapped_column(
        pg_enum(JobPriority, "job_priority"),
        nullable=False,
        default=JobPriority.NORMAL,
        server_default=JobPriority.NORMAL.value,
    )
    current_stage: Mapped[PipelineStage | None] = mapped_column(
        pg_enum(PipelineStage, "pipeline_stage"), nullable=True
    )
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")

    #: Structured failure detail: ``{stage, code, message, retryable, attempt,
    #: diagnostics}``. Kept as JSONB so a parser diagnostic dump and an LLM schema
    #: violation can both be represented without extra columns.
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    #: The stage the pipeline should resume from. Set when a job is retried so a
    #: resume never re-runs completed work (§10.1 frozen rules).
    resume_from_stage: Mapped[PipelineStage | None] = mapped_column(
        pg_enum(PipelineStage, "pipeline_stage"), nullable=True
    )

    #: Profile chosen by classification, pinned for this run.
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("document_profiles.id", ondelete="SET NULL"), nullable=True
    )
    profile_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: The execution plan the Workflow Engine produced, kept for audit and for
    #: answering "why did this job skip chunking?" after the fact.
    execution_plan: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: W3C ``traceparent`` captured at enqueue time so stage spans running minutes
    #: later still join the upload's trace.
    trace_context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    #: Cumulative cost/usage for the job, aggregated from stage runs.
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Heartbeat from the worker currently holding the job - drives stuck-job
    #: detection by the scheduler.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    is_reprocess: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    contract: Mapped[Contract] = relationship("Contract", back_populates="jobs")
    stage_runs: Mapped[list[JobStageRun]] = relationship(
        "JobStageRun",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobStageRun.created_at",
        lazy="selectin",
    )
    artifacts: Mapped[list[DocumentArtifact]] = relationship(
        "DocumentArtifact",
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="noload",
    )

    __table_args__ = (
        Index("ix_processing_jobs_project_state", "project_id", "state"),
        Index("ix_processing_jobs_state_created", "state", "created_at"),
        # Stuck-job sweep: only running jobs have a meaningful heartbeat.
        Index(
            "ix_processing_jobs_heartbeat",
            "heartbeat_at",
            postgresql_where=text(
                "state IN ('VALIDATING','PARSING','ENRICHING','CLASSIFYING',"
                "'CHUNKING','AI_EXTRACTION','EMBEDDING','INDEXING')"
            ),
        ),
        CheckConstraint("progress >= 0 AND progress <= 100", name="progress_range"),
    )

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def latest_run(self, stage: PipelineStage) -> JobStageRun | None:
        """Most recent attempt at ``stage``, successful or not."""
        runs = [run for run in self.stage_runs if run.stage == stage]
        return max(runs, key=lambda r: r.attempt, default=None)

    def checkpoint(self, stage: PipelineStage) -> JobStageRun | None:
        """The latest *successful* run of ``stage`` - its resumable checkpoint."""
        runs = [
            run
            for run in self.stage_runs
            if run.stage == stage and run.status is StageStatus.SUCCEEDED
        ]
        return max(runs, key=lambda r: r.attempt, default=None)


class JobStageRun(Base, UUIDPrimaryKeyMixin):
    """One attempt at one stage. Also the stage's checkpoint record.

    Kept append-only: a retry inserts a new row with an incremented ``attempt``
    rather than mutating the failed one, so the full processing history of a
    contract is reconstructible for audit.
    """

    __tablename__ = "job_stage_runs"

    job_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("processing_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    stage: Mapped[PipelineStage] = mapped_column(
        pg_enum(PipelineStage, "pipeline_stage"), nullable=False
    )
    status: Mapped[StageStatus] = mapped_column(
        pg_enum(StageStatus, "stage_status"),
        nullable=False,
        default=StageStatus.PENDING,
        server_default=StageStatus.PENDING.value,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    #: Storage path of the primary artifact this run produced.
    artifact_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: Component versions in effect for this run. Compared against the current
    #: registry to decide whether the checkpoint can be reused (§25).
    versions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Stage-specific counters (pages parsed, chunks created, tokens spent).
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    queue_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    job: Mapped[ProcessingJob] = relationship("ProcessingJob", back_populates="stage_runs")

    __table_args__ = (
        UniqueConstraint("job_id", "stage", "attempt", name="uq_job_stage_runs_job_stage_attempt"),
        Index("ix_job_stage_runs_job_stage", "job_id", "stage", "attempt"),
        # Checkpoint lookup: "latest successful run of this stage for this contract".
        Index(
            "ix_job_stage_runs_checkpoint",
            "contract_id",
            "stage",
            postgresql_where=text("status = 'succeeded'"),
        ),
        Index("ix_job_stage_runs_stage_status", "stage", "status"),
    )


class DocumentArtifact(Base, UUIDPrimaryKeyMixin):
    """Pointer to a stage output held in object storage.

    Every stage emits at least one artifact, and each artifact is a reusable
    checkpoint (§10.1). Only the pointer and light metadata live in Postgres.
    """

    __tablename__ = "document_artifacts"

    contract_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("processing_jobs.id", ondelete="SET NULL"), nullable=True
    )

    kind: Mapped[ArtifactKind] = mapped_column(
        pg_enum(ArtifactKind, "artifact_kind"), nullable=False
    )
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_type: Mapped[str] = mapped_column(
        String(128), nullable=False, default="application/json", server_default="application/json"
    )

    versions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Small summary kept inline so listing artifacts does not require reading
    #: object storage (page count, chunk count, item counts).
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    #: Generation counter. A regenerated artifact supersedes rather than
    #: overwrites, so an earlier extraction remains auditable.
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    job: Mapped[ProcessingJob | None] = relationship("ProcessingJob", back_populates="artifacts")

    __table_args__ = (
        # Exactly one current artifact per (contract, kind) - the uniqueness that
        # makes stage re-runs idempotent instead of accumulating duplicates.
        Index(
            "uq_document_artifacts_current",
            "contract_id",
            "kind",
            unique=True,
            postgresql_where=text("is_current = true"),
        ),
        Index("ix_document_artifacts_contract_kind", "contract_id", "kind", "generation"),
        Index("ix_document_artifacts_project_kind", "project_id", "kind"),
    )


__all__ = ["DocumentArtifact", "JobStageRun", "ProcessingJob"]

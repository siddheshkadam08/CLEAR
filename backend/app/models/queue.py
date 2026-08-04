"""The stage queue, when the dispatcher is Postgres rather than BullMQ.

One row is one *dispatch decision*: "run this stage for this job". Workers claim
rows with ``FOR UPDATE SKIP LOCKED``, which is the whole reason this can be a
table rather than a broker - several workers select from the same rows and
Postgres hands each of them a disjoint set instead of making them queue behind
one another.

Deliberately **not** :class:`~app.models.processing.JobStageRun`. That table is
the audit record of every attempt and grows without bound; using it as the queue
would make the claim query scan history to find work, and its ``attempt`` column
already means something else. This table stays small - rows leave it once they
are ``done``.

``claimed`` is a lease rather than a lock. The claiming transaction commits
before the stage executes, because a stage can run for minutes and holding the
transaction open would pin a connection and defeat ``SKIP LOCKED`` entirely.
Recovery is by timeout: the scheduler sweep returns rows whose ``claimed_at`` has
aged past the lease.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import JobPriority, PipelineStage, StageQueueState
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import pg_enum


class StageQueueEntry(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One queued stage execution."""

    __tablename__ = "stage_queue"

    #: Identifies one dispatch decision, and is what makes deduplication mean the
    #: right thing.
    #:
    #: The message carries this from :class:`~app.orchestrator.queue.StageMessage`,
    #: where it is generated once per decision. A driver retrying the same payload
    #: sends the same id and collapses onto the existing row; asking for the stage
    #: again builds a new message with a new id and therefore runs. That is the
    #: distinction BullMQ's `(job, stage, attempt)` key got wrong - it also
    #: collapsed a *deliberate* re-run onto the run that had already finished.
    dispatch_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)

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
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    stage: Mapped[PipelineStage] = mapped_column(
        pg_enum(PipelineStage, "pipeline_stage"), nullable=False
    )
    #: Ordering column. Native Postgres enums sort by declaration order, and
    #: ``JobPriority`` declares high → normal → low, so ``ORDER BY priority`` is
    #: already correct - see the note on that enum.
    priority: Mapped[JobPriority] = mapped_column(
        pg_enum(JobPriority, "job_priority"),
        nullable=False,
        default=JobPriority.NORMAL,
        server_default=JobPriority.NORMAL.value,
    )

    #: The whole ``StageMessage``, serialised by its own ``to_payload``. Storing the
    #: message rather than columns per field means the queue cannot drift from the
    #: message shape when a field is added.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )

    state: Mapped[StageQueueState] = mapped_column(
        pg_enum(StageQueueState, "stage_queue_state"),
        nullable=False,
        default=StageQueueState.PENDING,
        server_default=StageQueueState.PENDING.value,
    )

    #: When this row becomes claimable. Serves both the enqueue-time ``delay_ms``
    #: and retry backoff, so a delayed job and a backed-off job are the same thing
    #: to the claim query and there is no separate delay tier to sweep.
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        # The claim query's only access path. Partial, because the table is mostly
        # `done` rows between sweeps and a full index would carry them for nothing.
        Index(
            "ix_stage_queue_claimable",
            "priority",
            "available_at",
            postgresql_where=text("state = 'pending'"),
        ),
        # Reclaim scans by lease age; also partial, for the same reason.
        Index(
            "ix_stage_queue_claimed_at",
            "claimed_at",
            postgresql_where=text("state = 'claimed'"),
        ),
        Index("ix_stage_queue_state_stage", "state", "stage"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<StageQueueEntry {self.stage} {self.state} "
            f"attempt={self.attempt} dispatch={self.dispatch_id}>"
        )

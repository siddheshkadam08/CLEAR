"""Processing job and stage-run schemas.

The job view is what a user watches while a contract processes, so it carries the
things they actually ask about: which stage is running, how far through, what failed
and whether retrying is worth their time.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.core.enums import JobPriority, JobState, PipelineStage, StageStatus
from app.schemas.common import BaseSchema, ResponseSchema


class StageRunResponse(ResponseSchema):
    """One attempt at one stage. Append-only: a retry adds a row, never mutates."""

    id: uuid.UUID
    stage: PipelineStage
    status: StageStatus
    attempt: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    #: Present when the stage reused a version-compatible checkpoint instead of
    #: re-running (§25). Distinguishing "skipped" from "ran" is what makes a fast
    #: reprocess explainable rather than suspicious.
    reused_checkpoint: bool = False
    error: dict[str, Any] | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class JobResponse(ResponseSchema):
    """A processing job."""

    id: uuid.UUID
    contract_id: uuid.UUID
    project_id: uuid.UUID
    state: JobState
    priority: JobPriority
    current_stage: PipelineStage | None = None
    progress: int = 0
    retry_count: int = 0
    max_retries: int = 3
    error: dict[str, Any] | None = None
    profile_id: uuid.UUID | None = None
    profile_version: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    duration_ms: int | None = None

    #: Whether a retry could plausibly help. A validation failure on a corrupt file
    #: will fail identically every time, and offering a retry button for it wastes
    #: the user's time.
    is_retryable: bool = False
    stages: list[StageRunResponse] = Field(default_factory=list)


class JobListItem(ResponseSchema):
    """Row in the processing queue view."""

    id: uuid.UUID
    contract_id: uuid.UUID
    contract_title: str | None = None
    project_id: uuid.UUID
    state: JobState
    current_stage: PipelineStage | None = None
    progress: int = 0
    priority: JobPriority
    retry_count: int = 0
    created_at: datetime
    finished_at: datetime | None = None
    error_message: str | None = None


class ReprocessRequest(BaseSchema):
    """Re-run a contract from a stage."""

    from_stage: PipelineStage = Field(
        default=PipelineStage.VALIDATION,
        description="Stage to restart from. Every stage after it runs again; earlier "
        "stages keep their checkpoints.",
    )
    #: Ignore version-compatible checkpoints and genuinely re-run. Without it a
    #: reprocess of unchanged inputs is a no-op, which is not what someone clicking
    #: "reprocess" expects.
    force: bool = Field(
        default=True,
        description="Re-run even when a valid checkpoint exists.",
    )
    priority: JobPriority = JobPriority.NORMAL
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Stage options, e.g. {'only_clauses': ['limitation_of_liability']}.",
    )


class QueueStatsResponse(ResponseSchema):
    """Queue depth per stage, for the admin view."""

    queue: str
    waiting: int = 0
    active: int = 0
    completed: int = 0
    failed: int = 0
    delayed: int = 0


class PipelineHealthResponse(ResponseSchema):
    """Whether the pipeline can actually run.

    Surfaces stage handlers that failed to import: a stage with no handler silently
    truncates every job at that point, and finding that out from a stalled queue is
    far worse than reading it here.
    """

    registered_stages: list[str] = Field(default_factory=list)
    unavailable_stages: list[str] = Field(default_factory=list)
    import_errors: dict[str, str] = Field(default_factory=dict)
    queues: list[QueueStatsResponse] = Field(default_factory=list)
    dead_letter_count: int = 0
    in_flight: int = 0
    stalled: int = 0


__all__ = [
    "JobListItem",
    "JobResponse",
    "PipelineHealthResponse",
    "QueueStatsResponse",
    "ReprocessRequest",
    "StageRunResponse",
]

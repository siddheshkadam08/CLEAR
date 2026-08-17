"""Workflow Engine - the decision plane (§10.2).

Answers **what** should run. Given a job and its checkpoints it produces an
:class:`ExecutionPlan`: the ordered stages to execute, which may be skipped because
a version-compatible checkpoint already exists, and which conditional branches
apply (OCR for a scanned PDF, extra review steps a profile requests).

It never processes a document. It reads state and emits a plan; the Execution
Engine carries it out. That separation is what lets the decision logic stay a small
testable cluster while the execution side scales out.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    STAGE_DEPENDENCIES,
    STAGE_ORDER,
    FileType,
    JobState,
    PipelineStage,
    StageStatus,
    stage_index,
    stage_position,
    stages_from,
)
from app.core.errors import InvalidStateTransitionError, PipelineError
from app.core.logging import get_logger
from app.core.versions import ComponentVersions, current_versions_for_stage
from app.models.contract import Contract
from app.models.processing import JobStageRun, ProcessingJob
from app.models.profile import DocumentProfile
from app.repositories.processing import JobStageRunRepository

logger = get_logger(__name__)


@dataclass(slots=True)
class PlannedStage:
    """One stage in an execution plan."""

    stage: PipelineStage
    #: ``run`` executes the handler; ``skip`` reuses an existing checkpoint.
    action: str = "run"
    reason: str | None = None
    #: The checkpoint being reused, when ``action == "skip"``.
    checkpoint_id: uuid.UUID | None = None
    #: Versions this stage will stamp on its checkpoint.
    versions: dict[str, Any] = field(default_factory=dict)

    @property
    def should_run(self) -> bool:
        return self.action == "run"


@dataclass(slots=True)
class ExecutionPlan:
    """The Workflow Engine's output.

    Carries the full stage list including skips, so the UI can show a complete
    timeline and explain why a reprocess finished in seconds.
    """

    job_id: uuid.UUID
    contract_id: uuid.UUID
    project_id: uuid.UUID
    stages: list[PlannedStage] = field(default_factory=list)
    #: Conditional branches the plan activates, e.g. ``["ocr"]``.
    branches: list[str] = field(default_factory=list)
    #: Optional stages a profile adds (human review, compliance review).
    extensions: list[str] = field(default_factory=list)
    profile_key: str | None = None
    profile_version: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def next_stage(self) -> PipelineStage | None:
        """The first stage that must actually run."""
        return next((item.stage for item in self.stages if item.should_run), None)

    @property
    def runnable(self) -> list[PipelineStage]:
        return [item.stage for item in self.stages if item.should_run]

    @property
    def skipped(self) -> list[PipelineStage]:
        return [item.stage for item in self.stages if not item.should_run]

    def for_stage(self, stage: PipelineStage) -> PlannedStage | None:
        return next((item for item in self.stages if item.stage is stage), None)

    def to_dict(self) -> dict[str, Any]:
        """Serialise onto ``processing_jobs.execution_plan`` for audit."""
        return {
            "stages": [
                {
                    "stage": item.stage.value,
                    "action": item.action,
                    "reason": item.reason,
                    "checkpoint_id": str(item.checkpoint_id) if item.checkpoint_id else None,
                    "versions": item.versions,
                }
                for item in self.stages
            ],
            "branches": self.branches,
            "extensions": self.extensions,
            "profile_key": self.profile_key,
            "profile_version": self.profile_version,
            "notes": self.notes,
        }


class WorkflowEngine:
    """Builds execution plans. Makes no business changes of its own."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.runs = JobStageRunRepository(db)

    # =========================================================================
    # Planning
    # =========================================================================
    async def plan(
        self,
        *,
        job: ProcessingJob,
        contract: Contract,
        profile: DocumentProfile | None = None,
        from_stage: PipelineStage | None = None,
        force: bool = False,
    ) -> ExecutionPlan:
        """Build the plan for a job.

        ``from_stage`` restricts the plan to that stage and everything after it -
        the reprocess entry point. ``force`` disables checkpoint reuse, for when an
        operator wants a genuine clean run.
        """
        plan = ExecutionPlan(
            job_id=job.id,
            contract_id=contract.id,
            project_id=contract.project_id,
            profile_key=profile.key if profile else None,
            profile_version=profile.version if profile else None,
        )

        # Which stages are in scope at all.
        scope = list(stages_from(from_stage)) if from_stage else list(STAGE_ORDER)

        # Existing checkpoints, keyed by stage. Loaded by contract, not job, so a
        # reprocess can reuse the parse from an earlier run (§10.1).
        checkpoints = await self.runs.checkpoints(contract.id)

        # Anything before the scope must already have succeeded, or the plan is
        # invalid - a resume cannot start at embedding if chunking never ran.
        if from_stage is not None:
            self._validate_prerequisites(from_stage, checkpoints)

        # Once any stage is going to run, everything after it must run too.
        #
        # `_plan_stage` judges a stage only against its own versions, which is
        # correct in isolation and wrong in sequence: a checkpoint says "this
        # output is current for these versions", not "the input it was computed
        # from still exists". When an upstream stage re-runs it replaces that
        # input, and the downstream checkpoint is then describing rows that have
        # been deleted.
        #
        # Observed on a live retry: the LLM model had changed, so `docpipeline`
        # and `extraction` re-ran with `version_changed:model_name` and rebuilt
        # the chunks. `embedding` compared only its own versions - the embedding
        # model had not changed - skipped as `checkpoint_current`, and the job
        # finished READY with **zero** embeddings, because the vectors it was
        # reusing belonged to chunks that no longer existed. The contract was
        # complete, healthy-looking and invisible to every semantic search.
        upstream_reruns = False
        for stage in scope:
            planned = self._plan_stage(
                stage=stage,
                contract=contract,
                profile=profile,
                checkpoint=checkpoints.get(stage),
                force=force,
            )
            if upstream_reruns and planned.action == "skip":
                planned = PlannedStage(
                    stage=stage,
                    action="run",
                    reason="upstream_rerun",
                    versions=planned.versions,
                )
            upstream_reruns = upstream_reruns or planned.action == "run"
            plan.stages.append(planned)

        plan.branches = self._resolve_branches(contract)
        plan.extensions = list(profile.workflow_extensions or []) if profile else []

        if not plan.runnable:
            plan.notes.append("Every stage has a current checkpoint; nothing needs to run.")

        logger.info(
            "execution_plan_built",
            job_id=str(job.id),
            contract_id=str(contract.id),
            run=[s.value for s in plan.runnable],
            skip=[s.value for s in plan.skipped],
            branches=plan.branches,
        )
        return plan

    def _plan_stage(
        self,
        *,
        stage: PipelineStage,
        contract: Contract,
        profile: DocumentProfile | None,
        checkpoint: JobStageRun | None,
        force: bool,
    ) -> PlannedStage:
        """Decide whether one stage runs or reuses its checkpoint."""
        from app.orchestrator.stages.base import get_stage_handler

        current = self._versions_for(stage, profile)
        serialised = current.model_dump(exclude_none=True)

        if force:
            return PlannedStage(stage=stage, action="run", reason="forced", versions=serialised)

        if checkpoint is None:
            return PlannedStage(
                stage=stage, action="run", reason="no_checkpoint", versions=serialised
            )

        try:
            handler = get_stage_handler(stage)
        except PipelineError:
            # No handler registered yet (a stage still being built): plan to run it
            # so the failure is explicit rather than silently skipped.
            return PlannedStage(
                stage=stage, action="run", reason="handler_unavailable", versions=serialised
            )

        if not handler.cacheable:
            return PlannedStage(
                stage=stage, action="run", reason="stage_not_cacheable", versions=serialised
            )

        # The heart of incremental processing: reuse only when every version that
        # affects this stage's output is unchanged (§25).
        if current.matches(checkpoint.versions):
            return PlannedStage(
                stage=stage,
                action="skip",
                reason="checkpoint_current",
                checkpoint_id=checkpoint.id,
                versions=checkpoint.versions,
            )

        changed = current.diff(checkpoint.versions)
        return PlannedStage(
            stage=stage,
            action="run",
            reason="version_changed:" + ",".join(sorted(changed)),
            versions=serialised,
        )

    def _versions_for(
        self, stage: PipelineStage, profile: DocumentProfile | None
    ) -> ComponentVersions:
        return current_versions_for_stage(
            stage,
            profile_id=str(profile.id) if profile else None,
            profile_version=profile.version if profile else None,
        )

    def _validate_prerequisites(
        self, from_stage: PipelineStage, checkpoints: dict[PipelineStage, JobStageRun]
    ) -> None:
        """Refuse a resume whose upstream stages never succeeded.

        Without this a "retry from embedding" on a contract that failed at parsing
        would run against artifacts that do not exist and fail confusingly deep in
        the pipeline.
        """
        missing = [
            stage.value
            for stage in STAGE_ORDER[: stage_index(from_stage)]
            if stage not in checkpoints
        ]
        if missing:
            raise InvalidStateTransitionError(
                f"Cannot resume at '{from_stage.value}': earlier stages have not "
                f"completed ({', '.join(missing)}).",
                details={"from_stage": from_stage.value, "missing_stages": missing},
            )

    def _resolve_branches(self, contract: Contract) -> list[str]:
        """Conditional workflow selection (§10.2).

        ``PDF -> parser``, ``scanned PDF -> OCR -> parser``, ``DOCX -> parser``. The
        scanned decision cannot be made here - it needs the page text density the
        parser stage measures - so OCR is flagged as *available* and the parser
        stage activates it per page.
        """
        branches: list[str] = []
        if contract.file_type is FileType.PDF:
            branches.append("pdf")
            branches.append("ocr_available")
        elif contract.file_type is FileType.DOCX:
            branches.append("docx")
        return branches

    # =========================================================================
    # Transitions
    # =========================================================================
    def next_after(self, plan: ExecutionPlan, completed: PipelineStage) -> PipelineStage | None:
        """The next stage to dispatch after ``completed``, honouring skips."""
        try:
            position = stage_index(completed)
        except ValueError:  # pragma: no cover
            return None

        for item in plan.stages:
            index = stage_position(item.stage)
            if index is None:
                # A stage this plan was built with that is no longer in
                # STAGE_ORDER. It cannot be dispatched, so it cannot be next.
                continue
            if index <= position:
                continue
            if item.should_run:
                return item.stage
            # A skipped stage contributes nothing to run; keep looking.
        return None

    @staticmethod
    def validate_transition(job: ProcessingJob, stage: PipelineStage) -> None:
        """Guard against dispatching a stage the job is not eligible for."""
        if job.state in {JobState.CANCELLED, JobState.PAUSED}:
            raise InvalidStateTransitionError(
                f"This job is {str(job.state).lower()} and cannot run further stages.",
                details={"state": str(job.state), "stage": stage.value},
            )
        if job.state is JobState.READY and stage is not PipelineStage.INDEXING:
            raise InvalidStateTransitionError(
                "This job has already completed. Trigger a reprocess instead of a stage dispatch.",
                details={"state": str(job.state)},
            )

    @staticmethod
    def dependencies_satisfied(
        stage: PipelineStage, checkpoints: dict[PipelineStage, JobStageRun]
    ) -> bool:
        """Are this stage's declared dependencies all checkpointed?"""
        return all(
            dependency in checkpoints and checkpoints[dependency].status is StageStatus.SUCCEEDED
            for dependency in STAGE_DEPENDENCIES.get(stage, ())
        )

    @staticmethod
    def progress_for(stage: PipelineStage, *, completed: bool = False) -> int:
        """Progress percentage for a stage boundary.

        Weighted rather than linear: parsing and extraction dominate wall time on a
        150-page contract, so an evenly divided bar would sit at 25% for minutes and
        then jump.
        """
        weights: dict[PipelineStage, tuple[int, int]] = {
            PipelineStage.VALIDATION: (0, 3),
            PipelineStage.PARSER: (3, 35),
            # Clause location, then typed extraction. Extraction owns the larger
            # share because it makes several model calls to docpipeline's few.
            PipelineStage.DOCPIPELINE: (35, 50),
            PipelineStage.EXTRACTION: (50, 85),
            # Embedding gives up its tail to indexing. Both used to end at 100,
            # so the bar hit 100% while a stage was still running and then sat
            # there - the one reading a progress bar has of being lied to.
            PipelineStage.EMBEDDING: (85, 95),
            PipelineStage.INDEXING: (95, 100),
        }
        start, end = weights.get(stage, (0, 100))
        return end if completed else start


__all__ = ["ExecutionPlan", "PlannedStage", "WorkflowEngine"]

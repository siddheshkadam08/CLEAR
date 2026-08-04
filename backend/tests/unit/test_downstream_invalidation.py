"""A re-run stage invalidates every stage after it.

`_plan_stage` judges each stage against its own component versions. That is right
in isolation and wrong in sequence: a checkpoint records "this output is current
for these versions", not "the input it was computed from still exists". An
upstream re-run replaces that input, so the downstream checkpoint then describes
rows that have been deleted.

Found on a live retry, not by reading. The LLM model had changed, so `docpipeline`
and `extraction` re-ran with `version_changed:model_name` and rebuilt the chunks;
`embedding` compared only its own versions, skipped as `checkpoint_current`, and
the job reached READY with zero embeddings - the vectors it reused belonged to
chunks that no longer existed. The contract looked complete and was invisible to
every semantic search, which is the worst shape a bug can take: silent, and only
observable by asking a question that should have worked.
"""

from __future__ import annotations

from app.core.enums import PipelineStage
from app.orchestrator.workflow import ExecutionPlan, PlannedStage


def plan_of(*pairs: tuple[PipelineStage, str, str]) -> ExecutionPlan:
    plan = ExecutionPlan(job_id=None, contract_id=None, project_id=None)  # type: ignore[arg-type]
    for stage, action, reason in pairs:
        plan.stages.append(PlannedStage(stage=stage, action=action, reason=reason))
    return plan


def apply_downstream_rule(plan: ExecutionPlan) -> ExecutionPlan:
    """The rule under test, as the planner applies it while building the list."""
    upstream_reruns = False
    rebuilt: list[PlannedStage] = []
    for planned in plan.stages:
        if upstream_reruns and planned.action == "skip":
            planned = PlannedStage(
                stage=planned.stage, action="run", reason="upstream_rerun", versions=planned.versions
            )
        upstream_reruns = upstream_reruns or planned.action == "run"
        rebuilt.append(planned)
    plan.stages = rebuilt
    return plan


class TestDownstreamInvalidation:
    def test_the_live_failure_case(self) -> None:
        """docpipeline and extraction re-run; embedding and indexing must follow."""
        plan = apply_downstream_rule(
            plan_of(
                (PipelineStage.VALIDATION, "run", "stage_not_cacheable"),
                (PipelineStage.PARSER, "skip", "checkpoint_current"),
                (PipelineStage.DOCPIPELINE, "run", "version_changed:model_name"),
                (PipelineStage.EXTRACTION, "run", "version_changed:model_name"),
                (PipelineStage.EMBEDDING, "skip", "checkpoint_current"),
                (PipelineStage.INDEXING, "skip", "checkpoint_current"),
            )
        )
        actions = {s.stage: s.action for s in plan.stages}

        assert actions[PipelineStage.EMBEDDING] == "run", "embeddings were rebuilt against stale chunks"
        assert actions[PipelineStage.INDEXING] == "run"
        assert plan.for_stage(PipelineStage.EMBEDDING).reason == "upstream_rerun"

    def test_a_skip_before_any_rerun_is_left_alone(self) -> None:
        """The point of checkpointing is that untouched prefixes stay skipped -
        the rule must not turn every reprocess into a full rebuild."""
        plan = apply_downstream_rule(
            plan_of(
                (PipelineStage.VALIDATION, "skip", "checkpoint_current"),
                (PipelineStage.PARSER, "skip", "checkpoint_current"),
                (PipelineStage.DOCPIPELINE, "run", "version_changed:model_name"),
            )
        )
        actions = {s.stage: s.action for s in plan.stages}

        assert actions[PipelineStage.VALIDATION] == "skip"
        assert actions[PipelineStage.PARSER] == "skip"

    def test_all_skipped_stays_all_skipped(self) -> None:
        plan = apply_downstream_rule(
            plan_of(
                (PipelineStage.VALIDATION, "skip", "checkpoint_current"),
                (PipelineStage.EMBEDDING, "skip", "checkpoint_current"),
            )
        )

        assert all(s.action == "skip" for s in plan.stages)

    def test_the_first_stage_running_forces_the_rest(self) -> None:
        plan = apply_downstream_rule(
            plan_of(
                (PipelineStage.VALIDATION, "run", "forced"),
                (PipelineStage.PARSER, "skip", "checkpoint_current"),
                (PipelineStage.EMBEDDING, "skip", "checkpoint_current"),
            )
        )

        assert all(s.action == "run" for s in plan.stages)

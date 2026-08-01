"""Model routing: the tier a workload resolves to.

The regression these guard against is specific and was live in production: the
previous router fell through to ``settings.llm.model`` for any purpose it did not
explicitly list, so ``extraction``, ``classification`` and ``summary`` - the three
highest-volume workloads - inherited the *default* model. Pointing that default at
a reasoning model made every extraction 10-20x slower with nothing in the code to
show for it.
"""

from __future__ import annotations

import pytest

from app.ai.routing import (
    LEGACY_PURPOSE_TASKS,
    TASK_TIERS,
    LLMTask,
    ModelRouter,
    ModelTier,
    describe_routing,
)


class TestRoutingTable:
    def test_every_task_declares_a_tier(self) -> None:
        """A task without a tier must fail loudly, not inherit a default."""
        missing = [task for task in LLMTask if task not in TASK_TIERS]
        assert missing == [], f"tasks missing from TASK_TIERS: {missing}"

    @pytest.mark.parametrize(
        "task",
        [
            LLMTask.DOCUMENT_CLASSIFICATION,
            LLMTask.CLAUSE_EXTRACTION,
            LLMTask.METADATA_EXTRACTION,
            LLMTask.ENTITY_EXTRACTION,
            LLMTask.JSON_EXTRACTION,
            LLMTask.KEY_VALUE_EXTRACTION,
            LLMTask.SUMMARIZATION,
            LLMTask.OCR_CLEANUP,
            LLMTask.RETRIEVAL_PLANNING,
        ],
    )
    def test_extraction_tasks_never_use_a_reasoning_model(self, task: LLMTask) -> None:
        """Extraction is transcription, not deliberation."""
        assert TASK_TIERS[task] is ModelTier.SIMPLE

    @pytest.mark.parametrize("task", [LLMTask.LEGAL_REASONING, LLMTask.AMBIGUITY_RESOLUTION])
    def test_only_declared_tasks_reach_the_reasoning_tier(self, task: LLMTask) -> None:
        assert TASK_TIERS[task] is ModelTier.REASONING

    def test_reasoning_tier_is_not_over_used(self) -> None:
        """Guards against tier creep: reasoning is the exception, not the norm."""
        reasoning = [t for t, tier in TASK_TIERS.items() if tier is ModelTier.REASONING]
        assert len(reasoning) <= 2


class TestResolution:
    def test_simple_tier_resolves_to_the_simple_model(self) -> None:
        choice = ModelRouter().resolve(LLMTask.CLAUSE_EXTRACTION)
        assert choice.tier is ModelTier.SIMPLE
        assert choice.model  # configuration-driven, never empty

    def test_complex_and_simple_are_distinct_by_default(self) -> None:
        simple = ModelRouter().resolve(LLMTask.CLAUSE_EXTRACTION)
        complex_ = ModelRouter().resolve(LLMTask.CLAUSE_COMPARISON)
        assert simple.tier is not complex_.tier

    def test_provider_models_override_configuration(self) -> None:
        """A provider whose model names differ keeps that detail inside itself."""
        router = ModelRouter(provider_models={ModelTier.SIMPLE: "vendor-fast-1"})
        assert router.resolve(LLMTask.CLAUSE_EXTRACTION).model == "vendor-fast-1"

    def test_reasoning_falls_back_to_complex_not_default(self) -> None:
        """An unset reasoning model degrades to 'strong', never to 'whatever'."""
        router = ModelRouter()
        reasoning = router.resolve(LLMTask.LEGAL_REASONING)
        complex_ = router.resolve(LLMTask.CLAUSE_COMPARISON)
        assert reasoning.model == complex_.model

    def test_simple_tier_has_the_tightest_timeout(self) -> None:
        """A fast call that has not answered in its window is not going to."""
        router = ModelRouter()
        simple = router.resolve(LLMTask.CLAUSE_EXTRACTION)
        reasoning = router.resolve(LLMTask.LEGAL_REASONING)
        assert simple.timeout_seconds <= reasoning.timeout_seconds


class TestBackwardCompatibility:
    """Existing call sites pass legacy `purpose` strings and must keep working."""

    @pytest.mark.parametrize("purpose", sorted(LEGACY_PURPOSE_TASKS))
    def test_legacy_purposes_still_resolve(self, purpose: str) -> None:
        choice = ModelRouter().resolve(purpose)
        assert isinstance(choice.task, LLMTask)
        assert choice.model

    @pytest.mark.parametrize(
        ("purpose", "expected"),
        [
            ("extraction", ModelTier.SIMPLE),
            ("classification", ModelTier.SIMPLE),
            ("summary", ModelTier.SIMPLE),
            ("planner", ModelTier.SIMPLE),
            ("comparison", ModelTier.COMPLEX),
            ("report", ModelTier.COMPLEX),
        ],
    )
    def test_legacy_purposes_land_on_the_intended_tier(
        self, purpose: str, expected: ModelTier
    ) -> None:
        """`extraction`/`classification`/`summary` on SIMPLE is the whole point."""
        assert ModelRouter().resolve(purpose).tier is expected

    def test_unknown_task_routes_cheap_and_warns(self) -> None:
        """A typo must not silently become an expensive model."""
        choice = ModelRouter().resolve("not-a-real-task")
        assert choice.tier is ModelTier.SIMPLE


class TestIntrospection:
    def test_describe_routing_covers_every_task(self) -> None:
        rows = describe_routing()
        assert {row["task"] for row in rows} == {t.value for t in LLMTask}
        assert all(row["model"] for row in rows)


class TestNoHardcodedModels:
    """No service may name a model; they name a task."""

    def test_services_do_not_hardcode_model_names(self) -> None:
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[2] / "app"
        # Files allowed to mention concrete model names: configuration (defaults),
        # pricing tables, and the provider adapters that translate them.
        allowed = {"config.py", "providers.py", "routing.py"}
        pattern = re.compile(r"[\"'](?:claude-|gpt-|gemini-|z-ai/|glm-)[\w.\-/:]+[\"']")

        offenders: list[str] = []
        for path in root.rglob("*.py"):
            if path.name in allowed:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(root)}:{line} {match.group(0)}")

        assert offenders == [], "hardcoded model names outside configuration:\n" + "\n".join(
            offenders
        )

"""Model routing: which model serves which workload.

The pipeline mixes two genuinely different kinds of work, and conflating them is
expensive in every dimension that matters.

**Extraction** - classification, clause extraction, metadata, entities,
key-value pairs, summarisation, OCR cleanup - is constrained, schema-shaped and
deterministic. The model is being asked to *transcribe structure it can already
see*. A reasoning model adds hundreds of thinking tokens per call and answers no
better; measured on this pipeline, a reasoning model spent 43-100s per category
against 129 chunks, and ~30 categories put a single contract close to the request
timeout.

**Reasoning** - clause comparison, ambiguity resolution, recommendations - is
open-ended. Here the thinking tokens are the product, and a cheap model starts
inventing clause text, which is the one output this platform must never ship.

So the tier is a property of the *task*, declared once here, rather than a
decision each call site re-makes. Two rules follow:

* no service names a model - they name a :class:`LLMTask`;
* a task that is not mapped fails loudly at import rather than silently
  inheriting the default tier.

That second rule is what this module exists to prevent. The previous router keyed
off a ``Purpose`` literal and fell through to ``settings.llm.model`` for anything
unlisted - so ``extraction``, ``classification`` and ``summary``, the three
highest-volume workloads in the system, all quietly used the *default* model.
Pointing that default at a reasoning model made every extraction slow, and
nothing in the code said so.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.config import LLMSettings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class ModelTier(StrEnum):
    """Cost/latency tier a task resolves to."""

    #: Fast, cheap, deterministic. Structured output; no chain of thought.
    SIMPLE = "simple"
    #: Stronger model for multi-document and comparative work.
    COMPLEX = "complex"
    #: Explicit chain-of-thought. Never used for extraction.
    REASONING = "reasoning"


class LLMTask(StrEnum):
    """Every workload that reaches an inference provider.

    Adding a member without adding it to :data:`TASK_TIERS` raises at import, so a
    new call site cannot silently inherit a tier nobody chose for it.
    """

    # --- extraction: structured, deterministic, high volume ------------------
    DOCUMENT_CLASSIFICATION = "document_classification"
    CLAUSE_EXTRACTION = "clause_extraction"
    METADATA_EXTRACTION = "metadata_extraction"
    ENTITY_EXTRACTION = "entity_extraction"
    JSON_EXTRACTION = "json_extraction"
    KEY_VALUE_EXTRACTION = "key_value_extraction"
    SUMMARIZATION = "summarization"
    OCR_CLEANUP = "ocr_cleanup"
    RETRIEVAL_PLANNING = "retrieval_planning"

    # --- analysis: comparative, multi-document -------------------------------
    RISK_ANALYSIS = "risk_analysis"
    CLAUSE_COMPARISON = "clause_comparison"
    CONTRACT_ANALYSIS = "contract_analysis"
    RECOMMENDATION = "recommendation"
    COPILOT_CHAT = "copilot_chat"

    # --- reasoning: chain-of-thought genuinely required ----------------------
    LEGAL_REASONING = "legal_reasoning"
    AMBIGUITY_RESOLUTION = "ambiguity_resolution"


#: The routing table. This is the file's entire point: one place, reviewable in a
#: diff, that says what runs where.
TASK_TIERS: dict[LLMTask, ModelTier] = {
    # Extraction never reasons.
    LLMTask.DOCUMENT_CLASSIFICATION: ModelTier.SIMPLE,
    LLMTask.CLAUSE_EXTRACTION: ModelTier.SIMPLE,
    LLMTask.METADATA_EXTRACTION: ModelTier.SIMPLE,
    LLMTask.ENTITY_EXTRACTION: ModelTier.SIMPLE,
    LLMTask.JSON_EXTRACTION: ModelTier.SIMPLE,
    LLMTask.KEY_VALUE_EXTRACTION: ModelTier.SIMPLE,
    LLMTask.SUMMARIZATION: ModelTier.SIMPLE,
    LLMTask.OCR_CLEANUP: ModelTier.SIMPLE,
    LLMTask.RETRIEVAL_PLANNING: ModelTier.SIMPLE,
    # Comparative work across documents.
    LLMTask.RISK_ANALYSIS: ModelTier.COMPLEX,
    LLMTask.CLAUSE_COMPARISON: ModelTier.COMPLEX,
    LLMTask.CONTRACT_ANALYSIS: ModelTier.COMPLEX,
    LLMTask.RECOMMENDATION: ModelTier.COMPLEX,
    LLMTask.COPILOT_CHAT: ModelTier.COMPLEX,
    # Chain-of-thought.
    LLMTask.LEGAL_REASONING: ModelTier.REASONING,
    LLMTask.AMBIGUITY_RESOLUTION: ModelTier.REASONING,
}

#: Legacy ``Purpose`` strings mapped onto tasks.
#:
#: Kept so existing call sites and any persisted job options keep working while
#: they migrate. The mapping is deliberately explicit rather than a fallthrough:
#: `extraction` and `classification` land on SIMPLE here, which is precisely the
#: behaviour change this module is for.
LEGACY_PURPOSE_TASKS: dict[str, LLMTask] = {
    "extraction": LLMTask.CLAUSE_EXTRACTION,
    "classification": LLMTask.DOCUMENT_CLASSIFICATION,
    "summary": LLMTask.SUMMARIZATION,
    "planner": LLMTask.RETRIEVAL_PLANNING,
    "rag": LLMTask.COPILOT_CHAT,
    "comparison": LLMTask.CLAUSE_COMPARISON,
    "report": LLMTask.CONTRACT_ANALYSIS,
    # These two used to be routed by the provider adapter from a model set of its
    # own, so they never reached this table and `coerce` treated them as unknown
    # - which sends them to the *cheap* tier. Listing them here makes the tier a
    # property of the task rather than of whichever provider happens to be
    # configured, which is the whole point of this module.
    "risk_assessment": LLMTask.RISK_ANALYSIS,
    "compliance": LLMTask.CONTRACT_ANALYSIS,
}


def _assert_complete() -> None:
    """Every task must declare a tier. Import-time, so it cannot ship broken."""
    missing = [task.value for task in LLMTask if task not in TASK_TIERS]
    if missing:
        raise RuntimeError(
            "LLMTask members without a tier in TASK_TIERS: "
            + ", ".join(sorted(missing))
            + ". Add them rather than letting them inherit a default."
        )


_assert_complete()


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """The resolved routing decision for one call."""

    task: LLMTask
    tier: ModelTier
    model: str
    effort: str
    timeout_seconds: float
    max_output_tokens: int

    def as_log_fields(self) -> dict[str, str | int | float]:
        return {
            "llm_task": self.task.value,
            "llm_tier": self.tier.value,
            "model": self.model,
            "effort": self.effort,
            "timeout_seconds": self.timeout_seconds,
        }


class ModelRouter:
    """Resolves a task to a concrete model, effort and timeout.

    Stateless and configuration-driven: every value comes from settings, so a tier
    can be repointed at a different vendor without touching code. Constructed per
    call rather than cached because settings are themselves cached, and a fresh
    read keeps an admin-side settings change effective without a restart.
    """

    def __init__(self, *, provider_models: dict[ModelTier, str] | None = None) -> None:
        #: Optional override, injected by a provider whose model names differ from
        #: the generic ``LLM_MODEL_*`` values - Azure addresses a *deployment*
        #: rather than a model name. Passing them in keeps provider specifics in
        #: the provider.
        self._provider_models = provider_models or {}

    # ------------------------------------------------------------------ resolve
    def resolve(self, task: LLMTask | str) -> ModelChoice:
        """The model, effort and timeout for ``task``."""
        resolved = self.coerce(task)
        tier = TASK_TIERS[resolved]
        settings = get_settings().llm

        model = self._provider_models.get(tier) or self._model_for_tier(tier, settings)
        return ModelChoice(
            task=resolved,
            tier=tier,
            model=model,
            effort=self._effort_for_tier(tier, settings),
            timeout_seconds=float(self._timeout_for_tier(tier, settings)),
            max_output_tokens=settings.max_output_tokens,
        )

    @staticmethod
    def coerce(task: LLMTask | str) -> LLMTask:
        """Accept a task, a task value, or a legacy purpose string.

        Backward compatibility is the whole reason this is lenient: callers still
        pass ``purpose="extraction"``, and breaking them to land a routing change
        would be a poor trade.
        """
        if isinstance(task, LLMTask):
            return task
        text = str(task)
        try:
            return LLMTask(text)
        except ValueError:
            pass
        legacy = LEGACY_PURPOSE_TASKS.get(text)
        if legacy is not None:
            return legacy
        # Unknown: route to the safest *cheap* tier and say so. Defaulting to the
        # expensive tier would turn a typo into a bill.
        logger.warning(
            "llm_task_unknown",
            task=text,
            detail="Unrecognised task; routed to the simple tier. Add it to LLMTask.",
        )
        return LLMTask.JSON_EXTRACTION

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _model_for_tier(tier: ModelTier, settings: LLMSettings) -> str:
        if tier is ModelTier.SIMPLE:
            return str(settings.model_simple)
        if tier is ModelTier.COMPLEX:
            return str(settings.model_complex)
        # REASONING falls back to the complex model when no reasoning model is
        # configured, rather than to the *default* - an unset reasoning model
        # should degrade to "strong", never to "whatever LLM_MODEL happens to be".
        reasoning = str(settings.model_reasoning or "")
        return reasoning or str(settings.model_complex)

    @staticmethod
    def _effort_for_tier(tier: ModelTier, settings: LLMSettings) -> str:
        if tier is ModelTier.SIMPLE:
            return str(settings.effort_simple)
        if tier is ModelTier.COMPLEX:
            return str(settings.effort_complex)
        return str(settings.effort_reasoning or settings.effort)

    @staticmethod
    def _timeout_for_tier(tier: ModelTier, settings: LLMSettings) -> int:
        """Per-tier timeouts.

        A fast extraction call that has not answered in 60s is not going to; a
        reasoning call legitimately takes minutes. One shared ceiling has to be set
        for the slowest case, which means a hung extraction ties up a worker slot
        for ten times longer than it should.
        """
        if tier is ModelTier.SIMPLE:
            return int(settings.timeout_seconds_simple or settings.timeout_seconds)
        if tier is ModelTier.COMPLEX:
            return int(settings.timeout_seconds_complex or settings.timeout_seconds)
        return int(settings.timeout_seconds)


def get_router(provider_models: dict[ModelTier, str] | None = None) -> ModelRouter:
    """Router factory. Injected rather than imported so tests can substitute one."""
    return ModelRouter(provider_models=provider_models)


def describe_routing() -> list[dict[str, str]]:
    """The routing table, for the diagnostics endpoint and the docs."""
    router = ModelRouter()
    rows: list[dict[str, str]] = []
    for task in LLMTask:
        choice = router.resolve(task)
        rows.append(
            {
                "task": task.value,
                "tier": choice.tier.value,
                "model": choice.model,
                "effort": choice.effort,
                "timeout_seconds": str(choice.timeout_seconds),
            }
        )
    return rows


__all__ = [
    "LEGACY_PURPOSE_TASKS",
    "TASK_TIERS",
    "LLMTask",
    "ModelChoice",
    "ModelRouter",
    "ModelTier",
    "describe_routing",
    "get_router",
]

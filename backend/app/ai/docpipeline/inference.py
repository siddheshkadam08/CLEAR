"""One structured call, on a budget, with headroom if it overflows.

Every call this pipeline makes returns a few hundred tokens - a document type, a
list of heading-to-clause pairs, a list of paragraph refs. The provider defaults
to ``LLM_MAX_OUTPUT_TOKENS`` (16000), and a reasoning model spends whatever
budget it is given thinking before it answers. Handing a 16000-token budget to a
question whose answer is 200 tokens is most of this pipeline's latency.

Capping it introduces the opposite risk: a truncated structured response raises
``SchemaValidationError`` and, in :mod:`app.ai.rag.openai_provider`, is
deliberately never retried - re-sampling against the same budget reproduces the
same overflow. That reasoning is sound for a *fixed* budget and stops applying
the moment the retry *raises* it, which is why the exception lives here rather
than in the shared adapter: the adapter's rule stays correct for every other
caller.
"""

from __future__ import annotations

from typing import Any, cast

from app.ai.rag.providers import IInferenceProvider, Purpose, StructuredResult
from app.ai.routing import LLMTask
from app.core.config import get_settings
from app.core.errors import SchemaValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Output budget for this pipeline's calls. ``None`` means the provider default.
#:
#: Capping this was tried and abandoned, and the measurement is worth keeping
#: because the idea is an obvious one to have again. The answers really are a few
#: hundred tokens, so 4000 looked generous. It was not: glm-4.7's *thinking*
#: counts against the same budget, three of fourteen calls hit the ceiling and
#: had to be re-issued at the full budget, and the run went from 251s to 759s -
#: three times slower for the change that was supposed to make it faster.
#:
#: The retry below is kept, and so is the parameter. A model that answers within
#: a cap would benefit, and the machinery to find out is one constant away.
DEFAULT_MAX_TOKENS: int | None = None


async def call_structured(
    provider: IInferenceProvider,
    *,
    system: str,
    prompt: str,
    schema: dict[str, Any],
    task: LLMTask,
    max_tokens: int | None = DEFAULT_MAX_TOKENS,
) -> StructuredResult:
    """Call ``generate_structured`` on a budget, retrying once with headroom.

    The retry fires only on truncation, and only once. If
    ``docpipeline_truncation_retry`` shows up routinely in the logs then
    :data:`DEFAULT_MAX_TOKENS` is set too low and should be raised - the retry is
    a safety net, not a strategy.
    """
    purpose = cast(Purpose, task.value)
    try:
        return await provider.generate_structured(
            system=system,
            prompt=prompt,
            schema=schema,
            purpose=purpose,
            max_tokens=max_tokens,
            cache_prefix=True,
        )
    except SchemaValidationError as exc:
        if not _is_truncation(exc):
            raise

        headroom = get_settings().llm.max_output_tokens
        if max_tokens is None or headroom <= max_tokens:
            # No cap was applied, so there is no larger budget to retry with -
            # the answer genuinely does not fit.
            raise

        logger.warning(
            "docpipeline_truncation_retry",
            task=task.value,
            capped_at=max_tokens,
            retrying_at=headroom,
        )
        return await provider.generate_structured(
            system=system,
            prompt=prompt,
            schema=schema,
            purpose=purpose,
            max_tokens=headroom,
            cache_prefix=True,
        )


def _is_truncation(exc: SchemaValidationError) -> bool:
    """Did this failure come from the answer not fitting, rather than bad JSON?

    Matched on the provider's own wording. Both adapters raise the same message
    for this case, and it is the only place either of them says "truncated".
    """
    return "truncated" in str(exc).lower()

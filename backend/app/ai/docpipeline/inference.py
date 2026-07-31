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

#: Output budget for this pipeline's calls. Comfortably above the largest real
#: answer (a chunk search naming ten clauses with their refs) and far below the
#: 16000 the provider would otherwise allow a reasoning model to spend.
DEFAULT_MAX_TOKENS = 4000


async def call_structured(
    provider: IInferenceProvider,
    *,
    system: str,
    prompt: str,
    schema: dict[str, Any],
    task: LLMTask,
    max_tokens: int = DEFAULT_MAX_TOKENS,
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
        if headroom <= max_tokens:
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

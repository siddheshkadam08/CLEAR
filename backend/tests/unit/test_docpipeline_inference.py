"""The output-token budget, and the one retry that makes it safe."""

from __future__ import annotations

from typing import Any

import pytest

from app.ai.docpipeline.inference import DEFAULT_MAX_TOKENS, call_structured
from app.ai.rag.providers import InferenceResult, StructuredResult, TokenUsage
from app.ai.routing import LLMTask
from app.core.errors import SchemaValidationError

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

TRUNCATED = (
    "The structured response was truncated before completing. Raise "
    "LLM_MAX_OUTPUT_TOKENS - a reasoning model spends part of this budget on "
    "thinking before it emits the object."
)


class RecordingProvider:
    """Records the budget of every call; optionally fails the first one."""

    def __init__(self, *, fail_first_with: Exception | None = None) -> None:
        self.budgets: list[int | None] = []
        self._fail_first_with = fail_first_with

    async def generate_structured(self, *, max_tokens: int | None = None, **_: Any) -> StructuredResult:
        self.budgets.append(max_tokens)
        if self._fail_first_with is not None and len(self.budgets) == 1:
            raise self._fail_first_with
        return StructuredResult(
            data={"ok": True},
            inference=InferenceResult(
                text="", model="stub", usage=TokenUsage(), stop_reason="stop"
            ),
        )


async def _call(provider: Any) -> StructuredResult:
    return await call_structured(
        provider,
        system="s",
        prompt="p",
        schema=SCHEMA,
        task=LLMTask.CLAUSE_EXTRACTION,
    )


@pytest.mark.asyncio
async def test_calls_are_capped_rather_than_using_the_full_budget() -> None:
    """A reasoning model spends whatever budget it is given, and the answers
    here are a few hundred tokens."""
    provider = RecordingProvider()

    await _call(provider)

    assert provider.budgets == [DEFAULT_MAX_TOKENS]


@pytest.mark.asyncio
async def test_truncation_is_retried_once_with_headroom() -> None:
    """The adapter never retries a truncation because re-sampling against the
    same budget reproduces it. Raising the budget is a different question."""
    provider = RecordingProvider(
        fail_first_with=SchemaValidationError(TRUNCATED, stage="ai_extraction")
    )

    result = await _call(provider)

    assert result.data == {"ok": True}
    assert len(provider.budgets) == 2
    assert provider.budgets[0] == DEFAULT_MAX_TOKENS
    assert provider.budgets[1] > DEFAULT_MAX_TOKENS


@pytest.mark.asyncio
async def test_unparsable_json_is_not_retried_here() -> None:
    """Only truncation earns the second call.

    Malformed JSON is already re-sampled inside the provider; retrying it again
    here would quietly double the cost of every gateway hiccup.
    """
    provider = RecordingProvider(
        fail_first_with=SchemaValidationError(
            "The model structured response was not valid JSON.", stage="ai_extraction"
        )
    )

    with pytest.raises(SchemaValidationError, match="not valid JSON"):
        await _call(provider)

    assert len(provider.budgets) == 1


@pytest.mark.asyncio
async def test_a_cap_above_the_ceiling_does_not_retry() -> None:
    """Nothing to raise the budget to means nothing to retry with."""
    from app.core.config import get_settings

    ceiling = get_settings().llm.max_output_tokens
    provider = RecordingProvider(
        fail_first_with=SchemaValidationError(TRUNCATED, stage="ai_extraction")
    )

    with pytest.raises(SchemaValidationError, match="truncated"):
        await call_structured(
            provider,
            system="s",
            prompt="p",
            schema=SCHEMA,
            task=LLMTask.CLAUSE_EXTRACTION,
            max_tokens=ceiling,
        )

    assert len(provider.budgets) == 1

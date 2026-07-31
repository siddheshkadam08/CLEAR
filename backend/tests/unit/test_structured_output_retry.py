"""Recovering a structured response when the gateway cannot enforce a schema.

`LLM_STRUCTURED_OUTPUT=none` puts the schema in the prompt and trusts the model
to return JSON. It mostly does. When it does not - a code fence, a sentence of
preamble - the old behaviour reported the category as `not_found`, which reads
as "the document does not say" rather than "we could not parse the answer". A
mandatory confidentiality clause was lost that way from a contract that plainly
contained one.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.ai.rag.providers import InferenceResult, TokenUsage
from app.core.errors import SchemaValidationError

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"found": {"type": "boolean"}},
    "required": ["found"],
}


def _result(text: str, *, stop_reason: str = "stop") -> InferenceResult:
    return InferenceResult(
        text=text,
        model="test-model",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        latency_ms=1,
        stop_reason=stop_reason,
        provider="openai",
    )


def _provider(monkeypatch: pytest.MonkeyPatch, enforced: bool, replies: list[InferenceResult]):
    from app.ai.rag.openai_provider import OpenAIProvider

    provider = OpenAIProvider()
    monkeypatch.setattr(
        provider.settings.llm,
        "llm_structured_output",
        "json_schema" if enforced else "none",
    )

    calls: list[int] = []

    async def fake_invoke(**_: Any) -> InferenceResult:
        calls.append(1)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(provider, "_invoke", fake_invoke)
    return provider, calls


class TestUnenforcedSchema:
    @pytest.mark.asyncio
    async def test_valid_json_needs_one_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider, calls = _provider(monkeypatch, False, [_result('{"found": true}')])
        out = await provider.generate_structured(system="s", prompt="p", schema=SCHEMA)
        assert out.data == {"found": True}
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_prose_is_retried_and_recovers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The regression: one bad sample used to lose the clause entirely."""
        provider, calls = _provider(
            monkeypatch,
            False,
            [_result("Certainly! Here is the object:"), _result('{"found": true}')],
        )
        out = await provider.generate_structured(system="s", prompt="p", schema=SCHEMA)
        assert out.data == {"found": True}
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_it_gives_up_rather_than_looping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each attempt is a full-price call; two is the ceiling."""
        provider, calls = _provider(monkeypatch, False, [_result("not json at all")])
        with pytest.raises(SchemaValidationError):
            await provider.generate_structured(system="s", prompt="p", schema=SCHEMA)
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_truncation_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Re-sampling produces the same overflow - the budget is the problem."""
        provider, calls = _provider(
            monkeypatch, False, [_result('{"found": tr', stop_reason="length")]
        )
        with pytest.raises(SchemaValidationError) as err:
            await provider.generate_structured(system="s", prompt="p", schema=SCHEMA)
        assert len(calls) == 1
        assert "LLM_MAX_OUTPUT_TOKENS" in str(err.value)


class TestEnforcedSchema:
    @pytest.mark.asyncio
    async def test_no_retry_when_the_provider_enforces_the_schema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With json_schema the output is guaranteed; a retry would only cost money."""
        provider, calls = _provider(monkeypatch, True, [_result("not json")])
        with pytest.raises(SchemaValidationError):
            await provider.generate_structured(system="s", prompt="p", schema=SCHEMA)
        assert len(calls) == 1

"""Gemini provider: schema sanitisation, refusals, routing, failure translation."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.ai.rag.gemini_provider import GeminiProvider, _sanitise_schema
from app.core.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    SchemaValidationError,
)


def _provider(handler: Any) -> GeminiProvider:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://gemini.test/v1beta"
    )
    return GeminiProvider(client=client)


def _reply(text: str, *, finish: str = "STOP") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 5},
        },
    )


@pytest.fixture
def gemini_env(settings_env: Any) -> Any:
    return settings_env(
        LLM_PROVIDER="gemini",
        GOOGLE_API_KEY="test-key",
        GEMINI_BASE_URL="https://gemini.test/v1beta",
        LLM_MAX_RETRIES="2",
    )


# =============================================================================
# Schema sanitisation
# =============================================================================
def test_strips_keywords_gemini_rejects() -> None:
    cleaned = _sanitise_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"a": {"type": "string", "minLength": 2, "pattern": "^x"}},
        }
    )
    assert "additionalProperties" not in cleaned
    assert "minLength" not in cleaned["properties"]["a"]
    assert "pattern" not in cleaned["properties"]["a"]


def test_nullable_union_becomes_the_nullable_flag() -> None:
    """`type: [string, null]` is how the Clause Master expresses "may be absent".

    Gemini does not accept the union form. Dropping the nullability instead of
    translating it would lose the distinction between "the contract is silent" and
    "the extractor did not look" - which is a finding, not a formatting detail.
    """
    cleaned = _sanitise_schema({"type": ["string", "null"], "enum": ["a", "b"]})
    assert cleaned["type"] == "string"
    assert cleaned["nullable"] is True
    assert cleaned["enum"] == ["a", "b"]


def test_required_list_survives() -> None:
    cleaned = _sanitise_schema(
        {"type": "object", "required": ["a", "b"], "properties": {"a": {}, "b": {}}}
    )
    assert cleaned["required"] == ["a", "b"]


def test_sanitises_nested_structures() -> None:
    cleaned = _sanitise_schema(
        {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": ["string", "null"]},
                }
            },
        }
    )
    assert "minItems" not in cleaned["properties"]["items"]
    assert cleaned["properties"]["items"]["items"]["nullable"] is True


def test_the_real_liability_schema_survives_intact() -> None:
    """The highest-value schema in the product must pass through unharmed."""
    from app.db.clause_seeds import CLAUSE_SEEDS

    seed = next(s for s in CLAUSE_SEEDS if s.key == "limitation_of_liability")
    cleaned = _sanitise_schema(seed.output_schema)

    assert set(cleaned["properties"]) == set(seed.output_schema["properties"])
    assert cleaned["required"] == seed.output_schema["required"]
    # The cap dropdown's enumeration is what the UI renders and the risk engine
    # bands on; losing it would silently degrade both.
    assert "uncapped" in cleaned["properties"]["cap_basis"]["enum"]
    assert cleaned["properties"]["has_carve_outs"]["nullable"] is True


# =============================================================================
# Routing
# =============================================================================
async def test_cheap_tier_is_the_default(gemini_env: Any) -> None:
    provider = _provider(lambda request: _reply("ok"))
    assert provider.model_for("extraction") == "gemini-2.5-flash"
    assert provider.model_for("classification") == "gemini-2.5-flash-lite"


async def test_complex_intents_route_to_the_complex_model(gemini_env: Any) -> None:
    provider = _provider(lambda request: _reply("ok"))
    assert provider.model_for("risk_assessment") == provider._llm.gemini_model_complex


async def test_the_model_appears_in_the_request_path(gemini_env: Any) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return _reply("ok")

    await _provider(handler).generate(system="s", prompt="p", purpose="classification")
    assert "gemini-2.5-flash-lite:generateContent" in seen[0]


# =============================================================================
# Credentials
# =============================================================================
async def test_the_key_goes_in_a_header_not_the_url(gemini_env: Any) -> None:
    """A key in the query string ends up in access logs and proxy traces."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _reply("ok")

    await _provider(handler).generate(system="s", prompt="p")
    assert seen[0].headers.get("x-goog-api-key") == "test-key"
    assert "test-key" not in str(seen[0].url)


async def test_missing_key_fails_before_any_call(settings_env: Any) -> None:
    settings_env(LLM_PROVIDER="gemini", GOOGLE_API_KEY="")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made without a key")

    with pytest.raises(ProviderError, match="GOOGLE_API_KEY"):
        await _provider(handler).generate(system="s", prompt="p")


# =============================================================================
# Structured output
# =============================================================================
async def test_structured_output_sends_the_schema(gemini_env: Any) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return _reply('{"cap_basis": "uncapped"}')

    result = await _provider(handler).generate_structured(
        system="s", prompt="p", schema={"type": "object", "additionalProperties": False}
    )

    config = sent[0]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert "additionalProperties" not in config["responseSchema"]
    assert result.data == {"cap_basis": "uncapped"}


async def test_unparseable_structured_output_raises(gemini_env: Any) -> None:
    provider = _provider(lambda request: _reply("not json at all"))
    with pytest.raises(SchemaValidationError):
        await provider.generate_structured(system="s", prompt="p", schema={})


async def test_json_wrapped_in_prose_is_salvaged(gemini_env: Any) -> None:
    provider = _provider(lambda request: _reply('Here you go:\n```json\n{"a": 1}\n```'))
    result = await provider.generate_structured(system="s", prompt="p", schema={})
    assert result.data == {"a": 1}


# =============================================================================
# Refusals
# =============================================================================
async def test_a_safety_finish_is_a_refusal_not_an_error(gemini_env: Any) -> None:
    """A refusal is a first-class outcome the grounding layer reports as such."""
    provider = _provider(lambda request: _reply("", finish="SAFETY"))
    result = await provider.generate(system="s", prompt="p")

    assert result.refused is True
    assert result.refusal_category == "SAFETY"


async def test_a_blocked_prompt_is_a_refusal(gemini_env: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})

    result = await _provider(handler).generate(system="s", prompt="p")
    assert result.refused is True


async def test_a_refused_extraction_returns_empty_data(gemini_env: Any) -> None:
    provider = _provider(lambda request: _reply("", finish="SAFETY"))
    result = await provider.generate_structured(system="s", prompt="p", schema={})

    assert result.data == {}
    assert result.inference.refused is True


# =============================================================================
# Failure translation
# =============================================================================
async def test_bad_credentials_are_not_retried(gemini_env: Any) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(403, json={})

    with pytest.raises(ProviderError, match="GOOGLE_API_KEY"):
        await _provider(handler).generate(system="s", prompt="p")
    assert attempts == 1


async def test_unknown_model_is_not_retried(gemini_env: Any) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, json={})

    with pytest.raises(ProviderError, match="GEMINI_MODEL"):
        await _provider(handler).generate(system="s", prompt="p")
    assert attempts == 1


async def test_rate_limit_is_retried(gemini_env: Any) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={})

    with pytest.raises(ProviderRateLimitError):
        await _provider(handler).generate(system="s", prompt="p")
    assert attempts == 3


async def test_server_error_is_retried_then_recovers(gemini_env: Any) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, text="upstream") if attempts == 1 else _reply("ok")

    result = await _provider(handler).generate(system="s", prompt="p")
    assert result.text == "ok"
    assert attempts == 2


async def test_timeout_is_surfaced(gemini_env: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(ProviderUnavailableError, match="timed out"):
        await _provider(handler).generate(system="s", prompt="p")


# =============================================================================
# Usage accounting
# =============================================================================
async def test_usage_is_recorded(gemini_env: Any) -> None:
    result = await _provider(lambda request: _reply("ok")).generate(system="s", prompt="p")
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 5


async def test_provider_resolves_from_configuration(gemini_env: Any) -> None:
    from app.ai.rag.providers import get_inference_provider

    assert get_inference_provider().name == "gemini"

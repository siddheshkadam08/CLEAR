"""NVIDIA embedding provider: batching, ordering, retries, failure translation.

Every test drives the provider through a stub transport rather than the network, so
the assertions are about *this code's* behaviour - what it sends, how it reassembles
a response, which failures it retries - rather than about NVIDIA's uptime.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.ai.embedding.nvidia import NvidiaEmbeddingProvider, _backoff
from app.core.errors import ProviderError, ProviderRateLimitError, ProviderUnavailableError

#: Above the `EMBEDDING_DIM` floor of 64, which is a real guard: a handful of
#: dimensions is never a legitimate embedding width and catching it at config load
#: beats discovering it from unusable search results.
DIM = 64


def _vector(seed: float, dim: int = DIM) -> list[float]:
    return [seed + index for index in range(dim)]


def _ok_response(
    request: httpx.Request, *, dim: int = DIM, shuffle: bool = False
) -> httpx.Response:
    payload = json.loads(request.content)
    inputs = payload["input"]
    entries = [
        {"index": index, "embedding": _vector(float(index + 1), dim), "object": "embedding"}
        for index in range(len(inputs))
    ]
    if shuffle:
        entries.reverse()
    return httpx.Response(
        200,
        json={"data": entries, "usage": {"total_tokens": 11 * len(inputs)}, "model": "x"},
    )


def _provider(handler: Any, **env: str) -> NvidiaEmbeddingProvider:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://nim.test/v1")
    return NvidiaEmbeddingProvider(client=client)


@pytest.fixture
def nvidia_env(settings_env: Any) -> Any:
    return settings_env(
        EMBEDDING_PROVIDER="nvidia",
        EMBEDDING_MODEL="nvidia/nemotron-3-embed-1b",
        EMBEDDING_DIM=str(DIM),
        EMBEDDING_BATCH_SIZE="2",
        EMBEDDING_MAX_RETRIES="2",
        NVIDIA_API_KEY="test-key",
        NVIDIA_BASE_URL="https://nim.test/v1",
    )


# =============================================================================
# Happy path
# =============================================================================
async def test_embeds_and_preserves_order(nvidia_env: Any) -> None:
    provider = _provider(_ok_response)
    result = await provider.embed_many(["alpha", "beta", "gamma"])

    assert len(result.vectors) == 3
    assert all(len(vector) == DIM for vector in result.vectors)
    assert result.provider == "nvidia"


async def test_reorders_by_reported_index(nvidia_env: Any) -> None:
    """A response that arrives out of order must be re-sorted, not trusted.

    This is the failure that does not look like a failure: mis-paired vectors keep
    retrieval working while attaching one clause's meaning to another.
    """
    provider = _provider(lambda request: _ok_response(request, shuffle=True))
    result = await provider.embed_many(["one", "two"])

    # Vector for index 0 is built from seed 1.0, index 1 from seed 2.0. After
    # normalisation the ordering is preserved in the ratio of the first component.
    first, second = result.vectors
    assert first[0] < second[0]


async def test_batches_by_configured_size(nvidia_env: Any) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(len(json.loads(request.content)["input"]))
        return _ok_response(request)

    provider = _provider(handler)
    await provider.embed_many(["a", "b", "c", "d", "e"])

    assert calls == [2, 2, 1]  # EMBEDDING_BATCH_SIZE=2


async def test_vectors_are_unit_length(nvidia_env: Any) -> None:
    provider = _provider(_ok_response)
    result = await provider.embed_many(["alpha"])

    magnitude = sum(value * value for value in result.vectors[0]) ** 0.5
    assert magnitude == pytest.approx(1.0, abs=1e-9)


async def test_empty_input_makes_no_call(nvidia_env: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made for an empty batch")

    provider = _provider(handler)
    result = await provider.embed_many([])
    assert result.vectors == []


# =============================================================================
# Asymmetry - the quiet correctness bug
# =============================================================================
async def test_query_and_passage_use_different_prefixes(nvidia_env: Any) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return _ok_response(request)

    provider = _provider(handler)
    await provider.embed_many(["what is the cap?"], input_type="query")
    await provider.embed_many(["Liability is capped at fees paid."], input_type="passage")

    assert sent[0]["input"][0].startswith("query:")
    assert sent[0]["input_type"] == "query"
    assert sent[1]["input"][0].startswith("passage:")
    assert sent[1]["input_type"] == "passage"


async def test_embed_query_helper_uses_the_query_side(nvidia_env: Any) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return _ok_response(request)

    provider = _provider(handler)
    await provider.embed_query("termination for convenience")

    assert sent[0]["input_type"] == "query"


# =============================================================================
# Matryoshka truncation
# =============================================================================
async def test_truncates_and_renormalises(settings_env: Any) -> None:
    """A wider response is sliced to EMBEDDING_DIM and re-normalised.

    Re-normalisation is the part that matters: a slice of a unit vector is shorter
    than unit length, and mixing sliced with unsliced would make the sliced ones
    score systematically lower against everything.
    """
    settings_env(
        EMBEDDING_PROVIDER="nvidia",
        EMBEDDING_MODEL="nvidia/nemotron-3-embed-1b",
        EMBEDDING_DIM="64",
        EMBEDDING_BATCH_SIZE="8",
        NVIDIA_API_KEY="k",
        NVIDIA_BASE_URL="https://nim.test/v1",
    )
    provider = _provider(lambda request: _ok_response(request, dim=256))
    result = await provider.embed_many(["alpha"])

    assert len(result.vectors[0]) == 64
    magnitude = sum(v * v for v in result.vectors[0]) ** 0.5
    assert magnitude == pytest.approx(1.0, abs=1e-9)


async def test_narrower_than_configured_is_rejected_not_padded(settings_env: Any) -> None:
    settings_env(
        EMBEDDING_PROVIDER="nvidia",
        EMBEDDING_MODEL="nvidia/nemotron-3-embed-1b",
        EMBEDDING_DIM="128",
        NVIDIA_API_KEY="k",
        NVIDIA_BASE_URL="https://nim.test/v1",
    )
    provider = _provider(lambda request: _ok_response(request, dim=64))

    with pytest.raises(ProviderError, match="128 dimensions, got 64"):
        await provider.embed_many(["alpha"])


# =============================================================================
# Malformed responses
# =============================================================================
async def test_missing_vector_is_rejected(nvidia_env: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": _vector(1.0)}], "usage": {}}
        )

    provider = _provider(handler)
    with pytest.raises(ProviderError, match="missing"):
        await provider.embed_many(["a", "b"])


async def test_duplicate_index_is_rejected(nvidia_env: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "embedding": _vector(1.0)},
                    {"index": 0, "embedding": _vector(2.0)},
                ],
                "usage": {},
            },
        )

    provider = _provider(handler)
    with pytest.raises(ProviderError, match="repeated index"):
        await provider.embed_many(["a", "b"])


async def test_out_of_range_index_is_rejected(nvidia_env: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 7, "embedding": _vector(1.0)}]})

    provider = _provider(handler)
    with pytest.raises(ProviderError, match="out-of-range"):
        await provider.embed_many(["a"])


async def test_missing_data_array_is_rejected(nvidia_env: Any) -> None:
    provider = _provider(lambda request: httpx.Response(200, json={"object": "list"}))
    with pytest.raises(ProviderError, match="no 'data' array"):
        await provider.embed_many(["a"])


async def test_non_json_body_is_rejected(nvidia_env: Any) -> None:
    provider = _provider(lambda request: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(ProviderError, match="non-JSON"):
        await provider.embed_many(["a"])


# =============================================================================
# Failure translation and retries
# =============================================================================
async def test_invalid_api_key_is_not_retried(nvidia_env: Any) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"detail": "unauthorized"})

    provider = _provider(handler)
    with pytest.raises(ProviderError, match="NVIDIA_API_KEY"):
        await provider.embed_many(["a"])

    # A credential failure fails identically every time; retrying it only delays
    # the message the operator needs.
    assert attempts == 1


async def test_unknown_model_is_not_retried(nvidia_env: Any) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, json={"detail": "model not found"})

    provider = _provider(handler)
    with pytest.raises(ProviderError, match="does not recognise the model"):
        await provider.embed_many(["a"])
    assert attempts == 1


async def test_rate_limit_is_retried_then_surfaced(nvidia_env: Any) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, headers={"retry-after": "0"}, json={})

    provider = _provider(handler)
    with pytest.raises(ProviderRateLimitError):
        await provider.embed_many(["a"])

    assert attempts == 3  # EMBEDDING_MAX_RETRIES=2, so three attempts total


async def test_rate_limit_recovers_on_retry(nvidia_env: Any) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={})
        return _ok_response(request)

    provider = _provider(handler)
    result = await provider.embed_many(["a"])

    assert len(result.vectors) == 1
    assert attempts == 2


async def test_server_error_is_retried(nvidia_env: Any) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, text="upstream unavailable")

    provider = _provider(handler)
    with pytest.raises(ProviderUnavailableError):
        await provider.embed_many(["a"])
    assert attempts == 3


async def test_timeout_is_retried_then_surfaced(nvidia_env: Any) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("too slow", request=request)

    provider = _provider(handler)
    with pytest.raises(ProviderUnavailableError, match="timed out"):
        await provider.embed_many(["a"])
    assert attempts == 3


async def test_unreachable_endpoint_is_surfaced(nvidia_env: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    provider = _provider(handler)
    with pytest.raises(ProviderUnavailableError, match="unreachable"):
        await provider.embed_many(["a"])


async def test_probe_reports_failure_rather_than_raising(nvidia_env: Any) -> None:
    """The health endpoint needs a verdict, not an exception."""
    provider = _provider(lambda request: httpx.Response(401, json={}))
    probe = await provider.probe()

    assert probe.ok is False
    assert probe.error
    assert probe.provider == "nvidia"


async def test_probe_reports_the_returned_width(nvidia_env: Any) -> None:
    provider = _provider(_ok_response)
    probe = await provider.probe()

    assert probe.ok is True
    assert probe.dim == DIM


# =============================================================================
# Backoff
# =============================================================================
def test_backoff_respects_retry_after() -> None:
    assert _backoff(1, 5.0) == 5.0


def test_backoff_caps_a_hostile_retry_after() -> None:
    assert _backoff(1, 9999.0) == 30.0


def test_backoff_is_jittered() -> None:
    """Without jitter every worker that hit the same limit retries in lockstep."""
    samples = {_backoff(3, None) for _ in range(50)}
    assert len(samples) > 1

"""NVIDIA NIM embedding provider - ``nvidia/nemotron-3-embed-1b`` (§14).

Speaks the NIM embeddings REST API over ``httpx`` rather than through the OpenAI
SDK. The wire format is OpenAI-compatible, which makes borrowing the SDK tempting,
but three things here are not: the ``input_type`` field that selects query vs
passage behaviour, NVIDIA's own rate-limit headers, and the fact that pulling in the
OpenAI SDK would reintroduce exactly the dependency this deployment removed.

What this provider guarantees to everything downstream:

* **Positional alignment.** Responses are re-ordered by the ``index`` NIM returns,
  never trusted to arrive sorted. A silently re-ordered batch would attach one
  clause's vector to another and retrieval would keep working while being wrong.
* **Asymmetry.** ``query:`` and ``passage:`` prefixes are applied here, so no caller
  has to remember. Nemotron 3 Embed is trained with them; omitting them costs recall
  without raising anything.
* **Unit length.** Vectors are re-normalised after any Matryoshka truncation, so
  cosine distance and inner product stay equivalent and the HNSW indexes behave.
* **Retry only what can succeed.** A 401 or a 404 model name is retried zero times;
  a 429 or a 503 is retried with exponential backoff and honours ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Sequence
from typing import Any

import httpx

from app.ai.embedding.providers import (
    EmbeddingResult,
    EmbeddingUsage,
    IEmbeddingProvider,
    InputType,
)
from app.core import metrics
from app.core.config import get_settings
from app.core.errors import ProviderError, ProviderRateLimitError, ProviderUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: NIM maps these onto the model's own prefixes.
_INPUT_TYPES: dict[InputType, str] = {"query": "query", "passage": "passage"}

#: Retried with backoff. Everything else is a caller error that will fail again.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: Cap on a single backoff sleep, so a long ``Retry-After`` cannot stall a worker
#: past the stage timeout.
_MAX_BACKOFF_SECONDS = 30.0


class NvidiaEmbeddingProvider(IEmbeddingProvider):
    """Embeddings from an NVIDIA NIM endpoint, hosted or self-hosted."""

    name = "nvidia"

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings().embedding
        self._settings = settings
        self._owns_client = client is None
        # One pooled client for the process. Creating a client per call would open a
        # fresh TLS connection every time, which on a 4000-chunk contract is the
        # dominant cost of the whole embedding stage.
        self._client = client or httpx.AsyncClient(
            base_url=settings.nvidia_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.timeout_seconds, connect=10.0),
            limits=httpx.Limits(
                max_connections=settings.nvidia_max_connections,
                max_keepalive_connections=settings.nvidia_max_connections,
            ),
            headers=self._headers(),
        )
        self._last_success_at: float | None = None
        self._last_latency_ms: int = 0

    def _headers(self) -> dict[str, str]:
        settings = self._settings
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if settings.nvidia_api_key:
            headers["Authorization"] = f"Bearer {settings.nvidia_api_key}"
        return headers

    # ------------------------------------------------------------------ public
    @property
    def last_success_at(self) -> float | None:
        """Monotonic-independent wall clock of the last good response, or ``None``."""
        return self._last_success_at

    @property
    def last_latency_ms(self) -> int:
        return self._last_latency_ms

    async def embed_many(
        self, texts: Sequence[str], *, input_type: InputType = "passage"
    ) -> EmbeddingResult:
        """Embed a batch, chunked to ``EMBEDDING_BATCH_SIZE``."""
        items = list(texts)
        if not items:
            return EmbeddingResult(provider=self.name, model=self.model, dim=self.dim)

        started = time.perf_counter()
        batch_size = self._settings.batch_size
        vectors: list[list[float]] = []
        usage = EmbeddingUsage()

        for offset in range(0, len(items), batch_size):
            window = items[offset : offset + batch_size]
            batch_vectors, batch_usage = await self._embed_batch(window, input_type)
            vectors.extend(batch_vectors)
            usage = usage + batch_usage

        latency_ms = int((time.perf_counter() - started) * 1000)
        self._last_latency_ms = latency_ms
        self._last_success_at = time.time()

        # Validate the assembled batch, not each window: the alignment guarantee is
        # about the caller's list, and a per-window check would miss a lost window.
        self._validate(vectors, len(items))

        metrics.embedding_duration_seconds.labels(provider=self.name).observe(latency_ms / 1000)
        return EmbeddingResult(
            vectors=vectors,
            model=self.model,
            dim=self.dim,
            usage=usage,
            latency_ms=latency_ms,
            provider=self.name,
        )

    async def health(self) -> bool:
        probe = await self.probe()
        return probe.ok

    async def aclose(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    def metadata(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model": self.model,
            "dim": self.dim,
            "native_dim": self._settings.native_dim,
            "truncated": self._settings.is_truncated,
            "base_url": self._settings.nvidia_base_url,
            "batch_size": self._settings.batch_size,
        }

    # ----------------------------------------------------------------- private
    async def _embed_batch(
        self, texts: list[str], input_type: InputType
    ) -> tuple[list[list[float]], EmbeddingUsage]:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": [self._prefix(text, input_type) for text in texts],
            "input_type": _INPUT_TYPES[input_type],
            "encoding_format": "float",
            "truncate": "END",
        }

        data = await self._post_with_retry("/embeddings", payload)
        raw = data.get("data")
        if not isinstance(raw, list):
            raise ProviderError(
                "The NVIDIA embeddings response had no 'data' array.",
                provider=self.name,
                details={"keys": sorted(data)[:10]},
            )

        # Ordered by the index NIM reports, never by arrival. The API documents that
        # `data` is ordered, but relying on it makes a future change to that
        # behaviour a silent mis-pairing rather than a loud failure.
        ordered: list[list[float]] = [[] for _ in texts]
        seen: set[int] = set()
        for entry in raw:
            if not isinstance(entry, dict):
                raise ProviderError(
                    "The NVIDIA embeddings response contained a malformed entry.",
                    provider=self.name,
                )
            index = entry.get("index")
            if not isinstance(index, int) or not 0 <= index < len(texts):
                raise ProviderError(
                    f"The NVIDIA embeddings response carried an out-of-range index {index!r} "
                    f"for a batch of {len(texts)}.",
                    provider=self.name,
                )
            if index in seen:
                raise ProviderError(
                    f"The NVIDIA embeddings response repeated index {index}.",
                    provider=self.name,
                )
            seen.add(index)
            ordered[index] = self._finalise(entry.get("embedding"))

        if len(seen) != len(texts):
            missing = sorted(set(range(len(texts))) - seen)
            raise ProviderError(
                f"The NVIDIA embeddings response was missing {len(missing)} of "
                f"{len(texts)} vectors. A partial batch cannot be matched to its "
                "inputs by position.",
                provider=self.name,
                details={"missing_indexes": missing[:20]},
            )

        reported = data.get("usage") or {}
        usage = EmbeddingUsage(
            input_tokens=int(reported.get("total_tokens") or reported.get("prompt_tokens") or 0),
            requests=1,
        )
        return ordered, usage

    def _prefix(self, text: str, input_type: InputType) -> str:
        prefix = (
            self._settings.nvidia_query_prefix
            if input_type == "query"
            else self._settings.nvidia_passage_prefix
        )
        if not prefix:
            return text
        return f"{prefix} {text}"

    def _finalise(self, embedding: Any) -> list[float]:
        """Truncate (Matryoshka) if configured, then re-normalise."""
        if not isinstance(embedding, (list, tuple)):
            raise ProviderError(
                "The NVIDIA embeddings response contained a non-array embedding.",
                provider=self.name,
            )
        vector = [float(value) for value in embedding]
        if len(vector) > self.dim:
            # Deliberate Matryoshka slicing, which this model supports; the
            # re-normalisation inside `truncate` is what keeps sliced and unsliced
            # vectors comparable. Anything *shorter* than configured is a real
            # mismatch and is caught by `_validate`, never padded.
            vector = self.truncate(vector, self.dim)
        return self.normalise(vector)

    async def _post_with_retry(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with exponential backoff on retryable failures only."""
        attempts = self._settings.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.post(path, json=payload)
            except httpx.TimeoutException as exc:
                last_error = ProviderUnavailableError(
                    f"The NVIDIA embeddings endpoint timed out after "
                    f"{self._settings.timeout_seconds}s.",
                    provider=self.name,
                )
                logger.warning("nvidia_embedding_timeout", attempt=attempt, error=str(exc)[:200])
            except asyncio.CancelledError:
                # Cancellation is the caller withdrawing, not a provider failure.
                # Swallowing it here would make a cancelled job keep hammering NIM.
                raise
            except httpx.HTTPError as exc:
                last_error = ProviderUnavailableError(
                    f"The NVIDIA embeddings endpoint is unreachable: {exc}",
                    provider=self.name,
                )
                logger.warning(
                    "nvidia_embedding_transport_error", attempt=attempt, error=str(exc)[:200]
                )
            else:
                if response.status_code < 400:
                    try:
                        parsed = response.json()
                    except ValueError as exc:
                        raise ProviderError(
                            "The NVIDIA embeddings endpoint returned a non-JSON body.",
                            provider=self.name,
                        ) from exc
                    if not isinstance(parsed, dict):
                        raise ProviderError(
                            "The NVIDIA embeddings endpoint returned an unexpected body.",
                            provider=self.name,
                        )
                    return parsed

                error = self._translate(response)
                if response.status_code not in _RETRYABLE_STATUS:
                    # 401/403/404/422 will fail identically every time. Retrying
                    # them burns the budget and delays the real message.
                    raise error
                last_error = error
                retry_after = _retry_after_seconds(response)
                logger.warning(
                    "nvidia_embedding_retryable",
                    attempt=attempt,
                    status=response.status_code,
                    retry_after=retry_after,
                )
                if attempt < attempts:
                    await asyncio.sleep(_backoff(attempt, retry_after))
                    continue

            if attempt < attempts:
                await asyncio.sleep(_backoff(attempt, None))

        assert last_error is not None
        raise last_error

    def _translate(self, response: httpx.Response) -> ProviderError:
        """Turn an HTTP failure into the error the pipeline knows how to act on."""
        status = response.status_code
        body = response.text[:400]

        if status == 429:
            return ProviderRateLimitError(
                "The NVIDIA embeddings endpoint is rate limiting this deployment.",
                provider=self.name,
                details={"retry_after": _retry_after_seconds(response)},
            )
        if status in {401, 403}:
            return ProviderError(
                "NVIDIA rejected the credentials. Check NVIDIA_API_KEY.",
                provider=self.name,
                retryable=False,
                details={"status": status},
            )
        if status == 404:
            return ProviderError(
                f"NVIDIA does not recognise the model '{self.model}' at "
                f"{self._settings.nvidia_base_url}. Check EMBEDDING_MODEL and "
                "NVIDIA_BASE_URL.",
                provider=self.name,
                retryable=False,
                details={"status": status, "body": body},
            )
        if status in {400, 422}:
            return ProviderError(
                f"NVIDIA rejected the embeddings request: {body}",
                provider=self.name,
                retryable=False,
                details={"status": status},
            )
        if status >= 500:
            return ProviderUnavailableError(
                f"The NVIDIA embeddings endpoint returned {status}.",
                provider=self.name,
                details={"status": status, "body": body},
            )
        return ProviderError(
            f"The NVIDIA embeddings endpoint returned {status}: {body}",
            provider=self.name,
            details={"status": status},
        )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """``Retry-After``, when the server tells us how long to wait."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        # The header also permits an HTTP date; backoff is a fine fallback and
        # parsing it is not worth the dependency.
        return None


def _backoff(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with jitter, respecting a server-supplied delay.

    Jitter matters here because the embedding stage fans out: without it, every
    worker that hit the same rate limit retries in lockstep and trips it again.
    """
    if retry_after is not None:
        return min(retry_after, _MAX_BACKOFF_SECONDS)
    base = min(2.0 ** (attempt - 1), _MAX_BACKOFF_SECONDS)
    return base * (0.5 + random.random() / 2)  # noqa: S311 - jitter, not cryptography


__all__ = ["NvidiaEmbeddingProvider"]

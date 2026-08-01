"""``IEmbeddingProvider`` - the vector-model abstraction (§14).

Switching embedding provider is configuration plus a re-embed, never a schema
change: the dimension comes from ``EMBEDDING_DIM`` and the column is sized from it.

Three properties this layer guarantees, because the rest of retrieval assumes them:

* **Fixed dimensionality.** A provider returning the wrong width would be accepted
  by pgvector only to fail every distance query, so the dimension is checked here
  and a mismatch is a hard error naming both numbers.
* **Normalised vectors.** Every provider returns unit-length vectors, so cosine
  distance and inner product agree and the HNSW indexes (built with
  ``vector_cosine_ops``) behave as expected.
* **Order preservation.** ``embed_many`` returns vectors positionally aligned with
  its input. The engine pairs them back up with chunk ids by index, so a provider
  that reordered or dropped one would silently attach the wrong vector to the wrong
  clause - the worst possible failure, because retrieval would still *work*.
"""

from __future__ import annotations

import hashlib
import math
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core import metrics
from app.core.config import get_settings
from app.core.errors import ProviderError, ProviderRateLimitError, ProviderUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Published per-MTok pricing, for cost attribution on jobs. Embedding is cheap but
#: not free, and a 500-page contract set is a real line item.
#:
#: NVIDIA is deliberately absent: a self-hosted NIM has no per-token price, and the
#: hosted endpoint bills in credits rather than dollars-per-MTok. An unknown model
#: attributes 0.0 rather than a guess - a fabricated figure in a cost report is
#: worse than an obviously missing one.
_PRICING: dict[str, float] = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "text-embedding-ada-002": 0.10,
}


@dataclass(slots=True)
class EmbeddingUsage:
    """Token accounting for one embedding call."""

    input_tokens: int = 0
    requests: int = 0

    def cost_usd(self, model: str) -> float:
        rate = _PRICING.get(_pricing_key(model), 0.0)
        return round((self.input_tokens / 1_000_000) * rate, 8)

    def __add__(self, other: EmbeddingUsage) -> EmbeddingUsage:
        return EmbeddingUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            requests=self.requests + other.requests,
        )

    def as_dict(self) -> dict[str, int]:
        return {"input_tokens": self.input_tokens, "requests": self.requests}


def _pricing_key(model: str) -> str:
    normalised = model.split("/")[-1]
    for known in _PRICING:
        if normalised.startswith(known):
            return known
    return normalised


@dataclass(slots=True)
class ProviderProbe:
    """Outcome of a single round-trip against the embedding provider."""

    provider: str
    model: str
    ok: bool
    dim: int | None = None
    latency_ms: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "ok": self.ok,
            "dim": self.dim,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


@dataclass(slots=True)
class EmbeddingResult:
    """Vectors plus what it cost to produce them."""

    vectors: list[list[float]] = field(default_factory=list)
    model: str = ""
    dim: int = 0
    usage: EmbeddingUsage = field(default_factory=EmbeddingUsage)
    latency_ms: int = 0
    provider: str = ""

    @property
    def cost_usd(self) -> float:
        return self.usage.cost_usd(self.model)

    def as_audit(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "dim": self.dim,
            "count": len(self.vectors),
            "latency_ms": self.latency_ms,
            "usage": self.usage.as_dict(),
            "cost_usd": self.cost_usd,
        }


#: Which side of a retrieval pair a text is.
#:
#: Modern retrieval embedders are *asymmetric*: they are trained so that a short
#: question and the long passage answering it land near each other, and they are
#: told which is which by a prefix. Nemotron 3 Embed wants ``query:`` and
#: ``passage:``. Getting it wrong raises no error and returns a perfectly valid
#: vector - it just quietly costs recall, which is why this is a parameter on the
#: interface rather than a detail inside one provider.
InputType = Literal["query", "passage"]

#: How long a query vector stays cached. Bounded rather than indefinite: the model
#: is configuration, and a re-pointed provider must not keep serving vectors from
#: the space it left. The model name is in the key too, so this is belt and braces.
_QUERY_CACHE_TTL_SECONDS = 3600


class IEmbeddingProvider(ABC):
    """Embedding contract. Implementations must be safe to call concurrently."""

    name: str = "abstract"

    @property
    def model(self) -> str:
        return get_settings().embedding.model

    @property
    def dim(self) -> int:
        return get_settings().embedding.dim

    @abstractmethod
    async def embed_many(
        self, texts: Sequence[str], *, input_type: InputType = "passage"
    ) -> EmbeddingResult:
        """Embed a batch, returning vectors **positionally aligned** with ``texts``.

        ``input_type`` defaults to ``passage`` because the overwhelming majority of
        calls are indexing document text; the query path opts in explicitly.
        """

    async def embed(self, text: str, *, input_type: InputType = "passage") -> list[float]:
        """Embed one string. Convenience over :meth:`embed_many`."""
        result = await self.embed_many([text], input_type=input_type)
        if not result.vectors:
            raise ProviderError("The embedding provider returned no vector.", provider=self.name)
        return result.vectors[0]

    async def embed_query(self, text: str) -> list[float]:
        """Embed a search string. Always use this on the retrieval path.

        Cached, unlike the passage path. Two different reasons make it worth it:

        * Contract review asks the same questions repeatedly - the same clause
          across a portfolio, the same diligence checklist against every target -
          so the hit rate is high in a way it never is for document text, which is
          embedded once at ingest.
        * The Copilot pre-warms this concurrently with query classification and the
          retrieval engine then calls it again. Without a cache that is two billed
          round trips for one question.

        Keyed on the model as well as the text: vectors from two models do not
        share a space, and serving one for the other would produce a search whose
        distances mean nothing. Fails open - a Redis outage costs the round trip,
        never the answer.
        """
        import hashlib

        from app.core.cache import cache_get, cache_set, make_key

        key = make_key(
            "query_embedding",
            self.model,
            hashlib.sha256(text.strip().encode()).hexdigest()[:32],
        )
        hit = await cache_get(key, cache_name="query_embedding")
        if isinstance(hit, list) and len(hit) == self.dim:
            return [float(value) for value in hit]

        vector = await self.embed(text, input_type="query")
        await cache_set(key, vector, ttl=_QUERY_CACHE_TTL_SECONDS, cache_name="query_embedding")
        return vector

    @abstractmethod
    async def health(self) -> bool: ...

    def metadata(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "dim": self.dim}

    async def probe(self) -> ProviderProbe:
        """Round-trip one tiny embedding and report what came back.

        Used by startup validation and the health endpoint. Returns a result rather
        than raising, because both callers want to *report* a broken provider, and
        one of them has to decide whether to abort the process.
        """
        started = time.perf_counter()
        try:
            vector = await self.embed("healthcheck", input_type="query")
        except Exception as exc:  # noqa: BLE001 - the message is the payload
            return ProviderProbe(
                provider=self.name,
                model=self.model,
                ok=False,
                error=str(exc)[:300],
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        return ProviderProbe(
            provider=self.name,
            model=self.model,
            ok=True,
            dim=len(vector),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # ------------------------------------------------------------------ helpers
    def _validate(self, vectors: list[list[float]], expected: int) -> list[list[float]]:
        """Check every vector before anything is stored.

        Four separate failures, each of which is silent if unchecked:

        * **Wrong count** misaligns every vector after the gap, attaching clause 5's
          vector to clause 4. Retrieval keeps working and keeps being wrong, which is
          the worst outcome available.
        * **Wrong width** is accepted by the insert and then fails every distance
          query, or - worse, under a permissive driver - is padded and silently
          corrupts the space.
        * **NaN or infinity** propagates through cosine distance and makes the
          affected row compare as "closest" or "furthest" against everything.
        * **Null or non-numeric entries** blow up at insert time, far from the
          provider call that produced them.
        """
        if len(vectors) != expected:
            raise ProviderError(
                f"The embedding provider returned {len(vectors)} vectors for "
                f"{expected} inputs. Vectors are matched to their source by position, "
                "so a partial batch cannot be used safely.",
                provider=self.name,
                details={"returned": len(vectors), "expected": expected},
            )
        configured = self.dim
        for index, vector in enumerate(vectors):
            validate_vector(vector, configured, provider=self.name, index=index)
        return vectors

    @staticmethod
    def normalise(vector: list[float]) -> list[float]:
        """Scale to unit length.

        Cosine distance on unit vectors is equivalent to inner product, which is what
        the HNSW indexes are built for. A zero vector is returned unchanged - it has
        no direction to preserve, and dividing by zero would produce NaNs that
        poison every comparison against it.
        """
        magnitude = math.sqrt(sum(value * value for value in vector))
        if magnitude == 0.0:
            return vector
        return [value / magnitude for value in vector]

    @staticmethod
    def truncate(vector: list[float], dim: int) -> list[float]:
        """Matryoshka slice, re-normalised.

        Nemotron 3 Embed supports keeping a leading prefix of the vector (1024, 512)
        rather than the full 2048. The re-normalisation is mandatory, not cosmetic:
        a slice of a unit vector is shorter than unit length, and mixing full-length
        and sliced vectors in one index without re-normalising makes the sliced ones
        systematically score lower against everything.
        """
        if len(vector) <= dim:
            return vector
        sliced = vector[:dim]
        magnitude = math.sqrt(sum(value * value for value in sliced))
        if magnitude == 0.0:
            return sliced
        return [value / magnitude for value in sliced]


def validate_vector(
    vector: Any,
    expected_dim: int,
    *,
    provider: str = "unknown",
    index: int | None = None,
) -> list[float]:
    """Reject anything that must never reach the vector column.

    Raises :class:`~app.core.errors.ProviderError` naming the specific defect, and
    logs it, because a malformed vector is usually a provider-side change rather
    than a bug here - and the log line is what tells the two apart.
    """

    def fail(reason: str, **details: Any) -> None:
        logger.error(
            "embedding_vector_rejected",
            provider=provider,
            reason=reason,
            index=index,
            expected_dim=expected_dim,
            **details,
        )
        raise ProviderError(
            f"The embedding provider returned an unusable vector: {reason}.",
            provider=provider,
            details={"reason": reason, "index": index, "expected_dim": expected_dim, **details},
        )

    if vector is None:
        fail("the vector is null")
    if not isinstance(vector, (list, tuple)):
        fail("the vector is not an array", got=type(vector).__name__)

    values = list(vector)
    if len(values) != expected_dim:
        fail(
            f"expected {expected_dim} dimensions, got {len(values)}. Set EMBEDDING_DIM "
            "to the model's real width and re-embed - a mismatched vector is never "
            "truncated or padded to fit",
            got=len(values),
        )

    cleaned: list[float] = []
    for position, value in enumerate(values):
        if value is None:
            fail("the vector contains a null element", position=position)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            fail("the vector contains a non-numeric element", position=position)
        number = float(value)
        if math.isnan(number):
            fail("the vector contains NaN", position=position)
        if math.isinf(number):
            fail("the vector contains infinity", position=position)
        cleaned.append(number)

    return cleaned


# =============================================================================
# OpenAI / Azure OpenAI
# =============================================================================
class OpenAIEmbeddingProvider(IEmbeddingProvider):
    """OpenAI-compatible embeddings. Serves Azure OpenAI too."""

    def __init__(self, *, azure: bool = False) -> None:
        self.azure = azure
        self.name = "azure_openai" if azure else "openai"
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        settings = get_settings()
        try:
            if self.azure:
                from openai import AsyncAzureOpenAI

                # Embeddings may live on a different Azure resource from the chat
                # model. Each of these falls back to the LLM's value, so one
                # resource serving both still needs nothing set.
                endpoint = settings.embedding.azure_endpoint or settings.llm.azure_openai_endpoint
                if not endpoint:
                    raise ProviderError(
                        "AZURE_OPENAI_EMBEDDING_ENDPOINT (or AZURE_OPENAI_ENDPOINT) is "
                        "required for azure_openai embeddings.",
                        provider=self.name,
                        retryable=False,
                    )
                self._client = AsyncAzureOpenAI(
                    azure_endpoint=endpoint,
                    api_key=(settings.embedding.azure_api_key or settings.llm.azure_openai_api_key),
                    api_version=(
                        settings.embedding.azure_api_version
                        or settings.llm.azure_openai_api_version
                    ),
                    timeout=float(settings.embedding.timeout_seconds),
                    max_retries=settings.embedding.max_retries,
                )
            else:
                from openai import AsyncOpenAI

                self._client = AsyncOpenAI(
                    api_key=settings.llm.openai_api_key or None,
                    timeout=float(settings.embedding.timeout_seconds),
                    max_retries=settings.embedding.max_retries,
                    **(
                        {"base_url": settings.llm.openai_base_url}
                        if settings.llm.openai_base_url
                        else {}
                    ),
                )
        except ImportError as exc:  # pragma: no cover
            raise ProviderError(
                "OpenAI embeddings require the 'ai' extra: pip install '.[ai]'",
                provider=self.name,
            ) from exc
        return self._client

    def _request_model(self) -> str:
        """What goes in the request's ``model`` field.

        On Azure that is the deployment name, which is chosen per resource and
        need not match the model it serves. ``self.model`` stays the *model*
        identity - it is what gets written to `embeddings.model` and what the
        reuse lookup keys on, so it must describe the vector space rather than
        the URL that produced it.
        """
        if self.azure:
            settings = get_settings()
            deployment = settings.embedding.azure_deployment or settings.llm.azure_openai_deployment
            if deployment:
                return deployment
        return self.model

    async def embed_many(
        self, texts: Sequence[str], *, input_type: InputType = "passage"
    ) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(model=self.model, dim=self.dim, provider=self.name)

        client = self._get_client()
        settings = get_settings()
        started = time.perf_counter()

        request: dict[str, Any] = {
            # Azure addresses a *deployment*, not a model name. Sending
            # `text-embedding-3-small` where the deployment is called something
            # else is a 404 on a URL that looks correct.
            "model": self._request_model(),
            "input": list(texts),
            # Explicit, because the OpenAI SDK otherwise sends
            # `encoding_format: base64` on its own as a bandwidth optimisation.
            # That is an OpenAI extension, and an OpenAI-compatible gateway
            # fronting another vendor rejects it outright:
            #
            #   400 - Nvidia embeddings do not support base64 encoding_format.
            #         Use float instead, or omit encoding_format.
            #
            # `float` is the format the API spec defines as the default, so it is
            # universally accepted; the extra bytes are irrelevant next to a
            # provider round trip.
            "encoding_format": "float",
        }
        # text-embedding-3-* support dimension reduction; asking for the configured
        # width avoids storing a vector we would have to truncate ourselves. Not
        # sent to other models: a gateway that only serves one width rejects the
        # parameter even when the value matches.
        if self.model.startswith("text-embedding-3"):
            request["dimensions"] = self.dim

        try:
            response = await client.embeddings.create(**request)
        except Exception as exc:
            metrics.embedding_failures_total.labels(provider=self.name).inc()
            raise _translate_error(exc, self.name) from exc

        # Sorted by index, not trusted to arrive in order: the API documents an
        # `index` field precisely because ordering is not guaranteed, and a
        # misordered batch would attach every vector to the wrong chunk.
        entries = sorted(response.data, key=lambda item: item.index)
        vectors = [self.normalise(list(entry.embedding)) for entry in entries]
        self._validate(vectors, len(texts))

        usage = EmbeddingUsage(
            input_tokens=int(getattr(response.usage, "prompt_tokens", 0) or 0),
            requests=1,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)

        metrics.embedding_duration_seconds.labels(provider=self.name).observe(latency_ms / 1000)

        logger.debug(
            "embeddings_created",
            provider=self.name,
            model=self.model,
            count=len(vectors),
            tokens=usage.input_tokens,
            latency_ms=latency_ms,
            batch_size=settings.embedding.batch_size,
        )
        return EmbeddingResult(
            vectors=vectors,
            model=self.model,
            dim=self.dim,
            usage=usage,
            latency_ms=latency_ms,
            provider=self.name,
        )

    async def health(self) -> bool:
        try:
            await self.embed_many(["health check"])
            return True
        except Exception as exc:  # noqa: BLE001 - health must answer, never raise
            logger.warning("embedding_health_failed", provider=self.name, error=str(exc))
            return False


# =============================================================================
# Local sentence-transformers
# =============================================================================
class SentenceTransformersProvider(IEmbeddingProvider):
    """Local embeddings, for air-gapped deployments.

    The model is loaded once per process and inference runs in a worker thread:
    sentence-transformers is synchronous and CPU-bound, so calling it directly on the
    event loop would stall every other request on the worker.
    """

    name = "sentence_transformers"

    def __init__(self) -> None:
        self._model: Any = None

    @property
    def model(self) -> str:
        return get_settings().embedding.sentence_transformers_model

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover
            raise ProviderError(
                "Local embeddings require the 'local-ml' extra: pip install '.[local-ml]'",
                provider=self.name,
            ) from exc
        logger.info("loading_local_embedding_model", model=self.model)
        self._model = SentenceTransformer(self.model)
        return self._model

    async def embed_many(
        self, texts: Sequence[str], *, input_type: InputType = "passage"
    ) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(model=self.model, dim=self.dim, provider=self.name)

        import asyncio

        started = time.perf_counter()
        model = self._get_model()

        def encode() -> list[list[float]]:
            raw = model.encode(
                list(texts),
                batch_size=get_settings().embedding.batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return [list(map(float, vector)) for vector in raw]

        try:
            vectors = await asyncio.to_thread(encode)
        except Exception as exc:
            metrics.embedding_failures_total.labels(provider=self.name).inc()
            raise ProviderError(f"Local embedding failed: {exc}", provider=self.name) from exc

        # Already normalised by the encoder, but re-normalised so the invariant holds
        # regardless of the flag above.
        vectors = [self.normalise(vector) for vector in vectors]
        self._validate(vectors, len(texts))

        latency_ms = int((time.perf_counter() - started) * 1000)
        metrics.embedding_duration_seconds.labels(provider=self.name).observe(latency_ms / 1000)
        return EmbeddingResult(
            vectors=vectors,
            model=self.model,
            dim=self.dim,
            # Local inference has no token bill; requests are still counted so
            # throughput is visible.
            usage=EmbeddingUsage(requests=1),
            latency_ms=latency_ms,
            provider=self.name,
        )

    async def health(self) -> bool:
        try:
            self._get_model()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("embedding_health_failed", provider=self.name, error=str(exc))
            return False


# =============================================================================
# Mock
# =============================================================================
class MockEmbeddingProvider(IEmbeddingProvider):
    """Deterministic hash-based embeddings.

    Not semantically meaningful, but it has the two properties the pipeline needs to
    be exercisable without a key: identical text always yields an identical vector
    (so duplicate reuse is testable), and different text yields a different one (so
    nearest-neighbour ordering is not degenerate). Production refuses to start with
    ``EMBEDDING_PROVIDER=mock``.
    """

    name = "mock"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def model(self) -> str:
        """``mock-`` prefixed, so a fake vector can never be mistaken for a real one.

        The prefix has to live on the property rather than only on the result, or
        the two write paths disagree: generated rows were stamped ``mock-<model>``
        while *reused* rows took the bare provider model. Two names for one space
        make ``existing_hashes`` - which filters on ``model`` - miss every reuse
        candidate, and leave a table that looks like it holds two spaces.
        """
        return f"mock-{get_settings().embedding.model}"

    async def embed_many(
        self, texts: Sequence[str], *, input_type: InputType = "passage"
    ) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(model=self.model, dim=self.dim, provider=self.name)

        started = time.perf_counter()
        self.calls.append(list(texts))
        vectors = [self._vector(text) for text in texts]
        self._validate(vectors, len(texts))

        return EmbeddingResult(
            vectors=vectors,
            model=self.model,
            dim=self.dim,
            usage=EmbeddingUsage(
                input_tokens=sum(max(1, len(text) // 4) for text in texts), requests=1
            ),
            latency_ms=int((time.perf_counter() - started) * 1000),
            provider=self.name,
        )

    def _vector(self, text: str) -> list[float]:
        """A unit vector derived from the text's digest.

        The digest is stretched by re-hashing with a counter so any configured
        dimension is reachable from a 32-byte hash, and the result is normalised so
        it behaves like a real embedding under cosine distance.
        """
        dim = self.dim
        raw: list[float] = []
        counter = 0
        payload = text.encode("utf-8")
        while len(raw) < dim:
            digest = hashlib.sha256(payload + counter.to_bytes(4, "big")).digest()
            # Map each byte to [-1, 1] so vectors spread over the space rather than
            # occupying one positive orthant, where everything looks similar.
            raw.extend((byte - 127.5) / 127.5 for byte in digest)
            counter += 1
        return self.normalise(raw[:dim])

    async def health(self) -> bool:
        return True


# =============================================================================
# Errors
# =============================================================================
def _translate_error(exc: Exception, provider: str) -> ProviderError:
    """Map an SDK exception onto the platform's error taxonomy.

    Most specific first: a rate limit is retryable with backoff, a connection error
    is retryable immediately, and a 4xx is not retryable at all - re-sending the same
    bad request just burns the retry budget.
    """
    name = type(exc).__name__
    message = str(exc)

    if "RateLimit" in name or "429" in message:
        return ProviderRateLimitError(
            f"The embedding provider is rate limiting: {message}", provider=provider
        )
    if "AuthenticationError" in name or "PermissionDenied" in name:
        return ProviderError(
            f"The embedding provider rejected the credentials: {message}",
            provider=provider,
            retryable=False,
        )
    if "BadRequest" in name or "UnprocessableEntity" in name:
        return ProviderError(
            f"The embedding request was rejected: {message}",
            provider=provider,
            retryable=False,
        )
    if "APIConnection" in name or "Timeout" in name:
        return ProviderUnavailableError(
            f"Could not reach the embedding provider: {message}", provider=provider
        )
    return ProviderError(f"Embedding failed: {message}", provider=provider)


# =============================================================================
# Factory
# =============================================================================
_provider: IEmbeddingProvider | None = None


def get_embedding_provider() -> IEmbeddingProvider:
    """The configured embedding provider (one per process)."""
    global _provider
    if _provider is not None:
        return _provider

    settings = get_settings()
    name = settings.embedding.provider

    if name == "nvidia":
        from app.ai.embedding.nvidia import NvidiaEmbeddingProvider

        _provider = NvidiaEmbeddingProvider()
    elif name == "openai":
        _provider = OpenAIEmbeddingProvider()
    elif name == "azure_openai":
        _provider = OpenAIEmbeddingProvider(azure=True)
    elif name == "sentence_transformers":
        _provider = SentenceTransformersProvider()
    elif name == "mock":
        if settings.is_production:  # pragma: no cover - guarded at config load too
            raise ProviderError(
                "EMBEDDING_PROVIDER=mock is not permitted in production.",
                provider="mock",
                retryable=False,
            )
        _provider = MockEmbeddingProvider()
    else:  # pragma: no cover - Literal-constrained
        raise ProviderError(f"Unknown embedding provider '{name}'.", provider=str(name))

    logger.info(
        "embedding_provider_selected",
        provider=_provider.name,
        model=_provider.model,
        dim=_provider.dim,
    )
    return _provider


def set_embedding_provider(provider: IEmbeddingProvider | None) -> None:
    """Override the provider. Tests only."""
    global _provider
    _provider = provider


async def embedding_health() -> dict[str, Any]:
    provider = get_embedding_provider()
    return {**provider.metadata(), "healthy": await provider.health()}


def content_hash(text: str) -> str:
    """The duplicate-detection key for a piece of embeddable text.

    Hashed after whitespace normalisation so a re-parse that changes only spacing
    reuses the existing vector instead of paying to produce an identical one.
    """
    normalised = " ".join(text.split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


__all__ = [
    "EmbeddingResult",
    "EmbeddingUsage",
    "IEmbeddingProvider",
    "MockEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "SentenceTransformersProvider",
    "content_hash",
    "embedding_health",
    "get_embedding_provider",
    "set_embedding_provider",
]

"""``IInferenceProvider`` - the model abstraction (§17).

The RAG engine, extraction engine and classifier all talk to this interface, so
the platform is vendor-independent: switching provider is configuration plus one
adapter, with no change to prompts, schemas or downstream code.

Three things this layer owns, because getting them wrong is expensive:

* **Structured output is a first-class request, not a prompt trick.** Extraction
  needs JSON that conforms to a schema. Anthropic enforces that with
  ``output_config.format``, so a malformed response is a provider-level
  impossibility rather than something the validator has to catch. Free-form
  output is prohibited (§13).
* **Prompt caching.** Extraction sends the same system prompt and output schema
  across thousands of chunks; that prefix is the bulk of the token spend. A
  ``cache_control`` breakpoint on the stable prefix turns it into ~0.1x cache
  reads. The volatile chunk text goes *after* the breakpoint - caching is a prefix
  match, so putting it earlier would invalidate every entry.
* **Refusal handling.** Safety classifiers can decline a request with a *success*
  HTTP 200 and ``stop_reason: "refusal"``. Contract language around indemnities,
  security obligations and breach remedies sits close enough to those categories
  that false positives are a genuine operational risk, so the provider checks
  ``stop_reason`` before touching ``content`` and opts into a server-side
  fallback.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core import metrics
from app.core.config import get_settings
from app.core.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    SchemaValidationError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

Purpose = Literal[
    "extraction", "classification", "summary", "rag", "comparison", "report", "planner"
]

#: Published per-MTok pricing, used for cost attribution on jobs and answers.
#: Cache reads bill at ~0.1x input; 5-minute cache writes at 1.25x.
#:
#: Azure addresses a *deployment*, and the deployment name is what the response
#: reports, so the GPT rows are keyed on the model name the deployment is created
#: from - ``AZURE_OPENAI_DEPLOYMENT=gpt-4.1`` matches directly, and
#: :func:`_pricing_key` prefix-matches a deployment named after a dated snapshot.
#: An unlisted model prices at zero, which reads as "this run was free" rather
#: than as "we have no rate for it" - which is why the model in use is listed.
_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

_CACHE_READ_MULTIPLIER = 0.1
_CACHE_WRITE_MULTIPLIER = 1.25

#: Purposes routed to the stronger model / higher effort. Multi-document
#: reasoning is where a cheaper model starts inventing clause text.
_COMPLEX_PURPOSES: frozenset[str] = frozenset({"comparison", "report"})
#: Purposes cheap enough for the small model.
_SIMPLE_PURPOSES: frozenset[str] = frozenset({"planner"})


@dataclass(slots=True)
class TokenUsage:
    """Token accounting for one call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def cost_usd(self, model: str) -> float:
        """Estimated spend, honouring cache pricing.

        Uncached input, cache reads and cache writes bill at different rates, so a
        flat token count would overstate a cache-heavy extraction run several-fold.
        """
        input_rate, output_rate = _PRICING.get(_pricing_key(model), (0.0, 0.0))
        million = 1_000_000
        return round(
            (self.input_tokens / million) * input_rate
            + (self.cache_read_tokens / million) * input_rate * _CACHE_READ_MULTIPLIER
            + (self.cache_write_tokens / million) * input_rate * _CACHE_WRITE_MULTIPLIER
            + (self.output_tokens / million) * output_rate,
            6,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total,
        }

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


def _pricing_key(model: str) -> str:
    """Normalise a model id to its pricing key.

    Strips provider prefixes (Bedrock's ``anthropic.``) and any date suffix so a
    pinned snapshot still prices correctly.
    """
    normalised = model.split("/")[-1].removeprefix("anthropic.")
    if normalised in _PRICING:
        return normalised
    for known in _PRICING:
        if normalised.startswith(known):
            return known
    return normalised


@dataclass(slots=True)
class InferenceResult:
    """One completion plus everything needed to audit it."""

    text: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0
    stop_reason: str | None = None
    #: True when safety classifiers declined the request. A first-class outcome,
    #: not an exception - the caller decides whether to degrade or escalate.
    refused: bool = False
    refusal_category: str | None = None
    #: Populated when a server-side fallback model served the response.
    served_by_fallback: bool = False
    #: Summarised reasoning, when the caller asked to surface it.
    thinking: str | None = None
    provider: str = ""

    @property
    def cost_usd(self) -> float:
        return self.usage.cost_usd(self.model)

    def as_audit(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "stop_reason": self.stop_reason,
            "refused": self.refused,
            "refusal_category": self.refusal_category,
            "served_by_fallback": self.served_by_fallback,
            "latency_ms": self.latency_ms,
            "usage": self.usage.as_dict(),
            "cost_usd": self.cost_usd,
        }


@dataclass(slots=True)
class StructuredResult:
    """A schema-constrained response together with its audit record.

    The parsed data and the call metadata travel together because every extraction
    has to be attributable: which model produced this clause, at what cost, with how
    many cached tokens. Returning only the dict would leave per-category cost
    accounting (§17) and provenance (§25) with nothing to record.
    """

    data: dict[str, Any]
    inference: InferenceResult

    @property
    def usage(self) -> TokenUsage:
        return self.inference.usage

    @property
    def model(self) -> str:
        return self.inference.model

    @property
    def cost_usd(self) -> float:
        return self.inference.cost_usd


class IInferenceProvider(ABC):
    """Inference contract. Implementations must be safe to call concurrently."""

    name: str = "abstract"

    @abstractmethod
    async def generate(
        self,
        *,
        system: str,
        prompt: str,
        purpose: Purpose = "rag",
        max_tokens: int | None = None,
        cache_prefix: bool = True,
        surface_thinking: bool = False,
    ) -> InferenceResult:
        """Free-form completion. Used for narrative answers and summaries."""

    @abstractmethod
    async def generate_structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        purpose: Purpose = "extraction",
        max_tokens: int | None = None,
        cache_prefix: bool = True,
    ) -> StructuredResult:
        """Completion constrained to ``schema``, returned parsed and audited.

        The extraction path. Raises :class:`~app.core.errors.SchemaValidationError`
        when the response cannot be parsed - retryable, per §13.
        """

    @abstractmethod
    def stream(
        self,
        *,
        system: str,
        prompt: str,
        purpose: Purpose = "rag",
        max_tokens: int | None = None,
        cache_prefix: bool = True,
    ) -> AsyncIterator[str]:
        """Token stream for the Copilot.

        Declared ``def``, not ``async def``: every implementation is an async
        *generator*, which returns an ``AsyncIterator`` directly rather than a
        coroutine yielding one. Declaring it ``async def`` here would make the
        interface promise a coroutine, so anyone coding to the interface would write
        ``await provider.stream(...)`` and get a ``TypeError`` against every real
        implementation. Callers iterate it: ``async for token in provider.stream(...)``.
        """

    @abstractmethod
    async def health(self) -> bool: ...

    def metadata(self) -> dict[str, Any]:
        settings = get_settings()
        return {
            "provider": self.name,
            "model": settings.llm.model,
            "model_simple": settings.llm.model_simple,
            "model_complex": settings.llm.model_complex,
            "effort": settings.llm.effort,
            "max_output_tokens": settings.llm.max_output_tokens,
        }

    # ------------------------------------------------------------------ routing
    #
    # Delegated to `app.ai.routing`, which owns the task -> tier table. These stay
    # as the public surface because every provider and several services call them;
    # what changed is that they no longer fall through to `settings.llm.model` for
    # anything unlisted. `extraction`, `classification` and `summary` - the three
    # highest-volume workloads - previously inherited that default, so pointing it
    # at a reasoning model silently made the whole pipeline slow.
    @staticmethod
    def route_model(purpose: Purpose | str) -> str:
        """The model for a task or legacy purpose."""
        from app.ai.routing import get_router

        return get_router().resolve(purpose).model

    @staticmethod
    def route_effort(purpose: Purpose | str) -> str:
        from app.ai.routing import get_router

        return get_router().resolve(purpose).effort

    @staticmethod
    def route_timeout(purpose: Purpose | str) -> float:
        """Per-tier request timeout, in seconds."""
        from app.ai.routing import get_router

        return get_router().resolve(purpose).timeout_seconds

    # -------------------------------------------------------------- JSON parsing
    @staticmethod
    def parse_json(text: str, *, context: str = "response") -> dict[str, Any]:
        """Parse a JSON payload from model output.

        With ``output_config.format`` the text is already valid JSON, so the
        salvage path below only matters for providers without schema enforcement.
        """
        stripped = text.strip()
        if not stripped:
            raise SchemaValidationError(
                f"The model returned an empty {context}.", stage="ai_extraction"
            )

        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = _salvage_json(stripped)
            if parsed is None:
                raise SchemaValidationError(
                    f"The model {context} was not valid JSON.",
                    stage="ai_extraction",
                    details={"preview": stripped[:400]},
                ) from None

        if not isinstance(parsed, dict):
            raise SchemaValidationError(
                f"The model {context} was not a JSON object.",
                stage="ai_extraction",
                details={"type": type(parsed).__name__},
            )
        return parsed


def _salvage_json(text: str) -> dict[str, Any] | None:
    """Recover a JSON object wrapped in prose or a fenced code block."""
    if text.startswith("```"):
        body = text.split("```", 2)
        if len(body) >= 2:
            candidate = body[1]
            if candidate.startswith("json"):
                candidate = candidate[4:]
            try:
                loaded = json.loads(candidate.strip())
                return loaded if isinstance(loaded, dict) else None
            except json.JSONDecodeError:
                pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            loaded = json.loads(text[start : end + 1])
            return loaded if isinstance(loaded, dict) else None
        except json.JSONDecodeError:
            return None
    return None


# =============================================================================
# Anthropic
# =============================================================================
class AnthropicProvider(IInferenceProvider):
    """Claude via the official Anthropic SDK."""

    name = "anthropic"

    def __init__(self) -> None:
        self.settings = get_settings()
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover
                raise ProviderError(
                    "Anthropic support requires the 'ai' extra: pip install '.[ai]'",
                    provider=self.name,
                ) from exc

            kwargs: dict[str, Any] = {
                "timeout": float(self.settings.llm.timeout_seconds),
                # The SDK retries 429/5xx with backoff; the stage runner's own retry
                # sits above this for anything it cannot recover.
                "max_retries": self.settings.llm.max_retries,
            }
            # An unset key is not an error: the SDK also resolves an `ant auth login`
            # profile, and on AWS/Bedrock the platform client supplies credentials.
            if self.settings.llm.anthropic_api_key:
                kwargs["api_key"] = self.settings.llm.anthropic_api_key
            self._client = AsyncAnthropic(**kwargs)
        return self._client

    # ------------------------------------------------------------------ request
    def _build_request(
        self,
        *,
        system: str,
        prompt: str,
        purpose: Purpose,
        max_tokens: int | None,
        cache_prefix: bool,
        schema: dict[str, Any] | None = None,
        surface_thinking: bool = False,
        streaming: bool = False,
    ) -> dict[str, Any]:
        """Assemble the request.

        Deliberately omits ``temperature``/``top_p``/``top_k``: current Claude
        models reject them with a 400, and behaviour is steered by prompt and
        effort instead.
        """
        model = self.route_model(purpose)
        effort = self.route_effort(purpose)

        ceiling = (
            self.settings.llm.max_output_tokens_streaming
            if streaming
            else self.settings.llm.max_output_tokens
        )
        resolved_max = min(max_tokens or ceiling, ceiling)

        # System prompt as a block list so the stable prefix can carry a cache
        # breakpoint. Extraction reuses this prefix across every chunk.
        system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cache_prefix and self.settings.llm.prompt_caching_enabled:
            system_blocks[-1]["cache_control"] = {"type": "ephemeral"}

        request: dict[str, Any] = {
            "model": model,
            "max_tokens": resolved_max,
            "system": system_blocks,
            # Volatile content goes after the cached prefix - caching is a prefix
            # match, so anything varying per call must come last.
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": effort},
        }

        # Adaptive thinking: the model decides depth per request. No budget_tokens -
        # removed on current models and rejected with a 400.
        request["thinking"] = {
            "type": "adaptive",
            **({"display": "summarized"} if surface_thinking else {}),
        }

        if schema is not None:
            # Schema enforcement at the API layer: malformed JSON becomes
            # impossible rather than something the validator must catch (§13).
            request["output_config"]["format"] = {
                "type": "json_schema",
                "schema": schema,
            }

        return request

    def _fallback_kwargs(self) -> dict[str, Any]:
        """Server-side refusal fallback, when enabled.

        ``"default"`` lets Anthropic route by refusal category rather than pinning
        a model that would need migrating later.
        """
        if not self.settings.llm.refusal_fallback_enabled:
            return {}
        return {
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }

    # ------------------------------------------------------------------ generate
    async def generate(
        self,
        *,
        system: str,
        prompt: str,
        purpose: Purpose = "rag",
        max_tokens: int | None = None,
        cache_prefix: bool = True,
        surface_thinking: bool = False,
    ) -> InferenceResult:
        request = self._build_request(
            system=system,
            prompt=prompt,
            purpose=purpose,
            max_tokens=max_tokens,
            cache_prefix=cache_prefix,
            surface_thinking=surface_thinking,
        )
        return await self._invoke(request, purpose=purpose)

    async def generate_structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        purpose: Purpose = "extraction",
        max_tokens: int | None = None,
        cache_prefix: bool = True,
    ) -> StructuredResult:
        from app.ai.rag.schema_compat import compile_strict, restore_payload

        compiled = compile_strict(schema)
        request = self._build_request(
            system=system,
            prompt=prompt,
            purpose=purpose,
            max_tokens=max_tokens,
            cache_prefix=cache_prefix,
            schema=compiled.schema,
        )
        result = await self._invoke(request, purpose=purpose)

        if result.refused:
            raise ProviderError(
                "The model declined to process this content.",
                provider=self.name,
                details={"category": result.refusal_category},
            )
        if result.stop_reason == "max_tokens":
            # Schema-conformant but truncated: retry with more room rather than
            # letting a half-object reach the validator.
            raise SchemaValidationError(
                "The structured response was truncated before completing.",
                stage="ai_extraction",
                details={"max_tokens": request["max_tokens"]},
            )
        return StructuredResult(
            # Undoes the free-form rewrite, so the caller receives the shape its
            # own schema described rather than the one the provider could carry.
            data=restore_payload(
                self.parse_json(result.text, context="structured response"),
                compiled.freeform_paths,
            ),
            inference=result,
        )

    async def stream(
        self,
        *,
        system: str,
        prompt: str,
        purpose: Purpose = "rag",
        max_tokens: int | None = None,
        cache_prefix: bool = True,
    ) -> AsyncIterator[str]:
        request = self._build_request(
            system=system,
            prompt=prompt,
            purpose=purpose,
            max_tokens=max_tokens,
            cache_prefix=cache_prefix,
            streaming=True,
        )
        client = self._get_client()
        started = time.perf_counter()

        try:
            async with client.beta.messages.stream(**request, **self._fallback_kwargs()) as stream:
                async for chunk in stream.text_stream:
                    yield chunk
                final = await stream.get_final_message()
        except Exception as exc:
            raise _translate_anthropic_error(exc, self.name) from exc

        self._record(final, purpose=purpose, started=started, streaming=True)

    # -------------------------------------------------------------------- invoke
    async def _invoke(self, request: dict[str, Any], *, purpose: Purpose) -> InferenceResult:
        """One completion, retried on the failures a retry can fix.

        This used to be a bare single call. A 429 or a 5xx - which on a shared
        organisation rate limit is a routine morning, not an incident - propagated
        straight out as a ``ProviderError`` and became a 500 for whoever asked the
        question. The OpenAI adapter has gone through ``call_with_retries`` since it
        was written; the asymmetry was an oversight, not a decision.
        """
        from app.ai.resilience import call_with_retries, record_payload_sizes
        from app.ai.routing import get_router

        client = self._get_client()
        choice_spec = get_router().resolve(purpose)
        started = time.perf_counter()

        async def _attempt() -> Any:
            # The beta endpoint is required for the fallback parameter; the request
            # body is otherwise identical.
            return await client.beta.messages.create(**request, **self._fallback_kwargs())

        try:
            # Per-tier deadline, so a hung simple-tier call does not hold a worker
            # slot for the reasoning-tier timeout.
            response, _outcome = await call_with_retries(
                _attempt,
                provider=self.name,
                tier=choice_spec.tier.value,
                model=request["model"],
                task=choice_spec.task.value,
                attempt_timeout=choice_spec.timeout_seconds,
            )
        except Exception as exc:
            metrics.llm_requests_total.labels(
                provider=self.name, model=request["model"], outcome="error"
            ).inc()
            raise _translate_anthropic_error(exc, self.name) from exc

        record_payload_sizes(
            tier=choice_spec.tier.value,
            prompt_chars=sum(
                len(str(block.get("text", "")))
                for message in request.get("messages", [])
                for block in (
                    message.get("content", [])
                    if isinstance(message.get("content"), list)
                    else [{"text": message.get("content", "")}]
                )
            ),
            completion_chars=0,
        )
        return self._record(response, purpose=purpose, started=started)

    def _record(
        self,
        response: Any,
        *,
        purpose: Purpose,
        started: float,
        streaming: bool = False,
    ) -> InferenceResult:
        """Turn an SDK response into an :class:`InferenceResult` and emit metrics."""
        latency_ms = int((time.perf_counter() - started) * 1000)
        usage_raw = getattr(response, "usage", None)
        usage = TokenUsage(
            input_tokens=int(getattr(usage_raw, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage_raw, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage_raw, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage_raw, "cache_creation_input_tokens", 0) or 0),
        )

        model = str(getattr(response, "model", "") or "unknown")
        stop_reason = getattr(response, "stop_reason", None)
        refused = stop_reason == "refusal"

        # stop_details is informational and may be null even on a refusal, so it is
        # never the branch condition - stop_reason is.
        category: str | None = None
        details = getattr(response, "stop_details", None)
        if details is not None:
            category = getattr(details, "category", None)

        text = ""
        thinking: str | None = None
        served_by_fallback = False

        if not refused:
            for block in getattr(response, "content", []) or []:
                block_type = getattr(block, "type", None)
                if block_type == "text":
                    text += getattr(block, "text", "") or ""
                elif block_type == "thinking":
                    captured = getattr(block, "thinking", "") or ""
                    if captured:
                        thinking = (thinking or "") + captured
                elif block_type == "fallback":
                    served_by_fallback = True

        # Sticky fallback turns carry no fallback block, so the served-by signal
        # also lives in the per-attempt usage iterations.
        for entry in getattr(usage_raw, "iterations", None) or []:
            if getattr(entry, "type", None) == "fallback_message":
                served_by_fallback = True

        metrics.llm_requests_total.labels(
            provider=self.name,
            model=model,
            outcome="refusal" if refused else "ok",
        ).inc()
        metrics.llm_duration_seconds.labels(provider=self.name, model=model).observe(
            latency_ms / 1000
        )
        for kind, count in (
            ("input", usage.input_tokens),
            ("output", usage.output_tokens),
            ("cache_read", usage.cache_read_tokens),
            ("cache_write", usage.cache_write_tokens),
        ):
            if count:
                metrics.llm_tokens_total.labels(model=model, kind=kind).inc(count)

        cost = usage.cost_usd(model)
        if cost:
            metrics.llm_cost_usd_total.labels(model=model).inc(cost)

        if refused:
            logger.warning(
                "model_refused",
                provider=self.name,
                model=model,
                purpose=purpose,
                category=category,
                served_by_fallback=served_by_fallback,
            )
        elif served_by_fallback:
            logger.info("served_by_fallback_model", model=model, purpose=purpose)

        return InferenceResult(
            text=text,
            model=model,
            usage=usage,
            latency_ms=latency_ms,
            stop_reason=stop_reason,
            refused=refused,
            refusal_category=category,
            served_by_fallback=served_by_fallback,
            thinking=thinking,
            provider=self.name,
        )

    async def health(self) -> bool:
        try:
            client = self._get_client()
            await client.models.retrieve(self.settings.llm.model)
            metrics.provider_health.labels(provider=self.name, kind="llm").set(1)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("anthropic_health_check_failed", error=str(exc))
            metrics.provider_health.labels(provider=self.name, kind="llm").set(0)
            return False

    async def count_tokens(self, *, system: str, prompt: str) -> int:
        """Exact token count for a prompt.

        Uses the provider's own counter rather than a local approximation -
        third-party tokenisers materially undercount Claude tokens.
        """
        client = self._get_client()
        response = await client.messages.count_tokens(
            model=self.settings.llm.model,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return int(response.input_tokens)


def _translate_anthropic_error(exc: Exception, provider: str) -> ProviderError:
    """Map SDK exceptions onto the platform's error taxonomy.

    Ordered most-specific first so retryability is classified correctly: a 429 or
    5xx is worth retrying, a 400 never is.
    """
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return ProviderError(str(exc), provider=provider)

    if isinstance(exc, anthropic.RateLimitError):
        retry_after = None
        response = getattr(exc, "response", None)
        if response is not None:
            retry_after = response.headers.get("retry-after")
        return ProviderRateLimitError(
            "The AI provider rate-limited this request.",
            provider=provider,
            details={"retry_after": retry_after},
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderUnavailableError("Could not reach the AI provider.", provider=provider)
    if isinstance(exc, anthropic.NotFoundError):
        return ProviderError(
            "The configured model does not exist or is not available to this key.",
            provider=provider,
            retryable=False,
        )
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ProviderError(
            "The AI provider rejected the credentials.",
            provider=provider,
            retryable=False,
        )
    if isinstance(exc, anthropic.BadRequestError):
        return ProviderError(
            f"The AI provider rejected the request: {exc}",
            provider=provider,
            retryable=False,
        )
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", 0)
        if status >= 500:
            return ProviderUnavailableError(
                "The AI provider is temporarily unavailable.", provider=provider
            )
        return ProviderError(str(exc), provider=provider, retryable=False)

    return ProviderError(f"Unexpected AI provider error: {exc}", provider=provider)


def _sanitise_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The provider-ready form of ``schema``, without the restoration plan.

    Kept as the module's public spelling because several call sites and tests use
    it. Anything sending a *strict* request should call
    :func:`~app.ai.rag.schema_compat.compile_strict` instead and keep the
    :class:`~app.ai.rag.schema_compat.CompiledSchema`, so the response can be
    restored - see that module for what "restored" means and why it is needed.
    """
    from app.ai.rag.schema_compat import compile_strict

    return compile_strict(schema).schema


# =============================================================================
# Factory
# =============================================================================
_provider: IInferenceProvider | None = None


def get_inference_provider() -> IInferenceProvider:
    """The configured inference provider (one per process).

    Adapters are imported lazily so a deployment installs only the SDK it uses.
    """
    global _provider
    if _provider is not None:
        return _provider

    settings = get_settings()
    configured = settings.llm.provider

    if configured == "mock":
        from app.ai.rag.mock_provider import MockInferenceProvider

        _provider = MockInferenceProvider()
    elif configured == "anthropic":
        _provider = AnthropicProvider()
    elif configured == "openai":
        from app.ai.rag.openai_provider import OpenAIProvider

        _provider = OpenAIProvider(azure=False)
    elif configured in {"azure_openai", "local"}:
        from app.ai.rag.openai_provider import OpenAIProvider

        # A self-hosted OpenAI-compatible server speaks the same wire format.
        _provider = OpenAIProvider(azure=configured == "azure_openai")
    else:  # pragma: no cover - Literal type makes this unreachable
        raise ProviderError(f"Unknown LLM provider: {configured}")

    logger.info(
        "inference_provider_initialised",
        provider=_provider.name,
        model=settings.llm.model,
        effort=settings.llm.effort,
        prompt_caching=settings.llm.prompt_caching_enabled,
        refusal_fallback=settings.llm.refusal_fallback_enabled,
    )
    return _provider


def set_inference_provider(provider: IInferenceProvider | None) -> None:
    """Override the provider. Used by tests to inject a recording stub."""
    global _provider
    _provider = provider


async def provider_health() -> dict[str, Any]:
    """Health and configuration of the inference layer, for ``/readyz`` and admin."""
    provider = get_inference_provider()
    healthy = await provider.health()
    return {"healthy": healthy, **provider.metadata()}


def estimate_tokens(text: str) -> int:
    """Local, offline token estimate.

    Used **only** for chunk-size budgeting, where hundreds of estimates are needed
    per document and a network round trip per chunk would dominate the stage.
    Deliberately not used for cost or billing: those come from the provider's
    reported ``usage``, and an exact count comes from the provider's own
    ``count_tokens`` endpoint.

    Calibrated for legal prose (~3.6 characters per token - denser than general
    English because of long defined terms and citations), then adjusted for
    whitespace-heavy text such as tables.
    """
    if not text:
        return 0
    characters = len(text)
    whitespace = sum(1 for char in text if char.isspace())
    # Whitespace-dominated content (markdown tables) tokenises less densely.
    density = 3.6 if whitespace / characters < 0.25 else 3.0
    return max(1, int(characters / density))


__all__ = [
    "AnthropicProvider",
    "IInferenceProvider",
    "InferenceResult",
    "Purpose",
    "TokenUsage",
    "estimate_tokens",
    "get_inference_provider",
    "provider_health",
    "set_inference_provider",
]

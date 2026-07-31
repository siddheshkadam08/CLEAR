"""OpenAI / Azure OpenAI inference provider.

One adapter serves both: Azure differs only in client construction (endpoint,
api-version, deployment name in place of model), so the request path is shared.

Structured output uses ``response_format={"type": "json_schema", ...}`` with
``strict: true``, which is the OpenAI equivalent of the schema enforcement the
Anthropic adapter gets from ``output_config.format`` - malformed JSON is a
provider-level impossibility rather than something the validator must catch.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from app.ai.rag.providers import (
    IInferenceProvider,
    InferenceResult,
    Purpose,
    StructuredResult,
    TokenUsage,
    _sanitise_schema,
)
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


class OpenAIProvider(IInferenceProvider):
    """OpenAI-compatible chat completions."""

    name = "openai"

    def __init__(self, *, azure: bool = False) -> None:
        self.settings = get_settings()
        self.azure = azure
        if azure:
            self.name = "azure_openai"
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        try:
            if self.azure:
                from openai import AsyncAzureOpenAI

                if not self.settings.llm.azure_openai_endpoint:
                    raise ProviderError(
                        "AZURE_OPENAI_ENDPOINT is required for the azure_openai provider.",
                        provider=self.name,
                        retryable=False,
                    )
                self._client = AsyncAzureOpenAI(
                    azure_endpoint=self.settings.llm.azure_openai_endpoint,
                    api_key=self.settings.llm.azure_openai_api_key,
                    api_version=self.settings.llm.azure_openai_api_version,
                    timeout=float(self.settings.llm.timeout_seconds),
                    max_retries=self.settings.llm.max_retries,
                )
            else:
                from openai import AsyncOpenAI

                # `base_url` only when configured: passing None would override the
                # SDK's own default and break plain OpenAI use.
                self._client = AsyncOpenAI(
                    api_key=self.settings.llm.openai_api_key or None,
                    timeout=float(self.settings.llm.timeout_seconds),
                    max_retries=self.settings.llm.max_retries,
                    **(
                        {"base_url": self.settings.llm.openai_base_url}
                        if self.settings.llm.openai_base_url
                        else {}
                    ),
                )
        except ImportError as exc:  # pragma: no cover
            raise ProviderError(
                "OpenAI support requires the 'ai' extra: pip install '.[ai]'",
                provider=self.name,
            ) from exc

        return self._client

    def _model_for(self, purpose: Purpose) -> str:
        """Resolve the model, or the deployment name on Azure."""
        if self.azure and self.settings.llm.azure_openai_deployment:
            return self.settings.llm.azure_openai_deployment
        return self.route_model(purpose)

    # ------------------------------------------------------------------ requests
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
        return await self._invoke(
            system=system,
            prompt=prompt,
            purpose=purpose,
            max_tokens=max_tokens,
            response_format=None,
        )

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
        # Models behind an OpenAI-compatible gateway frequently do not implement
        # the strict schema modes. When that is the case the schema is carried in
        # the prompt instead and the object is recovered from the text, which
        # `parse_json` already does for providers without enforcement.
        if self.settings.llm.llm_structured_output == "none":
            result = await self._invoke(
                system=(
                    f"{system}\n\n"
                    "Respond with a single JSON object conforming to this schema. "
                    "Output JSON only - no prose, no explanation, no code fence.\n"
                    f"{json.dumps(_sanitise_schema(schema))}"
                ),
                prompt=prompt,
                purpose=purpose,
                max_tokens=max_tokens,
                response_format=None,
            )
        else:
            result = await self._invoke(
                system=system,
                prompt=prompt,
                purpose=purpose,
                max_tokens=max_tokens,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "extraction",
                        "strict": True,
                        "schema": _sanitise_schema(schema),
                    },
                },
            )
        if result.stop_reason == "length":
            raise SchemaValidationError(
                "The structured response was truncated before completing.",
                stage="ai_extraction",
            )
        return StructuredResult(
            data=self.parse_json(result.text, context="structured response"),
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
        client = self._get_client()
        model = self._model_for(purpose)
        try:
            stream = await client.chat.completions.create(
                model=model,
                max_completion_tokens=max_tokens or self.settings.llm.max_output_tokens_streaming,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                stream=True,
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
        except Exception as exc:
            raise _translate_openai_error(exc, self.name) from exc

    async def _invoke(
        self,
        *,
        system: str,
        prompt: str,
        purpose: Purpose,
        max_tokens: int | None,
        response_format: dict[str, Any] | None,
    ) -> InferenceResult:
        from app.ai.resilience import call_with_retries, record_payload_sizes
        from app.ai.routing import get_router

        client = self._get_client()
        choice_spec = get_router().resolve(purpose)
        # Azure addresses a deployment rather than a model name; the router's
        # tier decision still applies to timeout and effort.
        model = self._model_for(purpose)
        started = time.perf_counter()

        request: dict[str, Any] = {
            "model": model,
            "max_completion_tokens": max_tokens or self.settings.llm.max_output_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if response_format is not None:
            request["response_format"] = response_format

        async def _attempt() -> Any:
            return await client.chat.completions.create(**request)

        try:
            # Per-tier deadline: an extraction that has not answered inside the
            # simple-tier window is not going to, and holding a worker slot for
            # the reasoning-tier timeout starves the pool.
            response, _outcome = await call_with_retries(
                _attempt,
                provider=self.name,
                tier=choice_spec.tier.value,
                model=model,
                task=choice_spec.task.value,
                attempt_timeout=choice_spec.timeout_seconds,
            )
        except Exception as exc:
            metrics.llm_requests_total.labels(
                provider=self.name, model=model, outcome="error"
            ).inc()
            raise _translate_openai_error(exc, self.name) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        record_payload_sizes(
            tier=choice_spec.tier.value,
            prompt_chars=len(system) + len(prompt),
            completion_chars=len(
                (response.choices[0].message.content or "") if response.choices else ""
            ),
        )
        choice = response.choices[0] if response.choices else None
        text = (choice.message.content if choice and choice.message else "") or ""
        finish_reason = getattr(choice, "finish_reason", None) if choice else None

        raw_usage = getattr(response, "usage", None)
        cached = 0
        prompt_details = getattr(raw_usage, "prompt_tokens_details", None)
        if prompt_details is not None:
            cached = int(getattr(prompt_details, "cached_tokens", 0) or 0)

        usage = TokenUsage(
            # Cached tokens are reported inside prompt_tokens; separating them keeps
            # the cost estimate honest.
            input_tokens=max(0, int(getattr(raw_usage, "prompt_tokens", 0) or 0) - cached),
            output_tokens=int(getattr(raw_usage, "completion_tokens", 0) or 0),
            cache_read_tokens=cached,
        )

        metrics.llm_requests_total.labels(provider=self.name, model=model, outcome="ok").inc()
        metrics.llm_duration_seconds.labels(provider=self.name, model=model).observe(
            latency_ms / 1000
        )
        for kind, count in (
            ("input", usage.input_tokens),
            ("output", usage.output_tokens),
            ("cache_read", usage.cache_read_tokens),
        ):
            if count:
                metrics.llm_tokens_total.labels(model=model, kind=kind).inc(count)

        # A content filter refusing the request is the OpenAI analogue of a
        # classifier refusal, so it maps onto the same first-class outcome.
        refused = finish_reason == "content_filter"
        if refused:
            logger.warning("model_refused", provider=self.name, model=model, purpose=purpose)

        return InferenceResult(
            text=text,
            model=str(getattr(response, "model", model)),
            usage=usage,
            latency_ms=latency_ms,
            stop_reason=finish_reason,
            refused=refused,
            provider=self.name,
        )

    async def health(self) -> bool:
        try:
            client = self._get_client()
            await client.models.list()
            metrics.provider_health.labels(provider=self.name, kind="llm").set(1)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("openai_health_check_failed", provider=self.name, error=str(exc))
            metrics.provider_health.labels(provider=self.name, kind="llm").set(0)
            return False


def _translate_openai_error(exc: Exception, provider: str) -> ProviderError:
    """Map OpenAI SDK exceptions onto the platform taxonomy, specific first."""
    try:
        import openai
    except ImportError:  # pragma: no cover
        return ProviderError(str(exc), provider=provider)

    if isinstance(exc, openai.RateLimitError):
        return ProviderRateLimitError(
            "The AI provider rate-limited this request.", provider=provider
        )
    if isinstance(exc, openai.APIConnectionError):
        return ProviderUnavailableError("Could not reach the AI provider.", provider=provider)
    if isinstance(exc, openai.NotFoundError):
        return ProviderError(
            "The configured model or deployment does not exist.",
            provider=provider,
            retryable=False,
        )
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ProviderError(
            "The AI provider rejected the credentials.", provider=provider, retryable=False
        )
    if isinstance(exc, openai.BadRequestError):
        return ProviderError(
            f"The AI provider rejected the request: {exc}",
            provider=provider,
            retryable=False,
        )
    if isinstance(exc, openai.APIStatusError):
        if getattr(exc, "status_code", 0) >= 500:
            return ProviderUnavailableError(
                "The AI provider is temporarily unavailable.", provider=provider
            )
        return ProviderError(str(exc), provider=provider, retryable=False)

    return ProviderError(f"Unexpected AI provider error: {exc}", provider=provider)


__all__ = ["OpenAIProvider"]

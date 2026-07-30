"""Google Gemini inference provider (§12).

Speaks the Generative Language REST API over ``httpx`` rather than through the
``google-genai`` SDK, for the same reason the NVIDIA embedding provider does: it
keeps the base image free of a vendor SDK that only one deployment configuration
needs, and the surface actually used here - generateContent with a response schema -
is small and stable.

Two behaviours the rest of the pipeline depends on:

* **Structured output is schema-constrained server-side.** Extraction sends a
  ``responseSchema`` and asks for ``application/json``, so a malformed payload is a
  provider bug rather than a parsing gamble. The schema is sanitised first: Gemini's
  subset rejects several JSON Schema keywords the Clause Master emits.
* **A safety refusal is a result, not an exception.** Gemini can return no candidate
  at all, or finish with ``SAFETY``. Both surface as ``refused=True`` so the grounding
  layer can say "the model declined" rather than reporting a transport error, which
  would send an operator looking in the wrong place.

Cost routing follows the same shape as the other providers: a flash-tier model for
everything by default, with the complex tier reserved for intents the router marks
as such. Clause extraction is a constrained-schema task, not open-ended reasoning,
so the cheap tier is the correct default rather than a compromise.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.ai.rag.providers import (
    IInferenceProvider,
    InferenceResult,
    Purpose,
    StructuredResult,
    TokenUsage,
    _salvage_json,
)
from app.core.config import get_settings
from app.core.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    SchemaValidationError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_MAX_BACKOFF_SECONDS = 30.0

#: JSON Schema keywords Gemini's ``responseSchema`` subset does not accept. Passing
#: them through produces a 400 that names the field but not the reason, which is a
#: long debugging session for a solved problem.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "$schema",
        "$id",
        "$ref",
        "definitions",
        "$defs",
        "patternProperties",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minItems",
        "maxItems",
        "uniqueItems",
        "const",
        "examples",
        "default",
    }
)


def _sanitise_schema(schema: Any) -> Any:
    """Strip keywords Gemini rejects, recursively.

    Nullability is preserved by rewriting ``type: [T, "null"]`` into Gemini's
    ``nullable`` flag - the Clause Master relies on required-and-nullable to
    distinguish "the contract is silent" from "the extractor did not look", and
    dropping that distinction would lose a finding.
    """
    if isinstance(schema, list):
        return [_sanitise_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED_SCHEMA_KEYS:
            continue
        if key == "type" and isinstance(value, list):
            types = [entry for entry in value if entry != "null"]
            cleaned["type"] = types[0] if types else "string"
            if "null" in value:
                cleaned["nullable"] = True
            continue
        cleaned[key] = _sanitise_schema(value)
    return cleaned


class GeminiProvider(IInferenceProvider):
    """Inference from Google's Generative Language API."""

    name = "gemini"

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self.settings = settings
        self._llm = settings.llm
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._llm.gemini_base_url.rstrip("/"),
            timeout=httpx.Timeout(self._llm.timeout_seconds, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
            headers={"Content-Type": "application/json"},
        )

    # ------------------------------------------------------------------ public
    @property
    def model(self) -> str:
        return self._llm.gemini_model

    def model_for(self, purpose: Purpose) -> str:
        """Route by purpose. Cheap tier by default; complex only where it earns it."""
        if purpose in {"comparison", "risk_assessment", "compliance"}:
            return self._llm.gemini_model_complex
        if purpose in {"classification", "summary"}:
            return self._llm.gemini_model_simple
        return self._llm.gemini_model

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
        model = self.model_for(purpose)
        payload = self._payload(system, prompt, max_tokens)
        started = time.perf_counter()
        data = await self._post(model, payload)
        return self._to_result(data, model, started)

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
        model = self.model_for(purpose)
        payload = self._payload(system, prompt, max_tokens)
        payload["generationConfig"]["responseMimeType"] = "application/json"
        payload["generationConfig"]["responseSchema"] = _sanitise_schema(schema)

        started = time.perf_counter()
        data = await self._post(model, payload)
        result = self._to_result(data, model, started)

        if result.refused:
            # A refusal carries no JSON to parse. Returning an empty payload with
            # the refusal flag intact lets the extraction engine record "declined"
            # rather than "malformed", which are different problems.
            return StructuredResult(data={}, inference=result)

        try:
            parsed = json.loads(result.text)
        except json.JSONDecodeError:
            parsed = _salvage_json(result.text)
            if parsed is None:
                raise SchemaValidationError(
                    "Gemini returned a response that is not valid JSON despite a "
                    "response schema being supplied.",
                    details={"model": model, "preview": result.text[:300]},
                ) from None

        if not isinstance(parsed, dict):
            raise SchemaValidationError(
                "Gemini returned valid JSON that is not an object.",
                details={"model": model, "type": type(parsed).__name__},
            )
        return StructuredResult(data=parsed, inference=result)

    async def stream(
        self,
        *,
        system: str,
        prompt: str,
        purpose: Purpose = "rag",
        max_tokens: int | None = None,
        cache_prefix: bool = True,
    ) -> AsyncIterator[str]:
        """Token stream for the Copilot, over ``streamGenerateContent``.

        Declared ``async def`` returning an async generator to match the interface.
        Yields text fragments only: citations are validated after the text completes,
        so streaming a citation label would put a reference on screen that might then
        be withdrawn.
        """
        model = self.model_for(purpose)
        payload = self._payload(system, prompt, max_tokens)

        if not self._llm.google_api_key:
            raise ProviderError(
                "LLM_PROVIDER=gemini but GOOGLE_API_KEY is empty.",
                provider=self.name,
                retryable=False,
            )

        async with self._client.stream(
            "POST",
            f"/models/{model}:streamGenerateContent",
            params={"alt": "sse"},
            json=payload,
            headers={"x-goog-api-key": self._llm.google_api_key},
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise self._translate(response, model)

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                blob = line[5:].strip()
                if not blob or blob == "[DONE]":
                    continue
                try:
                    chunk = json.loads(blob)
                except json.JSONDecodeError:
                    continue
                for candidate in chunk.get("candidates") or []:
                    for part in (candidate.get("content") or {}).get("parts") or []:
                        text = part.get("text")
                        if text:
                            yield str(text)

    async def health(self) -> bool:
        try:
            await self.generate(system="ping", prompt="ping", max_tokens=8)
        except Exception as exc:  # noqa: BLE001 - health returns a verdict
            logger.warning("gemini_health_check_failed", error=str(exc)[:200])
            return False
        return True

    async def aclose(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    # ----------------------------------------------------------------- private
    def _payload(self, system: str, prompt: str, max_tokens: int | None) -> dict[str, Any]:
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens or self._llm.max_output_tokens,
            },
        }

    async def _post(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._llm.google_api_key:
            raise ProviderError(
                "LLM_PROVIDER=gemini but GOOGLE_API_KEY is empty.",
                provider=self.name,
                retryable=False,
            )

        path = f"/models/{model}:generateContent"
        attempts = self._llm.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.post(
                    path,
                    json=payload,
                    # Header rather than a query parameter: a key in the URL ends up
                    # in access logs and proxy traces.
                    headers={"x-goog-api-key": self._llm.google_api_key},
                )
            except httpx.TimeoutException:
                last_error = ProviderUnavailableError(
                    f"Gemini timed out after {self._llm.timeout_seconds}s.",
                    provider=self.name,
                )
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError as exc:
                last_error = ProviderUnavailableError(
                    f"Gemini is unreachable: {exc}", provider=self.name
                )
            else:
                if response.status_code < 400:
                    parsed = response.json()
                    if not isinstance(parsed, dict):
                        raise ProviderError(
                            "Gemini returned an unexpected body.", provider=self.name
                        )
                    return parsed

                error = self._translate(response, model)
                if response.status_code not in _RETRYABLE_STATUS:
                    raise error
                last_error = error

            if attempt < attempts:
                await asyncio.sleep(_backoff(attempt))

        if last_error is None:  # pragma: no cover - the loop cannot exit otherwise
            last_error = ProviderUnavailableError("Gemini did not respond.", provider=self.name)
        raise last_error

    def _to_result(self, data: dict[str, Any], model: str, started: float) -> InferenceResult:
        latency_ms = int((time.perf_counter() - started) * 1000)
        usage_raw = data.get("usageMetadata") or {}
        usage = TokenUsage(
            input_tokens=int(usage_raw.get("promptTokenCount") or 0),
            output_tokens=int(usage_raw.get("candidatesTokenCount") or 0),
            cache_read_tokens=int(usage_raw.get("cachedContentTokenCount") or 0),
        )

        candidates = data.get("candidates") or []
        if not candidates:
            # No candidate at all means the prompt itself was blocked upstream.
            reason = (data.get("promptFeedback") or {}).get("blockReason")
            return InferenceResult(
                text="",
                model=model,
                usage=usage,
                latency_ms=latency_ms,
                refused=True,
                refusal_category=str(reason) if reason else "blocked",
                stop_reason="blocked",
            )

        candidate = candidates[0]
        finish = str(candidate.get("finishReason") or "")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))

        if finish in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}:
            return InferenceResult(
                text=text,
                model=model,
                usage=usage,
                latency_ms=latency_ms,
                refused=True,
                refusal_category=finish,
                stop_reason=finish,
            )

        if finish == "MAX_TOKENS":
            # Surfaced rather than silently truncated: a half-written extraction is
            # worse than a failed one, because it looks complete.
            logger.warning("gemini_output_truncated", model=model, tokens=usage.output_tokens)

        return InferenceResult(
            text=text,
            model=model,
            usage=usage,
            latency_ms=latency_ms,
            stop_reason=finish or None,
        )

    def _translate(self, response: httpx.Response, model: str) -> ProviderError:
        status = response.status_code
        body = response.text[:400]

        if status == 429:
            return ProviderRateLimitError(
                "Gemini is rate limiting this deployment.", provider=self.name
            )
        if status in {401, 403}:
            return ProviderError(
                "Google rejected the credentials. Check GOOGLE_API_KEY.",
                provider=self.name,
                retryable=False,
                details={"status": status},
            )
        if status == 404:
            return ProviderError(
                f"Google does not recognise the model '{model}'. Check GEMINI_MODEL.",
                provider=self.name,
                retryable=False,
                details={"status": status, "body": body},
            )
        if status == 400:
            return ProviderError(
                f"Gemini rejected the request: {body}",
                provider=self.name,
                retryable=False,
                details={"status": status},
            )
        if status >= 500:
            return ProviderUnavailableError(
                f"Gemini returned {status}.", provider=self.name, details={"body": body}
            )
        return ProviderError(
            f"Gemini returned {status}: {body}", provider=self.name, details={"status": status}
        )


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter, so parallel workers do not retry in lockstep."""
    base = min(2.0 ** (attempt - 1), _MAX_BACKOFF_SECONDS)
    return base * (0.5 + random.random() / 2)  # noqa: S311 - jitter, not cryptography


__all__ = ["GeminiProvider"]

"""Retry, backoff and instrumentation for provider calls.

One place where every inference call is timed, retried and counted, so the
observability the operations team needs is a property of the call path rather
than something each adapter remembers to add.

Three decisions worth stating:

* **Retry only what a retry can fix.** A timeout, a 429 and a 5xx are transient;
  a 400, a schema violation and an auth failure will fail identically on every
  attempt, and retrying them burns the budget to reach the same answer three
  times slower. The classification is explicit rather than "retry everything".

* **Exponential backoff with jitter.** A provider rate limit is usually shared
  across the worker pool, so a fixed delay makes every worker retry in lockstep
  and re-trigger the limit together. Full jitter spreads them.

* **The per-tier timeout is the deadline for one attempt, not the whole call.**
  A three-attempt extraction with a 90s tier timeout can still take 4.5 minutes
  including backoff; ``deadline_seconds`` bounds the total so a stage cannot
  outlive its own worker lease.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.core import metrics
from app.core.config import get_settings
from app.core.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

#: Retry reasons, used as a metric label. Kept small and closed so the label
#: cardinality stays bounded.
REASON_TIMEOUT = "timeout"
REASON_RATE_LIMIT = "rate_limit"
REASON_TRANSPORT = "transport"
REASON_SERVER = "server"


@dataclass(slots=True)
class CallOutcome:
    """What happened across all attempts of one logical call."""

    attempts: int = 0
    retries: int = 0
    total_seconds: float = 0.0
    last_reason: str | None = None
    reasons: list[str] = field(default_factory=list)

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "retries": self.retries,
            "duration_seconds": round(self.total_seconds, 3),
            "retry_reasons": self.reasons or None,
        }


def classify_retryable(exc: BaseException) -> str | None:
    """The retry reason for ``exc``, or ``None`` when a retry cannot help.

    Errors are classified by *type* rather than by message. Matching on message
    text is how a provider's wording change silently turns a retryable timeout
    into a terminal failure months later.
    """
    # `asyncio.TimeoutError` is an alias of the builtin from 3.11; both listed so
    # the intent survives if that ever diverges again.
    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return REASON_TIMEOUT
    if isinstance(exc, ProviderRateLimitError):
        return REASON_RATE_LIMIT
    if isinstance(exc, ProviderUnavailableError):
        return REASON_SERVER
    if isinstance(exc, ProviderError):
        # The provider layer already decided; honour it rather than second-guessing.
        return REASON_SERVER if getattr(exc, "retryable", False) else None

    # Transport-level failures from httpx/openai that never reached the model.
    name = type(exc).__name__
    if name in {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "RemoteProtocolError",
        "PoolTimeout",
    }:
        return REASON_TIMEOUT if "Timeout" in name else REASON_TRANSPORT
    if name in {"RateLimitError"}:
        return REASON_RATE_LIMIT
    if name in {"InternalServerError", "APIStatusError"}:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status >= 500:
            return REASON_SERVER
        return None
    return None


def backoff_delay(attempt: int, *, base: float, cap: float = 60.0) -> float:
    """Full-jitter exponential backoff for ``attempt`` (1-based).

    ``random.uniform(0, base * 2**(n-1))`` rather than a fixed ``base * 2**(n-1)``:
    a rate limit is shared across the pool, so deterministic delays make every
    worker retry simultaneously and re-trigger the limit they are backing off from.
    """
    ceiling = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(0.0, ceiling)  # noqa: S311 - jitter, not cryptography


async def call_with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    provider: str,
    tier: str,
    model: str,
    task: str,
    attempt_timeout: float,
    max_attempts: int | None = None,
    deadline_seconds: float | None = None,
) -> tuple[T, CallOutcome]:
    """Run ``operation`` with per-attempt timeout, backoff and instrumentation.

    Returns the result and an outcome record. Raises the final exception when
    every attempt fails, so the caller's error handling is unchanged.
    """
    settings = get_settings().llm
    attempts_allowed = max_attempts if max_attempts is not None else settings.max_retries + 1
    attempts_allowed = max(1, attempts_allowed)
    base = float(getattr(settings, "retry_backoff_seconds", 1.0) or 1.0)

    outcome = CallOutcome()
    started = time.perf_counter()
    last_exc: BaseException | None = None

    for attempt in range(1, attempts_allowed + 1):
        outcome.attempts = attempt
        attempt_started = time.perf_counter()
        try:
            result = await asyncio.wait_for(operation(), timeout=attempt_timeout)
        except BaseException as exc:
            last_exc = exc
            elapsed = time.perf_counter() - attempt_started
            reason = classify_retryable(exc)

            if reason == REASON_TIMEOUT:
                metrics.llm_timeouts_total.labels(provider=provider, tier=tier, model=model).inc()
                logger.warning(
                    "llm_call_timeout",
                    provider=provider,
                    model=model,
                    task=task,
                    tier=tier,
                    attempt=attempt,
                    attempt_timeout=attempt_timeout,
                    elapsed_seconds=round(elapsed, 2),
                )

            if reason is None or attempt >= attempts_allowed:
                outcome.total_seconds = time.perf_counter() - started
                logger.warning(
                    "llm_call_failed",
                    provider=provider,
                    model=model,
                    task=task,
                    tier=tier,
                    terminal=True,
                    retryable=reason is not None,
                    error=type(exc).__name__,
                    **outcome.as_log_fields(),
                )
                raise

            delay = backoff_delay(attempt, base=base)
            # A retry that would blow the overall deadline is not worth starting -
            # the caller would time out mid-attempt and lose the error detail.
            if deadline_seconds is not None:
                spent = time.perf_counter() - started
                if spent + delay + attempt_timeout > deadline_seconds:
                    outcome.total_seconds = spent
                    logger.warning(
                        "llm_retry_abandoned_deadline",
                        provider=provider,
                        task=task,
                        tier=tier,
                        spent_seconds=round(spent, 2),
                        deadline_seconds=deadline_seconds,
                    )
                    raise

            outcome.retries += 1
            outcome.last_reason = reason
            outcome.reasons.append(reason)
            metrics.llm_retries_total.labels(provider=provider, tier=tier, reason=reason).inc()
            logger.info(
                "llm_call_retry",
                provider=provider,
                model=model,
                task=task,
                tier=tier,
                attempt=attempt,
                reason=reason,
                delay_seconds=round(delay, 2),
                error=type(exc).__name__,
            )
            await asyncio.sleep(delay)
            continue

        outcome.total_seconds = time.perf_counter() - started
        metrics.llm_task_duration_seconds.labels(
            task=task, tier=tier, provider=provider, model=model
        ).observe(outcome.total_seconds)
        logger.info(
            "llm_call_completed",
            provider=provider,
            model=model,
            task=task,
            tier=tier,
            **outcome.as_log_fields(),
        )
        return result, outcome

    # Unreachable: the loop either returns or raises.
    raise last_exc if last_exc else RuntimeError("call_with_retries exhausted without result")


def record_payload_sizes(*, tier: str, prompt_chars: int, completion_chars: int) -> None:
    """Track prompt/completion size so an evidence-budget regression is visible."""
    metrics.llm_payload_bytes.labels(direction="input", tier=tier).observe(max(0, prompt_chars))
    metrics.llm_payload_bytes.labels(direction="output", tier=tier).observe(
        max(0, completion_chars)
    )


__all__ = [
    "REASON_RATE_LIMIT",
    "REASON_SERVER",
    "REASON_TIMEOUT",
    "REASON_TRANSPORT",
    "CallOutcome",
    "backoff_delay",
    "call_with_retries",
    "classify_retryable",
    "record_payload_sizes",
]

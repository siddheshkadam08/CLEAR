"""Fan-out, severity filtering, retry and the no-raise guarantee.

Everything that must behave identically across channels lives here, so a provider
stays a formatter. Three rules this module enforces:

1. **An alert never changes the outcome of what raised it.** Every failure path is
   caught and logged. A Slack outage during an ingestion failure must not turn a
   recorded stage failure into a second, different exception - which would mask
   the original cause, the one thing an operator actually needs.
2. **Retries are bounded and backed off.** Chat webhooks rate-limit, and a batch of
   documents failing together would otherwise become a burst that gets the
   integration throttled exactly when it matters.
3. **A slow channel cannot hold up the pipeline.** Every attempt is wrapped in a
   timeout, and the whole dispatch is bounded by the per-provider budget.

Providers are dispatched concurrently: with several channels configured, the
alert should take as long as the slowest one, not the sum.
"""

from __future__ import annotations

import asyncio

from app.alerting.base import AlertEvent, AlertLevel, IAlertProvider
from app.core.logging import get_logger

logger = get_logger(__name__)


class AlertDispatcher:
    """Delivers an :class:`AlertEvent` to every configured provider."""

    def __init__(
        self,
        providers: list[IAlertProvider],
        *,
        min_level: AlertLevel = AlertLevel.ERROR,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
        backoff_max_seconds: float = 8.0,
        timeout_seconds: float = 10.0,
        enabled: bool = True,
    ) -> None:
        self._providers = providers
        self._min_level = min_level
        self._max_attempts = max(1, max_attempts)
        self._backoff_seconds = max(0.0, backoff_seconds)
        self._backoff_max_seconds = max(0.0, backoff_max_seconds)
        self._timeout_seconds = timeout_seconds
        self._enabled = enabled

    # ------------------------------------------------------------------ state
    @property
    def providers(self) -> list[IAlertProvider]:
        return list(self._providers)

    @property
    def min_level(self) -> AlertLevel:
        return self._min_level

    def should_dispatch(self, event: AlertEvent) -> bool:
        """Whether this event clears the configured floor."""
        return self._enabled and bool(self._providers) and event.level.at_least(self._min_level)

    # --------------------------------------------------------------- dispatch
    async def dispatch(self, event: AlertEvent) -> dict[str, bool]:
        """Deliver to every provider. Never raises.

        Returns a per-provider outcome map, which is what the tests assert on and
        what makes a partial delivery visible rather than silent.
        """
        if not self.should_dispatch(event):
            logger.debug(
                "alert_suppressed",
                reason="below_min_level" if self._enabled else "alerting_disabled",
                min_level=self._min_level.value,
                **event.as_log_fields(),
            )
            return {}

        results = await asyncio.gather(
            *(self._deliver(provider, event) for provider in self._providers),
            # Belt and braces: `_deliver` already swallows everything, but a bug in
            # it must still not propagate into the caller's failure path.
            return_exceptions=True,
        )

        outcome: dict[str, bool] = {}
        for provider, result in zip(self._providers, results, strict=True):
            outcome[provider.name] = result is True
        return outcome

    async def _deliver(self, provider: IAlertProvider, event: AlertEvent) -> bool:
        """One provider, with retry. Returns success; never raises."""
        delay = self._backoff_seconds

        for attempt in range(1, self._max_attempts + 1):
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    await provider.send(event)
            except asyncio.CancelledError:
                # Cancellation is the caller shutting us down, not a delivery
                # failure. Re-raise so shutdown is not silently swallowed.
                raise
            except Exception as exc:  # noqa: BLE001 - alerting must never propagate
                is_last = attempt >= self._max_attempts
                logger.warning(
                    "alert_delivery_failed",
                    provider=provider.name,
                    attempt=attempt,
                    max_attempts=self._max_attempts,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    will_retry=not is_last,
                    **event.as_log_fields(),
                )
                if is_last:
                    return False
                if delay > 0:
                    await asyncio.sleep(delay)
                delay = min(delay * 2, self._backoff_max_seconds) if delay else 0.0
                continue
            else:
                logger.info(
                    "alert_delivered",
                    provider=provider.name,
                    attempt=attempt,
                    **event.as_log_fields(),
                )
                return True

        return False

    async def aclose(self) -> None:
        """Release provider resources. Never raises."""
        for provider in self._providers:
            try:
                await provider.aclose()
            except Exception as exc:  # noqa: BLE001 - shutdown must not fail
                logger.warning(
                    "alert_provider_close_failed", provider=provider.name, error=str(exc)
                )


__all__ = ["AlertDispatcher"]

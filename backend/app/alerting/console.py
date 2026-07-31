"""Console provider - the default, and the reason alerting needs no configuration.

Writes the alert to the application's structured logger. That sounds like a no-op
next to Slack, but it is the difference between a failure that is *recorded* and
one that is silently swallowed, and it works in CI, in tests, on a laptop with no
network, and in a locked-down deployment with no outbound egress.
"""

from __future__ import annotations

from app.alerting.base import AlertEvent, AlertLevel, IAlertProvider
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Alert level -> logger method. CRITICAL maps to ``error`` rather than
#: ``critical`` because structlog's critical is rarely wired to anything distinct
#: in this deployment, and an ERROR-level record is what the log pipeline alerts on.
_LOG_METHOD: dict[AlertLevel, str] = {
    AlertLevel.INFO: "info",
    AlertLevel.WARNING: "warning",
    AlertLevel.ERROR: "error",
    AlertLevel.CRITICAL: "error",
}


class ConsoleAlertProvider(IAlertProvider):
    """Emit the alert as one structured log record."""

    name = "console"

    async def send(self, event: AlertEvent) -> None:
        method = getattr(logger, _LOG_METHOD[event.level])
        method(
            "operational_alert",
            alert_message=event.message,
            suggested_resolution=event.suggested_resolution,
            # The trace is the whole point of an operational alert, so unlike
            # `as_log_fields` this provider does include it.
            stack_trace=event.stack_trace,
            details=event.details or None,
            **event.as_log_fields(),
        )


__all__ = ["ConsoleAlertProvider"]

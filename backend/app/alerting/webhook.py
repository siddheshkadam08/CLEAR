"""Generic webhook provider.

Posts the alert as JSON with no vendor-specific shaping, which is what makes it
the integration point for PagerDuty Events, Opsgenie, an internal bus or a
customer's own receiver. The body is the :class:`~app.alerting.base.AlertEvent`
itself, so the schema is documented by the model rather than by prose here.
"""

from __future__ import annotations

from typing import Any

from app.alerting.base import AlertEvent
from app.alerting.http import HttpAlertProvider


class WebhookAlertProvider(HttpAlertProvider):
    """POST the alert payload verbatim to a configured URL."""

    name = "webhook"

    def __init__(
        self,
        url: str,
        *,
        timeout: float,
        token: str = "",
        headers: dict[str, str] | None = None,
        transport: Any = None,
    ) -> None:
        merged = dict(headers or {})
        if token:
            merged["Authorization"] = f"Bearer {token}"
        super().__init__(url, timeout=timeout, headers=merged, transport=transport)

    def build_payload(self, event: AlertEvent) -> dict[str, Any]:
        # mode="json" so UUIDs and datetimes are strings: the receiver is another
        # system, not Python, and a raw UUID object is not JSON-serialisable.
        payload = event.model_dump(mode="json")
        payload["dedupe_key"] = event.dedupe_key()
        return payload


__all__ = ["WebhookAlertProvider"]

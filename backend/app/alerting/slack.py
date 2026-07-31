"""Slack provider (incoming webhook).

Formatted with Block Kit rather than a plain ``text`` string so the important
fields - stage, document, trace - are scannable in a crowded channel. ``text`` is
still set because it is what Slack shows in the notification preview and in
clients that do not render blocks.
"""

from __future__ import annotations

from typing import Any

from app.alerting.base import AlertEvent, AlertLevel
from app.alerting.http import HttpAlertProvider

#: Leading emoji per level. Colour alone is not enough - a channel is read at a
#: glance and on mobile, where the attachment bar is a few pixels wide.
_EMOJI: dict[AlertLevel, str] = {
    AlertLevel.INFO: ":information_source:",
    AlertLevel.WARNING: ":warning:",
    AlertLevel.ERROR: ":x:",
    AlertLevel.CRITICAL: ":rotating_light:",
}

#: Slack truncates a section's text at 3000 characters and rejects the message
#: outright above it, so the trace is clipped well under the limit.
_MAX_TRACE_CHARS = 2400


class SlackAlertProvider(HttpAlertProvider):
    """Post an alert to a Slack incoming webhook."""

    name = "slack"

    def build_payload(self, event: AlertEvent) -> dict[str, Any]:
        header = f"{_EMOJI[event.level]} {event.level.value}: {event.title}"

        facts = [
            ("Environment", event.environment),
            ("Category", event.category.value),
            ("Stage", event.stage or "-"),
            ("Document", str(event.document_id) if event.document_id else "-"),
            ("Retry", str(event.retry_count)),
            ("Host", event.host_name),
        ]
        if event.worker_name:
            facts.append(("Worker", event.worker_name))
        if event.trace_id:
            facts.append(("Trace", event.trace_id))

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                # Slack rejects a header longer than 150 chars with a 400.
                "text": {"type": "plain_text", "text": header[:150], "emoji": True},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{event.message[:2000]}*"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{label}*\n{value}"} for label, value in facts
                ],
            },
        ]

        if event.suggested_resolution:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":wrench: *Suggested resolution*\n{event.suggested_resolution}",
                    },
                }
            )

        if event.stack_trace:
            trace = event.stack_trace[-_MAX_TRACE_CHARS:]
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"```{trace}```"},
                }
            )

        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"`{event.dedupe_key()}` · {event.timestamp.isoformat()}",
                    }
                ],
            }
        )

        return {"text": header, "blocks": blocks}


__all__ = ["SlackAlertProvider"]

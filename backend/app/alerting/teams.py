"""Microsoft Teams provider (incoming webhook).

Sends a MessageCard. The newer Adaptive Card format is richer, but Teams only
accepts it through Workflows/Power Automate connectors, while MessageCard works
with the plain "Incoming Webhook" connector that a team can add themselves - the
lower-friction option, and the one that does not break when a tenant has not
enabled Workflows.
"""

from __future__ import annotations

from typing import Any

from app.alerting.base import AlertEvent, AlertLevel
from app.alerting.http import HttpAlertProvider

#: MessageCard accepts a hex colour without the leading '#'. Chosen to read
#: correctly in both the light and dark Teams themes.
_THEME_COLOR: dict[AlertLevel, str] = {
    AlertLevel.INFO: "2563EB",
    AlertLevel.WARNING: "D97706",
    AlertLevel.ERROR: "DC2626",
    AlertLevel.CRITICAL: "7F1D1D",
}

_MAX_TRACE_CHARS = 2400


class MicrosoftTeamsAlertProvider(HttpAlertProvider):
    """Post an alert to a Teams incoming webhook."""

    name = "teams"

    def build_payload(self, event: AlertEvent) -> dict[str, Any]:
        facts = [
            {"name": "Environment", "value": event.environment},
            {"name": "Level", "value": event.level.value},
            {"name": "Category", "value": event.category.value},
            {"name": "Stage", "value": event.stage or "-"},
            {"name": "Document", "value": str(event.document_id) if event.document_id else "-"},
            {"name": "Project", "value": str(event.project_id) if event.project_id else "-"},
            {"name": "Retry count", "value": str(event.retry_count)},
            {"name": "Host", "value": event.host_name},
        ]
        if event.worker_name:
            facts.append({"name": "Worker", "value": event.worker_name})
        if event.exception_type:
            facts.append({"name": "Exception", "value": event.exception_type})
        if event.trace_id:
            facts.append({"name": "Trace", "value": event.trace_id})

        sections: list[dict[str, Any]] = [
            {
                "activityTitle": event.title,
                "activitySubtitle": event.timestamp.isoformat(),
                "facts": facts,
                "text": event.message,
                "markdown": True,
            }
        ]

        if event.suggested_resolution:
            sections.append({"title": "Suggested resolution", "text": event.suggested_resolution})

        if event.stack_trace:
            trace = event.stack_trace[-_MAX_TRACE_CHARS:]
            # Teams renders newlines only with explicit breaks in MessageCard text.
            sections.append({"title": "Stack trace", "text": f"<pre>{trace}</pre>"})

        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": f"{event.level.value}: {event.title}"[:200],
            "themeColor": _THEME_COLOR[event.level],
            "title": f"{event.level.value}: {event.title}",
            "sections": sections,
        }


__all__ = ["MicrosoftTeamsAlertProvider"]

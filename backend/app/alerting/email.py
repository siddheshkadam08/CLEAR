"""Email provider (SMTP).

Uses the standard library's :mod:`smtplib` rather than adding an async SMTP
dependency, and pushes the blocking call onto a worker thread with
:func:`asyncio.to_thread`. SMTP delivery is a handful of round trips on a rarely
used path; a thread hop costs nothing next to a new dependency in the base image,
and the event loop is never blocked.

Both a plain-text and an HTML part are attached. Operators read alerts on phones
as often as in a desktop client, and a text/plain fallback is what keeps the
message legible when HTML is stripped.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from html import escape

from app.alerting.base import AlertEvent, IAlertProvider


class EmailAlertProvider(IAlertProvider):
    """Deliver alerts over SMTP."""

    name = "email"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        recipients: list[str],
        use_tls: bool,
        timeout: float,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender = sender or username
        self._recipients = [r for r in recipients if r]
        self._use_tls = use_tls
        self._timeout = timeout

    def is_configured(self) -> bool:
        return bool(self._host and self._sender and self._recipients)

    def _build_message(self, event: AlertEvent) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = f"[{event.environment}] {event.level.value}: {event.title}"[:200]
        message["From"] = self._sender
        message["To"] = ", ".join(self._recipients)
        # Lets a mail client thread repeat failures of the same condition together,
        # the same way `dedupe_key` groups them everywhere else.
        message["X-CIP-Dedupe-Key"] = event.dedupe_key()
        if event.trace_id:
            message["X-CIP-Trace-Id"] = event.trace_id

        rows = [
            ("Environment", event.environment),
            ("Level", event.level.value),
            ("Category", event.category.value),
            ("Stage", event.stage or "-"),
            ("Project", str(event.project_id) if event.project_id else "-"),
            ("Document", str(event.document_id) if event.document_id else "-"),
            ("Job", str(event.job_id) if event.job_id else "-"),
            ("Exception", event.exception_type or "-"),
            ("Retry count", str(event.retry_count)),
            ("Worker", event.worker_name or "-"),
            ("Host", event.host_name),
            ("Trace", event.trace_id or "-"),
            ("Time", event.timestamp.isoformat()),
        ]

        text = [event.title, "", event.message, ""]
        text += [f"{label}: {value}" for label, value in rows]
        if event.suggested_resolution:
            text += ["", "Suggested resolution:", event.suggested_resolution]
        if event.stack_trace:
            text += ["", "Stack trace:", event.stack_trace]
        message.set_content("\n".join(text))

        table = "".join(
            f"<tr><th align='left' style='padding:2px 12px 2px 0'>{escape(label)}</th>"
            f"<td><code>{escape(value)}</code></td></tr>"
            for label, value in rows
        )
        html = [
            f"<h2>{escape(event.title)}</h2>",
            f"<p><strong>{escape(event.message)}</strong></p>",
            f"<table>{table}</table>",
        ]
        if event.suggested_resolution:
            html.append(f"<h3>Suggested resolution</h3><p>{escape(event.suggested_resolution)}</p>")
        if event.stack_trace:
            html.append(
                "<h3>Stack trace</h3>"
                f"<pre style='background:#f6f8fa;padding:8px;overflow:auto'>"
                f"{escape(event.stack_trace)}</pre>"
            )
        message.add_alternative("".join(html), subtype="html")
        return message

    def _send_sync(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as client:
            if self._use_tls:
                client.starttls()
            # Anonymous relays are normal on an internal MTA, so credentials are
            # optional rather than required.
            if self._username:
                client.login(self._username, self._password)
            client.send_message(message)

    async def send(self, event: AlertEvent) -> None:
        await asyncio.to_thread(self._send_sync, self._build_message(event))


__all__ = ["EmailAlertProvider"]

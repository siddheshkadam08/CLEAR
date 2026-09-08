"""Transactional email — messages addressed to one user.

Distinct from :mod:`app.alerting.email`, which exists for a different job and
cannot do this one. That provider fixes its recipients at construction from
``SMTP_TO`` (an operator distribution list) and accepts only a frozen
:class:`~app.alerting.base.AlertEvent`, so it can report that the pipeline is on
fire but cannot send a person their password-reset link. The SMTP mechanics are
the same, so the connection handling below is lifted from it deliberately rather
than reinvented: stdlib :mod:`smtplib` on a worker thread, no async SMTP
dependency in the base image.

**When no relay is configured the link is logged instead of sent.** Delivery is
optional infrastructure here: no environment has ``SMTP_HOST`` set today, and a
password reset that raises on a missing MTA would make the whole feature look
broken rather than unconfigured. An operator gets the link out of the server log
until a relay exists; setting the ``SMTP_*`` variables is then the only change
needed to switch to real delivery.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def is_configured() -> bool:
    """True when there is somewhere to actually send mail."""
    settings = get_settings().alerts
    return bool(settings.smtp_host and (settings.smtp_from or settings.smtp_username))


def _build(to: str, subject: str, text: str, html: str | None) -> EmailMessage:
    settings = get_settings().alerts
    message = EmailMessage()
    message["Subject"] = subject[:200]
    message["From"] = settings.smtp_from or settings.smtp_username
    message["To"] = to
    message.set_content(text)
    # A text/plain part is always set first so the message stays readable where
    # HTML is stripped; the reset link has to survive that.
    if html:
        message.add_alternative(html, subtype="html")
    return message


def _send_sync(message: EmailMessage) -> None:
    settings = get_settings().alerts
    with smtplib.SMTP(
        settings.smtp_host, settings.smtp_port, timeout=settings.timeout_seconds
    ) as client:
        if settings.smtp_use_tls:
            client.starttls()
        # Anonymous relays are normal on an internal MTA, so credentials stay
        # optional rather than required.
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)


async def send_mail(
    *,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    fallback_log_body: str | None = None,
) -> bool:
    """Send one message. Returns True when it reached an MTA.

    Never raises. A caller on an authentication path must not turn a mail-server
    problem into a failed request: the user would see an error for something they
    cannot influence, and on the enumeration-safe reset endpoint a failure that
    was visible to the caller would leak whether the address exists.

    ``fallback_log_body`` is what gets logged when no relay is configured - pass
    the actionable part (the link), not the whole rendered email.
    """
    if not is_configured():
        logger.warning(
            "mail_not_configured_logging_instead",
            to=to,
            subject=subject,
            body=fallback_log_body or text,
        )
        return False

    try:
        await asyncio.to_thread(_send_sync, _build(to, subject, text, html))
    except Exception as exc:  # noqa: BLE001 - delivery must not break the caller
        logger.error("mail_send_failed", to=to, subject=subject, error=str(exc))
        return False

    logger.info("mail_sent", to=to, subject=subject)
    return True


__all__ = ["is_configured", "send_mail"]

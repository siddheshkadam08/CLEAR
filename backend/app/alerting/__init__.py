"""Operational alerting (§19).

One import decides the channel, the same way :mod:`app.storage` decides the object
store, so call sites depend on :class:`~app.alerting.dispatcher.AlertDispatcher`
and never on Slack. Adapters are imported lazily: a deployment that uses the
console provider must not need ``httpx`` configuration or an SMTP host, and an
unused adapter's import cost is never paid.

Configuration lives in ``settings.alerts``; see :mod:`app.core.config`.
``ALERT_PROVIDER`` is a CSV, so fanning out to several channels is a config
change rather than a code change.
"""

from __future__ import annotations

from functools import lru_cache

from app.alerting.base import (
    AlertCategory,
    AlertEvent,
    AlertLevel,
    IAlertProvider,
    suggested_resolution,
)
from app.alerting.dispatcher import AlertDispatcher
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _build_provider(name: str, settings: object) -> IAlertProvider | None:
    """Construct one provider, or ``None`` when it is not usable.

    Returning ``None`` rather than raising is deliberate: a half-configured Slack
    URL should cost the deployment its Slack alerts and a loud warning, not its
    ability to boot. ``ALERT_PROVIDER`` itself is validated in settings, so a
    *typo* still fails at startup - what degrades here is a missing endpoint.
    """
    alerts = settings.alerts  # type: ignore[attr-defined]
    timeout = alerts.timeout_seconds

    if name == "console":
        from app.alerting.console import ConsoleAlertProvider

        return ConsoleAlertProvider()

    if name == "null":
        return None

    if name == "slack":
        from app.alerting.slack import SlackAlertProvider

        provider: IAlertProvider = SlackAlertProvider(alerts.slack_webhook_url, timeout=timeout)
    elif name == "teams":
        from app.alerting.teams import MicrosoftTeamsAlertProvider

        provider = MicrosoftTeamsAlertProvider(alerts.teams_webhook_url, timeout=timeout)
    elif name == "webhook":
        from app.alerting.webhook import WebhookAlertProvider

        provider = WebhookAlertProvider(
            alerts.webhook_url, timeout=timeout, token=alerts.webhook_token
        )
    elif name == "email":
        from app.alerting.email import EmailAlertProvider

        provider = EmailAlertProvider(
            host=alerts.smtp_host,
            port=alerts.smtp_port,
            username=alerts.smtp_username,
            password=alerts.smtp_password,
            sender=alerts.smtp_from,
            recipients=alerts.smtp_to,
            use_tls=alerts.smtp_use_tls,
            timeout=timeout,
        )
    else:  # pragma: no cover - settings validation rejects unknown names first
        logger.warning("alert_provider_unknown", provider=name)
        return None

    if not provider.is_configured():
        logger.warning(
            "alert_provider_not_configured",
            provider=name,
            detail=(
                "Named in ALERT_PROVIDER but its endpoint/credentials are unset. "
                "Alerts will not be delivered on this channel."
            ),
        )
        return None
    return provider


def build_dispatcher() -> AlertDispatcher:
    """Construct a dispatcher from current settings. Uncached."""
    settings = get_settings()
    alerts = settings.alerts

    providers: list[IAlertProvider] = []
    for name in alerts.providers:
        built = _build_provider(name, settings)
        if built is not None:
            providers.append(built)

    # Never leave the system mute when delivery was asked for: if every configured
    # channel turned out to be unusable, fall back to console so the alert is at
    # least recorded rather than dropped.
    if alerts.enabled and not providers and "null" not in alerts.providers:
        from app.alerting.console import ConsoleAlertProvider

        logger.warning(
            "alert_providers_unavailable",
            configured=alerts.providers,
            detail="Falling back to the console provider so alerts are still recorded.",
        )
        providers.append(ConsoleAlertProvider())

    dispatcher = AlertDispatcher(
        providers,
        min_level=AlertLevel.parse(alerts.min_level),
        max_attempts=alerts.max_attempts,
        backoff_seconds=alerts.retry_backoff_seconds,
        backoff_max_seconds=alerts.retry_backoff_max_seconds,
        timeout_seconds=alerts.timeout_seconds,
        enabled=alerts.enabled,
    )
    logger.info(
        "alert_dispatcher_ready",
        providers=[p.name for p in providers],
        min_level=dispatcher.min_level.value,
        enabled=alerts.enabled,
    )
    return dispatcher


@lru_cache(maxsize=1)
def get_alert_dispatcher() -> AlertDispatcher:
    """Process-wide dispatcher. Cached: providers hold reusable HTTP clients."""
    return build_dispatcher()


async def close_alert_dispatcher() -> None:
    """Release provider resources and drop the cache. Safe if never built."""
    if get_alert_dispatcher.cache_info().currsize:
        await get_alert_dispatcher().aclose()
    get_alert_dispatcher.cache_clear()


def reset_alert_dispatcher() -> None:
    """Drop the cached dispatcher without awaiting cleanup (tests)."""
    get_alert_dispatcher.cache_clear()


__all__ = [
    "AlertCategory",
    "AlertDispatcher",
    "AlertEvent",
    "AlertLevel",
    "IAlertProvider",
    "build_dispatcher",
    "close_alert_dispatcher",
    "get_alert_dispatcher",
    "reset_alert_dispatcher",
    "suggested_resolution",
]

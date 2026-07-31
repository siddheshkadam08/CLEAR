"""Unit tests for the alerting framework.

Covers the five behaviours the pipeline depends on: which providers get built,
what the payload contains, what the severity floor drops, how retry backs off,
and that a broken channel can never propagate an exception into the caller.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.alerting import build_dispatcher
from app.alerting.base import (
    AlertCategory,
    AlertEvent,
    AlertLevel,
    IAlertProvider,
    suggested_resolution,
)
from app.alerting.console import ConsoleAlertProvider
from app.alerting.dispatcher import AlertDispatcher


# =============================================================================
# Doubles
# =============================================================================
class RecordingProvider(IAlertProvider):
    """Captures what it was asked to send."""

    def __init__(self, name: str = "recording") -> None:
        self.name = name
        self.events: list[AlertEvent] = []

    async def send(self, event: AlertEvent) -> None:
        self.events.append(event)


class FlakyProvider(IAlertProvider):
    """Fails ``fail_times`` times, then succeeds."""

    def __init__(self, fail_times: int, name: str = "flaky") -> None:
        self.name = name
        self.fail_times = fail_times
        self.attempts = 0

    async def send(self, event: AlertEvent) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError(f"transient failure {self.attempts}")


class HangingProvider(IAlertProvider):
    """Never returns - stands in for a channel that has stopped responding."""

    name = "hanging"

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, event: AlertEvent) -> None:
        self.attempts += 1
        await asyncio.sleep(3600)


def make_event(level: AlertLevel = AlertLevel.ERROR, **overrides: Any) -> AlertEvent:
    fields: dict[str, Any] = {
        "level": level,
        "category": AlertCategory.PARSER,
        "title": "parser failed",
        "message": "the parser exploded",
        "stage": "parser",
    }
    fields.update(overrides)
    return AlertEvent(**fields)


def dispatcher(providers: list[IAlertProvider], **kwargs: Any) -> AlertDispatcher:
    kwargs.setdefault("min_level", AlertLevel.ERROR)
    kwargs.setdefault("backoff_seconds", 0.0)  # keep unit tests fast
    return AlertDispatcher(providers, **kwargs)


# =============================================================================
# Severity ladder
# =============================================================================
class TestAlertLevel:
    def test_ladder_is_ordered(self) -> None:
        assert AlertLevel.INFO.rank < AlertLevel.WARNING.rank
        assert AlertLevel.WARNING.rank < AlertLevel.ERROR.rank
        assert AlertLevel.ERROR.rank < AlertLevel.CRITICAL.rank

    def test_at_least_is_inclusive(self) -> None:
        assert AlertLevel.ERROR.at_least(AlertLevel.ERROR)
        assert AlertLevel.CRITICAL.at_least(AlertLevel.ERROR)
        assert not AlertLevel.WARNING.at_least(AlertLevel.ERROR)

    @pytest.mark.parametrize("raw", ["error", "ERROR", " Error "])
    def test_parse_is_case_insensitive(self, raw: str) -> None:
        assert AlertLevel.parse(raw) is AlertLevel.ERROR

    def test_parse_rejects_unknown(self) -> None:
        with pytest.raises(ValueError, match="Unknown alert level"):
            AlertLevel.parse("catastrophic")

    def test_every_level_maps_to_a_persisted_severity(self) -> None:
        # The alerts table has a NOT NULL severity, so a gap here would be an
        # IntegrityError at the worst possible moment.
        for level in AlertLevel:
            assert level.to_alert_severity() is not None


# =============================================================================
# Payload
# =============================================================================
class TestAlertEvent:
    def test_carries_the_operational_fields(self) -> None:
        project_id, document_id = uuid.uuid4(), uuid.uuid4()
        event = make_event(
            project_id=project_id,
            document_id=document_id,
            correlation_id="req-1",
            trace_id="trace-1",
            worker_name="worker-ai",
            retry_count=3,
        )
        assert event.project_id == project_id
        assert event.document_id == document_id
        assert event.correlation_id == "req-1"
        assert event.trace_id == "trace-1"
        assert event.retry_count == 3
        assert event.host_name  # always populated
        assert event.timestamp.tzinfo is not None  # timezone-aware

    def test_from_exception_captures_type_and_trace(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError as exc:
            event = AlertEvent.from_exception(exc, category=AlertCategory.EMBEDDING)

        assert event.exception_type == "ValueError"
        assert event.message == "boom"
        assert event.stack_trace is not None
        assert "ValueError: boom" in event.stack_trace
        assert event.suggested_resolution == suggested_resolution(AlertCategory.EMBEDDING)

    def test_from_exception_truncates_a_huge_trace(self) -> None:
        try:
            raise RuntimeError("x" * 50_000)
        except RuntimeError as exc:
            event = AlertEvent.from_exception(exc, max_stack_chars=500)

        assert event.stack_trace is not None
        assert len(event.stack_trace) < 700
        assert event.stack_trace.endswith("[truncated]")

    def test_dedupe_key_is_stable_for_the_same_condition(self) -> None:
        document_id = uuid.uuid4()
        first = make_event(document_id=document_id)
        second = make_event(document_id=document_id, message="different wording")
        assert first.dedupe_key() == second.dedupe_key()

    def test_dedupe_key_separates_stages(self) -> None:
        document_id = uuid.uuid4()
        assert (
            make_event(document_id=document_id, stage="parser").dedupe_key()
            != make_event(document_id=document_id, stage="embedding").dedupe_key()
        )

    def test_is_frozen(self) -> None:
        event = make_event()
        with pytest.raises(ValidationError):
            event.level = AlertLevel.INFO  # type: ignore[misc]

    def test_log_fields_exclude_the_stack_trace(self) -> None:
        # The trace goes in the alert body, not into every structured log line.
        event = make_event(stack_trace="secret-looking traceback")
        assert "stack_trace" not in event.as_log_fields()

    def test_every_category_has_a_suggested_resolution(self) -> None:
        for category in AlertCategory:
            assert suggested_resolution(category).strip()


# =============================================================================
# Severity filtering
# =============================================================================
class TestSeverityFiltering:
    async def test_drops_below_the_floor(self) -> None:
        provider = RecordingProvider()
        result = await dispatcher([provider], min_level=AlertLevel.ERROR).dispatch(
            make_event(AlertLevel.WARNING)
        )
        assert result == {}
        assert provider.events == []

    async def test_delivers_at_the_floor(self) -> None:
        provider = RecordingProvider()
        result = await dispatcher([provider], min_level=AlertLevel.ERROR).dispatch(
            make_event(AlertLevel.ERROR)
        )
        assert result == {"recording": True}
        assert len(provider.events) == 1

    async def test_delivers_above_the_floor(self) -> None:
        provider = RecordingProvider()
        await dispatcher([provider], min_level=AlertLevel.ERROR).dispatch(
            make_event(AlertLevel.CRITICAL)
        )
        assert len(provider.events) == 1

    async def test_info_floor_lets_everything_through(self) -> None:
        provider = RecordingProvider()
        target = dispatcher([provider], min_level=AlertLevel.INFO)
        for level in AlertLevel:
            await target.dispatch(make_event(level))
        assert len(provider.events) == len(AlertLevel)

    async def test_disabled_dispatcher_delivers_nothing(self) -> None:
        provider = RecordingProvider()
        result = await dispatcher([provider], enabled=False).dispatch(make_event())
        assert result == {}
        assert provider.events == []


# =============================================================================
# Retry
# =============================================================================
class TestRetry:
    async def test_retries_until_it_succeeds(self) -> None:
        provider = FlakyProvider(fail_times=2)
        result = await dispatcher([provider], max_attempts=3).dispatch(make_event())
        assert result == {"flaky": True}
        assert provider.attempts == 3

    async def test_gives_up_after_max_attempts(self) -> None:
        provider = FlakyProvider(fail_times=99)
        result = await dispatcher([provider], max_attempts=3).dispatch(make_event())
        assert result == {"flaky": False}
        assert provider.attempts == 3

    async def test_single_attempt_means_no_retry(self) -> None:
        provider = FlakyProvider(fail_times=99)
        await dispatcher([provider], max_attempts=1).dispatch(make_event())
        assert provider.attempts == 1

    async def test_backoff_doubles_and_is_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("app.alerting.dispatcher.asyncio.sleep", fake_sleep)

        provider = FlakyProvider(fail_times=99)
        await AlertDispatcher(
            [provider],
            max_attempts=5,
            backoff_seconds=1.0,
            backoff_max_seconds=4.0,
        ).dispatch(make_event())

        # 4 sleeps for 5 attempts; doubling 1 -> 2 -> 4, then clamped at 4.
        assert slept == [1.0, 2.0, 4.0, 4.0]

    async def test_timeout_is_enforced_per_attempt(self) -> None:
        provider = HangingProvider()
        result = await dispatcher([provider], max_attempts=2, timeout_seconds=0.05).dispatch(
            make_event()
        )
        assert result == {"hanging": False}
        assert provider.attempts == 2  # timed out and retried, rather than hanging


# =============================================================================
# Failure isolation - the guarantee the pipeline relies on
# =============================================================================
class TestFailureIsolation:
    async def test_a_failing_provider_never_raises(self) -> None:
        result = await dispatcher([FlakyProvider(fail_times=99)], max_attempts=1).dispatch(
            make_event()
        )
        assert result == {"flaky": False}

    async def test_one_bad_channel_does_not_stop_a_good_one(self) -> None:
        good = RecordingProvider("good")
        bad = FlakyProvider(fail_times=99, name="bad")
        result = await dispatcher([bad, good], max_attempts=1).dispatch(make_event())

        assert result == {"bad": False, "good": True}
        assert len(good.events) == 1

    async def test_close_survives_a_provider_that_fails_to_close(self) -> None:
        class BadClose(IAlertProvider):
            name = "bad-close"

            async def send(self, event: AlertEvent) -> None: ...

            async def aclose(self) -> None:
                raise RuntimeError("cannot close")

        await dispatcher([BadClose()]).aclose()  # must not raise


# =============================================================================
# Provider selection from configuration
# =============================================================================
class TestProviderSelection:
    def test_console_is_the_default(self, settings_env: Any) -> None:
        settings_env(ALERT_PROVIDER="console")
        assert [p.name for p in build_dispatcher().providers] == ["console"]

    def test_multiple_providers_fan_out(self, settings_env: Any) -> None:
        settings_env(
            ALERT_PROVIDER="console,slack,webhook",
            SLACK_WEBHOOK_URL="https://hooks.slack.test/x",
            ALERT_WEBHOOK_URL="https://receiver.test/alerts",
        )
        assert [p.name for p in build_dispatcher().providers] == ["console", "slack", "webhook"]

    def test_unconfigured_channel_is_dropped_with_a_console_fallback(
        self, settings_env: Any
    ) -> None:
        # Slack named but no URL: the deployment must still record alerts rather
        # than going silently mute.
        settings_env(ALERT_PROVIDER="slack", SLACK_WEBHOOK_URL="")
        names = [p.name for p in build_dispatcher().providers]
        assert names == ["console"]

    def test_null_provider_means_deliberate_silence(self, settings_env: Any) -> None:
        settings_env(ALERT_PROVIDER="null")
        assert build_dispatcher().providers == []

    def test_min_level_is_read_from_configuration(self, settings_env: Any) -> None:
        settings_env(ALERT_PROVIDER="console", ALERT_MIN_LEVEL="critical")
        assert build_dispatcher().min_level is AlertLevel.CRITICAL

    def test_email_needs_host_sender_and_recipients(self, settings_env: Any) -> None:
        settings_env(
            ALERT_PROVIDER="email",
            SMTP_HOST="smtp.test",
            SMTP_FROM="alerts@test",
            SMTP_TO="ops@test,oncall@test",
        )
        assert [p.name for p in build_dispatcher().providers] == ["email"]


# =============================================================================
# Configuration validation
# =============================================================================
class TestConfigurationValidation:
    def test_unknown_provider_fails_at_startup(self, settings_env: Any) -> None:
        # A typo must be a boot failure, not alerts that silently never arrive.
        with pytest.raises(Exception, match="unknown provider"):
            settings_env(ALERT_PROVIDER="slakc")

    def test_unknown_min_level_fails_at_startup(self, settings_env: Any) -> None:
        with pytest.raises(Exception, match="ALERT_MIN_LEVEL"):
            settings_env(ALERT_MIN_LEVEL="verbose")

    def test_min_level_is_normalised(self, settings_env: Any) -> None:
        assert settings_env(ALERT_MIN_LEVEL="warning").alerts.min_level == "WARNING"

    def test_providers_are_normalised_and_split(self, settings_env: Any) -> None:
        assert settings_env(ALERT_PROVIDER=" Console , NULL ").alerts.providers == [
            "console",
            "null",
        ]

    def test_defaults_need_no_configuration_at_all(self, settings_env: Any) -> None:
        settings = settings_env()
        assert settings.alerts.enabled is True
        assert settings.alerts.providers == ["console"]
        assert settings.alerts.min_level == "ERROR"


# =============================================================================
# Console provider
# =============================================================================
class TestConsoleProvider:
    async def test_writes_one_structured_record(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        caplog.set_level(logging.INFO)
        await ConsoleAlertProvider().send(make_event(message="disk on fire"))
        assert "operational_alert" in caplog.text

    def test_is_always_configured(self) -> None:
        # The point of the console provider: it works with nothing set up.
        assert ConsoleAlertProvider().is_configured()

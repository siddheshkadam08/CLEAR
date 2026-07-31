"""Integration tests for the notification providers.

These wire the *real* provider, the *real* ``httpx.AsyncClient`` and the real
payload construction together, and stop only at the socket by way of
``httpx.MockTransport``. That is the level worth testing: a provider bug is
almost always a malformed body or a mishandled status code, and both survive a
test that mocks the client away.

Hermetic - no Postgres, Redis or network - so they run in the default suite
rather than behind the ``integration`` marker.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from app.alerting.base import AlertCategory, AlertEvent, AlertLevel
from app.alerting.console import ConsoleAlertProvider
from app.alerting.dispatcher import AlertDispatcher
from app.alerting.email import EmailAlertProvider
from app.alerting.slack import SlackAlertProvider
from app.alerting.teams import MicrosoftTeamsAlertProvider
from app.alerting.webhook import WebhookAlertProvider


class Capture:
    """Collects the requests a provider makes, and replies with a chosen status."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.status, json={"ok": self.status < 300})

        return httpx.MockTransport(handler)

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def make_event(**overrides: Any) -> AlertEvent:
    fields: dict[str, Any] = {
        "level": AlertLevel.CRITICAL,
        "category": AlertCategory.EMBEDDING,
        "title": "embedding failed for acme-msa.pdf",
        "message": "vector dimension mismatch",
        "stage": "embedding",
        "project_id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "trace_id": "trace-abc",
        "retry_count": 2,
        "worker_name": "worker-ai",
        "suggested_resolution": "Run make embedding-check",
        "stack_trace": "Traceback (most recent call last):\n  ValueError: dim",
        "details": {"expected_dim": 2048, "actual_dim": 1536},
    }
    fields.update(overrides)
    return AlertEvent(**fields)


# =============================================================================
# Slack
# =============================================================================
class TestSlackProvider:
    async def test_posts_block_kit_to_the_webhook(self) -> None:
        capture = Capture()
        provider = SlackAlertProvider(
            "https://hooks.slack.test/services/x", timeout=5, transport=capture.transport()
        )
        await provider.send(make_event())
        await provider.aclose()

        assert len(capture.requests) == 1
        assert str(capture.requests[0].url) == "https://hooks.slack.test/services/x"
        body = capture.body
        assert "CRITICAL" in body["text"]
        assert body["blocks"][0]["type"] == "header"

    async def test_body_carries_the_operational_detail(self) -> None:
        capture = Capture()
        provider = SlackAlertProvider(
            "https://hooks.slack.test/x", timeout=5, transport=capture.transport()
        )
        await provider.send(make_event())
        await provider.aclose()

        rendered = json.dumps(capture.body)
        assert "vector dimension mismatch" in rendered
        assert "embedding" in rendered
        assert "worker-ai" in rendered
        assert "trace-abc" in rendered
        assert "Run make embedding-check" in rendered

    async def test_header_stays_within_slacks_limit(self) -> None:
        # Slack answers 400 for a header over 150 chars, which would look like an
        # outage rather than a formatting bug.
        capture = Capture()
        provider = SlackAlertProvider(
            "https://hooks.slack.test/x", timeout=5, transport=capture.transport()
        )
        await provider.send(make_event(title="T" * 400))
        await provider.aclose()

        assert len(capture.body["blocks"][0]["text"]["text"]) <= 150

    async def test_non_2xx_raises_so_the_dispatcher_can_retry(self) -> None:
        capture = Capture(status=500)
        provider = SlackAlertProvider(
            "https://hooks.slack.test/x", timeout=5, transport=capture.transport()
        )
        with pytest.raises(httpx.HTTPStatusError):
            await provider.send(make_event())
        await provider.aclose()

    async def test_unconfigured_without_a_url(self) -> None:
        assert not SlackAlertProvider("", timeout=5).is_configured()


# =============================================================================
# Microsoft Teams
# =============================================================================
class TestTeamsProvider:
    async def test_posts_a_message_card(self) -> None:
        capture = Capture()
        provider = MicrosoftTeamsAlertProvider(
            "https://outlook.office.test/webhook/x", timeout=5, transport=capture.transport()
        )
        await provider.send(make_event())
        await provider.aclose()

        body = capture.body
        assert body["@type"] == "MessageCard"
        assert body["@context"] == "https://schema.org/extensions"
        assert body["themeColor"]  # required for the card to render coloured
        assert "CRITICAL" in body["title"]

    async def test_facts_carry_the_context(self) -> None:
        capture = Capture()
        provider = MicrosoftTeamsAlertProvider(
            "https://outlook.office.test/x", timeout=5, transport=capture.transport()
        )
        await provider.send(make_event())
        await provider.aclose()

        facts = {f["name"]: f["value"] for f in capture.body["sections"][0]["facts"]}
        assert facts["Stage"] == "embedding"
        assert facts["Retry count"] == "2"
        assert facts["Worker"] == "worker-ai"
        assert facts["Trace"] == "trace-abc"

    async def test_summary_is_always_present(self) -> None:
        # Teams rejects a card with no summary and no title.
        capture = Capture()
        provider = MicrosoftTeamsAlertProvider(
            "https://outlook.office.test/x", timeout=5, transport=capture.transport()
        )
        await provider.send(make_event(title="T" * 500))
        await provider.aclose()
        assert 0 < len(capture.body["summary"]) <= 200

    async def test_non_2xx_raises(self) -> None:
        capture = Capture(status=429)
        provider = MicrosoftTeamsAlertProvider(
            "https://outlook.office.test/x", timeout=5, transport=capture.transport()
        )
        with pytest.raises(httpx.HTTPStatusError):
            await provider.send(make_event())
        await provider.aclose()


# =============================================================================
# Generic webhook
# =============================================================================
class TestWebhookProvider:
    async def test_posts_the_event_as_json(self) -> None:
        capture = Capture()
        provider = WebhookAlertProvider(
            "https://receiver.test/alerts", timeout=5, transport=capture.transport()
        )
        event = make_event()
        await provider.send(event)
        await provider.aclose()

        body = capture.body
        assert body["level"] == "CRITICAL"
        assert body["category"] == "embedding"
        assert body["stage"] == "embedding"
        assert body["document_id"] == str(event.document_id)
        assert body["dedupe_key"] == event.dedupe_key()
        assert body["details"]["expected_dim"] == 2048

    async def test_payload_is_json_serialisable_end_to_end(self) -> None:
        # UUIDs and datetimes must already be strings; a receiver is not Python.
        capture = Capture()
        provider = WebhookAlertProvider(
            "https://receiver.test/a", timeout=5, transport=capture.transport()
        )
        await provider.send(make_event())
        await provider.aclose()
        json.dumps(capture.body)  # must not raise

    async def test_bearer_token_is_sent(self) -> None:
        capture = Capture()
        provider = WebhookAlertProvider(
            "https://receiver.test/a",
            timeout=5,
            token="s3cret",
            transport=capture.transport(),
        )
        await provider.send(make_event())
        await provider.aclose()

        assert capture.requests[0].headers["authorization"] == "Bearer s3cret"

    async def test_no_token_means_no_authorization_header(self) -> None:
        capture = Capture()
        provider = WebhookAlertProvider(
            "https://receiver.test/a", timeout=5, transport=capture.transport()
        )
        await provider.send(make_event())
        await provider.aclose()
        assert "authorization" not in capture.requests[0].headers


# =============================================================================
# Console
# =============================================================================
class TestConsoleProvider:
    async def test_emits_a_record_including_the_trace(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        caplog.set_level(logging.INFO)
        await ConsoleAlertProvider().send(make_event())
        assert "operational_alert" in caplog.text

    async def test_every_level_is_emitted(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        caplog.set_level(logging.INFO)
        provider = ConsoleAlertProvider()
        for level in AlertLevel:
            await provider.send(make_event(level=level))
        assert caplog.text.count("operational_alert") == len(AlertLevel)


# =============================================================================
# Email
# =============================================================================
class TestEmailProvider:
    def _provider(self, **overrides: Any) -> EmailAlertProvider:
        fields: dict[str, Any] = {
            "host": "smtp.test",
            "port": 587,
            "username": "user",
            "password": "pass",
            "sender": "alerts@test",
            "recipients": ["ops@test"],
            "use_tls": True,
            "timeout": 5,
        }
        fields.update(overrides)
        return EmailAlertProvider(**fields)

    def test_builds_a_multipart_message(self) -> None:
        message = self._provider()._build_message(make_event())
        assert message["To"] == "ops@test"
        assert message["From"] == "alerts@test"
        assert "CRITICAL" in message["Subject"]
        assert message.is_multipart()  # text/plain + text/html

    def test_carries_the_dedupe_key_for_client_side_threading(self) -> None:
        event = make_event()
        message = self._provider()._build_message(event)
        assert message["X-CIP-Dedupe-Key"] == event.dedupe_key()

    def test_html_part_escapes_the_payload(self) -> None:
        # An exception message can contain anything; it must not become markup.
        message = self._provider()._build_message(make_event(message="<script>x</script>"))
        html = message.get_body(preferencelist=("html",))
        assert html is not None
        assert "&lt;script&gt;" in html.get_content()

    def test_unconfigured_without_recipients(self) -> None:
        assert not self._provider(recipients=[]).is_configured()

    def test_unconfigured_without_a_host(self) -> None:
        assert not self._provider(host="").is_configured()

    async def test_send_never_blocks_the_event_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[Any] = []

        def fake_send(message: Any) -> None:
            sent.append(message)

        provider = self._provider()
        monkeypatch.setattr(provider, "_send_sync", fake_send)
        await provider.send(make_event())
        assert len(sent) == 1


# =============================================================================
# Fan-out through the dispatcher
# =============================================================================
class TestDispatcherFanOut:
    async def test_delivers_to_every_channel(self) -> None:
        slack, teams, hook = Capture(), Capture(), Capture()
        dispatcher = AlertDispatcher(
            [
                SlackAlertProvider(
                    "https://hooks.slack.test/x", timeout=5, transport=slack.transport()
                ),
                MicrosoftTeamsAlertProvider(
                    "https://outlook.office.test/x", timeout=5, transport=teams.transport()
                ),
                WebhookAlertProvider(
                    "https://receiver.test/a", timeout=5, transport=hook.transport()
                ),
                ConsoleAlertProvider(),
            ],
            min_level=AlertLevel.ERROR,
            backoff_seconds=0.0,
        )

        result = await dispatcher.dispatch(make_event())
        await dispatcher.aclose()

        assert result == {"slack": True, "teams": True, "webhook": True, "console": True}
        assert len(slack.requests) == len(teams.requests) == len(hook.requests) == 1

    async def test_a_dead_channel_does_not_stop_the_others(self) -> None:
        dead, alive = Capture(status=503), Capture()
        dispatcher = AlertDispatcher(
            [
                SlackAlertProvider(
                    "https://hooks.slack.test/x", timeout=5, transport=dead.transport()
                ),
                WebhookAlertProvider(
                    "https://receiver.test/a", timeout=5, transport=alive.transport()
                ),
            ],
            min_level=AlertLevel.ERROR,
            max_attempts=2,
            backoff_seconds=0.0,
        )

        result = await dispatcher.dispatch(make_event())
        await dispatcher.aclose()

        assert result == {"slack": False, "webhook": True}
        assert len(dead.requests) == 2  # retried
        assert len(alive.requests) == 1

    async def test_retry_reaches_a_channel_that_recovers(self) -> None:
        calls: list[int] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200 if len(calls) > 2 else 500, json={})

        dispatcher = AlertDispatcher(
            [
                WebhookAlertProvider(
                    "https://receiver.test/a",
                    timeout=5,
                    transport=httpx.MockTransport(handler),
                )
            ],
            min_level=AlertLevel.ERROR,
            max_attempts=3,
            backoff_seconds=0.0,
        )
        result = await dispatcher.dispatch(make_event())
        await dispatcher.aclose()

        assert result == {"webhook": True}
        assert len(calls) == 3

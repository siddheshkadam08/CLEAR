"""Unit tests for :class:`app.services.alerts.AlertService`.

The service sits on the orchestrator's terminal-failure path, so the property that
matters most is negative: it must never raise. A failure to alert has to leave the
original stage failure as the thing the operator sees, because that is the
evidence. Everything else here - categorisation, severity, de-duplication - is in
service of making the alert worth reading when it does arrive.

The session is a stub rather than a live Postgres: these assert the service's own
logic, and the ORM mapping is already exercised by the migration tests.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.alerting import AlertCategory, AlertLevel, reset_alert_dispatcher
from app.core.enums import AlertSeverity, AlertStatus, AlertType, PipelineStage
from app.services.alerts import AlertService


# =============================================================================
# Doubles
# =============================================================================
class StubResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class StubSession:
    """The slice of AsyncSession this service touches."""

    def __init__(self, existing: Any = None) -> None:
        self.existing = existing
        self.added: list[Any] = []
        self.flushes = 0
        self.execute_calls = 0

    async def execute(self, _statement: Any) -> StubResult:
        self.execute_calls += 1
        return StubResult(self.existing)

    def add(self, obj: Any) -> None:
        # Mimic the DB default so `alert.id` is populated after flush.
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1


class ExplodingSession(StubSession):
    async def execute(self, _statement: Any) -> StubResult:
        raise RuntimeError("database is on fire")


class FakeContract:
    def __init__(self, **overrides: Any) -> None:
        self.id = overrides.get("id", uuid.uuid4())
        self.project_id = overrides.get("project_id", uuid.uuid4())
        self.title = overrides.get("title", "Acme / Globex MSA")
        self.original_file_name = overrides.get("original_file_name", "acme-msa.pdf")


def error(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": "pipeline_error",
        "message": "The parser artifact is not a valid normalized document.",
        "stage": "enrichment",
        "attempt": 3,
        "retryable": False,
        "details": {"artifact": "projects/x/normalized_document/g1.json"},
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def console_only(settings_env: Any) -> None:
    """Keep every alert on the console provider, at a floor that lets it through."""
    settings_env(ALERT_PROVIDER="console", ALERT_MIN_LEVEL="INFO")
    reset_alert_dispatcher()


# =============================================================================
# Processing failures
# =============================================================================
class TestRaiseProcessingFailure:
    async def test_persists_and_dispatches(self) -> None:
        session = StubSession()
        outcome = await AlertService(session).raise_processing_failure(
            contract=FakeContract(), stage=PipelineStage.ENRICHMENT, error=error()
        )

        assert outcome.raised is True
        assert outcome.persisted is True
        assert outcome.deduplicated is False
        assert outcome.delivered == {"console": True}
        assert len(session.added) == 1

    async def test_persisted_row_is_shaped_for_the_alerts_screen(self) -> None:
        session = StubSession()
        contract = FakeContract()
        await AlertService(session).raise_processing_failure(
            contract=contract, stage=PipelineStage.ENRICHMENT, error=error()
        )

        alert = session.added[0]
        assert alert.project_id == contract.project_id
        assert alert.contract_id == contract.id
        assert alert.alert_type is AlertType.PROCESSING_FAILED
        assert alert.status is AlertStatus.OPEN
        assert alert.severity is AlertSeverity.HIGH  # ERROR -> HIGH
        assert "enrichment" in alert.title
        assert alert.details["error_code"] == "pipeline_error"
        assert alert.details["retry_count"] == 3
        assert alert.details["occurrences"] == 1

    async def test_title_uses_the_human_document_name(self) -> None:
        session = StubSession()
        await AlertService(session).raise_processing_failure(
            contract=FakeContract(title="Acme / Globex MSA"),
            stage=PipelineStage.PARSER,
            error=error(),
        )
        assert "Acme / Globex MSA" in session.added[0].title

    async def test_falls_back_to_the_file_name(self) -> None:
        contract = FakeContract(title=None)
        session = StubSession()
        await AlertService(session).raise_processing_failure(
            contract=contract, stage=PipelineStage.PARSER, error=error()
        )
        assert "acme-msa.pdf" in session.added[0].title

    @pytest.mark.parametrize(
        ("stage", "expected"),
        [
            (PipelineStage.PARSER, AlertCategory.PARSER),
            (PipelineStage.EMBEDDING, AlertCategory.EMBEDDING),
            (PipelineStage.AI_EXTRACTION, AlertCategory.AI_EXTRACTION),
            (PipelineStage.CHUNKING, AlertCategory.DOCUMENT_PROCESSING),
        ],
    )
    async def test_stage_selects_the_category(
        self, stage: PipelineStage, expected: AlertCategory
    ) -> None:
        session = StubSession()
        await AlertService(session).raise_processing_failure(
            contract=FakeContract(), stage=stage, error=error()
        )
        assert session.added[0].details["category"] == expected.value

    async def test_infrastructure_failures_escalate_to_critical(self) -> None:
        session = StubSession()
        await AlertService(session).raise_processing_failure(
            contract=FakeContract(),
            stage=PipelineStage.EMBEDDING,
            error=error(code="database_error"),
        )

        alert = session.added[0]
        assert alert.severity is AlertSeverity.CRITICAL
        assert alert.details["level"] == AlertLevel.CRITICAL.value
        assert alert.details["category"] == AlertCategory.DATABASE.value

    async def test_a_bad_document_stays_error_not_critical(self) -> None:
        # One awkward PDF is routine; it must not page the on-call rota.
        session = StubSession()
        await AlertService(session).raise_processing_failure(
            contract=FakeContract(), stage=PipelineStage.PARSER, error=error()
        )
        assert session.added[0].severity is AlertSeverity.HIGH

    async def test_accepts_a_plain_stage_string(self) -> None:
        session = StubSession()
        outcome = await AlertService(session).raise_processing_failure(
            contract=FakeContract(), stage="embedding", error=error()
        )
        assert outcome.persisted is True

    async def test_suggested_resolution_is_included(self) -> None:
        session = StubSession()
        await AlertService(session).raise_processing_failure(
            contract=FakeContract(), stage=PipelineStage.EMBEDDING, error=error()
        )
        assert "embedding-check" in session.added[0].details["suggested_resolution"]


# =============================================================================
# De-duplication
# =============================================================================
class TestDeduplication:
    async def test_reuses_the_open_alert_for_the_same_condition(self) -> None:
        existing = type(
            "OpenAlert",
            (),
            {"id": uuid.uuid4(), "details": {"occurrences": 1}, "message": "", "severity": None},
        )()
        session = StubSession(existing=existing)

        outcome = await AlertService(session).raise_processing_failure(
            contract=FakeContract(), stage=PipelineStage.PARSER, error=error()
        )

        assert outcome.deduplicated is True
        assert outcome.alert_id == existing.id
        assert session.added == []  # no second row
        assert existing.details["occurrences"] == 2

    async def test_dedupe_key_separates_stages(self) -> None:
        contract = FakeContract()
        service = AlertService(StubSession())

        first = service._event(
            level=AlertLevel.ERROR,
            category=AlertCategory.PARSER,
            title="t",
            message="m",
            stage="parser",
            document_id=contract.id,
        )
        second = first.model_copy(update={"stage": "embedding"})
        assert first.dedupe_key() != second.dedupe_key()


# =============================================================================
# The guarantee: alerting never breaks the caller
# =============================================================================
class TestNeverRaises:
    async def test_a_database_failure_does_not_propagate(self) -> None:
        outcome = await AlertService(ExplodingSession()).raise_processing_failure(
            contract=FakeContract(), stage=PipelineStage.PARSER, error=error()
        )

        assert outcome.raised is True
        assert outcome.persisted is False  # honestly reported
        assert outcome.delivered == {"console": True}  # notification still happened

    async def test_works_without_a_session(self) -> None:
        # Worker crash handlers and scheduler ticks have no session to hand.
        outcome = await AlertService(None).raise_processing_failure(
            contract=FakeContract(), stage=PipelineStage.PARSER, error=error()
        )
        assert outcome.raised is True
        assert outcome.persisted is False
        assert outcome.delivered == {"console": True}

    async def test_a_dispatcher_failure_does_not_propagate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode() -> Any:
            raise RuntimeError("dispatcher construction failed")

        monkeypatch.setattr("app.services.alerts.get_alert_dispatcher", explode)

        outcome = await AlertService(StubSession()).raise_processing_failure(
            contract=FakeContract(), stage=PipelineStage.PARSER, error=error()
        )
        assert outcome.raised is True
        assert outcome.delivered == {}

    async def test_a_malformed_error_payload_is_tolerated(self) -> None:
        # The error dict comes from whatever raised; it must not need to be perfect.
        outcome = await AlertService(StubSession()).raise_processing_failure(
            contract=FakeContract(), stage=PipelineStage.PARSER, error={}
        )
        assert outcome.raised is True

    async def test_an_incomplete_contract_is_tolerated(self) -> None:
        outcome = await AlertService(StubSession()).raise_processing_failure(
            contract=object(), stage=PipelineStage.PARSER, error=error()
        )
        assert outcome.raised is True
        assert outcome.persisted is False  # no project_id to scope a row to


# =============================================================================
# Exception alerts
# =============================================================================
class TestRaiseException:
    async def test_captures_type_and_trace(self) -> None:
        try:
            raise ConnectionError("redis unreachable")
        except ConnectionError as exc:
            outcome = await AlertService(None).raise_exception(
                exc, category=AlertCategory.QUEUE, level=AlertLevel.CRITICAL
            )
        assert outcome.raised is True
        assert outcome.delivered == {"console": True}

    async def test_does_not_persist_without_a_project(self) -> None:
        session = StubSession()
        outcome = await AlertService(session).raise_exception(
            RuntimeError("boom"), category=AlertCategory.SCHEDULER, persist=True
        )
        assert outcome.persisted is False
        assert session.added == []

    async def test_persists_when_scoped_to_a_project(self) -> None:
        session = StubSession()
        outcome = await AlertService(session).raise_exception(
            RuntimeError("boom"),
            category=AlertCategory.STORAGE,
            persist=True,
            project_id=uuid.uuid4(),
        )
        assert outcome.persisted is True
        assert len(session.added) == 1


# =============================================================================
# Severity floor
# =============================================================================
class TestSeverityFloor:
    async def test_floor_suppresses_delivery_but_not_persistence(self, settings_env: Any) -> None:
        # The row is still the record of what happened; only the notification is
        # filtered. Losing the row would defeat the Alerts screen.
        settings_env(ALERT_PROVIDER="console", ALERT_MIN_LEVEL="CRITICAL")
        reset_alert_dispatcher()

        session = StubSession()
        outcome = await AlertService(session).raise_processing_failure(
            contract=FakeContract(), stage=PipelineStage.PARSER, error=error()
        )

        assert outcome.persisted is True
        assert outcome.delivered == {}

    async def test_critical_passes_a_critical_floor(self, settings_env: Any) -> None:
        settings_env(ALERT_PROVIDER="console", ALERT_MIN_LEVEL="CRITICAL")
        reset_alert_dispatcher()

        outcome = await AlertService(StubSession()).raise_processing_failure(
            contract=FakeContract(),
            stage=PipelineStage.EMBEDDING,
            error=error(code="database_error"),
        )
        assert outcome.delivered == {"console": True}

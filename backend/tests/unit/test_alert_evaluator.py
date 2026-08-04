"""Unit tests for :mod:`app.services.alert_evaluator`.

Two halves, tested differently.

The evaluators are pure - a row, a rule and a date in, a draft or ``None`` out -
so these read as statements about the domain: "a contract expiring in 200 days
with a 90-day window is not an alert". No session, no clock, no fixtures.

The reconciler is the part with teeth, and the tests that matter there are the
negative ones. It must not resolve an alert nobody told it to, must not undo an
acknowledgement, and must not walk an alert up the severity ladder once per
sweep. Each of those is a way for a background job to quietly destroy an
operator's work, and none of them would be noticed by a test that only checked
that alerts get raised.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from app.core.enums import (
    AlertSeverity,
    AlertStatus,
    AlertType,
    ContractStatus,
    ObligationStatus,
)
from app.models.alert import Alert
from app.services.alert_evaluator import (
    EVALUATED_TYPES,
    AlertEvaluator,
    EvaluationOutcome,
    ResolvedRule,
    evaluate_auto_renewal,
    evaluate_expiring,
    evaluate_high_risk,
    evaluate_missing_clause,
    evaluate_obligation,
    evaluate_review_required,
)

TODAY = date(2026, 8, 3)


# =============================================================================
# Doubles
# =============================================================================
class Row:
    """Anything with attributes. The evaluators only ever read attributes."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


def contract(**overrides: Any) -> Row:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "title": "Acme / Globex MSA",
        "needs_review": False,
        "status": ContractStatus.READY,
    }
    return Row(**{**base, **overrides})


def metadata(**overrides: Any) -> Row:
    base: dict[str, Any] = {
        "expiration_date": None,
        "notice_deadline": None,
        "auto_renewal": False,
        "auto_renewal_notice_days": None,
        "renewal_term_months": None,
        "risk_score": None,
        "risk_band": None,
        "risk_factors": [],
        "missing_mandatory_clauses": [],
        "has_unlimited_liability": None,
        "contract_value": None,
        "currency": None,
    }
    return Row(**{**base, **overrides})


def rule(alert_type: AlertType, **overrides: Any) -> ResolvedRule:
    return ResolvedRule(
        alert_type=alert_type,
        severity=overrides.pop("severity", AlertSeverity.MEDIUM),
        config=overrides.pop("config", {}),
        escalate_after_days=overrides.pop("escalate_after_days", None),
        rule_id=overrides.pop("rule_id", uuid.uuid4()),
    )


class StubResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def unique(self) -> StubResult:
        return self

    def scalars(self) -> StubResult:
        return self

    def all(self) -> list[Any]:
        return self._rows


class StubSession:
    """Returns a canned result for every query. Used only by reconciler tests."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.added: list[Any] = []
        self.flushes = 0

    async def execute(self, _statement: Any) -> StubResult:
        return StubResult(self.rows)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1


def stored(
    dedupe_key: str,
    *,
    alert_type: AlertType = AlertType.CONTRACT_EXPIRING,
    project_id: uuid.UUID | None = None,
    status: AlertStatus = AlertStatus.OPEN,
    severity: AlertSeverity = AlertSeverity.MEDIUM,
    age_days: int = 0,
    details: dict[str, Any] | None = None,
) -> Alert:
    """An Alert row as the database would hand it back."""
    row = Alert(
        project_id=project_id or uuid.uuid4(),
        alert_type=alert_type,
        severity=severity,
        status=status,
        title="Stored",
        message="Stored",
        dedupe_key=dedupe_key,
        details=details or {},
    )
    row.created_at = datetime.now(tz=None).astimezone() - timedelta(days=age_days)
    return row


# =============================================================================
# Expiry
# =============================================================================
class TestExpiring:
    def test_inside_the_window_raises(self) -> None:
        draft = evaluate_expiring(
            contract(),
            metadata(expiration_date=TODAY + timedelta(days=60)),
            rule(AlertType.CONTRACT_EXPIRING, config={"window_days": 90}),
            TODAY,
        )
        assert draft is not None
        assert draft.alert_type is AlertType.CONTRACT_EXPIRING
        assert draft.due_date == TODAY + timedelta(days=60)
        assert draft.details["days_remaining"] == 60

    def test_outside_the_window_is_silent(self) -> None:
        assert (
            evaluate_expiring(
                contract(),
                metadata(expiration_date=TODAY + timedelta(days=200)),
                rule(AlertType.CONTRACT_EXPIRING, config={"window_days": 90}),
                TODAY,
            )
            is None
        )

    def test_already_expired_is_silent(self) -> None:
        """The first sweep must not alert on every historical contract."""
        assert (
            evaluate_expiring(
                contract(),
                metadata(expiration_date=TODAY - timedelta(days=1)),
                rule(AlertType.CONTRACT_EXPIRING, config={"window_days": 90}),
                TODAY,
            )
            is None
        )

    def test_no_expiry_date_is_silent(self) -> None:
        assert (
            evaluate_expiring(contract(), metadata(), rule(AlertType.CONTRACT_EXPIRING), TODAY)
            is None
        )

    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (5, AlertSeverity.CRITICAL),
            (20, AlertSeverity.HIGH),
            (80, AlertSeverity.MEDIUM),
        ],
    )
    def test_severity_climbs_as_the_date_approaches(
        self, days: int, expected: AlertSeverity
    ) -> None:
        draft = evaluate_expiring(
            contract(),
            metadata(expiration_date=TODAY + timedelta(days=days)),
            rule(
                AlertType.CONTRACT_EXPIRING,
                severity=AlertSeverity.MEDIUM,
                config={"window_days": 90, "escalate_days": 30, "critical_days": 7},
            ),
            TODAY,
        )
        assert draft is not None
        assert draft.severity is expected

    def test_severity_never_drops_below_the_rule(self) -> None:
        """A rule set to HIGH means at least HIGH, whatever the date says."""
        draft = evaluate_expiring(
            contract(),
            metadata(expiration_date=TODAY + timedelta(days=80)),
            rule(
                AlertType.CONTRACT_EXPIRING,
                severity=AlertSeverity.HIGH,
                config={"window_days": 90},
            ),
            TODAY,
        )
        assert draft is not None
        assert draft.severity is AlertSeverity.HIGH

    def test_the_date_is_part_of_the_identity(self) -> None:
        """A corrected expiry retires the old alert instead of mutating it."""
        subject = contract()
        first = evaluate_expiring(
            subject,
            metadata(expiration_date=TODAY + timedelta(days=30)),
            rule(AlertType.CONTRACT_EXPIRING),
            TODAY,
        )
        second = evaluate_expiring(
            subject,
            metadata(expiration_date=TODAY + timedelta(days=31)),
            rule(AlertType.CONTRACT_EXPIRING),
            TODAY,
        )
        assert first is not None and second is not None
        assert first.dedupe_key != second.dedupe_key

    def test_a_malformed_threshold_falls_back_rather_than_raising(self) -> None:
        """`config` is JSONB typed into a form; junk must not break the sweep."""
        draft = evaluate_expiring(
            contract(),
            metadata(expiration_date=TODAY + timedelta(days=60)),
            rule(AlertType.CONTRACT_EXPIRING, config={"window_days": "ninety"}),
            TODAY,
        )
        assert draft is not None  # fell back to the 90-day default


# =============================================================================
# Auto-renewal
# =============================================================================
class TestAutoRenewal:
    def test_not_auto_renewing_is_silent(self) -> None:
        assert (
            evaluate_auto_renewal(
                contract(),
                metadata(auto_renewal=False, notice_deadline=TODAY + timedelta(days=10)),
                rule(AlertType.AUTO_RENEWAL_NOTICE),
                TODAY,
            )
            is None
        )

    def test_uses_the_extracted_deadline_when_there_is_one(self) -> None:
        deadline = TODAY + timedelta(days=20)
        draft = evaluate_auto_renewal(
            contract(),
            metadata(auto_renewal=True, notice_deadline=deadline),
            rule(AlertType.AUTO_RENEWAL_NOTICE, config={"lead_days": 45}),
            TODAY,
        )
        assert draft is not None
        assert draft.due_date == deadline
        assert draft.details["derived_deadline"] is False

    def test_derives_the_deadline_by_counting_back_from_expiry(self) -> None:
        draft = evaluate_auto_renewal(
            contract(),
            metadata(
                auto_renewal=True,
                expiration_date=TODAY + timedelta(days=40),
                auto_renewal_notice_days=30,
            ),
            rule(AlertType.AUTO_RENEWAL_NOTICE, config={"lead_days": 45}),
            TODAY,
        )
        assert draft is not None
        assert draft.due_date == TODAY + timedelta(days=10)
        assert draft.details["derived_deadline"] is True
        # The message has to say so - a derived date is weaker evidence than one
        # the contract states, and the reader needs to know which they have.
        assert "calculated from the expiry date" in draft.message

    def test_silent_when_neither_deadline_nor_expiry_is_known(self) -> None:
        assert (
            evaluate_auto_renewal(
                contract(),
                metadata(auto_renewal=True),
                rule(AlertType.AUTO_RENEWAL_NOTICE),
                TODAY,
            )
            is None
        )

    def test_a_passed_deadline_is_silent(self) -> None:
        assert (
            evaluate_auto_renewal(
                contract(),
                metadata(auto_renewal=True, notice_deadline=TODAY - timedelta(days=1)),
                rule(AlertType.AUTO_RENEWAL_NOTICE),
                TODAY,
            )
            is None
        )


# =============================================================================
# Risk, missing clauses, review
# =============================================================================
class TestThresholds:
    def test_risk_below_the_cutoff_is_silent(self) -> None:
        assert (
            evaluate_high_risk(
                contract(),
                metadata(risk_score=66),
                rule(AlertType.HIGH_RISK, config={"risk_score_cutoff": 67}),
                TODAY,
            )
            is None
        )

    def test_risk_at_the_cutoff_raises(self) -> None:
        draft = evaluate_high_risk(
            contract(),
            metadata(risk_score=67),
            rule(AlertType.HIGH_RISK, config={"risk_score_cutoff": 67}),
            TODAY,
        )
        assert draft is not None
        assert draft.details["risk_score"] == 67

    def test_a_re_scored_contract_keeps_the_same_identity(self) -> None:
        """Risk is a standing condition, not a deadline: one alert, refreshed."""
        subject = contract()
        low = evaluate_high_risk(subject, metadata(risk_score=70), rule(AlertType.HIGH_RISK), TODAY)
        high = evaluate_high_risk(
            subject, metadata(risk_score=95), rule(AlertType.HIGH_RISK), TODAY
        )
        assert low is not None and high is not None
        assert low.dedupe_key == high.dedupe_key
        assert high.severity is AlertSeverity.CRITICAL

    def test_missing_clauses_can_be_narrowed_to_the_ones_a_project_cares_about(self) -> None:
        draft = evaluate_missing_clause(
            contract(),
            metadata(missing_mandatory_clauses=["indemnity", "force_majeure"]),
            rule(AlertType.MISSING_MANDATORY_CLAUSE, config={"clause_types": ["indemnity"]}),
            TODAY,
        )
        assert draft is not None
        assert draft.details["missing_clauses"] == ["indemnity"]

    def test_missing_clauses_below_the_minimum_is_silent(self) -> None:
        assert (
            evaluate_missing_clause(
                contract(),
                metadata(missing_mandatory_clauses=["indemnity"]),
                rule(AlertType.MISSING_MANDATORY_CLAUSE, config={"min_missing": 2}),
                TODAY,
            )
            is None
        )

    def test_nothing_missing_is_silent(self) -> None:
        assert (
            evaluate_missing_clause(
                contract(), metadata(), rule(AlertType.MISSING_MANDATORY_CLAUSE), TODAY
            )
            is None
        )

    def test_review_fires_on_the_contract_flag(self) -> None:
        draft = evaluate_review_required(
            contract(needs_review=True), metadata(), rule(AlertType.REVIEW_REQUIRED), TODAY
        )
        assert draft is not None
        assert draft.details["contract_flagged"] is True

    def test_review_fires_on_a_clause_backlog_alone(self) -> None:
        draft = evaluate_review_required(
            contract(),
            metadata(),
            rule(AlertType.REVIEW_REQUIRED, config={"min_items": 3}),
            TODAY,
            pending_clauses=4,
        )
        assert draft is not None
        assert "4 clauses are waiting" in draft.message

    def test_review_is_silent_when_neither_trigger_holds(self) -> None:
        assert (
            evaluate_review_required(
                contract(),
                metadata(),
                rule(AlertType.REVIEW_REQUIRED, config={"min_items": 3}),
                TODAY,
                pending_clauses=2,
            )
            is None
        )


# =============================================================================
# Obligations
# =============================================================================
def obligation(**overrides: Any) -> Row:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "contract_id": uuid.uuid4(),
        "status": ObligationStatus.OPEN,
        "due_date": TODAY + timedelta(days=7),
        "action": "Deliver the quarterly compliance report",
        "responsible_party": "Globex",
    }
    return Row(**{**base, **overrides})


class TestObligations:
    def test_due_inside_the_window_raises(self) -> None:
        draft = evaluate_obligation(
            obligation(),
            contract(),
            rule(AlertType.OBLIGATION_DUE, config={"window_days": 14}),
            TODAY,
        )
        assert draft is not None
        assert draft.details["overdue"] is False

    def test_a_fulfilled_obligation_is_silent(self) -> None:
        assert (
            evaluate_obligation(
                obligation(status=ObligationStatus.FULFILLED),
                contract(),
                rule(AlertType.OBLIGATION_DUE),
                TODAY,
            )
            is None
        )

    def test_overdue_is_raised_and_escalated(self) -> None:
        draft = evaluate_obligation(
            obligation(due_date=TODAY - timedelta(days=3)),
            contract(),
            rule(AlertType.OBLIGATION_DUE, severity=AlertSeverity.MEDIUM),
            TODAY,
        )
        assert draft is not None
        assert draft.details["overdue"] is True
        assert draft.severity is AlertSeverity.HIGH
        assert "3 days overdue" in draft.title

    def test_long_overdue_falls_out_of_scope(self) -> None:
        """Otherwise a four-year-old contract resurfaces on every sweep."""
        assert (
            evaluate_obligation(
                obligation(due_date=TODAY - timedelta(days=400)),
                contract(),
                rule(AlertType.OBLIGATION_DUE, config={"overdue_days": 30}),
                TODAY,
            )
            is None
        )

    def test_an_undated_obligation_is_silent(self) -> None:
        assert (
            evaluate_obligation(
                obligation(due_date=None), contract(), rule(AlertType.OBLIGATION_DUE), TODAY
            )
            is None
        )


# =============================================================================
# Rule resolution
# =============================================================================
class TestRuleResolution:
    def test_a_project_rule_beats_the_platform_default(self) -> None:
        project = uuid.uuid4()
        platform = rule(AlertType.CONTRACT_EXPIRING, config={"window_days": 90})
        override = rule(AlertType.CONTRACT_EXPIRING, config={"window_days": 180})
        rules = {
            (None, AlertType.CONTRACT_EXPIRING): platform,
            (project, AlertType.CONTRACT_EXPIRING): override,
        }
        assert AlertEvaluator._rule_for(rules, project, AlertType.CONTRACT_EXPIRING) is override
        assert (
            AlertEvaluator._rule_for(rules, uuid.uuid4(), AlertType.CONTRACT_EXPIRING) is platform
        )

    def test_an_unconfigured_type_resolves_to_nothing(self) -> None:
        assert AlertEvaluator._rule_for({}, uuid.uuid4(), AlertType.HIGH_RISK) is None

    def test_processing_failures_are_not_this_sweeps_business(self) -> None:
        """The orchestrator raises them, and only it knows when they are over."""
        assert AlertType.PROCESSING_FAILED not in EVALUATED_TYPES


# =============================================================================
# Reconciliation
# =============================================================================
@pytest.mark.asyncio
class TestReconcile:
    async def test_a_new_condition_is_raised(self) -> None:
        db = StubSession(rows=[])
        evaluator = AlertEvaluator(db, today=TODAY)  # type: ignore[arg-type]
        subject = contract()
        draft = evaluate_expiring(
            subject,
            metadata(expiration_date=TODAY + timedelta(days=30)),
            rule(AlertType.CONTRACT_EXPIRING),
            TODAY,
        )
        assert draft is not None

        outcome = EvaluationOutcome()
        rules = {(None, AlertType.CONTRACT_EXPIRING): rule(AlertType.CONTRACT_EXPIRING)}
        await evaluator._reconcile([draft], rules, outcome, project_id=None)

        assert outcome.raised == 1
        assert len(db.added) == 1
        assert db.added[0].dedupe_key == draft.dedupe_key
        assert db.added[0].details["source"] == "evaluator"

    async def test_an_existing_condition_refreshes_rather_than_duplicating(self) -> None:
        subject = contract()
        draft = evaluate_expiring(
            subject,
            metadata(expiration_date=TODAY + timedelta(days=5)),
            rule(AlertType.CONTRACT_EXPIRING, config={"critical_days": 7}),
            TODAY,
        )
        assert draft is not None

        existing = stored(draft.dedupe_key, project_id=subject.project_id)
        db = StubSession(rows=[existing])
        evaluator = AlertEvaluator(db, today=TODAY)  # type: ignore[arg-type]

        outcome = EvaluationOutcome()
        rules = {(None, AlertType.CONTRACT_EXPIRING): rule(AlertType.CONTRACT_EXPIRING)}
        await evaluator._reconcile([draft], rules, outcome, project_id=None)

        assert outcome.raised == 0
        assert outcome.refreshed == 1
        assert db.added == []
        assert existing.severity is AlertSeverity.CRITICAL
        assert existing.status is AlertStatus.OPEN

    async def test_refreshing_does_not_undo_an_acknowledgement(self) -> None:
        """The sweep owns the facts. Status and note belong to the operator."""
        subject = contract()
        draft = evaluate_expiring(
            subject,
            metadata(expiration_date=TODAY + timedelta(days=30)),
            rule(AlertType.CONTRACT_EXPIRING),
            TODAY,
        )
        assert draft is not None

        existing = stored(
            draft.dedupe_key, project_id=subject.project_id, status=AlertStatus.ACKNOWLEDGED
        )
        existing.note = "Legal is on it."
        db = StubSession(rows=[existing])
        evaluator = AlertEvaluator(db, today=TODAY)  # type: ignore[arg-type]

        rules = {(None, AlertType.CONTRACT_EXPIRING): rule(AlertType.CONTRACT_EXPIRING)}
        await evaluator._reconcile([draft], rules, EvaluationOutcome(), project_id=None)

        assert existing.status is AlertStatus.ACKNOWLEDGED
        assert existing.note == "Legal is on it."
        assert db.added == []

    async def test_a_condition_that_no_longer_holds_is_retired(self) -> None:
        existing = stored("expiring:gone:2026-09-01")
        db = StubSession(rows=[existing])
        evaluator = AlertEvaluator(db, today=TODAY)  # type: ignore[arg-type]

        outcome = EvaluationOutcome()
        rules = {(None, AlertType.CONTRACT_EXPIRING): rule(AlertType.CONTRACT_EXPIRING)}
        await evaluator._reconcile([], rules, outcome, project_id=None)

        assert outcome.retired == 1
        assert existing.status is AlertStatus.RESOLVED
        assert existing.resolved_at is not None
        assert existing.details["resolved_by"] == "evaluator"

    async def test_a_disabled_rule_leaves_its_alerts_alone(self) -> None:
        """Switching a rule off is about the future, not about the queue."""
        existing = stored("expiring:gone:2026-09-01")
        db = StubSession(rows=[existing])
        evaluator = AlertEvaluator(db, today=TODAY)  # type: ignore[arg-type]

        outcome = EvaluationOutcome()
        # No rule for CONTRACT_EXPIRING - `_load_rules` drops disabled rows.
        await evaluator._reconcile([], {}, outcome, project_id=None)

        assert outcome.retired == 0
        assert existing.status is AlertStatus.OPEN

    async def test_an_alert_this_sweep_does_not_own_is_never_retired(self) -> None:
        existing = stored("pipeline:x", alert_type=AlertType.PROCESSING_FAILED)
        db = StubSession(rows=[existing])
        evaluator = AlertEvaluator(db, today=TODAY)  # type: ignore[arg-type]

        outcome = EvaluationOutcome()
        rules = {(None, AlertType.CONTRACT_EXPIRING): rule(AlertType.CONTRACT_EXPIRING)}
        await evaluator._reconcile([], rules, outcome, project_id=None)

        assert outcome.retired == 0
        assert existing.status is AlertStatus.OPEN

    async def test_an_ignored_alert_escalates_once(self) -> None:
        subject = contract()
        draft = evaluate_expiring(
            subject,
            metadata(expiration_date=TODAY + timedelta(days=60)),
            rule(AlertType.CONTRACT_EXPIRING),
            TODAY,
        )
        assert draft is not None

        existing = stored(
            draft.dedupe_key,
            project_id=subject.project_id,
            severity=AlertSeverity.MEDIUM,
            age_days=45,
        )
        db = StubSession(rows=[existing])
        evaluator = AlertEvaluator(db, today=TODAY)  # type: ignore[arg-type]
        rules = {
            (None, AlertType.CONTRACT_EXPIRING): rule(
                AlertType.CONTRACT_EXPIRING, escalate_after_days=30
            )
        }

        outcome = EvaluationOutcome()
        await evaluator._reconcile([draft], rules, outcome, project_id=None)
        assert outcome.escalated == 1
        assert existing.severity is AlertSeverity.HIGH

        # A second sweep must not walk it to CRITICAL by attrition.
        second = EvaluationOutcome()
        await evaluator._reconcile([draft], rules, second, project_id=None)
        assert second.escalated == 0
        assert existing.severity is AlertSeverity.HIGH

    async def test_a_young_alert_does_not_escalate(self) -> None:
        subject = contract()
        draft = evaluate_expiring(
            subject,
            metadata(expiration_date=TODAY + timedelta(days=60)),
            rule(AlertType.CONTRACT_EXPIRING),
            TODAY,
        )
        assert draft is not None

        existing = stored(draft.dedupe_key, project_id=subject.project_id, age_days=2)
        db = StubSession(rows=[existing])
        evaluator = AlertEvaluator(db, today=TODAY)  # type: ignore[arg-type]
        rules = {
            (None, AlertType.CONTRACT_EXPIRING): rule(
                AlertType.CONTRACT_EXPIRING, escalate_after_days=30
            )
        }

        outcome = EvaluationOutcome()
        await evaluator._reconcile([draft], rules, outcome, project_id=None)
        assert outcome.escalated == 0
        assert existing.severity is AlertSeverity.MEDIUM

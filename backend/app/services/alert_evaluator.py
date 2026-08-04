"""Alert evaluator - derive time- and threshold-based alerts from extracted data.

The counterpart to :mod:`app.services.alerts`. That service raises alerts about the
*platform* (a stage failed, a worker died) from the code path where the failure
happens. This one raises alerts about the *contracts*: an expiry approaching, an
auto-renewal notice window closing, an obligation falling due, a risk score over
the line, a mandatory clause absent, a document still waiting on a reviewer.

Nothing pushes those conditions; they arrive by the calendar moving. So they need a
sweep, which is what ``python -m app.cli scheduler`` runs and what
``python -m app.cli evaluate-alerts`` runs once on demand.

**Rules decide, code does not.** Every threshold comes from an :class:`AlertRule`
row - seeded as a platform default with ``project_id IS NULL``, optionally
overridden per project. A disabled rule means the type is not evaluated at all,
and (deliberately) its existing alerts are left alone rather than mass-resolved:
turning a rule off is a statement about the future, and silently clearing an
operator's queue is not what they asked for.

**Three things happen per sweep**, in this order:

1. *Raise* - a condition that holds and has no alert yet becomes one.
2. *Refresh* - a condition that holds and already has an open alert updates that
   row in place. Ninety days to expiry becoming sixty is new information about the
   same fact, not a second alert.
3. *Retire* - an alert whose condition no longer holds is resolved. Without this a
   renewed contract keeps warning about an expiry that has been dealt with, and
   the screen fills with things nobody can action.

De-duplication is by ``dedupe_key``, which is unique among *open* alerts at the
database level. The keys here embed the date the alert is about, so a corrected
expiry date retires the old alert and raises a new one rather than quietly
mutating the old one's meaning.

``PROCESSING_FAILED`` is not in :data:`_EVALUATORS` and never will be: it is raised
by the orchestrator, which knows things this sweep cannot. Listing it here would
make step 3 resolve every processing failure the moment a sweep ran.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.enums import (
    AlertSeverity,
    AlertStatus,
    AlertType,
    ContractStatus,
    ObligationStatus,
    ReviewStatus,
)
from app.core.logging import get_logger
from app.models.alert import Alert, AlertRule
from app.models.contract import Contract, ContractMetadata
from app.models.knowledge import Clause, Obligation

logger = get_logger(__name__)

#: Severity, weakest first. Escalation walks one step up this ladder; nothing ever
#: walks down, because an alert that has been urgent does not become less so by
#: being ignored.
_LADDER: tuple[AlertSeverity, ...] = (
    AlertSeverity.INFO,
    AlertSeverity.LOW,
    AlertSeverity.MEDIUM,
    AlertSeverity.HIGH,
    AlertSeverity.CRITICAL,
)

#: Statuses an alert can be in and still represent a live condition. A resolved or
#: dismissed alert is history: the same condition recurring gets a fresh row.
_LIVE_STATUSES = (AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED)


# =============================================================================
# Value objects
# =============================================================================
@dataclass(slots=True, frozen=True)
class ResolvedRule:
    """One rule as it applies to one project.

    Built from the project's own row when it has one, otherwise from the platform
    default. The evaluators only ever see this, so they cannot accidentally read a
    threshold from the wrong scope.
    """

    alert_type: AlertType
    severity: AlertSeverity
    config: Mapping[str, Any]
    escalate_after_days: int | None = None
    rule_id: uuid.UUID | None = None

    def days(self, key: str, default: int) -> int:
        """A ``*_days`` threshold from ``config``, tolerant of what an admin typed.

        ``config`` is free-form JSONB edited through a form, so a string, a float
        or a null are all reachable. A malformed threshold falls back to the
        default rather than failing the sweep for every other project.
        """
        return _int(self.config.get(key), default)


@dataclass(slots=True, frozen=True)
class AlertDraft:
    """A condition that holds, before it is reconciled against what is stored."""

    alert_type: AlertType
    severity: AlertSeverity
    project_id: uuid.UUID
    title: str
    message: str
    dedupe_key: str
    contract_id: uuid.UUID | None = None
    due_date: date | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EvaluationOutcome:
    """What one sweep did, for the log line and for the CLI summary."""

    raised: int = 0
    refreshed: int = 0
    retired: int = 0
    escalated: int = 0
    contracts_examined: int = 0
    obligations_examined: int = 0
    rules_applied: int = 0

    def as_log_fields(self) -> dict[str, int]:
        return {
            "raised": self.raised,
            "refreshed": self.refreshed,
            "retired": self.retired,
            "escalated": self.escalated,
            "contracts": self.contracts_examined,
            "obligations": self.obligations_examined,
            "rules": self.rules_applied,
        }

    def changed(self) -> bool:
        return bool(self.raised or self.refreshed or self.retired or self.escalated)


# =============================================================================
# Pure evaluators
#
# Each takes a row-shaped object, the rule that applies to it and today's date,
# and answers "is this a condition worth alerting on?". No session, no clock, no
# settings - so a test states a contract and a date and asserts the answer.
# =============================================================================
def evaluate_expiring(
    contract: Any, metadata: Any, rule: ResolvedRule, today: date
) -> AlertDraft | None:
    """A contract term ending inside the warning window.

    Already-expired contracts are excluded. They are a different condition - one
    that needs a decision about the record, not a deadline reminder - and
    including them would raise an alert for every historical contract in the
    estate the first time this ran.
    """
    expiry = _as_date(getattr(metadata, "expiration_date", None))
    if expiry is None:
        return None

    window = rule.days("window_days", 90)
    remaining = (expiry - today).days
    if remaining < 0 or remaining > window:
        return None

    severity = rule.severity
    if remaining <= rule.days("critical_days", 7):
        severity = _at_least(severity, AlertSeverity.CRITICAL)
    elif remaining <= rule.days("escalate_days", 30):
        severity = _at_least(severity, AlertSeverity.HIGH)

    label = contract_label(contract)
    return AlertDraft(
        alert_type=AlertType.CONTRACT_EXPIRING,
        severity=severity,
        project_id=contract.project_id,
        contract_id=contract.id,
        title=f"{label} expires in {remaining} days" if remaining else f"{label} expires today",
        message=(
            f"The term ends on {expiry.isoformat()}. "
            "Decide whether to renew, renegotiate or let it lapse before then."
        ),
        # The date is part of the identity: a corrected expiry is a different
        # deadline, so it retires this alert and raises its own.
        dedupe_key=f"expiring:{contract.id}:{expiry.isoformat()}",
        due_date=expiry,
        details={
            "days_remaining": remaining,
            "expiration_date": expiry.isoformat(),
            "window_days": window,
            "contract_value": _decimal_str(getattr(metadata, "contract_value", None)),
            "currency": getattr(metadata, "currency", None),
        },
    )


def evaluate_auto_renewal(
    contract: Any, metadata: Any, rule: ResolvedRule, today: date
) -> AlertDraft | None:
    """An auto-renewing contract whose notice window is closing.

    The deadline is the contract's own ``notice_deadline`` when extraction found
    one. Otherwise it is derived by counting back from expiry - which is the whole
    point of the alert: miss that date and the term renews whether or not anybody
    intended it.
    """
    if not getattr(metadata, "auto_renewal", False):
        return None

    lead = rule.days("lead_days", 45)
    deadline = _as_date(getattr(metadata, "notice_deadline", None))
    derived = False
    if deadline is None:
        expiry = _as_date(getattr(metadata, "expiration_date", None))
        if expiry is None:
            return None
        notice_days = _int(getattr(metadata, "auto_renewal_notice_days", None), lead)
        deadline = expiry - timedelta(days=notice_days)
        derived = True

    remaining = (deadline - today).days
    if remaining < 0 or remaining > lead:
        return None

    severity = rule.severity
    if remaining <= rule.days("critical_days", 14):
        severity = _at_least(severity, AlertSeverity.CRITICAL)

    label = contract_label(contract)
    return AlertDraft(
        alert_type=AlertType.AUTO_RENEWAL_NOTICE,
        severity=severity,
        project_id=contract.project_id,
        contract_id=contract.id,
        title=f"{label} auto-renews unless notice is given by {deadline.isoformat()}",
        message=(
            f"This contract renews automatically. Notice must be served within {remaining} days"
            f" ({deadline.isoformat()}) to stop it."
            + (
                " That date is calculated from the expiry date and the notice period,"
                " because no explicit notice deadline was extracted."
                if derived
                else ""
            )
        ),
        dedupe_key=f"renewal:{contract.id}:{deadline.isoformat()}",
        due_date=deadline,
        details={
            "days_remaining": remaining,
            "notice_deadline": deadline.isoformat(),
            "derived_deadline": derived,
            "renewal_term_months": getattr(metadata, "renewal_term_months", None),
        },
    )


def evaluate_high_risk(
    contract: Any, metadata: Any, rule: ResolvedRule, today: date
) -> AlertDraft | None:
    """A risk score at or above the cutoff.

    No date in the dedupe key: the risk is a standing condition, not a deadline,
    so a re-scored contract refreshes the same alert instead of accumulating one
    per sweep.
    """
    score = getattr(metadata, "risk_score", None)
    if score is None:
        return None
    score = _int(score, 0)
    cutoff = _int(rule.config.get("risk_score_cutoff"), 67)
    if score < cutoff:
        return None

    severity = rule.severity
    if score >= _int(rule.config.get("critical_score"), 85):
        severity = _at_least(severity, AlertSeverity.CRITICAL)

    factors = getattr(metadata, "risk_factors", None) or []
    return AlertDraft(
        alert_type=AlertType.HIGH_RISK,
        severity=severity,
        project_id=contract.project_id,
        contract_id=contract.id,
        title=f"{contract_label(contract)} scored {score} for risk",
        message=(
            f"The overall risk score is {score}, at or above the {cutoff} threshold"
            f" for this project. {len(factors)} risk factors were identified."
        ),
        dedupe_key=f"high_risk:{contract.id}",
        details={
            "risk_score": score,
            "cutoff": cutoff,
            "risk_band": _enum_value(getattr(metadata, "risk_band", None)),
            "has_unlimited_liability": getattr(metadata, "has_unlimited_liability", None),
            # Names only. The full factor objects belong on the contract, not
            # duplicated into every alert row.
            "risk_factors": [
                str(entry.get("name") or entry.get("type") or entry)
                for entry in factors
                if isinstance(entry, dict)
            ][:10],
        },
    )


def evaluate_missing_clause(
    contract: Any, metadata: Any, rule: ResolvedRule, today: date
) -> AlertDraft | None:
    """Mandatory clause types the extraction did not find.

    ``clause_types`` in the rule narrows this to the ones a project actually cares
    about; empty means "whatever the contract's profile marks mandatory", which is
    already what ``missing_mandatory_clauses`` holds.
    """
    missing = [str(entry) for entry in (getattr(metadata, "missing_mandatory_clauses", None) or [])]
    watched = [str(entry) for entry in (rule.config.get("clause_types") or [])]
    if watched:
        watched_set = {entry.lower() for entry in watched}
        missing = [entry for entry in missing if entry.lower() in watched_set]

    if len(missing) < max(1, _int(rule.config.get("min_missing"), 1)):
        return None

    return AlertDraft(
        alert_type=AlertType.MISSING_MANDATORY_CLAUSE,
        severity=rule.severity,
        project_id=contract.project_id,
        contract_id=contract.id,
        title=f"{contract_label(contract)} is missing {len(missing)} mandatory clauses",
        message=(
            "These clause types are required by the document profile but were not found: "
            + ", ".join(entry.replace("_", " ") for entry in sorted(missing))
            + "."
        ),
        dedupe_key=f"missing_clause:{contract.id}",
        details={"missing_clauses": sorted(missing), "missing_count": len(missing)},
    )


def evaluate_review_required(
    contract: Any, metadata: Any, rule: ResolvedRule, today: date, pending_clauses: int = 0
) -> AlertDraft | None:
    """Extraction flagged the document, or clauses are queued for a human.

    Two independent triggers, because they come from different places: the
    contract-level ``needs_review`` flag is set by the pipeline's own confidence
    checks, while pending clauses accumulate from the review rules in the profile.
    Either one means somebody has to look.
    """
    flagged = (
        bool(getattr(contract, "needs_review", False))
        or _enum_value(getattr(contract, "status", None)) == ContractStatus.NEEDS_REVIEW.value
    )
    minimum = max(1, _int(rule.config.get("min_items"), 1))
    if not flagged and pending_clauses < minimum:
        return None

    reason = (
        f"{pending_clauses} clauses are waiting on a reviewer"
        if pending_clauses
        else "extraction confidence fell below the profile's threshold"
    )
    return AlertDraft(
        alert_type=AlertType.REVIEW_REQUIRED,
        severity=rule.severity,
        project_id=contract.project_id,
        contract_id=contract.id,
        title=f"{contract_label(contract)} needs review",
        message=f"This contract was flagged for human review: {reason}.",
        dedupe_key=f"review_required:{contract.id}",
        details={"pending_clauses": pending_clauses, "contract_flagged": flagged},
    )


def evaluate_obligation(
    obligation: Any, contract: Any, rule: ResolvedRule, today: date
) -> AlertDraft | None:
    """An unfulfilled obligation falling due, or recently missed.

    Overdue obligations are included - an obligation register that cannot tell you
    what is late is not much of a register - but only within ``overdue_days``, so
    a contract from four years ago does not resurface forever.
    """
    status = _enum_value(getattr(obligation, "status", None))
    if status not in {ObligationStatus.OPEN.value, ObligationStatus.IN_PROGRESS.value}:
        return None

    due = _as_date(getattr(obligation, "due_date", None))
    if due is None:
        return None

    window = rule.days("window_days", 14)
    overdue_window = rule.days("overdue_days", 30)
    remaining = (due - today).days
    if remaining > window or remaining < -overdue_window:
        return None

    severity = rule.severity
    if remaining < 0:
        severity = _at_least(severity, AlertSeverity.HIGH)

    action = str(getattr(obligation, "action", "") or "An obligation")
    short = action if len(action) <= 90 else f"{action[:87]}..."
    party = getattr(obligation, "responsible_party", None)
    return AlertDraft(
        alert_type=AlertType.OBLIGATION_DUE,
        severity=severity,
        project_id=obligation.project_id,
        contract_id=obligation.contract_id,
        title=(
            f"Obligation {abs(remaining)} days overdue: {short}"
            if remaining < 0
            else f"Obligation due in {remaining} days: {short}"
        ),
        message=(
            f"{action}\n\nDue {due.isoformat()}"
            + (f", owed by {party}" if party else "")
            + f". Contract: {contract_label(contract)}."
        ),
        dedupe_key=f"obligation:{obligation.id}:{due.isoformat()}",
        due_date=due,
        details={
            "days_remaining": remaining,
            "overdue": remaining < 0,
            "responsible_party": party,
            "obligation_id": str(obligation.id),
            "obligation_status": status,
        },
    )


#: Contract-level evaluators, keyed by the type whose rule governs them.
#: ``PROCESSING_FAILED`` is absent on purpose - see the module docstring.
_EVALUATORS = {
    AlertType.CONTRACT_EXPIRING: evaluate_expiring,
    AlertType.AUTO_RENEWAL_NOTICE: evaluate_auto_renewal,
    AlertType.HIGH_RISK: evaluate_high_risk,
    AlertType.MISSING_MANDATORY_CLAUSE: evaluate_missing_clause,
}

#: Every type this sweep owns. Retirement is confined to these, so an alert raised
#: by anything else is never resolved behind its author's back.
EVALUATED_TYPES: frozenset[AlertType] = frozenset(
    {*_EVALUATORS, AlertType.REVIEW_REQUIRED, AlertType.OBLIGATION_DUE}
)


# =============================================================================
# The sweep
# =============================================================================
class AlertEvaluator:
    """Run one pass over the estate. Constructed per sweep, like the repositories.

    The caller owns the transaction: this flushes but never commits, so the
    scheduler's ``session_scope`` commits the whole sweep or none of it.
    """

    def __init__(self, db: AsyncSession, *, today: date | None = None) -> None:
        self.db = db
        #: Injectable so a test can state "it is the 1st of March" without patching
        #: the clock globally.
        self.today = today or datetime.now(UTC).date()

    async def run(self, *, project_id: uuid.UUID | None = None) -> EvaluationOutcome:
        outcome = EvaluationOutcome()
        rules = await self._load_rules()
        if not rules:
            logger.info("alert_evaluator_no_rules")
            return outcome
        outcome.rules_applied = len(rules)

        drafts = await self._collect(rules, outcome, project_id=project_id)
        await self._reconcile(drafts, rules, outcome, project_id=project_id)
        logger.info("alert_evaluator_swept", **outcome.as_log_fields())
        return outcome

    # ------------------------------------------------------------------- rules
    async def _load_rules(self) -> dict[tuple[uuid.UUID | None, AlertType], ResolvedRule]:
        """Enabled rules, keyed by ``(project_id, alert_type)``.

        A NULL-project row is the platform default; :meth:`_rule_for` falls back to
        it. Disabled rows are dropped here rather than checked later, so a disabled
        type is invisible to both raising *and* retirement.
        """
        rows = (
            (await self.db.execute(select(AlertRule).where(AlertRule.is_enabled.is_(True))))
            .scalars()
            .all()
        )
        resolved: dict[tuple[uuid.UUID | None, AlertType], ResolvedRule] = {}
        for row in rows:
            alert_type = AlertType(_enum_value(row.alert_type))
            if alert_type not in EVALUATED_TYPES:
                continue
            resolved[(row.project_id, alert_type)] = ResolvedRule(
                alert_type=alert_type,
                severity=AlertSeverity(_enum_value(row.severity)),
                config=dict(row.config or {}),
                escalate_after_days=row.escalate_after_days,
                rule_id=row.id,
            )
        return resolved

    @staticmethod
    def _rule_for(
        rules: Mapping[tuple[uuid.UUID | None, AlertType], ResolvedRule],
        project_id: uuid.UUID,
        alert_type: AlertType,
    ) -> ResolvedRule | None:
        return rules.get((project_id, alert_type)) or rules.get((None, alert_type))

    # ------------------------------------------------------------- collection
    async def _collect(
        self,
        rules: Mapping[tuple[uuid.UUID | None, AlertType], ResolvedRule],
        outcome: EvaluationOutcome,
        *,
        project_id: uuid.UUID | None,
    ) -> list[AlertDraft]:
        drafts: list[AlertDraft] = []

        contract_filter: list[ColumnElement[bool]] = [
            Contract.deleted_at.is_(None),
            Contract.status.notin_([ContractStatus.ARCHIVED, ContractStatus.FAILED]),
        ]
        if project_id is not None:
            contract_filter.append(Contract.project_id == project_id)

        # One pass over contracts feeds five of the six types. Metadata is an outer
        # join because a contract that has not finished extraction still has a
        # `needs_review` flag worth alerting on.
        rows = (
            await self.db.execute(
                select(Contract, ContractMetadata)
                .join(
                    ContractMetadata,
                    ContractMetadata.contract_id == Contract.id,
                    isouter=True,
                )
                .where(*contract_filter)
            )
        ).all()

        pending = await self._pending_clause_counts(project_id)

        for contract, metadata in rows:
            outcome.contracts_examined += 1
            for alert_type, evaluate in _EVALUATORS.items():
                rule = self._rule_for(rules, contract.project_id, alert_type)
                if rule is None or metadata is None:
                    continue
                draft = evaluate(contract, metadata, rule, self.today)
                if draft is not None:
                    drafts.append(draft)

            review_rule = self._rule_for(rules, contract.project_id, AlertType.REVIEW_REQUIRED)
            if review_rule is not None:
                draft = evaluate_review_required(
                    contract,
                    metadata,
                    review_rule,
                    self.today,
                    pending_clauses=pending.get(contract.id, 0),
                )
                if draft is not None:
                    drafts.append(draft)

        drafts.extend(await self._collect_obligations(rules, outcome, project_id=project_id))
        return drafts

    async def _pending_clause_counts(self, project_id: uuid.UUID | None) -> dict[uuid.UUID, int]:
        """How many clauses per contract are waiting on a reviewer."""
        stmt = (
            select(Clause.contract_id, func.count().label("pending"))
            .where(Clause.review_status == ReviewStatus.PENDING.value)
            .group_by(Clause.contract_id)
        )
        if project_id is not None:
            stmt = stmt.where(Clause.project_id == project_id)
        return {row[0]: int(row[1]) for row in (await self.db.execute(stmt)).all()}

    async def _collect_obligations(
        self,
        rules: Mapping[tuple[uuid.UUID | None, AlertType], ResolvedRule],
        outcome: EvaluationOutcome,
        *,
        project_id: uuid.UUID | None,
    ) -> list[AlertDraft]:
        # Skipped entirely when no project has the rule enabled, so a deployment
        # that does not want obligation alerts does not pay for the join.
        if not any(key[1] is AlertType.OBLIGATION_DUE for key in rules):
            return []

        window = max(
            rule.days("window_days", 14)
            for key, rule in rules.items()
            if key[1] is AlertType.OBLIGATION_DUE
        )
        overdue = max(
            rule.days("overdue_days", 30)
            for key, rule in rules.items()
            if key[1] is AlertType.OBLIGATION_DUE
        )

        conditions: list[ColumnElement[bool]] = [
            Contract.deleted_at.is_(None),
            Obligation.due_date.is_not(None),
            Obligation.due_date <= self.today + timedelta(days=window),
            Obligation.due_date >= self.today - timedelta(days=overdue),
            Obligation.status.in_([ObligationStatus.OPEN, ObligationStatus.IN_PROGRESS]),
        ]
        if project_id is not None:
            conditions.append(Obligation.project_id == project_id)

        rows = (
            await self.db.execute(
                select(Obligation, Contract)
                .join(Contract, Contract.id == Obligation.contract_id)
                .where(*conditions)
            )
        ).all()

        drafts: list[AlertDraft] = []
        for obligation, contract in rows:
            outcome.obligations_examined += 1
            rule = self._rule_for(rules, obligation.project_id, AlertType.OBLIGATION_DUE)
            if rule is None:
                continue
            draft = evaluate_obligation(obligation, contract, rule, self.today)
            if draft is not None:
                drafts.append(draft)
        return drafts

    # ------------------------------------------------------------ persistence
    async def _reconcile(
        self,
        drafts: Sequence[AlertDraft],
        rules: Mapping[tuple[uuid.UUID | None, AlertType], ResolvedRule],
        outcome: EvaluationOutcome,
        *,
        project_id: uuid.UUID | None,
    ) -> None:
        """Raise, refresh, retire, escalate - against what is already stored."""
        existing = await self._load_live_alerts(project_id)
        by_key = {row.dedupe_key: row for row in existing}
        seen: set[str] = set()
        now = datetime.now(UTC)

        for draft in drafts:
            seen.add(draft.dedupe_key)
            current = by_key.get(draft.dedupe_key)
            if current is None:
                self.db.add(self._to_row(draft, rules))
                outcome.raised += 1
                continue

            # Refresh in place. Status and note are the operator's, and are never
            # touched here - acknowledging an alert must not be undone by a sweep.
            #
            # Severity is recomputed from the rule, then the ageing bump from a
            # previous sweep is re-applied on top. Assigning the draft's severity
            # flat would silently undo an escalation, and because `_escalate`
            # fires once per alert it would never be re-applied - the alert would
            # sink back to its base severity and stay there.
            severity = draft.severity
            if (current.details or {}).get("escalated_at"):
                severity = _promote(severity)

            changed = (
                current.severity != severity
                or current.title != draft.title[:255]
                or current.message != draft.message
                or current.due_date != draft.due_date
            )
            current.severity = severity
            current.title = draft.title[:255]
            current.message = draft.message
            current.due_date = draft.due_date
            current.details = {
                **dict(current.details or {}),
                **dict(draft.details),
                "last_evaluated_at": now.isoformat(),
            }
            if changed:
                outcome.refreshed += 1

        outcome.retired = self._retire(existing, seen, rules, now)
        outcome.escalated = self._escalate(existing, seen, rules, now)
        await self.db.flush()

    async def _load_live_alerts(self, project_id: uuid.UUID | None) -> list[Alert]:
        conditions: list[ColumnElement[bool]] = [
            Alert.status.in_(_LIVE_STATUSES),
            Alert.alert_type.in_(sorted(EVALUATED_TYPES, key=lambda item: item.value)),
        ]
        if project_id is not None:
            conditions.append(Alert.project_id == project_id)
        return list(
            (await self.db.execute(select(Alert).where(*conditions))).unique().scalars().all()
        )

    def _to_row(
        self,
        draft: AlertDraft,
        rules: Mapping[tuple[uuid.UUID | None, AlertType], ResolvedRule],
    ) -> Alert:
        rule = self._rule_for(rules, draft.project_id, draft.alert_type)
        return Alert(
            project_id=draft.project_id,
            contract_id=draft.contract_id,
            alert_type=draft.alert_type,
            severity=draft.severity,
            status=AlertStatus.OPEN,
            title=draft.title[:255],
            message=draft.message,
            details={
                **dict(draft.details),
                "source": "evaluator",
                "first_seen_at": datetime.now(UTC).isoformat(),
            },
            due_date=draft.due_date,
            dedupe_key=draft.dedupe_key[:255],
            # So retuning a threshold can find the alerts it produced.
            rule_id=rule.rule_id if rule else None,
        )

    def _retire(
        self,
        existing: Iterable[Alert],
        seen: set[str],
        rules: Mapping[tuple[uuid.UUID | None, AlertType], ResolvedRule],
        now: datetime,
    ) -> int:
        """Resolve alerts whose condition no longer holds.

        Confined to types whose rule is currently enabled for that project. A
        disabled rule produces no drafts, and retiring on that basis would clear
        the queue the moment somebody switched a rule off - which is the opposite
        of what switching it off means.
        """
        retired = 0
        for row in existing:
            if row.dedupe_key in seen:
                continue
            alert_type = AlertType(_enum_value(row.alert_type))
            if self._rule_for(rules, row.project_id, alert_type) is None:
                continue
            row.status = AlertStatus.RESOLVED
            row.resolved_at = now
            row.details = {
                **dict(row.details or {}),
                "resolved_by": "evaluator",
                "resolved_reason": "The condition that raised this alert no longer holds.",
            }
            retired += 1
        return retired

    def _escalate(
        self,
        existing: Iterable[Alert],
        seen: set[str],
        rules: Mapping[tuple[uuid.UUID | None, AlertType], ResolvedRule],
        now: datetime,
    ) -> int:
        """Raise the severity of an alert nobody has dealt with.

        ``escalate_after_days`` is an *age*, so this measures from when the alert
        was raised, not from the deadline it is about. It fires once - the marker
        in ``details`` is what stops a weekly sweep walking an alert to CRITICAL
        by attrition.
        """
        escalated = 0
        for row in existing:
            if row.dedupe_key not in seen or row.status is not AlertStatus.OPEN:
                continue
            alert_type = AlertType(_enum_value(row.alert_type))
            rule = self._rule_for(rules, row.project_id, alert_type)
            if rule is None or not rule.escalate_after_days:
                continue
            details = dict(row.details or {})
            if details.get("escalated_at"):
                continue
            created = row.created_at
            if created is None:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if (now - created).days < rule.escalate_after_days:
                continue

            promoted = _promote(AlertSeverity(_enum_value(row.severity)))
            row.severity = promoted
            row.details = {**details, "escalated_at": now.isoformat()}
            escalated += 1
        return escalated


# =============================================================================
# Helpers
# =============================================================================
def contract_label(contract: Any) -> str:
    """Best available human label for a document."""
    for attribute in ("title", "contract_number", "original_file_name"):
        value = getattr(contract, attribute, None)
        if value:
            return str(value)
    identifier = getattr(contract, "id", None)
    return f"Contract {identifier}" if identifier else "An unknown contract"


def _int(value: Any, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _decimal_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _at_least(current: AlertSeverity, floor: AlertSeverity) -> AlertSeverity:
    """The more severe of the two. Rules set a floor, conditions can raise it."""
    return current if _LADDER.index(current) >= _LADDER.index(floor) else floor


def _promote(current: AlertSeverity) -> AlertSeverity:
    index = _LADDER.index(current)
    return _LADDER[min(index + 1, len(_LADDER) - 1)]


__all__ = [
    "EVALUATED_TYPES",
    "AlertDraft",
    "AlertEvaluator",
    "EvaluationOutcome",
    "ResolvedRule",
    "contract_label",
    "evaluate_auto_renewal",
    "evaluate_expiring",
    "evaluate_high_risk",
    "evaluate_missing_clause",
    "evaluate_obligation",
    "evaluate_review_required",
]

"""Risk scoring and missing-clause detection (§7.4, §13).

Deterministic and explainable. The score is not a model output: it is derived from
extracted attributes by named rules, each contributing a stated number, so a
reviewer can decompose "78, high" into the findings that produced it and argue with
any one of them. A score a user cannot decompose is not usable in a negotiation.

Two findings this module treats as first-class, because they are what the clause
list was prioritised around:

* **Effective unlimited liability.** A cap of "2x fees paid" with carve-outs for IP
  infringement, confidentiality breach and gross negligence is not a limited
  liability contract. The carve-outs are scored separately from the cap so a tidy
  cap value cannot hide them.
* **Missing mandatory clauses.** An absent limitation of liability is a bigger
  finding than a bad one, and absence has no clause text to attach to - so omission
  risks are marked ``is_omission`` and are exempt from the evidence checks that
  assume there is something to quote.

Weights come from the profile (``risk_mapping.weights``), so an insurance policy
and an NDA can weight the same risk type differently without touching this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ai.extraction.models import (
    ExtractedClause,
    ExtractedRisk,
    RiskAssessment,
    ValidationIssue,
)
from app.core.enums import (
    ClauseType,
    IndemnityPosture,
    LiabilityCapBasis,
    LiabilityCarveOut,
    PartySide,
    RiskBand,
    RiskSeverity,
    RiskType,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Carve-outs that convert a capped contract into unlimited exposure for the
#: matters they name. Payment obligations are excluded deliberately: a carve-out for
#: "amounts owed" is universal drafting and says nothing about risk appetite.
_MATERIAL_CARVE_OUTS: frozenset[str] = frozenset(
    {
        LiabilityCarveOut.IP_INFRINGEMENT.value,
        LiabilityCarveOut.CONFIDENTIALITY_BREACH.value,
        LiabilityCarveOut.GROSS_NEGLIGENCE.value,
        LiabilityCarveOut.WILFUL_MISCONDUCT.value,
        LiabilityCarveOut.DATA_PROTECTION_BREACH.value,
        LiabilityCarveOut.INDEMNIFICATION_OBLIGATIONS.value,
        LiabilityCarveOut.BREACH_OF_LAW.value,
    }
)

#: Renewal notice windows below this are a practical trap: the deadline passes
#: before anyone reviews the contract, and it renews.
_TIGHT_RENEWAL_NOTICE_DAYS = 30

#: Payment terms at or below this are aggressive for a commercial agreement.
_TIGHT_PAYMENT_DAYS = 15

#: A cap multiple at or below this leaves little headroom relative to fees.
_LOW_CAP_MULTIPLE = 1.0

#: Score contribution ceiling for one risk, so a single finding cannot saturate the
#: scale and hide everything else.
_MAX_SINGLE_CONTRIBUTION = 40


@dataclass(slots=True)
class RiskFinding:
    """An intermediate finding before it becomes an :class:`ExtractedRisk`."""

    risk_type: str
    severity: RiskSeverity
    description: str
    recommendation: str | None = None
    clause_type: str | None = None
    category: str | None = None
    is_omission: bool = False
    #: The clause the finding came from, so the risk inherits its evidence.
    source: ExtractedClause | None = None


class RiskAssessor:
    """Derives risks and the 0-100 score from extracted clauses.

    Stateless per call; construct with the profile so weighting and banding are
    configuration rather than code.
    """

    def __init__(
        self,
        *,
        mandatory_clauses: list[str] | None = None,
        risk_mapping: dict[str, Any] | None = None,
    ) -> None:
        mapping = risk_mapping or {}
        self._mandatory = [str(key) for key in (mandatory_clauses or [])]
        self._weights: dict[str, str] = dict(mapping.get("weights") or {})
        score_config = mapping.get("score") or {}
        self._low_max = int(score_config.get("low_max", 33))
        self._medium_max = int(score_config.get("medium_max", 66))

    # ------------------------------------------------------------------- public
    def assess(
        self,
        *,
        clauses: list[ExtractedClause],
        model_risks: list[ExtractedRisk] | None = None,
    ) -> RiskAssessment:
        """Produce the assessment for one contract."""
        findings: list[RiskFinding] = []
        found_types = {clause.clause_type for clause in clauses}

        findings.extend(self._missing_mandatory(found_types))
        for clause in clauses:
            findings.extend(self._from_clause(clause))

        risks = [self._to_risk(finding) for finding in findings]

        # Model-identified risks are merged, not substituted: the rules see
        # structure, the model sees drafting. Duplicates on (type, clause) are
        # dropped in favour of the deterministic finding, which carries a
        # reproducible score.
        seen = {(risk.risk_type, risk.clause_type) for risk in risks}
        for risk in model_risks or []:
            key = (risk.risk_type, risk.clause_type)
            if key in seen:
                continue
            seen.add(key)
            risk.score_contribution = self._contribution(risk.severity)
            risks.append(risk)

        score, breakdown = self._score(risks)
        band = self._band(score)

        return RiskAssessment(
            score=score,
            band=band,
            risks=risks,
            missing_mandatory=[key for key in self._mandatory if key not in found_types],
            breakdown=breakdown,
            has_unlimited_liability=self._has_unlimited_liability(clauses),
        )

    # ---------------------------------------------------------------- omissions
    def _missing_mandatory(self, found: set[str]) -> list[RiskFinding]:
        findings: list[RiskFinding] = []
        for key in self._mandatory:
            if key in found:
                continue
            severity = self._severity_for_missing(key)
            findings.append(
                RiskFinding(
                    risk_type=(
                        RiskType.MISSING_LIABILITY_CAP.value
                        if key == ClauseType.LIMITATION_OF_LIABILITY.value
                        else RiskType.MISSING_MANDATORY_CLAUSE.value
                    ),
                    severity=severity,
                    description=(
                        f"The agreement contains no {key.replace('_', ' ')} clause, "
                        "which this document type requires."
                    ),
                    recommendation=(
                        f"Confirm the omission is deliberate; otherwise negotiate a "
                        f"{key.replace('_', ' ')} clause before signature."
                    ),
                    clause_type=key,
                    category="omission",
                    is_omission=True,
                )
            )
        return findings

    def _severity_for_missing(self, clause_key: str) -> RiskSeverity:
        """Severity of an omission, from the profile's weighting where stated."""
        if clause_key == ClauseType.LIMITATION_OF_LIABILITY.value:
            configured = self._weights.get(RiskType.MISSING_LIABILITY_CAP.value)
        else:
            configured = self._weights.get(RiskType.MISSING_MANDATORY_CLAUSE.value)
        return _severity(configured, RiskSeverity.HIGH)

    # ------------------------------------------------------------ clause rules
    def _from_clause(self, clause: ExtractedClause) -> list[RiskFinding]:
        handler = {
            ClauseType.LIMITATION_OF_LIABILITY.value: self._liability_risks,
            ClauseType.INDEMNIFICATION.value: self._indemnity_risks,
            ClauseType.AUTO_RENEWAL.value: self._renewal_risks,
            ClauseType.PAYMENT_TERMS.value: self._payment_risks,
            ClauseType.TERMINATION_FOR_CONVENIENCE.value: self._termination_risks,
            ClauseType.TERMINATION_FOR_CAUSE.value: self._termination_cause_risks,
            ClauseType.INTELLECTUAL_PROPERTY.value: self._ip_risks,
            ClauseType.GOVERNING_LAW.value: self._governing_law_risks,
            ClauseType.EXCLUSIVITY.value: self._exclusivity_risks,
            ClauseType.ASSIGNMENT.value: self._assignment_risks,
            ClauseType.AUDIT_RIGHTS.value: self._audit_risks,
            ClauseType.FORCE_MAJEURE.value: self._force_majeure_risks,
            ClauseType.NON_SOLICITATION.value: self._non_solicit_risks,
        }.get(clause.clause_type)

        if handler is None:
            return []
        try:
            return handler(clause)
        except Exception as exc:  # noqa: BLE001
            # A rule bug must not lose the clause or fail the job.
            logger.warning("risk_rule_failed", clause_type=clause.clause_type, error=str(exc))
            return []

    def _liability_risks(self, clause: ExtractedClause) -> list[RiskFinding]:
        attributes = clause.attributes
        basis = attributes.get("cap_basis")
        findings: list[RiskFinding] = []

        if basis == LiabilityCapBasis.UNCAPPED.value:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.UNLIMITED_LIABILITY.value,
                    severity=_severity(
                        self._weights.get(RiskType.UNLIMITED_LIABILITY.value),
                        RiskSeverity.CRITICAL,
                    ),
                    description=(
                        "Liability is not capped. Exposure under this agreement is unlimited."
                    ),
                    recommendation=(
                        "Negotiate an aggregate cap, conventionally expressed as a "
                        "multiple of fees paid."
                    ),
                    clause_type=clause.clause_type,
                    category="liability",
                    source=clause,
                )
            )
        elif basis == LiabilityCapBasis.NOT_SPECIFIED.value:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.MISSING_LIABILITY_CAP.value,
                    severity=_severity(
                        self._weights.get(RiskType.MISSING_LIABILITY_CAP.value),
                        RiskSeverity.HIGH,
                    ),
                    description=(
                        "A limitation of liability clause is present but states no cap, "
                        "so the extent of the limitation is undetermined."
                    ),
                    recommendation="Clarify the cap basis and amount in writing.",
                    clause_type=clause.clause_type,
                    category="liability",
                    source=clause,
                )
            )

        # Carve-outs: scored independently of the cap, because this is exactly the
        # exposure a cap-only view hides.
        carve_outs = [str(item) for item in (attributes.get("carve_outs") or [])]
        material = [item for item in carve_outs if item in _MATERIAL_CARVE_OUTS]
        if material:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.UNLIMITED_LIABILITY.value,
                    severity=(RiskSeverity.CRITICAL if len(material) >= 3 else RiskSeverity.HIGH),
                    description=(
                        "The cap does not apply to "
                        + ", ".join(item.replace("_", " ") for item in material)
                        + ". Liability for those matters is unlimited despite the cap."
                    ),
                    recommendation=(
                        "Bring the carve-outs inside a super-cap, or accept them "
                        "explicitly as unlimited exposure."
                    ),
                    clause_type=clause.clause_type,
                    category="liability",
                    source=clause,
                )
            )
        elif attributes.get("has_carve_outs") and not carve_outs:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.UNLIMITED_LIABILITY.value,
                    severity=RiskSeverity.MEDIUM,
                    description=(
                        "The clause carves matters out of the cap but the carve-outs "
                        "could not be identified, so the unlimited exposure is "
                        "unquantified."
                    ),
                    recommendation="Review the clause text and record the carve-outs.",
                    clause_type=clause.clause_type,
                    category="liability",
                    source=clause,
                )
            )

        multiple = attributes.get("cap_multiple")
        if (
            isinstance(multiple, (int, float))
            and multiple <= _LOW_CAP_MULTIPLE
            and basis != LiabilityCapBasis.UNCAPPED.value
        ):
            findings.append(
                RiskFinding(
                    risk_type=RiskType.OTHER.value,
                    severity=RiskSeverity.LOW,
                    description=(
                        f"The cap is {multiple:g}x fees paid, which leaves little "
                        "headroom relative to the value of the engagement."
                    ),
                    recommendation="Consider whether the cap covers realistic loss.",
                    clause_type=clause.clause_type,
                    category="liability",
                    source=clause,
                )
            )

        if attributes.get("is_mutual") is False:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.OTHER.value,
                    severity=RiskSeverity.MEDIUM,
                    description=(
                        "The limitation of liability is not mutual: it protects one party only."
                    ),
                    recommendation="Seek reciprocity in the limitation.",
                    clause_type=clause.clause_type,
                    category="liability",
                    source=clause,
                )
            )
        return findings

    def _indemnity_risks(self, clause: ExtractedClause) -> list[RiskFinding]:
        attributes = clause.attributes
        findings: list[RiskFinding] = []
        posture = attributes.get("posture")
        capped = attributes.get("is_capped")

        if capped is False:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.UNCAPPED_INDEMNITY.value,
                    severity=_severity(
                        self._weights.get(RiskType.UNCAPPED_INDEMNITY.value),
                        RiskSeverity.CRITICAL,
                    ),
                    description=(
                        "The indemnity is not subject to any cap, so it carries "
                        "unlimited exposure independently of the liability clause."
                    ),
                    recommendation=(
                        "Bring the indemnity within the liability cap or give it its own cap."
                    ),
                    clause_type=clause.clause_type,
                    category="indemnity",
                    source=clause,
                )
            )

        if posture == IndemnityPosture.ONE_SIDED_AGAINST_US.value:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.OTHER.value,
                    severity=RiskSeverity.HIGH,
                    description=(
                        "The indemnity runs one way only, against us: we indemnify the "
                        "counterparty without a reciprocal obligation."
                    ),
                    recommendation="Seek a mutual indemnity, or narrow the covered claims.",
                    clause_type=clause.clause_type,
                    category="indemnity",
                    source=clause,
                )
            )
        return findings

    def _renewal_risks(self, clause: ExtractedClause) -> list[RiskFinding]:
        attributes = clause.attributes
        findings: list[RiskFinding] = []
        if attributes.get("auto_renews"):
            notice = attributes.get("renewal_notice_days")
            tight = isinstance(notice, (int, float)) and notice < _TIGHT_RENEWAL_NOTICE_DAYS
            findings.append(
                RiskFinding(
                    risk_type=RiskType.AUTO_RENEWAL.value,
                    severity=_severity(
                        self._weights.get(RiskType.AUTO_RENEWAL.value),
                        RiskSeverity.HIGH if tight else RiskSeverity.MEDIUM,
                    ),
                    description=(
                        "The agreement renews automatically"
                        + (
                            f", and notice to prevent renewal must be given "
                            f"{notice:g} days in advance - a window short enough to "
                            "pass unnoticed."
                            if tight
                            else (
                                f", with {notice:g} days' notice required to prevent it."
                                if isinstance(notice, (int, float))
                                else ", and the notice period could not be determined."
                            )
                        )
                    ),
                    recommendation=(
                        "Diarise the notice deadline; an alert should be configured well before it."
                    ),
                    clause_type=clause.clause_type,
                    category="term",
                    source=clause,
                )
            )
        return findings

    def _payment_risks(self, clause: ExtractedClause) -> list[RiskFinding]:
        attributes = clause.attributes
        findings: list[RiskFinding] = []
        days = attributes.get("payment_days")
        if isinstance(days, (int, float)) and 0 < days <= _TIGHT_PAYMENT_DAYS:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.SHORT_PAYMENT_TERMS.value,
                    severity=_severity(
                        self._weights.get(RiskType.SHORT_PAYMENT_TERMS.value),
                        RiskSeverity.MEDIUM,
                    ),
                    description=(
                        f"Payment falls due within {days:g} days, which is short for a "
                        "commercial agreement and tight against normal invoicing cycles."
                    ),
                    recommendation="Confirm the term is operationally achievable.",
                    clause_type=clause.clause_type,
                    category="financial",
                    source=clause,
                )
            )
        late_fee = attributes.get("late_fee_percent")
        if isinstance(late_fee, (int, float)) and late_fee > 0:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.LATE_PAYMENT_PENALTY.value,
                    severity=_severity(
                        self._weights.get(RiskType.LATE_PAYMENT_PENALTY.value),
                        RiskSeverity.LOW,
                    ),
                    description=f"Late payment attracts a charge of {late_fee:g}%.",
                    recommendation="Ensure invoice approval cycles fit the payment term.",
                    clause_type=clause.clause_type,
                    category="financial",
                    source=clause,
                )
            )
        return findings

    def _termination_risks(self, clause: ExtractedClause) -> list[RiskFinding]:
        """Termination for convenience - who holds the right, and on what notice."""
        attributes = clause.attributes
        findings: list[RiskFinding] = []
        terminating = attributes.get("terminating_side")
        # The user's requirement: whether *we* can terminate is a first-class answer,
        # so it is read from its own attribute and not inferred from the side field.
        can_we = attributes.get("can_we_terminate")

        one_sided_against_us = terminating == PartySide.COUNTERPARTY.value or (
            can_we == PartySide.NEITHER.value
        )
        if attributes.get("is_permitted") is not False and one_sided_against_us:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.UNILATERAL_TERMINATION.value,
                    severity=_severity(
                        self._weights.get(RiskType.UNILATERAL_TERMINATION.value),
                        RiskSeverity.HIGH,
                    ),
                    description=(
                        "Only the counterparty may terminate for convenience; we are "
                        "bound for the full term."
                    ),
                    recommendation="Seek a reciprocal right to terminate for convenience.",
                    clause_type=clause.clause_type,
                    category="term",
                    source=clause,
                )
            )

        notice = attributes.get("notice_days")
        if (
            isinstance(notice, (int, float))
            and notice <= _TIGHT_RENEWAL_NOTICE_DAYS
            and terminating in {PartySide.COUNTERPARTY.value, PartySide.BOTH.value}
        ):
            findings.append(
                RiskFinding(
                    risk_type=RiskType.BROAD_TERMINATION_RIGHTS.value,
                    severity=_severity(
                        self._weights.get(RiskType.BROAD_TERMINATION_RIGHTS.value),
                        RiskSeverity.MEDIUM,
                    ),
                    description=(
                        f"The agreement can be terminated for convenience on {notice:g} "
                        "days' notice, giving little certainty of revenue or supply."
                    ),
                    recommendation="Negotiate a longer notice period or a minimum term.",
                    clause_type=clause.clause_type,
                    category="term",
                    source=clause,
                )
            )
        return findings

    def _termination_cause_risks(self, clause: ExtractedClause) -> list[RiskFinding]:
        """Termination for cause - whether a breach can realistically be cured."""
        attributes = clause.attributes
        findings: list[RiskFinding] = []

        if attributes.get("is_curable") is False:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.BROAD_TERMINATION_RIGHTS.value,
                    severity=RiskSeverity.HIGH,
                    description=(
                        "Breach is not curable: the agreement can be terminated for "
                        "cause with no opportunity to remedy."
                    ),
                    recommendation="Negotiate a cure period for remediable breaches.",
                    clause_type=clause.clause_type,
                    category="term",
                    source=clause,
                )
            )

        cure = attributes.get("cure_period_days")
        if isinstance(cure, (int, float)) and 0 < cure < 15:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.BROAD_TERMINATION_RIGHTS.value,
                    severity=RiskSeverity.MEDIUM,
                    description=(
                        f"The cure period is only {cure:g} days, which may be too short "
                        "to remedy a substantive breach."
                    ),
                    recommendation="Seek a longer cure period, typically 30 days.",
                    clause_type=clause.clause_type,
                    category="term",
                    source=clause,
                )
            )
        return findings

    def _ip_risks(self, clause: ExtractedClause) -> list[RiskFinding]:
        attributes = clause.attributes
        findings: list[RiskFinding] = []

        if attributes.get("we_retain_pre_existing_ip") in {
            PartySide.COUNTERPARTY.value,
            PartySide.NEITHER.value,
        }:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.IP_ASSIGNMENT_RISK.value,
                    severity=_severity(
                        self._weights.get(RiskType.IP_ASSIGNMENT_RISK.value),
                        RiskSeverity.CRITICAL,
                    ),
                    description=(
                        "The agreement does not preserve our rights in pre-existing "
                        "intellectual property, so background IP may transfer."
                    ),
                    recommendation=(
                        "Add an express carve-out retaining all pre-existing and "
                        "independently developed IP."
                    ),
                    clause_type=clause.clause_type,
                    category="ip",
                    source=clause,
                )
            )

        # The side field, not the free-text owner name: the name is whatever the
        # contract calls the party, the side is the resolved answer.
        owner = attributes.get("work_product_owner_side")
        if owner == PartySide.COUNTERPARTY.value:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.IP_ASSIGNMENT_RISK.value,
                    severity=RiskSeverity.HIGH,
                    description=(
                        "Work product created under this agreement is owned by the counterparty."
                    ),
                    recommendation=(
                        "Confirm this is the commercial intent, and secure a licence "
                        "back for anything we need to reuse."
                    ),
                    clause_type=clause.clause_type,
                    category="ip",
                    source=clause,
                )
            )
        return findings

    def _governing_law_risks(self, clause: ExtractedClause) -> list[RiskFinding]:
        attributes = clause.attributes
        if not attributes.get("governing_law") and not attributes.get("country"):
            return [
                RiskFinding(
                    risk_type=RiskType.UNFAVOURABLE_GOVERNING_LAW.value,
                    severity=RiskSeverity.MEDIUM,
                    description=(
                        "A governing law clause is present but does not name a "
                        "jurisdiction, leaving the applicable law unsettled."
                    ),
                    recommendation="Specify the governing law expressly.",
                    clause_type=clause.clause_type,
                    category="legal",
                    source=clause,
                )
            ]
        return []

    def _exclusivity_risks(self, clause: ExtractedClause) -> list[RiskFinding]:
        attributes = clause.attributes
        bound = attributes.get("bound_side")
        if bound in {PartySide.OUR_ORGANISATION.value, PartySide.BOTH.value}:
            return [
                RiskFinding(
                    risk_type=RiskType.EXCLUSIVITY.value,
                    severity=_severity(
                        self._weights.get(RiskType.EXCLUSIVITY.value), RiskSeverity.HIGH
                    ),
                    description=(
                        "We are bound by an exclusivity obligation, restricting who we "
                        "may deal with for its scope and duration."
                    ),
                    recommendation=(
                        "Confirm the scope and term are commercially justified and narrowly drawn."
                    ),
                    clause_type=clause.clause_type,
                    category="commercial",
                    source=clause,
                )
            ]
        return []

    def _assignment_risks(self, clause: ExtractedClause) -> list[RiskFinding]:
        attributes = clause.attributes
        if attributes.get("consent_required_on_change_of_control") or attributes.get(
            "change_of_control_permits_termination"
        ):
            return [
                RiskFinding(
                    risk_type=RiskType.CHANGE_OF_CONTROL.value,
                    severity=_severity(
                        self._weights.get(RiskType.CHANGE_OF_CONTROL.value),
                        RiskSeverity.MEDIUM,
                    ),
                    description=(
                        "A change of control affects this agreement, so a corporate "
                        "transaction could require consent or trigger termination."
                    ),
                    recommendation="Flag to corporate development ahead of any transaction.",
                    clause_type=clause.clause_type,
                    category="legal",
                    source=clause,
                )
            ]
        return []

    def _audit_risks(self, clause: ExtractedClause) -> list[RiskFinding]:
        attributes = clause.attributes
        findings: list[RiskFinding] = []

        if attributes.get("has_audit_rights") is False:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.NO_AUDIT_RIGHTS.value,
                    severity=_severity(
                        self._weights.get(RiskType.NO_AUDIT_RIGHTS.value), RiskSeverity.LOW
                    ),
                    description=(
                        "An audit rights clause is present but grants no right of audit, "
                        "so compliance cannot be verified."
                    ),
                    recommendation="Negotiate a right to audit against the obligations owed.",
                    clause_type=clause.clause_type,
                    category="compliance",
                    source=clause,
                )
            )

        notice = attributes.get("notice_days")
        if isinstance(notice, (int, float)) and notice <= 0:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.OTHER.value,
                    severity=RiskSeverity.MEDIUM,
                    description=(
                        "Audits may be conducted without advance notice, which is "
                        "operationally disruptive."
                    ),
                    recommendation="Negotiate a reasonable notice period for audits.",
                    clause_type=clause.clause_type,
                    category="compliance",
                    source=clause,
                )
            )
        return findings

    def _force_majeure_risks(self, clause: ExtractedClause) -> list[RiskFinding]:
        attributes = clause.attributes
        findings: list[RiskFinding] = []
        if attributes.get("excuses_payment") is True:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.OTHER.value,
                    severity=RiskSeverity.HIGH,
                    description=(
                        "Force majeure excuses payment obligations, so revenue can be "
                        "suspended by an event outside either party's control."
                    ),
                    recommendation="Carve payment obligations out of force majeure.",
                    clause_type=clause.clause_type,
                    category="financial",
                    source=clause,
                )
            )
        if attributes.get("covers_pandemic") is False:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.OTHER.value,
                    severity=RiskSeverity.LOW,
                    description=(
                        "The force majeure clause does not cover epidemic or pandemic events."
                    ),
                    recommendation="Consider adding express pandemic coverage.",
                    clause_type=clause.clause_type,
                    category="operational",
                    source=clause,
                )
            )
        return findings

    def _non_solicit_risks(self, clause: ExtractedClause) -> list[RiskFinding]:
        attributes = clause.attributes
        months = attributes.get("duration_months")
        bound = attributes.get("bound_side")
        if (
            isinstance(months, (int, float))
            and months > 24
            and bound != PartySide.COUNTERPARTY.value
        ):
            return [
                RiskFinding(
                    risk_type=RiskType.NON_COMPETE_BREADTH.value,
                    severity=_severity(
                        self._weights.get(RiskType.NON_COMPETE_BREADTH.value),
                        RiskSeverity.MEDIUM,
                    ),
                    description=(
                        f"The non-solicitation obligation runs for {months:g} months, "
                        "which is long and may be unenforceable in some jurisdictions."
                    ),
                    recommendation="Consider shortening the restricted period.",
                    clause_type=clause.clause_type,
                    category="commercial",
                    source=clause,
                )
            ]
        return []

    # ------------------------------------------------------------------ scoring
    def _to_risk(self, finding: RiskFinding) -> ExtractedRisk:
        risk = ExtractedRisk(
            risk_type=finding.risk_type,
            severity=finding.severity,
            description=finding.description,
            recommendation=finding.recommendation,
            category=finding.category,
            is_omission=finding.is_omission,
            clause_type=finding.clause_type,
            score_contribution=self._contribution(finding.severity),
            # An omission is asserted by this engine from the mandatory-clause list,
            # so its confidence is the engine's, not a model's.
            confidence=1.0 if finding.is_omission else 0.95,
        )
        if finding.source is not None:
            risk.evidence = list(finding.source.evidence)
            risk.chunk_id = finding.source.chunk_id
            risk.prompt_id = finding.source.prompt_id
            risk.prompt_version = finding.source.prompt_version
            risk.model_version = finding.source.model_version
        elif not finding.is_omission:
            # A non-omission risk with no source clause has nothing to point at.
            risk.issues.append(
                ValidationIssue(
                    code="risk_without_evidence",
                    message="This risk has no clause evidence attached.",
                    severity="warning",
                )
            )
        return risk

    def _contribution(self, severity: RiskSeverity) -> int:
        return min(severity.weight, _MAX_SINGLE_CONTRIBUTION)

    def _score(self, risks: list[ExtractedRisk]) -> tuple[int, list[dict[str, Any]]]:
        """Combine contributions into 0-100.

        Saturating rather than additive: ten medium findings must not sum past a
        single critical one, and the scale has to stay interpretable at the top end.
        Each contribution consumes a proportion of the *remaining* headroom, so
        every finding still moves the score and none can be ignored.

        A **severity floor** then applies. Saturation alone caps a single critical
        finding at its weight, which would put a contract with genuinely unlimited
        liability in the medium band - a number that reads as "look at this later"
        for the one finding that should stop a signature. So one critical finding
        floors the score into the high band, and one high finding floors it into
        medium. The floor is derived from the profile's own band thresholds and is
        recorded in the breakdown when it binds, so the score stays explainable.
        """
        remaining = 100.0
        breakdown: list[dict[str, Any]] = []

        # Highest severity first, so the score is dominated by the worst findings and
        # is independent of the order extraction happened to produce them in.
        ordered = sorted(
            risks,
            key=lambda risk: (-risk.score_contribution, risk.risk_type, risk.description),
        )

        for risk in ordered:
            applied = remaining * (risk.score_contribution / 100.0)
            remaining -= applied
            breakdown.append(
                {
                    "risk_type": risk.risk_type,
                    "severity": risk.severity.value,
                    "clause_type": risk.clause_type,
                    "weight": risk.score_contribution,
                    "applied": round(applied, 2),
                    "is_omission": risk.is_omission,
                    "description": risk.description[:200],
                }
            )

        score = min(max(round(100.0 - remaining), 0), 100)

        floor, reason = self._severity_floor(ordered)
        if floor > score:
            breakdown.append(
                {
                    "risk_type": "severity_floor",
                    "severity": reason,
                    "clause_type": None,
                    "weight": 0,
                    "applied": round(float(floor - score), 2),
                    "is_omission": False,
                    "description": (
                        f"Raised from {score} to {floor}: a {reason} finding places this "
                        f"contract in the {self._band(floor).value} band on its own."
                    ),
                }
            )
            score = floor

        return score, breakdown

    def _severity_floor(self, risks: list[ExtractedRisk]) -> tuple[int, str]:
        """The minimum score the worst single finding justifies."""
        severities = {risk.severity for risk in risks}
        if RiskSeverity.CRITICAL in severities:
            return self._medium_max + 1, RiskSeverity.CRITICAL.value
        if RiskSeverity.HIGH in severities:
            return self._low_max + 1, RiskSeverity.HIGH.value
        return 0, ""

    def _band(self, score: int) -> RiskBand:
        """Band the score using the profile's thresholds."""
        if score > self._medium_max:
            return RiskBand.HIGH
        if score > self._low_max:
            return RiskBand.MEDIUM
        return RiskBand.LOW

    def _has_unlimited_liability(self, clauses: list[ExtractedClause]) -> bool:
        """True when exposure is unlimited, whether by cap or by carve-out.

        The flag the repository filters on, so it must reflect *effective* exposure:
        a 2x cap with an IP infringement carve-out is unlimited liability for IP
        infringement, and answering "no" to that question would be misleading.
        """
        for clause in clauses:
            if clause.clause_type != ClauseType.LIMITATION_OF_LIABILITY.value:
                continue
            attributes = clause.attributes
            if attributes.get("cap_basis") == LiabilityCapBasis.UNCAPPED.value:
                return True
            carve_outs = {str(item) for item in (attributes.get("carve_outs") or [])}
            if carve_outs & _MATERIAL_CARVE_OUTS:
                return True
        return False


def _severity(value: Any, default: RiskSeverity) -> RiskSeverity:
    """Coerce a configured severity, falling back rather than raising.

    Weights are administrator-editable data; a typo in a profile must degrade to the
    default, not fail every extraction that touches that risk type.
    """
    if isinstance(value, RiskSeverity):
        return value
    if isinstance(value, str):
        try:
            return RiskSeverity(value)
        except ValueError:
            logger.warning("unknown_risk_severity", value=value)
    return default


__all__ = ["RiskAssessor", "RiskFinding"]

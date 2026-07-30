"""Clause Master seed definitions, in business-priority order.

This is the authoritative clause list. Order is meaningful:

* ``priority`` (1 = highest) drives **extraction order**, so a job that partially
  fails still produced the terms that matter most, and it is the default sort of
  every clause list in the UI.
* ``output_schema`` is the per-clause attribute contract. The extraction
  validator enforces it, so ``clauses.attributes`` is queryable structured data -
  "every contract with an uncapped liability cap" is an indexed JSONB lookup, not
  a text search.
* ``ui_config`` decides how the frontend surfaces the clause. Limitation of
  Liability gets ``placement: dedicated_tab``; everything else lists. Promoting
  another clause to its own tab is an admin edit, not a code change.

Party-side attributes (``can_we_terminate``, ``we_retain_pre_existing_ip``) use
:class:`~app.core.enums.PartySide`, resolved by matching extracted party names
against ``ORGANIZATION_LEGAL_NAMES``. No company name is hardcoded here.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from app.core.enums import (
    ClauseType,
    IndemnityPosture,
    LiabilityCapBasis,
    LiabilityCarveOut,
    LicenceExclusivity,
    PartySide,
    RiskSeverity,
)


def _enum_values(enum_cls: type[Enum]) -> list[str]:
    return [member.value for member in enum_cls]


def _string(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": ["string", "null"], "description": description, **extra}


def _integer(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": ["integer", "null"], "description": description, **extra}


def _number(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": ["number", "null"], "description": description, **extra}


def _boolean(description: str) -> dict[str, Any]:
    return {"type": ["boolean", "null"], "description": description}


def _enum(enum_cls: type[Enum], description: str) -> dict[str, Any]:
    return {
        "type": ["string", "null"],
        "enum": [*_enum_values(enum_cls), None],
        "description": description,
    }


def _enum_array(enum_cls: type[Enum], description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string", "enum": _enum_values(enum_cls)},
        "description": description,
    }


def _party_side(description: str) -> dict[str, Any]:
    return _enum(PartySide, description)


def _schema(**properties: dict[str, Any]) -> dict[str, Any]:
    """A clause attribute schema.

    ``additionalProperties: false`` is deliberate: the model must return the
    agreed attribute names or fail validation, rather than inventing keys that
    silently never reach a filter.

    Every property is **required and nullable** rather than omissible. The values
    above are already nullable unions, so a required null is how the model says
    "the contract does not state this" - which is a real and common answer, and one
    that has to be distinguishable from having missed the attribute. It also keeps
    ``clauses.attributes`` uniform, so a JSONB filter such as "every contract whose
    cap has no stated multiple" is an index lookup rather than a key-existence
    test that silently excludes rows.
    """
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _rule(
    *,
    headings: list[str],
    keywords: list[str],
    must_not: list[str] | None = None,
    min_tokens: int = 15,
) -> dict[str, Any]:
    """Deterministic pre-filter applied before the LLM sees a chunk.

    Cheap heading/keyword gating decides which chunks are worth an LLM call. This
    is where most of the extraction cost saving on a 150-page contract comes from.
    """
    return {
        "heading_patterns": headings,
        "keywords": keywords,
        "must_not_contain": must_not or [],
        "min_tokens": min_tokens,
        "search_scope": "clause",
    }


class ClauseSeed:
    """One Clause Master category plus its baseline extraction rule."""

    __slots__ = (
        "confidence_threshold",
        "extraction_rule",
        "group_name",
        "key",
        "mandatory",
        "missing_severity",
        "name",
        "notes",
        "output_schema",
        "priority",
        "synonyms",
        "ui_config",
    )

    def __init__(
        self,
        *,
        priority: int,
        key: str,
        name: str,
        group_name: str,
        mandatory: bool,
        missing_severity: RiskSeverity | None,
        extraction_rule: dict[str, Any],
        synonyms: list[str],
        output_schema: dict[str, Any],
        confidence_threshold: float = 0.85,
        ui_config: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> None:
        self.priority = priority
        self.key = key
        self.name = name
        self.group_name = group_name
        self.mandatory = mandatory
        self.missing_severity = missing_severity
        self.extraction_rule = extraction_rule
        self.synonyms = synonyms
        self.output_schema = output_schema
        self.confidence_threshold = confidence_threshold
        self.ui_config = ui_config or {"placement": "list"}
        self.notes = notes


# =============================================================================
# The priority list
# =============================================================================
CLAUSE_SEEDS: tuple[ClauseSeed, ...] = (
    # ---------------------------------------------------------------- 1
    ClauseSeed(
        priority=1,
        key=ClauseType.LIMITATION_OF_LIABILITY,
        name="Limitation of Liability",
        group_name="Risk",
        mandatory=True,
        missing_severity=RiskSeverity.CRITICAL,
        confidence_threshold=0.90,
        # Highest-value commercial term in the repository, so it gets its own tab
        # with the cap expressed as a first-class enumerated value.
        ui_config={
            "placement": "dedicated_tab",
            "tab_label": "Limitation of Liability",
            "primary_fields": ["cap_basis", "cap_multiple", "cap_amount", "has_carve_outs"],
            "highlight_when": {"cap_basis": "uncapped", "has_carve_outs": True},
            "cap_dropdown": {
                "field": "cap_basis",
                "options": _enum_values(LiabilityCapBasis),
            },
        },
        extraction_rule=_rule(
            headings=[
                "limitation of liability",
                "limitations of liability",
                "liability",
                "limitation on damages",
                "exclusion of liability",
            ],
            keywords=[
                "in no event shall",
                "aggregate liability",
                "shall not exceed",
                "total liability",
                "consequential damages",
                "indirect damages",
                "fees paid",
                "nothing in this agreement shall limit",
            ],
        ),
        synonyms=[
            "Liability Cap",
            "Limitation on Damages",
            "Exclusion of Liability",
            "Limitation of Remedies",
        ],
        output_schema=_schema(
            cap_basis=_enum(
                LiabilityCapBasis,
                "How the cap is expressed. Use 1x_fees_paid / 2x_fees_paid when the "
                "cap is that multiple of fees paid; uncapped when liability is not "
                "limited; not_specified when the contract is silent.",
            ),
            cap_multiple=_number(
                "The numeric multiple when the cap is a multiple of fees "
                "(1 for 1x, 2 for 2x, 0.5 for half). Null otherwise."
            ),
            cap_amount=_number("Absolute cap amount when cap_basis is fixed_amount."),
            cap_currency=_string("ISO currency code for cap_amount."),
            cap_reference_period_months=_integer(
                "Look-back window for 'fees paid in the preceding N months'."
            ),
            cap_applies_to=_party_side("Which side the cap protects."),
            # A capped contract with broad carve-outs is still unlimited exposure;
            # tracked separately so that cannot hide behind a tidy cap value.
            has_carve_outs=_boolean(
                "True when any liability is excluded from the cap, i.e. unlimited "
                "for those matters."
            ),
            carve_outs=_enum_array(
                LiabilityCarveOut,
                "Matters excluded from the cap and therefore subject to unlimited liability.",
            ),
            carve_out_text=_string("Verbatim carve-out wording."),
            excludes_consequential_damages=_boolean(
                "True when indirect/consequential/special damages are excluded."
            ),
            excludes_lost_profits=_boolean("True when loss of profit is excluded."),
            is_mutual=_boolean("True when the limitation applies to both parties equally."),
        ),
        notes="Cap value and carve-outs are tracked independently; carve-outs can make a capped contract effectively uncapped.",
    ),
    # ---------------------------------------------------------------- 2
    ClauseSeed(
        priority=2,
        key=ClauseType.INDEMNIFICATION,
        name="Indemnification",
        group_name="Risk",
        mandatory=True,
        missing_severity=RiskSeverity.CRITICAL,
        confidence_threshold=0.88,
        ui_config={
            "placement": "dedicated_tab",
            "tab_label": "Indemnification",
            "primary_fields": ["posture", "indemnifying_party", "is_capped"],
            "highlight_when": {"is_capped": False, "posture": "one_sided_against_us"},
        },
        extraction_rule=_rule(
            headings=["indemnification", "indemnity", "indemnities", "hold harmless"],
            keywords=[
                "shall indemnify",
                "defend and hold harmless",
                "indemnified party",
                "indemnifying party",
                "at its own expense defend",
            ],
        ),
        synonyms=["Indemnity", "Hold Harmless", "Defence Obligations", "Indemnities"],
        output_schema=_schema(
            posture=_enum(IndemnityPosture, "Direction and symmetry of the indemnity."),
            indemnifying_party=_string("Name of the party giving the indemnity."),
            indemnified_party=_string("Name of the party receiving the indemnity."),
            indemnifying_side=_party_side("Which side gives the indemnity."),
            is_mutual=_boolean("True when each party indemnifies the other."),
            is_capped=_boolean(
                "True when the indemnity is subject to the liability cap or its own cap."
            ),
            cap_amount=_number("Indemnity-specific cap amount, if any."),
            cap_reference=_string(
                "How the indemnity cap is expressed, e.g. 'subject to Section 11 cap'."
            ),
            covered_claims={
                "type": "array",
                "items": {"type": "string"},
                "description": "Claim types covered, e.g. IP infringement, third-party bodily injury.",
            },
            has_defence_obligation=_boolean(
                "True when the indemnitor must defend, not just reimburse."
            ),
            requires_prompt_notice=_boolean("True when notice is a condition of the indemnity."),
        ),
    ),
    # ---------------------------------------------------------------- 3
    ClauseSeed(
        priority=3,
        key=ClauseType.INTELLECTUAL_PROPERTY,
        name="IP Ownership",
        group_name="Legal",
        mandatory=True,
        missing_severity=RiskSeverity.HIGH,
        confidence_threshold=0.88,
        ui_config={
            "placement": "dedicated_tab",
            "tab_label": "IP Ownership",
            "primary_fields": [
                "work_product_owner_side",
                "we_retain_pre_existing_ip",
                "assignment_required",
            ],
            "highlight_when": {"we_retain_pre_existing_ip": False},
        },
        extraction_rule=_rule(
            headings=[
                "intellectual property",
                "ownership",
                "ownership of deliverables",
                "work product",
                "ip rights",
                "proprietary rights",
            ],
            keywords=[
                "intellectual property rights",
                "shall own",
                "work made for hire",
                "hereby assigns",
                "background ip",
                "pre-existing",
                "deliverables",
                "retains all right, title",
            ],
        ),
        synonyms=[
            "IP Rights",
            "Ownership of Deliverables",
            "Proprietary Rights",
            "Ownership of Work Product",
            "Background IP",
        ],
        output_schema=_schema(
            work_product_owner=_string("Named party that owns work product/deliverables."),
            work_product_owner_side=_party_side(
                "Which side owns work product created under the agreement."
            ),
            assignment_required=_boolean(
                "True when one party must assign IP it creates to the other."
            ),
            assigning_side=_party_side("Which side must assign IP it creates."),
            # The question that matters commercially: do we keep what we walked in with?
            we_retain_pre_existing_ip=_boolean(
                "True when our organisation expressly retains ownership of its "
                "pre-existing / background IP."
            ),
            pre_existing_ip_treatment=_string(
                "How pre-existing/background IP is handled, verbatim if concise."
            ),
            licence_back_granted=_boolean(
                "True when the assigning party receives a licence back to use the IP."
            ),
            licence_back_scope=_string("Scope of any licence-back."),
            includes_moral_rights_waiver=_boolean("True when moral rights are waived."),
            third_party_ip_restrictions=_string(
                "Restrictions on embedding third-party or open-source IP."
            ),
        ),
    ),
    # ---------------------------------------------------------------- 4
    ClauseSeed(
        priority=4,
        key=ClauseType.PAYMENT_TERMS,
        name="Payment Terms",
        group_name="Commercial",
        mandatory=True,
        missing_severity=RiskSeverity.HIGH,
        ui_config={
            "placement": "dedicated_tab",
            "tab_label": "Payment",
            "primary_fields": ["amount", "currency", "payment_days", "payment_trigger"],
        },
        extraction_rule=_rule(
            headings=[
                "payment",
                "payment terms",
                "fees",
                "fees and payment",
                "invoicing",
                "compensation",
                "consideration",
                "charges",
            ],
            keywords=[
                "net 30",
                "net 60",
                "days of invoice",
                "shall pay",
                "payable within",
                "invoice date",
                "due and payable",
                "late payment",
            ],
        ),
        synonyms=["Fees and Payment", "Invoicing", "Compensation", "Consideration", "Charges"],
        output_schema=_schema(
            amount=_number("Contract or fee amount stated in this clause."),
            currency=_string("ISO 4217 currency code, e.g. USD, EUR, INR."),
            amount_description=_string(
                "How the amount is expressed when not a single figure (rate card, hourly rate)."
            ),
            payment_days=_integer(
                "Number of days within which payment must be made, e.g. 30 for 'Net 30'."
            ),
            # 'within 30 days of invoice' and 'within 30 days of receipt' are
            # materially different deadlines; both the count and the trigger matter.
            payment_days_basis=_string(
                "What the day count runs from: invoice_date, invoice_receipt, "
                "delivery, month_end, milestone."
            ),
            payment_trigger=_string("Event that makes payment due."),
            due_date=_string("Absolute due date if one is stated (ISO 8601)."),
            payment_frequency=_string("monthly, quarterly, annually, milestone, one_time."),
            paying_side=_party_side("Which side pays."),
            late_fee_percent=_number("Interest or late fee percentage."),
            late_fee_basis=_string("Period the late fee applies over, e.g. per month, per annum."),
            invoicing_requirements=_string("Purchase order or invoice-format preconditions."),
            includes_taxes=_boolean("True when the amount is stated inclusive of taxes."),
            right_to_withhold=_boolean("True when disputed amounts may be withheld."),
        ),
    ),
    # ---------------------------------------------------------------- 5
    ClauseSeed(
        priority=5,
        key=ClauseType.TERM,
        name="Term / Duration",
        group_name="Commercial",
        mandatory=True,
        missing_severity=RiskSeverity.HIGH,
        ui_config={
            "placement": "summary_card",
            "primary_fields": ["start_date", "end_date", "term_months"],
        },
        extraction_rule=_rule(
            headings=["term", "duration", "term of agreement", "period", "commencement"],
            keywords=[
                "term of this agreement",
                "shall commence",
                "shall continue",
                "initial term",
                "effective date",
                "expire on",
            ],
        ),
        synonyms=["Duration", "Contract Period", "Term of Agreement", "Initial Term"],
        output_schema=_schema(
            start_date=_string("Start / commencement date (ISO 8601)."),
            end_date=_string("End / expiry date (ISO 8601)."),
            term_months=_integer("Total length of the agreement in months."),
            term_description=_string(
                "Verbatim term wording when the dates are relative or conditional."
            ),
            is_perpetual=_boolean("True when the agreement has no fixed end date."),
            is_evergreen=_boolean("True when the term continues until terminated."),
            commencement_trigger=_string("Event that starts the term, if not a fixed date."),
        ),
    ),
    # ---------------------------------------------------------------- 6
    ClauseSeed(
        priority=6,
        key=ClauseType.AUTO_RENEWAL,
        name="Renewal",
        group_name="Commercial",
        mandatory=False,
        missing_severity=RiskSeverity.MEDIUM,
        ui_config={
            "placement": "summary_card",
            "primary_fields": ["auto_renews", "renewal_notice_days", "renewal_notice_deadline"],
            "highlight_when": {"auto_renews": True},
        },
        extraction_rule=_rule(
            headings=["renewal", "automatic renewal", "extension", "term and renewal"],
            keywords=[
                "automatically renew",
                "successive",
                "unless either party",
                "evergreen",
                "renewal term",
                "extend for",
                "notice of non-renewal",
            ],
        ),
        synonyms=["Evergreen Clause", "Automatic Extension", "Renewal Term", "Non-Renewal"],
        output_schema=_schema(
            auto_renews=_boolean("True when the agreement renews automatically."),
            renewal_term_months=_integer("Length of each renewal term in months."),
            # The date that actually needs an alert: miss it and the contract rolls.
            renewal_notice_days=_integer(
                "Days before expiry by which non-renewal notice must be given."
            ),
            renewal_notice_deadline=_string(
                "Absolute deadline for non-renewal notice if stated (ISO 8601)."
            ),
            renewal_notice_side=_party_side("Which side must give non-renewal notice."),
            max_renewals=_integer("Maximum number of renewal terms, if capped."),
            price_increase_on_renewal=_boolean("True when renewal permits a price increase."),
            price_increase_cap_percent=_number("Cap on any renewal price increase."),
            requires_mutual_agreement=_boolean(
                "True when renewal requires both parties to agree (i.e. not automatic)."
            ),
        ),
    ),
    # ---------------------------------------------------------------- 7
    ClauseSeed(
        priority=7,
        key=ClauseType.GOVERNING_LAW,
        name="Governing Law",
        group_name="Legal",
        mandatory=True,
        missing_severity=RiskSeverity.MEDIUM,
        confidence_threshold=0.90,
        ui_config={"placement": "summary_card", "primary_fields": ["country", "governing_law"]},
        extraction_rule=_rule(
            headings=[
                "governing law",
                "applicable law",
                "choice of law",
                "governing law and jurisdiction",
            ],
            keywords=[
                "governed by the laws of",
                "construed in accordance with",
                "laws of the state of",
                "without regard to conflict",
            ],
        ),
        synonyms=["Applicable Law", "Choice of Law", "Governing Law and Jurisdiction"],
        output_schema=_schema(
            governing_law=_string(
                "Full governing law as stated, e.g. 'laws of England and Wales'."
            ),
            country=_string("Country whose law governs."),
            state_or_province=_string("State/province when the law is sub-national."),
            jurisdiction=_string("Courts having jurisdiction."),
            venue=_string("Agreed venue for proceedings."),
            excludes_conflict_of_laws=_boolean("True when conflict-of-laws rules are excluded."),
            excludes_cisg=_boolean("True when the UN CISG is expressly excluded."),
        ),
    ),
    # ---------------------------------------------------------------- 8
    ClauseSeed(
        priority=8,
        key=ClauseType.DISPUTE_RESOLUTION,
        name="Dispute Resolution",
        group_name="Legal",
        mandatory=False,
        missing_severity=RiskSeverity.MEDIUM,
        ui_config={
            "placement": "list",
            "primary_fields": ["arbitration_applies", "arbitration_seat", "arbitration_language"],
        },
        extraction_rule=_rule(
            headings=[
                "dispute resolution",
                "disputes",
                "arbitration",
                "mediation",
                "governing law and dispute resolution",
            ],
            keywords=[
                "binding arbitration",
                "resolve any dispute",
                "mediation",
                "arbitral tribunal",
                "rules of arbitration",
                "seat of arbitration",
                "escalation",
            ],
        ),
        synonyms=["Arbitration", "Disputes", "Conflict Resolution", "Escalation"],
        output_schema=_schema(
            arbitration_applies=_boolean("True when disputes go to arbitration."),
            method=_string("litigation, arbitration, mediation, expert_determination, escalation."),
            arbitration_seat=_string("Legal seat/place of arbitration."),
            arbitration_language=_string("Language of the arbitration."),
            arbitration_rules=_string("Institutional rules, e.g. ICC, LCIA, SIAC, AAA."),
            arbitrator_count=_integer("Number of arbitrators."),
            is_binding=_boolean("True when the outcome is final and binding."),
            requires_escalation_first=_boolean(
                "True when negotiation or mediation must precede formal proceedings."
            ),
            escalation_days=_integer("Days allowed for informal resolution before escalation."),
            waives_jury_trial=_boolean("True when jury trial is waived."),
            waives_class_action=_boolean("True when class actions are waived."),
        ),
    ),
    # ---------------------------------------------------------------- 9
    ClauseSeed(
        priority=9,
        key=ClauseType.CONFIDENTIALITY,
        name="Confidentiality / NDA",
        group_name="Legal",
        mandatory=True,
        missing_severity=RiskSeverity.HIGH,
        ui_config={
            "placement": "dedicated_tab",
            "tab_label": "Confidentiality",
            "primary_fields": ["is_mutual", "duration_years", "survives_termination"],
        },
        extraction_rule=_rule(
            headings=[
                "confidentiality",
                "confidential information",
                "non-disclosure",
                "secrecy",
            ],
            keywords=[
                "confidential information",
                "shall not disclose",
                "proprietary information",
                "need to know",
                "trade secret",
            ],
        ),
        synonyms=["Non-Disclosure", "Secrecy", "Proprietary Information", "NDA"],
        output_schema=_schema(
            is_mutual=_boolean("True when both parties owe confidentiality; false when one-way."),
            direction=_party_side("Which side owes the obligation when it is one-way."),
            duration_years=_number("Duration of the confidentiality obligation in years."),
            duration_description=_string(
                "Verbatim duration wording, e.g. 'perpetually for trade secrets'."
            ),
            is_perpetual=_boolean("True when the obligation never expires."),
            survives_termination=_boolean("True when the obligation survives termination."),
            survival_years=_number("Years the obligation survives termination."),
            carve_outs={
                "type": "array",
                "items": {"type": "string"},
                "description": "Standard exclusions: publicly known, independently developed, "
                "required by law.",
            },
            permitted_recipients=_string("Who information may be shared with."),
            return_or_destroy_required=_boolean(
                "True when information must be returned or destroyed on termination."
            ),
        ),
    ),
    # ---------------------------------------------------------------- 10
    ClauseSeed(
        priority=10,
        key=ClauseType.SURVIVAL,
        name="Surviving Clauses",
        group_name="Legal",
        mandatory=False,
        missing_severity=RiskSeverity.MEDIUM,
        ui_config={"placement": "list", "primary_fields": ["surviving_clauses"]},
        extraction_rule=_rule(
            headings=["survival", "surviving provisions", "effect of termination"],
            keywords=[
                "shall survive",
                "survive termination",
                "survive the expiration",
                "remain in full force",
            ],
        ),
        synonyms=["Surviving Provisions", "Effect of Termination", "Survival of Terms"],
        output_schema=_schema(
            surviving_clauses={
                "type": "array",
                "items": {"type": "string"},
                "description": "Clause names or section numbers that survive termination.",
            },
            survival_period_years=_number("Period for which the listed clauses survive."),
            survives_indefinitely=_boolean("True when survival is unlimited in time."),
            survival_text=_string("Verbatim survival wording."),
        ),
    ),
    # ---------------------------------------------------------------- 11
    ClauseSeed(
        priority=11,
        key=ClauseType.TERMINATION_FOR_CONVENIENCE,
        name="Termination for Convenience",
        group_name="Legal",
        mandatory=False,
        missing_severity=RiskSeverity.HIGH,
        ui_config={
            "placement": "dedicated_tab",
            "tab_label": "Termination",
            "primary_fields": ["can_we_terminate", "notice_days", "terminating_side"],
            "highlight_when": {"can_we_terminate": "counterparty"},
        },
        extraction_rule=_rule(
            headings=[
                "termination for convenience",
                "termination without cause",
                "termination at will",
                "termination",
            ],
            keywords=[
                "for convenience",
                "without cause",
                "for any reason",
                "upon.*days.*written notice",
                "at any time",
            ],
        ),
        synonyms=[
            "Termination Without Cause",
            "Termination at Will",
            "Termination for Any Reason",
        ],
        output_schema=_schema(
            is_permitted=_boolean("True when either party may terminate for convenience."),
            terminating_side=_party_side("Which side may terminate for convenience."),
            # Asymmetric convenience rights are a material commercial risk, so this
            # is captured explicitly rather than inferred from prose.
            can_we_terminate=_party_side(
                "Whether our organisation can invoke termination for convenience: "
                "our_organisation, counterparty, both, or neither."
            ),
            notice_days=_integer("Days of written notice required."),
            notice_method=_string("Required notice method, if specified."),
            earliest_termination_date=_string(
                "Earliest date convenience termination may take effect."
            ),
            requires_payment_on_termination=_boolean(
                "True when termination triggers a break fee or payment for work done."
            ),
            termination_fee=_number("Break fee amount, if any."),
            wind_down_days=_integer("Transition or wind-down period in days."),
        ),
    ),
    # ---------------------------------------------------------------- 12
    ClauseSeed(
        priority=12,
        key=ClauseType.TERMINATION_FOR_CAUSE,
        name="Termination for Cause",
        group_name="Legal",
        mandatory=True,
        missing_severity=RiskSeverity.HIGH,
        ui_config={
            "placement": "dedicated_tab",
            "tab_label": "Termination",
            "primary_fields": ["breach_triggers", "cure_period_days", "notice_days"],
        },
        extraction_rule=_rule(
            headings=[
                "termination for cause",
                "termination for breach",
                "termination for default",
                "termination",
            ],
            keywords=[
                "material breach",
                "cure period",
                "fails to cure",
                "within.*days.*of notice",
                "insolvency",
                "default",
            ],
        ),
        synonyms=["Termination for Breach", "Termination for Default", "Events of Default"],
        output_schema=_schema(
            breach_triggers={
                "type": "array",
                "items": {"type": "string"},
                "description": "Events permitting termination for cause: material breach, "
                "insolvency, change of control, regulatory failure.",
            },
            cure_period_days=_integer("Days allowed to cure a breach before termination."),
            is_curable=_boolean("False when certain breaches permit immediate termination."),
            notice_days=_integer("Notice required to terminate for cause."),
            terminating_side=_party_side("Which side may terminate for cause."),
            immediate_termination_events={
                "type": "array",
                "items": {"type": "string"},
                "description": "Events allowing termination with no cure period.",
            },
            includes_insolvency=_boolean("True when insolvency or bankruptcy is a trigger."),
        ),
    ),
    # ---------------------------------------------------------------- 13
    ClauseSeed(
        priority=13,
        key=ClauseType.LIQUIDATED_DAMAGES,
        name="Liquidated Damages",
        group_name="Risk",
        mandatory=False,
        missing_severity=None,
        ui_config={
            "placement": "list",
            "primary_fields": ["amount", "formula", "trigger_event"],
        },
        extraction_rule=_rule(
            headings=["liquidated damages", "damages", "service credits", "penalties"],
            keywords=[
                "liquidated damages",
                "per day of delay",
                "as liquidated damages and not as a penalty",
                "service credit",
                "shall pay.*for each",
            ],
        ),
        synonyms=["Delay Damages", "Penalties", "Service Credits", "Agreed Damages"],
        output_schema=_schema(
            amount=_number("Fixed liquidated damages amount."),
            currency=_string("ISO currency code for the amount."),
            formula=_string(
                "Calculation basis when not a fixed amount, e.g. '0.5% of contract "
                "value per week of delay'."
            ),
            rate_percent=_number("Percentage rate when expressed as a percentage."),
            per_unit=_string("Unit the amount accrues per: day, week, month, incident."),
            trigger_event=_string("Event that triggers liquidated damages."),
            cap_amount=_number("Maximum total liquidated damages."),
            cap_percent=_number("Cap expressed as a percentage of contract value."),
            paying_side=_party_side("Which side pays."),
            is_sole_remedy=_boolean("True when liquidated damages are the exclusive remedy."),
        ),
    ),
    # ---------------------------------------------------------------- 14
    ClauseSeed(
        priority=14,
        key=ClauseType.INSURANCE,
        name="Insurance Requirements",
        group_name="Risk",
        mandatory=False,
        missing_severity=RiskSeverity.MEDIUM,
        ui_config={
            "placement": "list",
            "primary_fields": ["coverage_types", "minimum_amount", "currency"],
        },
        extraction_rule=_rule(
            headings=["insurance", "insurance requirements", "insurance and indemnity"],
            keywords=[
                "shall maintain insurance",
                "commercial general liability",
                "professional indemnity",
                "certificate of insurance",
                "errors and omissions",
                "cyber liability",
            ],
        ),
        synonyms=["Insurance Requirements", "Coverage", "Insurance and Indemnity"],
        output_schema=_schema(
            coverage_types={
                "type": "array",
                "items": {"type": "string"},
                "description": "Required coverage: commercial_general_liability, "
                "professional_indemnity, cyber, workers_compensation, "
                "employers_liability, auto, umbrella.",
            },
            minimum_amount=_number("Minimum required coverage amount (largest stated)."),
            currency=_string("ISO currency code."),
            coverage_details={
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "amount": {"type": ["number", "null"]},
                        "per": {"type": ["string", "null"]},
                    },
                    "additionalProperties": False,
                },
                "description": "Per-coverage minimums where the contract lists several.",
            },
            insuring_side=_party_side("Which side must carry the insurance."),
            requires_certificate=_boolean("True when a certificate of insurance must be provided."),
            requires_additional_insured=_boolean(
                "True when the other party must be named as additional insured."
            ),
            requires_waiver_of_subrogation=_boolean("True when subrogation must be waived."),
            minimum_insurer_rating=_string("Required insurer credit rating, if any."),
        ),
    ),
    # ---------------------------------------------------------------- 15
    ClauseSeed(
        priority=15,
        key=ClauseType.WARRANTY,
        name="Warranties / Representations",
        group_name="Legal",
        mandatory=False,
        missing_severity=RiskSeverity.MEDIUM,
        ui_config={
            "placement": "list",
            "primary_fields": ["warranted_items", "warranty_months", "is_disclaimed"],
        },
        extraction_rule=_rule(
            headings=[
                "warranty",
                "warranties",
                "representations and warranties",
                "disclaimer of warranties",
            ],
            keywords=[
                "represents and warrants",
                "warranty period",
                "as is",
                "disclaims all warranties",
                "fitness for a particular purpose",
                "merchantability",
            ],
        ),
        synonyms=["Representations and Warranties", "Guarantees", "Disclaimer of Warranties"],
        output_schema=_schema(
            warranted_items={
                "type": "array",
                "items": {"type": "string"},
                "description": "What is warranted: authority, non-infringement, "
                "conformance to specification, professional standard, no viruses.",
            },
            warranty_months=_integer("Warranty duration in months."),
            warranty_start_trigger=_string("What starts the warranty period."),
            warranting_side=_party_side("Which side gives the warranties."),
            is_mutual=_boolean("True when both parties give equivalent warranties."),
            is_disclaimed=_boolean("True when warranties are expressly disclaimed (as-is)."),
            disclaimed_warranties={
                "type": "array",
                "items": {"type": "string"},
                "description": "Warranties expressly excluded, e.g. merchantability, "
                "fitness for purpose.",
            },
            remedy=_string("Remedy for breach of warranty: repair, replace, refund, re-perform."),
            is_sole_remedy=_boolean("True when the stated remedy is exclusive."),
        ),
    ),
    # ---------------------------------------------------------------- 16
    ClauseSeed(
        priority=16,
        key=ClauseType.FORCE_MAJEURE,
        name="Force Majeure",
        group_name="Risk",
        mandatory=False,
        missing_severity=RiskSeverity.LOW,
        ui_config={
            "placement": "list",
            "primary_fields": ["covers_pandemic", "excuses_payment", "covered_events"],
        },
        extraction_rule=_rule(
            headings=["force majeure", "acts of god", "excused performance", "impossibility"],
            keywords=[
                "force majeure",
                "beyond the reasonable control",
                "acts of god",
                "epidemic",
                "pandemic",
                "government action",
            ],
        ),
        synonyms=["Acts of God", "Excused Delay", "Unforeseeable Events", "Impossibility"],
        output_schema=_schema(
            covered_events={
                "type": "array",
                "items": {"type": "string"},
                "description": "Events listed as force majeure.",
            },
            # Post-2020 this is the question asked of every force majeure clause.
            covers_pandemic=_boolean(
                "True when pandemic, epidemic or public health emergency is covered."
            ),
            covers_acts_of_god=_boolean("True when natural disasters / acts of God are covered."),
            covers_government_action=_boolean(
                "True when government action or law change is covered."
            ),
            covers_cyber_attack=_boolean("True when cyber attack is covered."),
            covers_supply_chain=_boolean("True when supplier or supply-chain failure is covered."),
            # A clause that excuses payment is very different from one that does not.
            excuses_payment=_boolean(
                "True when force majeure excuses payment obligations; false when "
                "payment obligations expressly continue."
            ),
            notice_days=_integer("Days within which force majeure must be notified."),
            mitigation_required=_boolean("True when the affected party must mitigate."),
            termination_after_days=_integer(
                "Days of continued force majeure after which either party may terminate."
            ),
        ),
    ),
    # ---------------------------------------------------------------- 17
    ClauseSeed(
        priority=17,
        key=ClauseType.LICENSE_GRANT,
        name="License Grants",
        group_name="Legal",
        mandatory=False,
        missing_severity=RiskSeverity.MEDIUM,
        ui_config={
            "placement": "list",
            "primary_fields": ["exclusivity", "is_perpetual", "field_of_use"],
        },
        extraction_rule=_rule(
            headings=["license", "licence", "license grant", "grant of rights", "grant of licence"],
            keywords=[
                "hereby grants",
                "non-exclusive",
                "exclusive licence",
                "right to use",
                "sublicensable",
                "royalty-free",
                "perpetual",
                "revocable",
            ],
        ),
        synonyms=["Grant of Rights", "Licence Grant", "Right to Use", "Usage Rights"],
        output_schema=_schema(
            exclusivity=_enum(LicenceExclusivity, "Exclusive, non-exclusive or sole licence."),
            licensor=_string("Party granting the licence."),
            licensee=_string("Party receiving the licence."),
            licensor_side=_party_side("Which side grants the licence."),
            is_perpetual=_boolean("True when the licence is perpetual."),
            is_revocable=_boolean("True when the licence is revocable."),
            duration_months=_integer("Licence duration in months when time-limited."),
            field_of_use=_string("Permitted field of use / purpose."),
            territory=_string("Geographic scope of the licence."),
            is_sublicensable=_boolean("True when sublicensing is permitted."),
            is_transferable=_boolean("True when the licence may be assigned."),
            is_royalty_free=_boolean("True when no royalty is payable."),
            royalty_terms=_string("Royalty or fee terms when payable."),
            permitted_users=_string(
                "Who may use the licensed material, e.g. affiliates, contractors."
            ),
            restrictions={
                "type": "array",
                "items": {"type": "string"},
                "description": "Express restrictions: no reverse engineering, no benchmarking, "
                "no competitive use.",
            },
        ),
    ),
    # ---------------------------------------------------------------- 18
    ClauseSeed(
        priority=18,
        key=ClauseType.NON_SOLICITATION,
        name="Non-Solicit",
        group_name="Legal",
        mandatory=False,
        missing_severity=None,
        ui_config={
            "placement": "list",
            "primary_fields": ["duration_months", "covers_employees", "covers_customers"],
        },
        extraction_rule=_rule(
            headings=[
                "non-solicitation",
                "non-solicit",
                "no solicitation",
                "restrictive covenants",
            ],
            keywords=[
                "shall not solicit",
                "not employ or engage",
                "induce any employee",
                "poach",
                "solicit any customer",
            ],
        ),
        synonyms=["No Solicitation", "Non-Solicit", "Anti-Poaching"],
        output_schema=_schema(
            duration_months=_integer("Duration of the non-solicit in months."),
            duration_from=_string("What the period runs from: termination, expiry, last contact."),
            covers_employees=_boolean("True when soliciting employees is restricted."),
            covers_customers=_boolean("True when soliciting customers/clients is restricted."),
            covers_contractors=_boolean("True when contractors are covered."),
            covers_suppliers=_boolean("True when suppliers are covered."),
            bound_side=_party_side("Which side is bound by the restriction."),
            is_mutual=_boolean("True when both parties are bound."),
            has_general_advertising_carve_out=_boolean(
                "True when general job advertising is expressly permitted."
            ),
            geography=_string("Geographic scope, if limited."),
        ),
    ),
    # ---------------------------------------------------------------- 19
    ClauseSeed(
        priority=19,
        key=ClauseType.EXCLUSIVITY,
        name="Exclusivity",
        group_name="Commercial",
        mandatory=False,
        missing_severity=None,
        ui_config={
            "placement": "list",
            "primary_fields": ["bound_side", "scope_type", "territory"],
            "highlight_when": {"bound_side": "our_organisation"},
        },
        extraction_rule=_rule(
            headings=["exclusivity", "exclusive arrangement", "exclusive dealing"],
            keywords=[
                "exclusive",
                "shall not engage any other",
                "sole supplier",
                "sole provider",
                "minimum purchase",
                "requirements contract",
            ],
        ),
        synonyms=["Exclusive Dealing", "Sole Source", "Exclusive Arrangement"],
        output_schema=_schema(
            is_exclusive=_boolean("True when an exclusivity obligation exists."),
            bound_side=_party_side("Which side is restricted by the exclusivity."),
            beneficiary_side=_party_side("Which side benefits from the exclusivity."),
            scope_type=_string(
                "What the exclusivity covers: territory, product_line, "
                "customer_segment, channel, field_of_use."
            ),
            territory=_string("Geographic scope of the exclusivity."),
            product_or_service_scope=_string("Product line or service scope covered."),
            duration_months=_integer("Duration of exclusivity in months."),
            minimum_commitment=_number("Minimum volume or spend that sustains exclusivity."),
            minimum_commitment_currency=_string("ISO currency code for the commitment."),
            has_carve_outs=_boolean("True when pre-existing relationships are carved out."),
            carve_outs=_string("Verbatim carve-out wording."),
        ),
    ),
    # ---------------------------------------------------------------- 20
    ClauseSeed(
        priority=20,
        key=ClauseType.ASSIGNMENT,
        name="Assignment / Change of Control",
        group_name="Legal",
        mandatory=False,
        missing_severity=RiskSeverity.MEDIUM,
        ui_config={
            "placement": "list",
            "primary_fields": [
                "consent_required_for_assignment",
                "consent_required_on_change_of_control",
            ],
        },
        extraction_rule=_rule(
            headings=[
                "assignment",
                "assignment and change of control",
                "transfer",
                "successors and assigns",
                "change of control",
            ],
            keywords=[
                "may not assign",
                "prior written consent",
                "successors and assigns",
                "change of control",
                "merger",
                "operation of law",
            ],
        ),
        synonyms=["Transfer", "Novation", "Successors and Assigns", "Change in Control"],
        output_schema=_schema(
            consent_required_for_assignment=_boolean(
                "True when assignment requires the other party's consent."
            ),
            consent_standard=_string(
                "Consent standard: absolute discretion, not_unreasonably_withheld, none."
            ),
            # M&A blockers: whether a sale of the business needs counterparty consent.
            consent_required_on_change_of_control=_boolean(
                "True when a change of control / M&A requires consent."
            ),
            change_of_control_permits_termination=_boolean(
                "True when the other party may terminate on a change of control."
            ),
            affiliate_assignment_permitted=_boolean(
                "True when assignment to an affiliate is permitted without consent."
            ),
            assignment_to_successor_permitted=_boolean(
                "True when assignment in a merger or asset sale is permitted."
            ),
            restricted_side=_party_side("Which side is restricted from assigning."),
            is_mutual=_boolean("True when the restriction binds both parties."),
        ),
    ),
    # ---------------------------------------------------------------- 21
    ClauseSeed(
        priority=21,
        key=ClauseType.AUDIT_RIGHTS,
        name="Audit Rights",
        group_name="Compliance",
        mandatory=False,
        missing_severity=RiskSeverity.MEDIUM,
        ui_config={
            "placement": "list",
            "primary_fields": ["frequency", "notice_days", "auditing_side"],
        },
        extraction_rule=_rule(
            headings=[
                "audit",
                "audit rights",
                "records and audit",
                "inspection",
                "books and records",
            ],
            keywords=[
                "right to audit",
                "books and records",
                "upon reasonable notice",
                "inspect",
                "once per year",
                "during normal business hours",
            ],
        ),
        synonyms=["Right to Audit", "Inspection Rights", "Records", "Books and Records"],
        output_schema=_schema(
            has_audit_rights=_boolean("True when an audit right exists."),
            auditing_side=_party_side("Which side may audit."),
            frequency=_string("How often audits are permitted, e.g. once per year."),
            max_audits_per_year=_integer("Maximum audits allowed per year."),
            notice_days=_integer("Days of advance notice required."),
            scope=_string("What may be audited: financial records, security, compliance, usage."),
            permits_third_party_auditor=_boolean("True when an external auditor may be used."),
            cost_bearer=_party_side("Which side bears audit costs."),
            cost_shifts_on_findings=_boolean(
                "True when the audited party pays if material discrepancies are found."
            ),
            record_retention_years=_integer("Years records must be retained for audit."),
            business_hours_only=_boolean("True when audits are limited to business hours."),
        ),
    ),
    # ---------------------------------------------------------------- 22
    ClauseSeed(
        priority=22,
        key=ClauseType.NOTICE,
        name="Notice Requirements",
        group_name="Operational",
        mandatory=False,
        missing_severity=None,
        confidence_threshold=0.80,
        ui_config={
            "placement": "list",
            "primary_fields": ["permitted_methods", "notice_addresses"],
        },
        extraction_rule=_rule(
            headings=["notice", "notices", "communications", "notices and communications"],
            keywords=[
                "all notices",
                "shall be in writing",
                "deemed given",
                "sent to the address",
                "certified mail",
                "registered post",
                "by email to",
                "courier",
            ],
        ),
        synonyms=["Notices", "Communications", "Notices and Communications"],
        output_schema=_schema(
            permitted_methods={
                "type": "array",
                "items": {"type": "string"},
                "description": "Permitted notice methods: email, certified_mail, "
                "registered_post, courier, hand_delivery, portal, fax.",
            },
            email_permitted=_boolean("True when email is a valid notice method."),
            requires_written_notice=_boolean("True when notice must be in writing."),
            # The addresses on file - what someone actually needs when serving notice.
            notice_addresses={
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "party": {"type": ["string", "null"]},
                        "attention": {"type": ["string", "null"]},
                        "address": {"type": ["string", "null"]},
                        "email": {"type": ["string", "null"]},
                        "copy_to": {"type": ["string", "null"]},
                    },
                    "additionalProperties": False,
                },
                "description": "Contact / address on file for each party.",
            },
            deemed_received_days=_integer("Days after despatch when notice is deemed received."),
            deemed_received_rule=_string("How receipt is deemed, verbatim if concise."),
            requires_copy_to_legal=_boolean("True when a copy must go to a legal contact."),
        ),
    ),
    # ---------------------------------------------------------------- 23
    ClauseSeed(
        priority=23,
        key=ClauseType.AMENDMENT_PROCEDURE,
        name="Amendment Procedure",
        group_name="Operational",
        mandatory=False,
        missing_severity=None,
        confidence_threshold=0.80,
        ui_config={
            "placement": "list",
            "primary_fields": ["requires_written_consent", "requires_both_parties"],
        },
        extraction_rule=_rule(
            headings=["amendment", "amendments", "modification", "variation", "changes"],
            keywords=[
                "may only be amended",
                "in writing signed by both",
                "no modification",
                "variation of this agreement",
                "written instrument",
                "change order",
            ],
        ),
        synonyms=["Modification", "Variation", "Changes", "Amendments and Waivers"],
        output_schema=_schema(
            requires_written_consent=_boolean("True when amendments must be in writing."),
            # "Signed by both parties" vs "written notice by one" is the distinction
            # that decides whether a counterparty can change terms unilaterally.
            requires_both_parties=_boolean(
                "True when written consent from both parties is required."
            ),
            requires_signature=_boolean("True when a signed instrument is required."),
            authorised_signatories=_string("Who may sign an amendment, if specified."),
            permits_unilateral_change=_boolean(
                "True when one party may change terms unilaterally, e.g. by posting updated terms."
            ),
            unilateral_change_side=_party_side("Which side may change terms unilaterally."),
            unilateral_change_notice_days=_integer(
                "Notice required before a unilateral change takes effect."
            ),
            change_order_process=_string("Formal change-control process, if described."),
            email_amendment_permitted=_boolean("True when email agreement suffices."),
        ),
    ),
)


# =============================================================================
# Supporting categories
# =============================================================================
# Below the priority list, but required by the seeded Document Intelligence
# Profiles: a profile that marks `scope_of_work` mandatory needs a Clause Master
# category to define how it is found, otherwise missing-clause detection would
# flag every contract. Priorities start at 24 so the priority list above always
# extracts first.
SUPPORTING_CLAUSE_SEEDS: tuple[ClauseSeed, ...] = (
    ClauseSeed(
        priority=24,
        key=ClauseType.SCOPE_OF_WORK,
        name="Scope of Work",
        group_name="Operational",
        mandatory=False,
        missing_severity=RiskSeverity.MEDIUM,
        extraction_rule=_rule(
            headings=["scope of work", "services", "statement of work", "deliverables", "scope"],
            keywords=["shall provide", "services described", "deliverables", "scope of services"],
        ),
        synonyms=["Services", "Statement of Work", "Deliverables", "Scope of Services"],
        output_schema=_schema(
            summary=_string("One-sentence summary of what is being delivered."),
            deliverables={
                "type": "array",
                "items": {"type": "string"},
                "description": "Named deliverables or work products.",
            },
            performing_side=_party_side("Which side performs the work."),
            acceptance_criteria=_string("How deliverables are accepted."),
            acceptance_days=_integer("Days allowed for acceptance review."),
            is_time_and_materials=_boolean("True for time-and-materials rather than fixed scope."),
            references_sow=_boolean("True when scope is delegated to a separate SOW or schedule."),
        ),
    ),
    ClauseSeed(
        priority=25,
        key=ClauseType.DATA_PROTECTION,
        name="Data Protection",
        group_name="Compliance",
        mandatory=False,
        missing_severity=RiskSeverity.HIGH,
        extraction_rule=_rule(
            headings=[
                "data protection",
                "data privacy",
                "personal data",
                "gdpr",
                "data processing",
            ],
            keywords=[
                "personal data",
                "data processor",
                "data controller",
                "gdpr",
                "processing of data",
                "data subject",
                "sub-processor",
            ],
        ),
        synonyms=["Privacy", "GDPR Compliance", "Data Processing", "Data Processing Addendum"],
        output_schema=_schema(
            our_role=_string("Our role: controller, processor, joint_controller, none."),
            regulations={
                "type": "array",
                "items": {"type": "string"},
                "description": "Named regimes: GDPR, UK_GDPR, CCPA, HIPAA, DPDP.",
            },
            has_dpa=_boolean("True when a data processing agreement is incorporated."),
            breach_notice_hours=_integer("Hours within which a breach must be notified."),
            permits_sub_processors=_boolean("True when sub-processing is permitted."),
            requires_sub_processor_consent=_boolean("True when sub-processors need consent."),
            cross_border_transfer_permitted=_boolean(
                "True when international transfers are allowed."
            ),
            transfer_mechanism=_string("Transfer safeguard, e.g. SCCs, adequacy, BCRs."),
            security_standard=_string("Required security standard, e.g. ISO 27001, SOC 2."),
            deletion_on_termination_days=_integer("Days within which data must be deleted."),
        ),
    ),
    ClauseSeed(
        priority=26,
        key=ClauseType.COMPLIANCE,
        name="Compliance with Laws",
        group_name="Compliance",
        mandatory=False,
        missing_severity=RiskSeverity.MEDIUM,
        extraction_rule=_rule(
            headings=["compliance", "compliance with laws", "anti-corruption", "anti-bribery"],
            keywords=[
                "comply with all applicable",
                "anti-bribery",
                "sanctions",
                "export control",
                "fcpa",
                "modern slavery",
                "code of conduct",
            ],
        ),
        synonyms=["Compliance with Laws", "Legal Compliance", "Anti-Corruption", "Ethical Conduct"],
        output_schema=_schema(
            regimes={
                "type": "array",
                "items": {"type": "string"},
                "description": "Named regimes: FCPA, UK_Bribery_Act, sanctions, export_control, "
                "modern_slavery, SOX.",
            },
            bound_side=_party_side("Which side owes the compliance obligation."),
            is_mutual=_boolean("True when both parties are bound."),
            requires_code_of_conduct=_boolean("True when a supplier code of conduct applies."),
            permits_termination_on_breach=_boolean(
                "True when a compliance breach permits immediate termination."
            ),
            requires_certification=_boolean(
                "True when periodic compliance certification is required."
            ),
        ),
    ),
    ClauseSeed(
        priority=27,
        key=ClauseType.SERVICE_LEVEL,
        name="Service Level",
        group_name="Operational",
        mandatory=False,
        missing_severity=RiskSeverity.MEDIUM,
        extraction_rule=_rule(
            headings=["service level", "sla", "service level agreement", "performance standards"],
            keywords=[
                "uptime",
                "availability",
                "service credits",
                "response time",
                "resolution time",
            ],
        ),
        synonyms=["SLA", "Performance Standards", "Service Commitments", "Availability"],
        output_schema=_schema(
            uptime_percent=_number("Committed availability percentage."),
            measurement_period=_string("Period availability is measured over."),
            response_time=_string("Committed response time, with units."),
            resolution_time=_string("Committed resolution time, with units."),
            has_service_credits=_boolean("True when failures earn service credits."),
            service_credit_terms=_string("How credits are calculated."),
            credit_cap_percent=_number("Cap on credits as a percentage of fees."),
            credits_are_sole_remedy=_boolean("True when credits are the exclusive remedy."),
            permits_termination_for_chronic_failure=_boolean(
                "True when repeated SLA failure permits termination."
            ),
        ),
    ),
    ClauseSeed(
        priority=28,
        key=ClauseType.PUBLICITY,
        name="Publicity & Use of Name",
        group_name="Operational",
        mandatory=False,
        missing_severity=None,
        confidence_threshold=0.80,
        extraction_rule=_rule(
            headings=["publicity", "use of name", "press releases", "marketing", "announcements"],
            keywords=[
                "shall not use the name",
                "press release",
                "prior written approval",
                "logo",
                "trademark",
                "case study",
                "publication",
            ],
        ),
        synonyms=["Use of Name", "Press Releases", "Marketing Rights", "Announcements"],
        output_schema=_schema(
            requires_approval=_boolean("True when publicity requires the other party's approval."),
            restricted_side=_party_side("Which side is restricted."),
            is_mutual=_boolean("True when the restriction is mutual."),
            permits_logo_use=_boolean("True when logo or trademark use is permitted."),
            permits_customer_reference=_boolean("True when the party may be named as a reference."),
            permits_publication=_boolean("True when research results may be published."),
            publication_review_days=_integer("Days the other party has to review a publication."),
        ),
    ),
    ClauseSeed(
        priority=29,
        key=ClauseType.DEFINITIONS,
        name="Definitions",
        group_name="Operational",
        mandatory=False,
        missing_severity=None,
        confidence_threshold=0.80,
        extraction_rule=_rule(
            headings=["definitions", "defined terms", "interpretation", "construction"],
            keywords=["shall mean", "for purposes of this agreement", "as defined", "means"],
        ),
        synonyms=["Defined Terms", "Interpretation", "Construction", "Glossary"],
        output_schema=_schema(
            # Feeds the knowledge graph `defines` edges, which is how "what does
            # 'Confidential Information' mean here?" gets answered per contract.
            defined_terms={
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "term": {"type": ["string", "null"]},
                        "definition": {"type": ["string", "null"]},
                    },
                    "additionalProperties": False,
                },
                "description": "Defined terms and their definitions.",
            },
            term_count=_integer("Number of defined terms."),
        ),
    ),
    ClauseSeed(
        priority=30,
        key=ClauseType.ENTIRE_AGREEMENT,
        name="Entire Agreement",
        group_name="Legal",
        mandatory=False,
        missing_severity=None,
        confidence_threshold=0.80,
        extraction_rule=_rule(
            headings=["entire agreement", "integration", "complete agreement", "whole agreement"],
            keywords=["entire agreement", "supersedes all prior", "no other representations"],
        ),
        synonyms=["Integration", "Merger Clause", "Whole Agreement"],
        output_schema=_schema(
            supersedes_prior_agreements=_boolean("True when prior agreements are superseded."),
            incorporated_documents={
                "type": "array",
                "items": {"type": "string"},
                "description": "Schedules, exhibits or policies incorporated by reference.",
            },
            order_of_precedence=_string(
                "Stated precedence between the agreement and its schedules."
            ),
            excludes_reliance_on_representations=_boolean(
                "True when reliance on pre-contractual representations is excluded."
            ),
        ),
    ),
)


#: Every seeded category: the priority list first, then supporting categories.
ALL_CLAUSE_SEEDS: tuple[ClauseSeed, ...] = (*CLAUSE_SEEDS, *SUPPORTING_CLAUSE_SEEDS)

#: Clause keys ordered by business priority - the extraction order.
PRIORITY_CLAUSE_ORDER: tuple[str, ...] = tuple(str(seed.key) for seed in ALL_CLAUSE_SEEDS)

#: Just the explicitly prioritised clauses, in order.
TOP_PRIORITY_ORDER: tuple[str, ...] = tuple(str(seed.key) for seed in CLAUSE_SEEDS)

#: Keys the platform treats as mandatory by default. Profiles may add or relax.
DEFAULT_MANDATORY_CLAUSES: tuple[str, ...] = tuple(
    str(seed.key) for seed in ALL_CLAUSE_SEEDS if seed.mandatory
)

#: Clause keys the UI renders on a dedicated tab.
DEDICATED_TAB_CLAUSES: tuple[str, ...] = tuple(
    str(seed.key) for seed in ALL_CLAUSE_SEEDS if seed.ui_config.get("placement") == "dedicated_tab"
)


def seed_by_key(key: str) -> ClauseSeed | None:
    return next((seed for seed in ALL_CLAUSE_SEEDS if str(seed.key) == key), None)


def top_priority_clauses(count: int) -> tuple[str, ...]:
    """The ``count`` highest-priority clause keys."""
    return PRIORITY_CLAUSE_ORDER[:count]


def attribute_schema(key: str) -> dict[str, Any]:
    """Attribute JSON Schema for a clause type, or an open object if unknown.

    Unknown keys return a permissive schema rather than raising: an administrator
    can add a clause category at runtime, and extraction must keep working for it
    before anyone writes a schema.
    """
    seed = seed_by_key(key)
    if seed is None:
        return {"type": "object", "additionalProperties": True}
    return seed.output_schema


__all__ = [
    "ALL_CLAUSE_SEEDS",
    "CLAUSE_SEEDS",
    "DEDICATED_TAB_CLAUSES",
    "DEFAULT_MANDATORY_CLAUSES",
    "PRIORITY_CLAUSE_ORDER",
    "SUPPORTING_CLAUSE_SEEDS",
    "TOP_PRIORITY_ORDER",
    "ClauseSeed",
    "attribute_schema",
    "seed_by_key",
    "top_priority_clauses",
]

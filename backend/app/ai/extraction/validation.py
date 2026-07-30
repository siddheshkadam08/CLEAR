"""Extraction validation (§13).

Two layers, in this order:

1. **Schema conformance.** The provider already constrains output to the schema, so
   this is a second line of defence - it catches a mock provider, a non-strict
   OpenAI deployment, and the case where a schema and its consumer have drifted
   apart. Cheap, and it fails loudly rather than letting a malformed attribute
   reach a JSONB column that something later filters on.

2. **Business validation**, which is where the value is. A schema cannot express
   that ``cap_basis: "1x_fees_paid"`` with ``cap_multiple: 2`` is nonsense, that a
   carve-out list contradicts ``has_carve_outs: false``, that an effective date
   after an expiration date is impossible, or that a party named as ours cannot
   simultaneously be the counterparty. Every one of those has been observed from a
   real model.

**Citation and quote verification is the mechanical half of "never invent contract
content".** The model may only cite evidence block ids it was given, and the text
it quotes must actually appear in that evidence. A clause whose wording is not in
the evidence is rejected no matter how plausible it reads - which is precisely the
failure mode a reviewer cannot catch by eye, because invented contract language
reads exactly like real contract language.

Where a finding is unambiguously correctable - a flag that contradicts its own list -
the validator corrects it and records a warning rather than discarding the whole
clause. Where the correction would be a guess, it rejects.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any

from app.ai.extraction.evidence import EvidenceBundle, normalise_quote
from app.ai.extraction.models import ValidationIssue
from app.core.enums import (
    ClauseType,
    IndemnityPosture,
    LiabilityCapBasis,
    PartySide,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

#: A quote must match the evidence at least this closely, measured on word
#: sequences, to be accepted with a warning rather than rejected. Below it, the
#: text is treated as not present in the evidence.
_QUOTE_MATCH_THRESHOLD = 0.85

#: Above this, a near-match is accepted silently: the remaining difference is
#: whitespace or hyphenation the PDF introduced, not different wording.
_QUOTE_CLEAN_THRESHOLD = 0.98

#: Beyond this many months, a stated duration is more likely a units mistake
#: ("60" meaning years) than a real term. Flagged, never corrected.
_IMPLAUSIBLE_MONTHS = 1_200

#: Field-name suffixes that carry a duration, and the unit each implies.
_DURATION_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_days", "days"),
    ("_months", "months"),
    ("_years", "years"),
    ("_hours", "hours"),
)

_AMOUNT_HINTS = ("amount", "value", "fee", "price", "penalty", "premium")
_CURRENCY_HINTS = ("currency",)

_DATE_FIELD_HINTS = ("date", "deadline")

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# =============================================================================
# Context
# =============================================================================
@dataclass(slots=True)
class ValidationContext:
    """Everything validation needs beyond the payload itself."""

    #: Evidence the model was given. The allow-list for citations and the corpus
    #: for quote verification.
    bundle: EvidenceBundle | None = None
    #: Names that identify our own organisation, for party-side checks.
    organisation_aliases: list[str] = field(default_factory=list)
    #: Extracted party names, lowercased, mapped to whether they are ours.
    party_sides: dict[str, bool] = field(default_factory=dict)
    #: ``profile.validation_rules``.
    rules: dict[str, Any] = field(default_factory=dict)
    #: Category confidence threshold from the Clause Master.
    confidence_threshold: float = 0.85
    #: Set when the call carried no document evidence (the summary call), which
    #: disables citation checks that would otherwise reject everything.
    requires_citations: bool = True

    _evidence_words: list[str] | None = field(default=None, repr=False)
    _evidence_text: str | None = field(default=None, repr=False)

    @property
    def allowed_chunk_ids(self) -> set[str]:
        return self.bundle.chunk_ids if self.bundle else set()

    @property
    def evidence_text(self) -> str:
        """Normalised concatenation of the evidence, cached."""
        if self._evidence_text is None:
            raw = "\n".join(chunk.text for chunk in self.bundle.chunks) if self.bundle else ""
            self._evidence_text = normalise_quote(raw)
        return self._evidence_text

    @property
    def evidence_words(self) -> list[str]:
        if self._evidence_words is None:
            self._evidence_words = self.evidence_text.split()
        return self._evidence_words

    def rule(self, path: str, default: Any = None) -> Any:
        """Read a dotted path out of the profile's validation rules."""
        node: Any = self.rules
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def side_of(self, party_name: str | None) -> PartySide:
        """Which side a named party is on, by alias match.

        Matching is on normalised containment in both directions, because a
        contract writes "IRIS RegTech Solutions Private Limited" where the alias is
        "IRIS RegTech" and sometimes the reverse.
        """
        if not party_name:
            return PartySide.UNKNOWN
        needle = normalise_party_name(party_name)
        if not needle:
            return PartySide.UNKNOWN
        for alias in self.organisation_aliases:
            candidate = normalise_party_name(alias)
            if candidate and (candidate in needle or needle in candidate):
                return PartySide.OUR_ORGANISATION
        if needle in self.party_sides:
            return (
                PartySide.OUR_ORGANISATION if self.party_sides[needle] else PartySide.COUNTERPARTY
            )
        # A named party that is neither ours nor a known extracted party: the most
        # honest answer is that we do not know which side it sits on.
        return PartySide.UNKNOWN


def normalise_party_name(value: str) -> str:
    """Lowercase, strip company suffixes and punctuation, for alias matching."""
    lowered = re.sub(r"[^a-z0-9 ]+", " ", value.lower())
    lowered = re.sub(
        r"\b(private|pvt|limited|ltd|llc|inc|incorporated|corp|corporation|plc|gmbh|"
        r"llp|company|co|sa|bv|nv|ag|pte)\b",
        " ",
        lowered,
    )
    return re.sub(r"\s+", " ", lowered).strip()


@dataclass(slots=True)
class FieldOutcome:
    """Validation outcome for one payload."""

    issues: list[ValidationIssue] = field(default_factory=list)
    #: Fields the validator corrected, with the reason - surfaced to the reviewer
    #: so a corrected value is never silently different from what the model said.
    corrections: dict[str, Any] = field(default_factory=dict)
    checks_run: int = 0
    checks_passed: int = 0

    def add(
        self,
        code: str,
        message: str,
        *,
        field_name: str | None = None,
        severity: str = "error",
    ) -> None:
        self.issues.append(
            ValidationIssue(code=code, message=message, field=field_name, severity=severity)
        )

    def check(self, passed: bool) -> bool:
        self.checks_run += 1
        if passed:
            self.checks_passed += 1
        return passed

    def correct(self, field_name: str, value: Any, reason: str) -> None:
        self.corrections[field_name] = value
        self.add(
            "auto_corrected",
            f"{field_name} was corrected: {reason}",
            field_name=field_name,
            severity="warning",
        )

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.is_error]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if not issue.is_error]

    @property
    def has_errors(self) -> bool:
        return any(issue.is_error for issue in self.issues)

    @property
    def score(self) -> float:
        if self.checks_run == 0:
            return 1.0
        return round(self.checks_passed / self.checks_run, 4)

    def merge(self, other: FieldOutcome) -> None:
        self.issues.extend(other.issues)
        self.corrections.update(other.corrections)
        self.checks_run += other.checks_run
        self.checks_passed += other.checks_passed


# =============================================================================
# Schema conformance (subset)
# =============================================================================
def validate_against_schema(
    payload: Any, schema: dict[str, Any], *, path: str = ""
) -> list[ValidationIssue]:
    """Check a payload against the JSON Schema subset this platform emits.

    Handles type unions, enums, objects with ``required``/``additionalProperties``,
    and arrays. Deliberately not a general-purpose validator: the schemas are
    generated by this codebase, so supporting the whole specification would be
    unused surface area.
    """
    issues: list[ValidationIssue] = []
    where = path or "(root)"

    declared = schema.get("type")
    types = [declared] if isinstance(declared, str) else list(declared) if declared else []

    if types and not _matches_type(payload, types):
        issues.append(
            ValidationIssue(
                code="schema_type_mismatch",
                message=f"Expected {'|'.join(types)}, received {type(payload).__name__}.",
                field=where,
            )
        )
        return issues

    if payload is None:
        return issues

    if "enum" in schema and payload not in schema["enum"]:
        issues.append(
            ValidationIssue(
                code="schema_enum_violation",
                message=f"Value {payload!r} is not one of the permitted values.",
                field=where,
            )
        )

    if "object" in types and isinstance(payload, dict):
        properties: dict[str, Any] = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in payload:
                issues.append(
                    ValidationIssue(
                        code="schema_missing_field",
                        message=f"Required field '{name}' is absent.",
                        field=f"{where}.{name}" if path else name,
                    )
                )
        if schema.get("additionalProperties") is False:
            for name in payload:
                if name not in properties:
                    issues.append(
                        ValidationIssue(
                            code="schema_unexpected_field",
                            message=f"Field '{name}' is not part of this schema.",
                            field=f"{where}.{name}" if path else name,
                            severity="warning",
                        )
                    )
        for name, sub in properties.items():
            if name in payload:
                issues.extend(
                    validate_against_schema(
                        payload[name], sub, path=f"{path}.{name}" if path else name
                    )
                )

    if "array" in types and isinstance(payload, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, entry in enumerate(payload):
                issues.extend(validate_against_schema(entry, items, path=f"{path}[{index}]"))

    return issues


def _matches_type(value: Any, types: list[str]) -> bool:
    for declared in types:
        if declared == "null" and value is None:
            return True
        if declared == "string" and isinstance(value, str):
            return True
        if declared == "boolean" and isinstance(value, bool):
            return True
        # bool is a subclass of int; a boolean where a number is expected is a
        # genuine type error, not a coercible one.
        if declared == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if declared == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if declared == "object" and isinstance(value, dict):
            return True
        if declared == "array" and isinstance(value, list):
            return True
    return False


# =============================================================================
# Grounding checks
# =============================================================================
def check_citations(
    chunk_ids: Any, ctx: ValidationContext, outcome: FieldOutcome, *, field_name: str
) -> list[str]:
    """Verify cited evidence ids, returning the ids that are legitimate."""
    if not ctx.requires_citations:
        return []

    ids = [str(value) for value in chunk_ids] if isinstance(chunk_ids, list) else []

    if not outcome.check(bool(ids)):
        outcome.add(
            "missing_citation",
            "The item cites no evidence. Every extracted fact must reference the "
            "evidence block it came from.",
            field_name=field_name,
        )
        return []

    allowed = ctx.allowed_chunk_ids
    valid = [value for value in ids if value in allowed]
    invented = [value for value in ids if value not in allowed]

    if not outcome.check(not invented):
        outcome.add(
            "unknown_evidence_citation",
            "The item cites evidence ids that were not supplied: "
            f"{', '.join(invented[:5])}. A citation to evidence the model did not "
            "receive cannot be verified and the item is rejected.",
            field_name=field_name,
        )
    return valid


def check_quote(
    quote: str, ctx: ValidationContext, outcome: FieldOutcome, *, field_name: str = "text"
) -> None:
    """Verify quoted text actually appears in the supplied evidence.

    The single most important check in the platform. An extraction whose quoted
    wording is absent from the evidence is fabricated contract language, and it is
    rejected outright - a reviewer cannot detect it by reading, because it reads
    like a contract.
    """
    if not ctx.requires_citations or not ctx.bundle:
        return

    stripped = (quote or "").strip()
    if not outcome.check(bool(stripped)):
        outcome.add("empty_quote", "The quoted clause text is empty.", field_name=field_name)
        return

    normalised = normalise_quote(stripped)
    evidence = ctx.evidence_text

    if normalised and normalised in evidence:
        outcome.check(True)
        return

    ratio = _sequence_coverage(normalised.split(), ctx.evidence_words)

    if ratio >= _QUOTE_CLEAN_THRESHOLD:
        # Whitespace or hyphenation noise from PDF extraction; the wording matches.
        outcome.check(True)
        return

    if ratio >= _QUOTE_MATCH_THRESHOLD:
        outcome.check(True)
        outcome.add(
            "quote_partially_matched",
            f"Only {ratio:.0%} of the quoted text was found in the evidence. The "
            "wording may have been altered or joined across passages; verify before "
            "relying on it.",
            field_name=field_name,
            severity="warning",
        )
        return

    outcome.check(False)
    outcome.add(
        "quote_not_in_evidence",
        f"The quoted text does not appear in the supplied evidence (matched "
        f"{ratio:.0%}). Rejected: extracted clause text must be a verbatim quote, "
        "never composed or paraphrased.",
        field_name=field_name,
    )


def _sequence_coverage(needle: list[str], haystack: list[str]) -> float:
    """Fraction of ``needle``'s words matched, in order, inside ``haystack``.

    Bounded input: quote verification must not become the slowest thing in the
    pipeline on a pathological response.
    """
    if not needle:
        return 0.0
    matcher = SequenceMatcher(None, needle[:2_000], haystack[:20_000], autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return min(1.0, matched / len(needle[:2_000]))


def check_confidence(value: Any, outcome: FieldOutcome) -> float:
    """Coerce and range-check a confidence value."""
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        outcome.check(False)
        outcome.add(
            "invalid_confidence",
            "Confidence was not a number; treated as zero and flagged for review.",
            field_name="confidence",
            severity="warning",
        )
        return 0.0

    if not outcome.check(0.0 <= confidence <= 1.0):
        clamped = min(max(confidence, 0.0), 1.0)
        outcome.add(
            "confidence_out_of_range",
            f"Confidence {confidence} is outside 0..1; clamped to {clamped}.",
            field_name="confidence",
            severity="warning",
        )
        return clamped
    return confidence


# =============================================================================
# Generic attribute rules
# =============================================================================
def check_generic_attributes(
    attributes: dict[str, Any], ctx: ValidationContext, outcome: FieldOutcome
) -> None:
    """Rules that apply to any attribute set, by field-name convention.

    Convention-driven rather than per-clause: the Clause Master is
    administrator-extensible, so a clause type added at runtime gets these checks
    without anyone writing code for it.
    """
    require_currency = bool(ctx.rule("money.require_currency_with_amount", True))
    max_value = ctx.rule("money.max_reasonable_value")
    reject_impossible_dates = bool(ctx.rule("dates.reject_impossible_dates", True))

    for key, value in list(attributes.items()):
        if value is None:
            continue

        # --- durations ---------------------------------------------------------
        for suffix, unit in _DURATION_SUFFIXES:
            if key.endswith(suffix) and isinstance(value, (int, float)):
                if not outcome.check(value >= 0):
                    outcome.add(
                        "negative_duration",
                        f"{key} is {value}; a duration cannot be negative.",
                        field_name=key,
                    )
                months = (
                    value
                    if unit == "months"
                    else value / 30
                    if unit == "days"
                    else value * 12
                    if unit == "years"
                    else 0
                )
                if months and not outcome.check(months <= _IMPLAUSIBLE_MONTHS):
                    outcome.add(
                        "implausible_duration",
                        f"{key} is {value} {unit}, which is implausibly long. Verify "
                        "the units stated in the agreement.",
                        field_name=key,
                        severity="warning",
                    )
                break

        # --- money -------------------------------------------------------------
        if (
            any(hint in key for hint in _AMOUNT_HINTS)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            if not outcome.check(value >= 0):
                outcome.add(
                    "negative_amount",
                    f"{key} is {value}; a monetary amount cannot be negative.",
                    field_name=key,
                )
            if max_value and not outcome.check(value <= float(max_value)):
                outcome.add(
                    "implausible_amount",
                    f"{key} is {value}, above the configured plausibility ceiling. "
                    "Verify the figure and its units.",
                    field_name=key,
                    severity="warning",
                )
            if require_currency:
                currency = _paired_currency(attributes, key)
                if not outcome.check(bool(currency)):
                    outcome.add(
                        "amount_without_currency",
                        f"{key} has a value but no currency was stated, so the amount "
                        "cannot be compared or reported.",
                        field_name=key,
                        severity="warning",
                    )

        # --- currency codes ----------------------------------------------------
        if any(hint in key for hint in _CURRENCY_HINTS) and isinstance(value, str):
            code = value.strip().upper()
            if not outcome.check(len(code) == 3 and code.isalpha()):
                outcome.add(
                    "invalid_currency_code",
                    f"{key} is {value!r}; expected a three-letter ISO 4217 code.",
                    field_name=key,
                    severity="warning",
                )
            elif code != value:
                outcome.correct(key, code, "normalised to upper-case ISO 4217")

        # --- dates -------------------------------------------------------------
        if (
            any(hint in key for hint in _DATE_FIELD_HINTS)
            and isinstance(value, str)
            and value.strip()
            and not key.endswith("_expression")
        ):
            parsed = parse_date(value)
            if parsed is None and reject_impossible_dates:
                if not outcome.check(False):
                    outcome.add(
                        "invalid_date",
                        f"{key} is {value!r}, which is not a valid YYYY-MM-DD date. If "
                        "the agreement expresses it relatively, it belongs in an "
                        "expression field instead.",
                        field_name=key,
                        severity="warning",
                    )
            else:
                outcome.check(True)


def _paired_currency(attributes: dict[str, Any], amount_key: str) -> str | None:
    """Find the currency that goes with an amount field.

    Tries the field-specific pairing first (``cap_amount`` → ``cap_currency``), then
    any currency on the same attribute set - a clause that states one currency for
    the whole clause is normal drafting.
    """
    stem = amount_key.rsplit("_", 1)[0]
    for candidate in (f"{stem}_currency", "currency"):
        value = attributes.get(candidate)
        if isinstance(value, str) and value.strip():
            return value
    for key, value in attributes.items():
        if "currency" in key and isinstance(value, str) and value.strip():
            return value
    return None


def parse_date(value: Any) -> date | None:
    """Parse an ISO date, returning None for anything else.

    Strict: a model asked for YYYY-MM-DD that returns "Q3 2026" has given a date
    *expression*, and silently guessing a day for it would invent a deadline.
    """
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _ISO_DATE.match(text):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


# =============================================================================
# Clause-specific business rules
# =============================================================================
def check_liability_cap(attributes: dict[str, Any], outcome: FieldOutcome) -> None:
    """Coherence of the liability cap - the highest-value term in the repository.

    ``cap_basis`` and the numeric fields must agree. A cap recorded as "1x fees
    paid" with a multiple of 2 is not a small inconsistency: the dedicated
    Limitation of Liability tab filters and sorts on these fields, so an incoherent
    pair produces a repository that answers "show me every 1x cap" wrongly.
    """
    basis = attributes.get("cap_basis")
    multiple = attributes.get("cap_multiple")
    amount = attributes.get("cap_amount")

    expected_multiple = {
        LiabilityCapBasis.ONE_X_FEES_PAID.value: 1.0,
        LiabilityCapBasis.TWO_X_FEES_PAID.value: 2.0,
    }.get(str(basis) if basis else "")

    if expected_multiple is not None:
        if multiple is None:
            # Implied by the basis and unambiguous, so fill it in rather than
            # leaving a queryable field null.
            outcome.check(True)
            outcome.correct(
                "cap_multiple",
                expected_multiple,
                f"implied by cap_basis {basis}",
            )
        elif not outcome.check(float(multiple) == expected_multiple):
            outcome.add(
                "cap_multiple_conflicts_with_basis",
                f"cap_basis is {basis} but cap_multiple is {multiple}. These state "
                "different caps; the clause cannot be recorded until they agree.",
                field_name="cap_multiple",
            )

    if basis == LiabilityCapBasis.OTHER_MULTIPLE_OF_FEES.value and not outcome.check(
        multiple is not None
    ):
        outcome.add(
            "cap_multiple_required",
            "cap_basis is a multiple of fees but no multiple was extracted, so "
            "the cap has no value.",
            field_name="cap_multiple",
        )

    if basis == LiabilityCapBasis.FIXED_AMOUNT.value and not outcome.check(amount is not None):
        outcome.add(
            "cap_amount_required",
            "cap_basis is a fixed amount but no amount was extracted, so the cap has no value.",
            field_name="cap_amount",
        )

    if basis == LiabilityCapBasis.UNCAPPED.value and not outcome.check(
        multiple is None and amount is None
    ):
        outcome.add(
            "uncapped_with_cap_value",
            f"cap_basis is uncapped but a cap value was also extracted "
            f"(multiple={multiple}, amount={amount}). Liability is either limited "
            "or it is not.",
            field_name="cap_basis",
        )

    # "Not specified" and a concrete cap value are mutually exclusive statements. The
    # value is the more specific evidence, so the basis is what is wrong - but which
    # basis it should be is a guess, so this rejects rather than corrects. Left
    # unresolved, the cap dropdown would show "not specified" for a contract that
    # plainly states a cap.
    if basis in (None, LiabilityCapBasis.NOT_SPECIFIED.value) and not outcome.check(
        multiple is None and amount is None
    ):
        outcome.add(
            "cap_value_without_basis",
            f"cap_basis is {basis!r} but a cap value was extracted "
            f"(multiple={multiple}, amount={amount}). The basis must state how "
            "the cap is expressed.",
            field_name="cap_basis",
        )

    if basis == LiabilityCapBasis.FEES_PAID_IN_PERIOD.value and not outcome.check(
        attributes.get("cap_reference_period_months") is not None
    ):
        outcome.add(
            "cap_period_missing",
            "The cap is fees paid in a period, but the period length was not "
            "extracted, so the cap cannot be quantified.",
            field_name="cap_reference_period_months",
            severity="warning",
        )

    # --- carve-outs: the flag and the list must agree -------------------------
    carve_outs = attributes.get("carve_outs") or []
    carve_text = attributes.get("carve_out_text")
    has_carve_outs = attributes.get("has_carve_outs")

    if carve_outs and has_carve_outs is False:
        outcome.check(True)
        outcome.correct(
            "has_carve_outs",
            True,
            f"{len(carve_outs)} carve-out(s) were extracted, which contradicts the flag",
        )
    elif has_carve_outs and not carve_outs and not carve_text:
        if not outcome.check(False):
            outcome.add(
                "carve_outs_unspecified",
                "Carve-outs are flagged but none were identified. A carve-out is "
                "unlimited exposure, so it must be named to be assessable.",
                field_name="carve_outs",
                severity="warning",
            )
    else:
        outcome.check(True)


def check_indemnity(attributes: dict[str, Any], outcome: FieldOutcome) -> None:
    """Coherence of an indemnity's direction and symmetry."""
    posture = attributes.get("posture")
    is_mutual = attributes.get("is_mutual")

    one_sided = {
        IndemnityPosture.ONE_SIDED_IN_OUR_FAVOUR.value,
        IndemnityPosture.ONE_SIDED_AGAINST_US.value,
    }

    if is_mutual is True and posture in one_sided:
        if not outcome.check(False):
            outcome.add(
                "indemnity_posture_conflict",
                f"The indemnity is marked mutual but its posture is {posture}. An "
                "indemnity cannot be both reciprocal and one-sided.",
                field_name="posture",
            )
    elif is_mutual is False and posture == IndemnityPosture.MUTUAL.value:
        if not outcome.check(False):
            outcome.add(
                "indemnity_posture_conflict",
                "The posture is mutual but is_mutual is false.",
                field_name="is_mutual",
            )
    else:
        outcome.check(True)

    if posture == IndemnityPosture.MUTUAL.value and is_mutual is None:
        outcome.correct("is_mutual", True, "implied by a mutual posture")


def check_party_sides(
    attributes: dict[str, Any], ctx: ValidationContext, outcome: FieldOutcome
) -> None:
    """Verify each ``*_side`` value against the party it names.

    The model is told which names are ours, but the mapping is exactly the sort of
    instruction a model applies inconsistently across twenty-three separate calls.
    Resolving it from the party name is deterministic, so where the two disagree the
    name wins.
    """
    sides = {key: value for key, value in attributes.items() if key.endswith("_side")}
    for key, stated in sides.items():
        stem = key[: -len("_side")]
        # cap_applies_to -> no name; indemnifying_side -> indemnifying_party
        name = attributes.get(f"{stem}_party") or attributes.get(f"{stem}_name")
        if not isinstance(name, str) or not name.strip():
            continue
        resolved = ctx.side_of(name)
        if resolved is PartySide.UNKNOWN:
            continue
        if stated in (None, PartySide.UNKNOWN.value):
            outcome.check(True)
            outcome.correct(key, resolved.value, f"resolved from the party name {name!r}")
        elif not outcome.check(stated == resolved.value):
            outcome.add(
                "party_side_conflict",
                f"{key} is {stated!r} but {name!r} resolves to {resolved.value}. "
                "Corrected from the party name, which is deterministic.",
                field_name=key,
                severity="warning",
            )
            outcome.corrections[key] = resolved.value


def check_termination_periods(attributes: dict[str, Any], outcome: FieldOutcome) -> None:
    """Notice and cure periods must be usable together."""
    notice = attributes.get("notice_days")
    cure = attributes.get("cure_period_days")
    if (
        isinstance(notice, (int, float))
        and isinstance(cure, (int, float))
        and not outcome.check(cure <= notice or notice == 0)
    ):
        outcome.add(
            "cure_exceeds_notice",
            f"The cure period ({cure} days) is longer than the notice period "
            f"({notice} days), so the breach could never be cured in time. "
            "Unusual drafting - verify against the clause.",
            field_name="cure_period_days",
            severity="warning",
        )

    # A clause that says breach is not curable but also states a cure period is
    # self-contradictory, and the cure period is the more specific statement.
    if (
        attributes.get("is_curable") is False
        and isinstance(cure, (int, float))
        and cure > 0
        and not outcome.check(False)
    ):
        outcome.add(
            "cure_period_contradicts_incurable",
            f"The clause is marked not curable but states a {cure:g}-day cure "
            "period. Verify which the wording actually provides.",
            field_name="is_curable",
            severity="warning",
        )


def check_date_order(facts: dict[str, Any], outcome: FieldOutcome, ctx: ValidationContext) -> None:
    """Document-level date sanity."""
    if not ctx.rule("dates.effective_before_expiration", True):
        return

    effective = parse_date(facts.get("effective_date"))
    expiration = parse_date(facts.get("expiration_date"))
    execution = parse_date(facts.get("execution_date"))

    if effective and expiration and not outcome.check(effective <= expiration):
        outcome.add(
            "effective_after_expiration",
            f"The effective date ({effective}) is after the expiration date "
            f"({expiration}). One of the two was misread.",
            field_name="effective_date",
        )
    if (
        execution
        and effective
        and ctx.rule("dates.execution_not_after_effective", False)
        and not outcome.check(execution <= effective)
    ):
        outcome.add(
            "execution_after_effective",
            f"The execution date ({execution}) is after the effective date ({effective}).",
            field_name="execution_date",
            severity="warning",
        )


#: Clause-specific rules, keyed by Clause Master key. Each takes
#: ``(attributes, outcome)``. A clause type an administrator adds at runtime has no
#: entry here and gets the convention-driven generic rules, which is why extraction
#: keeps working for it without a code change.
ClauseRule = Callable[[dict[str, Any], FieldOutcome], None]

_CLAUSE_RULES: dict[str, tuple[ClauseRule, ...]] = {
    ClauseType.LIMITATION_OF_LIABILITY.value: (check_liability_cap,),
    ClauseType.INDEMNIFICATION.value: (check_indemnity,),
    ClauseType.TERMINATION_FOR_CONVENIENCE.value: (check_termination_periods,),
    ClauseType.TERMINATION_FOR_CAUSE.value: (check_termination_periods,),
}


def validate_clause_attributes(
    *,
    clause_key: str,
    attributes: dict[str, Any],
    schema: dict[str, Any],
    ctx: ValidationContext,
) -> FieldOutcome:
    """Full validation of one clause's attributes.

    Returns the outcome; the caller applies ``corrections`` and decides whether the
    errors are fatal for that clause.
    """
    outcome = FieldOutcome()

    for issue in validate_against_schema(attributes, schema, path="attributes"):
        outcome.check(False)
        outcome.issues.append(issue)

    check_generic_attributes(attributes, ctx, outcome)
    # Needs the context (the organisation aliases), so it is not in _CLAUSE_RULES.
    check_party_sides(attributes, ctx, outcome)

    for rule in _CLAUSE_RULES.get(clause_key, ()):
        try:
            rule(attributes, outcome)
        except Exception as exc:  # noqa: BLE001
            # A bug in one rule must not fail the extraction: record it and carry on,
            # because losing the clause is worse than losing one check.
            logger.warning(
                "clause_rule_failed", clause_key=clause_key, rule=rule.__name__, error=str(exc)
            )
            outcome.add(
                "rule_error",
                f"A validation rule failed to run: {rule.__name__}.",
                severity="warning",
            )

    return outcome


__all__ = [
    "ClauseRule",
    "FieldOutcome",
    "ValidationContext",
    "check_citations",
    "check_confidence",
    "check_date_order",
    "check_generic_attributes",
    "check_indemnity",
    "check_liability_cap",
    "check_party_sides",
    "check_quote",
    "check_termination_periods",
    "normalise_party_name",
    "parse_date",
    "validate_against_schema",
    "validate_clause_attributes",
]

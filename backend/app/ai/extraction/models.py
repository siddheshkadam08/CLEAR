"""Extraction result types (§13).

Every extracted fact carries its evidence. That is not decoration: an attribute
without a page reference and a bounding box cannot be shown to a reviewer, cannot
be defended in a negotiation, and cannot be distinguished from a hallucination.
:class:`EvidenceRef` therefore travels with each item rather than being attached
afterwards, and the validator rejects any item that arrives without one.

Nothing here talks to the database or to a provider. The engine produces these
objects; the stage handler persists them. Keeping them separate is what lets the
extraction engine be tested against fixture documents with no infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.core.enums import (
    DateType,
    EntityType,
    ObligationStatus,
    ReviewStatus,
    RiskBand,
    RiskSeverity,
    RiskType,
)


@dataclass(slots=True)
class EvidenceRef:
    """Where an extracted fact came from.

    ``chunk_id`` is the database id of the chunk, so the UI can resolve the
    highlight without re-deriving anything, and ``bounding_boxes`` is copied rather
    than referenced so the evidence survives a re-chunk that changes ids.
    """

    chunk_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    bounding_boxes: list[dict[str, Any]] = field(default_factory=list)
    section_id: str | None = None
    section_title: str | None = None
    clause_number: str | None = None
    #: The exact quoted span, kept so a citation can be verified without a DB read.
    quote: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "bounding_boxes": self.bounding_boxes,
            "section_id": self.section_id,
            "section_title": self.section_title,
            "clause_number": self.clause_number,
            "quote": self.quote,
        }


@dataclass(slots=True)
class ValidationIssue:
    """One validation finding against an extracted item."""

    code: str
    message: str
    field: str | None = None
    #: ``error`` blocks acceptance; ``warning`` records a concern and lets it pass.
    severity: str = "error"

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "severity": self.severity,
        }


@dataclass(slots=True)
class ExtractedItem:
    """Shared provenance for everything the extraction engine produces."""

    confidence: float = 0.0
    evidence: list[EvidenceRef] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    #: 0..1 - the fraction of validation rules this item satisfied.
    validation_score: float = 1.0
    review_status: ReviewStatus = ReviewStatus.NOT_REQUIRED
    prompt_id: str | None = None
    prompt_version: str | None = None
    model_version: str | None = None

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.is_error]

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def primary_evidence(self) -> EvidenceRef | None:
        return self.evidence[0] if self.evidence else None

    def provenance(self) -> dict[str, Any]:
        return {
            "confidence": round(self.confidence, 4),
            "validation_score": round(self.validation_score, 4),
            "review_status": self.review_status.value,
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "model_version": self.model_version,
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(slots=True)
class ExtractedClause(ExtractedItem):
    """One clause of one category, with its typed attributes."""

    clause_type: str = ""
    title: str | None = None
    text: str = ""
    summary: str | None = None
    clause_number: str | None = None
    section_id: str | None = None
    section_title: str | None = None
    #: Validated against the category's ``output_schema``, so this is queryable
    #: structured data rather than free-form JSON.
    attributes: dict[str, Any] = field(default_factory=dict)
    is_mandatory: bool = False
    is_risk_flagged: bool = False
    deviation_score: float | None = None
    #: The chunk this clause was primarily read from.
    chunk_id: str | None = None


@dataclass(slots=True)
class ExtractedParty(ExtractedItem):
    """A contracting party or other named entity."""

    name: str = ""
    entity_type: EntityType = EntityType.PARTY
    legal_name: str | None = None
    aliases: list[str] = field(default_factory=list)
    role: str | None = None
    jurisdiction: str | None = None
    registration_number: str | None = None
    address: str | None = None
    contact: dict[str, Any] = field(default_factory=dict)
    is_primary: bool = False
    #: True when this party matched a configured organisation alias - the basis for
    #: resolving every "can we terminate?" style attribute.
    is_our_organisation: bool = False


@dataclass(slots=True)
class ExtractedObligation(ExtractedItem):
    """A duty one party owes."""

    action: str = ""
    responsible_party: str | None = None
    due_date: date | None = None
    due_description: str | None = None
    trigger_event: str | None = None
    dependency: str | None = None
    frequency: str | None = None
    is_recurring: bool = False
    status: ObligationStatus = ObligationStatus.OPEN
    penalty: str | None = None
    clause_type: str | None = None
    chunk_id: str | None = None


@dataclass(slots=True)
class ExtractedRisk(ExtractedItem):
    """A risk finding, with the score it contributes."""

    risk_type: str = RiskType.OTHER.value
    severity: RiskSeverity = RiskSeverity.MEDIUM
    description: str = ""
    recommendation: str | None = None
    category: str | None = None
    score_contribution: int = 0
    #: True when the risk is the *absence* of something - a missing mandatory
    #: clause has no clause text and no coordinates, and must not be filtered out
    #: by an evidence check that assumes there is a quote to point at.
    is_omission: bool = False
    clause_type: str | None = None
    chunk_id: str | None = None


@dataclass(slots=True)
class ExtractedKeyDate(ExtractedItem):
    """A date or a date expression that matters commercially."""

    date_type: DateType = DateType.OTHER
    date_value: date | None = None
    #: Kept when the contract expresses a date relatively ("30 days after
    #: execution"). Dropping it in favour of a computed date would lose the term.
    date_expression: str | None = None
    description: str | None = None
    is_recurring: bool = False
    chunk_id: str | None = None


@dataclass(slots=True)
class ExtractedRelationship(ExtractedItem):
    """An edge for the knowledge graph."""

    relation: str = ""
    source_type: str = ""
    source_ref: str = ""
    target_type: str = ""
    target_ref: str = ""
    label: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ContractFacts:
    """Document-level facts, as they land on ``contract_metadata``."""

    title: str | None = None
    summary: str | None = None
    executive_summary: str | None = None
    key_topics: list[str] = field(default_factory=list)
    party_a: str | None = None
    party_b: str | None = None
    contract_value: float | None = None
    currency: str | None = None
    effective_date: date | None = None
    execution_date: date | None = None
    expiration_date: date | None = None
    term_months: int | None = None
    auto_renewal: bool | None = None
    renewal_notice_days: int | None = None
    payment_terms_days: int | None = None
    governing_law: str | None = None
    jurisdiction: str | None = None
    dispute_resolution: str | None = None
    confidence: float = 0.0
    issues: list[ValidationIssue] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "executive_summary": self.executive_summary,
            "key_topics": self.key_topics,
            "party_a": self.party_a,
            "party_b": self.party_b,
            "contract_value": self.contract_value,
            "currency": self.currency,
            "effective_date": self.effective_date.isoformat() if self.effective_date else None,
            "execution_date": self.execution_date.isoformat() if self.execution_date else None,
            "expiration_date": self.expiration_date.isoformat() if self.expiration_date else None,
            "term_months": self.term_months,
            "auto_renewal": self.auto_renewal,
            "renewal_notice_days": self.renewal_notice_days,
            "payment_terms_days": self.payment_terms_days,
            "governing_law": self.governing_law,
            "jurisdiction": self.jurisdiction,
            "dispute_resolution": self.dispute_resolution,
            "confidence": round(self.confidence, 4),
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(slots=True)
class RiskAssessment:
    """The 0-100 score, its band, and how it was arrived at.

    ``breakdown`` exists so the score is explainable (§0 principle 12): a number a
    reviewer cannot decompose into named findings is not defensible in a
    negotiation.
    """

    score: int = 0
    band: RiskBand = RiskBand.LOW
    risks: list[ExtractedRisk] = field(default_factory=list)
    missing_mandatory: list[str] = field(default_factory=list)
    breakdown: list[dict[str, Any]] = field(default_factory=list)
    has_unlimited_liability: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "band": self.band.value,
            "risk_count": len(self.risks),
            "by_severity": self.counts_by_severity(),
            "missing_mandatory_clauses": self.missing_mandatory,
            "has_unlimited_liability": self.has_unlimited_liability,
            "breakdown": self.breakdown,
        }

    def counts_by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for risk in self.risks:
            counts[risk.severity.value] = counts.get(risk.severity.value, 0) + 1
        return counts


@dataclass(slots=True)
class CategoryOutcome:
    """What one extraction call produced, including its cost.

    Recorded per category so a partially failed job can report exactly which
    categories succeeded, and so cost is attributable to the work that incurred it
    (§17) rather than smeared across the job.
    """

    category: str
    prompt_id: str
    status: str = "ok"  # ok | skipped | not_found | invalid | refused | error
    item_count: int = 0
    evidence_chunks: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    #: Tokens of retrieved evidence in this call's prompt - the part that is
    #: genuinely about *this* document. `evidence_chunks` counts passages; this
    #: sizes them.
    evidence_tokens: int = 0
    #: `input_tokens - evidence_tokens`: system prompt, schema, synonyms,
    #: standard text and party context. Identical on every clause call for a
    #: given agreement type, so it is re-sent once per category per document.
    #:
    #: Measured, not assumed. This is the number that decides whether batching
    #: clause calls is worth doing: if scaffolding is a small fraction of the
    #: prompt there is nothing to win, and if it dominates, N calls are paying
    #: for the same tokens N times.
    repeated_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str | None = None
    detail: str | None = None
    issues: list[ValidationIssue] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "prompt_id": self.prompt_id,
            "status": self.status,
            "items": self.item_count,
            "evidence_chunks": self.evidence_chunks,
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "evidence_tokens": self.evidence_tokens,
            "repeated_tokens": self.repeated_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "latency_ms": self.latency_ms,
            "model": self.model,
            "detail": self.detail,
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(slots=True)
class ExtractionResult:
    """Everything one extraction run produced."""

    facts: ContractFacts = field(default_factory=ContractFacts)
    clauses: list[ExtractedClause] = field(default_factory=list)
    parties: list[ExtractedParty] = field(default_factory=list)
    obligations: list[ExtractedObligation] = field(default_factory=list)
    risks: list[ExtractedRisk] = field(default_factory=list)
    key_dates: list[ExtractedKeyDate] = field(default_factory=list)
    relationships: list[ExtractedRelationship] = field(default_factory=list)
    assessment: RiskAssessment = field(default_factory=RiskAssessment)
    outcomes: list[CategoryOutcome] = field(default_factory=list)
    #: Set when any item tripped a review trigger (§13).
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- accounting
    @property
    def total_cost_usd(self) -> float:
        return round(sum(outcome.cost_usd for outcome in self.outcomes), 6)

    @property
    def total_tokens(self) -> int:
        return sum(
            outcome.input_tokens + outcome.output_tokens + outcome.cache_read_tokens
            for outcome in self.outcomes
        )

    @property
    def llm_calls(self) -> int:
        return sum(outcome.llm_calls for outcome in self.outcomes)

    @property
    def failed_categories(self) -> list[str]:
        return [o.category for o in self.outcomes if o.status in {"error", "refused", "invalid"}]

    def clause_by_type(self, clause_type: str) -> ExtractedClause | None:
        """The highest-confidence clause of a type.

        A contract can state the same term twice - a cap in the body and a
        restatement in an addendum. The most confident extraction is the one the
        document-level facts should use.
        """
        candidates = [c for c in self.clauses if c.clause_type == clause_type]
        if not candidates:
            return None
        return max(candidates, key=lambda c: c.confidence)

    def clause_types_found(self) -> set[str]:
        return {clause.clause_type for clause in self.clauses}

    def statistics(self) -> dict[str, Any]:
        return {
            "clauses": len(self.clauses),
            "clause_types": len(self.clause_types_found()),
            "parties": len(self.parties),
            "obligations": len(self.obligations),
            "risks": len(self.risks),
            "key_dates": len(self.key_dates),
            "relationships": len(self.relationships),
            "risk_score": self.assessment.score,
            "risk_band": self.assessment.band.value,
            "missing_mandatory_clauses": len(self.assessment.missing_mandatory),
            "llm_calls": self.llm_calls,
            "total_tokens": self.total_tokens,
            "cost_usd": self.total_cost_usd,
            "needs_review": self.needs_review,
            "failed_categories": self.failed_categories,
        }


__all__ = [
    "CategoryOutcome",
    "ContractFacts",
    "EvidenceRef",
    "ExtractedClause",
    "ExtractedItem",
    "ExtractedKeyDate",
    "ExtractedObligation",
    "ExtractedParty",
    "ExtractedRelationship",
    "ExtractedRisk",
    "ExtractionResult",
    "RiskAssessment",
    "ValidationIssue",
]

"""Extracted-knowledge schemas: clauses, entities, obligations, risks, dates.

Every response here carries its evidence. That is not decoration - an extracted term
without a page reference and a bounding box cannot be shown next to its source, and
the platform's promise is that it always can be (§0 explainable AI).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import Field

from app.core.enums import (
    DateType,
    EntityType,
    ObligationStatus,
    ReviewStatus,
    RiskBand,
    RiskSeverity,
)
from app.schemas.common import BaseSchema, BoundingBox, ProvenanceInfo, ResponseSchema


class ClauseResponse(ResponseSchema):
    """One extracted clause with its typed attributes and evidence."""

    id: uuid.UUID
    contract_id: uuid.UUID
    clause_type: str
    title: str | None = None
    text: str
    summary: str | None = None
    clause_number: str | None = None
    section_title: str | None = None
    #: Validated against the Clause Master's ``output_schema``, so this is queryable
    #: structured data rather than free-form JSON.
    attributes: dict[str, Any] = Field(default_factory=dict)
    is_mandatory: bool = False
    is_risk_flagged: bool = False
    deviation_score: float | None = None

    page_start: int | None = None
    page_end: int | None = None
    bounding_boxes: list[BoundingBox] = Field(default_factory=list)
    chunk_id: uuid.UUID | None = None
    provenance: ProvenanceInfo | None = None
    review_status: ReviewStatus = ReviewStatus.NOT_REQUIRED
    #: Validation findings, including anything the validator corrected. A corrected
    #: value is never silently different from what the model returned.
    issues: list[dict[str, Any]] = Field(default_factory=list)


class ClauseTabGroup(ResponseSchema):
    """A clause category rendered as its own tab in the contract view.

    Driven by ``clause_master.ui_config.placement == 'dedicated_tab'``, so promoting
    a clause to its own tab is an administrator edit rather than a frontend change.
    """

    key: str
    label: str
    priority: int
    #: Fields the tab shows prominently, in order.
    primary_fields: list[str] = Field(default_factory=list)
    #: For an enumerated field rendered as a dropdown - the liability cap basis.
    dropdown: dict[str, Any] | None = None
    #: Attribute conditions that mark a clause for attention, e.g. an uncapped cap.
    highlight_when: dict[str, Any] = Field(default_factory=dict)
    clauses: list[ClauseResponse] = Field(default_factory=list)
    #: True when the category is mandatory for this document type and absent.
    is_missing: bool = False


class EntityResponse(ResponseSchema):
    id: uuid.UUID
    contract_id: uuid.UUID
    entity_type: EntityType
    name: str
    legal_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    role: str | None = None
    jurisdiction: str | None = None
    registration_number: str | None = None
    address: str | None = None
    contact: dict[str, Any] = Field(default_factory=dict)
    is_primary: bool = False
    page_start: int | None = None
    bounding_boxes: list[BoundingBox] = Field(default_factory=list)
    provenance: ProvenanceInfo | None = None


class ObligationResponse(ResponseSchema):
    id: uuid.UUID
    contract_id: uuid.UUID
    clause_id: uuid.UUID | None = None
    action: str
    responsible_party: str | None = None
    due_date: date | None = None
    #: Kept when the contract expresses the deadline relatively ("within 30 days of
    #: invoice"). That wording *is* the obligation; a computed date would lose it.
    due_description: str | None = None
    trigger_event: str | None = None
    frequency: str | None = None
    is_recurring: bool = False
    status: ObligationStatus = ObligationStatus.OPEN
    penalty: str | None = None
    page_start: int | None = None
    bounding_boxes: list[BoundingBox] = Field(default_factory=list)
    provenance: ProvenanceInfo | None = None


class RiskResponse(ResponseSchema):
    id: uuid.UUID
    contract_id: uuid.UUID
    clause_id: uuid.UUID | None = None
    risk_type: str
    severity: RiskSeverity
    description: str
    recommendation: str | None = None
    category: str | None = None
    score_contribution: int | None = None
    #: True when the risk is the *absence* of something. An omission has no clause
    #: text and no coordinates by definition, so the UI must not expect them.
    is_omission: bool = False
    page_start: int | None = None
    bounding_boxes: list[BoundingBox] = Field(default_factory=list)
    provenance: ProvenanceInfo | None = None


class KeyDateResponse(ResponseSchema):
    id: uuid.UUID
    contract_id: uuid.UUID
    date_type: DateType
    date_value: date | None = None
    date_expression: str | None = None
    description: str | None = None
    is_recurring: bool = False
    page_start: int | None = None
    bounding_boxes: list[BoundingBox] = Field(default_factory=list)


class RiskAssessmentResponse(ResponseSchema):
    """The 0-100 score and the findings behind it.

    ``breakdown`` is what makes the number defensible: a score a reviewer cannot
    decompose into named findings is not usable in a negotiation.
    """

    score: int = 0
    band: RiskBand = RiskBand.LOW
    by_severity: dict[str, int] = Field(default_factory=dict)
    missing_mandatory_clauses: list[str] = Field(default_factory=list)
    has_unlimited_liability: bool = False
    breakdown: list[dict[str, Any]] = Field(default_factory=list)
    risks: list[RiskResponse] = Field(default_factory=list)


class ContractKnowledgeResponse(ResponseSchema):
    """Everything extracted from one contract, as the detail screen needs it."""

    contract_id: uuid.UUID
    clause_count: int = 0
    #: Clause categories with a dedicated tab, in Clause Master priority order.
    tabs: list[ClauseTabGroup] = Field(default_factory=list)
    #: Everything else, listed.
    clauses: list[ClauseResponse] = Field(default_factory=list)
    parties: list[EntityResponse] = Field(default_factory=list)
    obligations: list[ObligationResponse] = Field(default_factory=list)
    key_dates: list[KeyDateResponse] = Field(default_factory=list)
    assessment: RiskAssessmentResponse = Field(default_factory=RiskAssessmentResponse)
    summary: str | None = None
    key_topics: list[str] = Field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)


class ClauseReviewRequest(BaseSchema):
    """A reviewer's verdict on an extracted clause (§13 human review)."""

    review_status: ReviewStatus = Field(description="approved, rejected, or corrected.")
    #: Corrected attribute values. Recorded as a correction, not an overwrite: the
    #: model's original output stays in the provenance so the two can be compared.
    attributes: dict[str, Any] | None = None
    text: str | None = None
    note: str | None = None


class EvidenceResponse(ResponseSchema):
    """Resolved evidence for one citation, for the viewer overlay."""

    contract_id: uuid.UUID
    chunk_id: uuid.UUID | None = None
    text: str
    page_start: int | None = None
    page_end: int | None = None
    bounding_boxes: list[BoundingBox] = Field(default_factory=list)
    clause_number: str | None = None
    section_title: str | None = None
    #: Signed URL for the source document, so the viewer can render the page.
    document_url: str | None = None
    expires_at: datetime | None = None


__all__ = [
    "ClauseResponse",
    "ClauseReviewRequest",
    "ClauseTabGroup",
    "ContractKnowledgeResponse",
    "EntityResponse",
    "EvidenceResponse",
    "KeyDateResponse",
    "ObligationResponse",
    "RiskAssessmentResponse",
    "RiskResponse",
]

"""Search and Copilot schemas.

The search response deliberately exposes *how* the answer was reached - the detected
intent, the strategy, the filters the planner inferred. A user who asked "which
contracts expire next quarter" and got nothing needs to see that it resolved to a
date range, not guess why similarity failed them.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import Field, field_validator

from app.core.enums import (
    ConfidenceBand,
    QueryIntent,
    ResponseFormat,
    RetrievalStrategy,
    SearchMode,
    SearchScope,
)
from app.schemas.common import BaseSchema, BoundingBox, ResponseSchema


class SearchRequest(BaseSchema):
    """A search or question."""

    query: str = Field(min_length=1, max_length=2000)
    scope: SearchScope = SearchScope.PROJECT
    mode: SearchMode = SearchMode.HYBRID
    #: Narrow to one project. Omitted means every project the caller belongs to -
    #: never the whole table (§1.1).
    project_id: uuid.UUID | None = None
    contract_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    agreement_types: list[str] = Field(default_factory=list, max_length=20)
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("The query cannot be empty.")
        return cleaned


class SearchHit(ResponseSchema):
    """One retrieved passage."""

    level: str
    ref_id: uuid.UUID
    contract_id: uuid.UUID
    contract_title: str | None = None
    text: str
    score: float
    #: How it was found: vector, keyword, fused, neighbour, graph. Exposed because
    #: "why is this in my results" is a fair question.
    source: str
    rank: int
    clause_type: str | None = None
    clause_number: str | None = None
    section_title: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    bounding_boxes: list[BoundingBox] = Field(default_factory=list)
    chunk_id: uuid.UUID | None = None


class ContractMatch(ResponseSchema):
    """A contract matched by the metadata pre-filter."""

    contract_id: uuid.UUID
    title: str | None = None
    agreement_type: str | None = None
    risk_score: int | None = None
    risk_band: str | None = None
    effective_date: date | None = None
    expiration_date: date | None = None
    contract_value: float | None = None
    currency: str | None = None
    party_a: str | None = None
    party_b: str | None = None
    has_unlimited_liability: bool | None = None
    missing_mandatory_clauses: list[str] = Field(default_factory=list)


class PlanExplanation(ResponseSchema):
    """How the planner interpreted the question.

    Surfaced so an empty or surprising result set is explainable rather than
    mysterious - the single most common support question about a search box.
    """

    intent: QueryIntent
    strategy: RetrievalStrategy
    scope: SearchScope
    mode: SearchMode
    filters: dict[str, Any] = Field(default_factory=dict)
    reasoning: list[str] = Field(default_factory=list)
    levels: list[dict[str, Any]] = Field(default_factory=list)


class SearchResponse(ResponseSchema):
    """Search results plus the plan that produced them."""

    query: str
    hits: list[SearchHit] = Field(default_factory=list)
    contracts: list[ContractMatch] = Field(default_factory=list)
    total_hits: int = 0
    plan: PlanExplanation
    duration_ms: int = 0
    warnings: list[str] = Field(default_factory=list)


class CitationResponse(ResponseSchema):
    """A numbered citation the answer references and the viewer can resolve."""

    label: int
    contract_id: uuid.UUID
    contract_title: str | None = None
    level: str
    ref_id: uuid.UUID
    text: str
    clause_type: str | None = None
    clause_number: str | None = None
    section_title: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    page_range: str = ""
    bounding_boxes: list[BoundingBox] = Field(default_factory=list)
    chunk_id: uuid.UUID | None = None
    score: float = 0.0


class AskRequest(SearchRequest):
    """A question for the Copilot."""

    response_format: ResponseFormat | None = Field(
        default=None,
        description="Override the format. Omitted means the planner's intent decides.",
    )
    session_id: uuid.UUID | None = Field(
        default=None,
        description="Continue an existing conversation, so pronouns resolve.",
    )
    stream: bool = False


class AnswerResponse(ResponseSchema):
    """A grounded answer."""

    answer: str
    citations: list[CitationResponse] = Field(default_factory=list)
    confidence: float = 0.0
    confidence_band: ConfidenceBand = ConfidenceBand.LOW
    response_format: ResponseFormat = ResponseFormat.NATURAL_LANGUAGE
    #: True when the model declined. A first-class outcome, not an error - contract
    #: language sits close enough to safety categories that false positives happen.
    refused: bool = False
    #: True when the answer should be checked before being relied on: a fabricated
    #: citation, no citations at all, or low grounding confidence.
    needs_review: bool = False
    warnings: list[str] = Field(default_factory=list)
    plan: PlanExplanation | None = None
    session_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None
    model: str | None = None
    duration_ms: int = 0
    #: Token cost, so a user or an admin can see what a question cost (§17).
    tokens: int = 0
    cost_usd: float = 0.0


class ChatMessageResponse(ResponseSchema):
    id: uuid.UUID
    role: str
    content: str
    created_at: datetime
    citations: list[CitationResponse] = Field(default_factory=list)
    confidence: float | None = None
    needs_review: bool = False


class ChatSessionResponse(ResponseSchema):
    id: uuid.UUID
    project_id: uuid.UUID | None = None
    contract_id: uuid.UUID | None = None
    title: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    message_count: int = 0
    messages: list[ChatMessageResponse] = Field(default_factory=list)


class ChatSessionCreate(BaseSchema):
    project_id: uuid.UUID | None = None
    contract_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=255)


__all__ = [
    "AnswerResponse",
    "AskRequest",
    "ChatMessageResponse",
    "ChatSessionCreate",
    "ChatSessionResponse",
    "CitationResponse",
    "ContractMatch",
    "PlanExplanation",
    "SearchHit",
    "SearchRequest",
    "SearchResponse",
]

"""Shared API contracts: base models, pagination, errors, evidence, references.

Every response model inherits :class:`BaseSchema`, which is configured once here
so serialisation is uniform: ORM objects convert directly, enums serialise to
their values, and ``Decimal`` becomes a JSON number rather than a string.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_serializer, model_validator

T = TypeVar("T")

#: Standard pagination bounds. The upper limit is a guard against a client asking
#: for a million rows in one request.
Page = Annotated[int, Field(ge=1, le=100_000, description="1-based page number")]
PageSize = Annotated[int, Field(ge=1, le=200, description="Items per page")]


class BaseSchema(BaseModel):
    """Base for every request/response model."""

    model_config = ConfigDict(
        from_attributes=True,  # construct straight from ORM objects
        use_enum_values=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        ser_json_timedelta="float",
        extra="forbid",  # reject unexpected fields rather than ignoring them
    )

    @field_serializer("*", when_used="json", check_fields=False)
    def _serialize_decimal(self, value: Any) -> Any:
        # Decimal is used for confidence, scores and money. Serialising as a JSON
        # number keeps the frontend from having to parse strings.
        if isinstance(value, Decimal):
            return float(value)
        return value


class ResponseSchema(BaseSchema):
    """Base for responses - allows extra fields so adding one is not breaking."""

    model_config = ConfigDict(
        from_attributes=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="ignore",
    )


# =============================================================================
# Errors
# =============================================================================
class ErrorDetail(ResponseSchema):
    code: str = Field(description="Stable machine-readable error code")
    message: str = Field(description="Human-readable message, safe to display")
    details: dict[str, Any] = Field(default_factory=dict)
    trace_id: str = Field(description="Correlates this response with logs and traces")


class ErrorResponse(ResponseSchema):
    """The single error envelope every endpoint returns (§19)."""

    error: ErrorDetail


class FieldError(ResponseSchema):
    field: str
    message: str
    type: str = ""


# =============================================================================
# Pagination
# =============================================================================
class PaginationParams(BaseSchema):
    """Offset pagination query parameters."""

    page: Page = 1
    size: PageSize = 25

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size

    @property
    def limit(self) -> int:
        return self.size


class SortParams(BaseSchema):
    """Sorting query parameters.

    ``sort_by`` is validated against an allow-list per endpoint before reaching
    SQL - a user-supplied column name must never be interpolated into a query.
    """

    sort_by: str | None = None
    sort_dir: str = Field(default="desc", pattern="^(asc|desc)$")


class PageMeta(ResponseSchema):
    page: int
    size: int
    total: int
    pages: int
    has_next: bool
    has_prev: bool

    @classmethod
    def build(cls, *, page: int, size: int, total: int) -> Self:
        pages = (total + size - 1) // size if size else 0
        return cls(
            page=page,
            size=size,
            total=total,
            pages=pages,
            has_next=page < pages,
            has_prev=page > 1,
        )


class Paginated(ResponseSchema, Generic[T]):
    """Envelope for every list endpoint."""

    items: list[T]
    meta: PageMeta

    @classmethod
    def build(cls, items: list[T], *, page: int, size: int, total: int) -> Self:
        return cls(items=items, meta=PageMeta.build(page=page, size=size, total=total))


class CursorPage(ResponseSchema, Generic[T]):
    """Cursor pagination, used where deep offsets would be expensive (chunks, audit)."""

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


# =============================================================================
# Evidence & citations - the explainability contract
# =============================================================================
class BoundingBox(ResponseSchema):
    """A highlight rectangle on a page.

    Coordinates are in the PDF's own coordinate space as reported by the parser;
    the viewer scales them to the rendered viewport. Kept as raw page units
    rather than normalised fractions so re-rendering at a different zoom cannot
    accumulate rounding drift.
    """

    page_number: int = Field(ge=1)
    x: float
    y: float
    width: float
    height: float
    #: Page dimensions the coordinates were captured against, so the viewer can
    #: scale correctly even if it renders at a different size.
    page_width: float | None = None
    page_height: float | None = None

    @model_validator(mode="after")
    def _validate_geometry(self) -> Self:
        if self.width < 0 or self.height < 0:
            raise ValueError("bounding box width and height must be non-negative")
        return self


class Evidence(ResponseSchema):
    """Provenance for one extracted fact (FR-3).

    Returned by ``GET /clauses/{id}/evidence`` and embedded in every extraction
    response. Enough for the viewer to open the right page and draw the highlight
    without a second round trip.
    """

    contract_id: uuid.UUID | None = None
    contract_title: str | None = None
    chunk_id: uuid.UUID | None = None
    clause_id: uuid.UUID | None = None
    section_id: str | None = None
    section_title: str | None = None
    paragraph_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    bounding_boxes: list[BoundingBox] = Field(default_factory=list)
    #: The exact source text, so a reviewer can verify the extraction verbatim.
    text: str | None = None
    snippet: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    artifact_version: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def page_range(self) -> str | None:
        if self.page_start is None:
            return None
        if self.page_end is None or self.page_end == self.page_start:
            return str(self.page_start)
        return f"{self.page_start}-{self.page_end}"


class Citation(Evidence):
    """A citation attached to an AI answer segment (§17).

    Extends :class:`Evidence` with the answer-side link, so the UI can map a
    sentence in the answer back to the clause that supports it.
    """

    #: Index of the answer segment this citation supports.
    segment_index: int | None = None
    #: Short label rendered inline, e.g. "[1]" or "MSA §11.2".
    label: str | None = None
    relevance_score: float | None = None


class ProvenanceInfo(ResponseSchema):
    """How an AI-extracted record was produced (§25).

    Mirrors ``ExtractionProvenanceMixin``: the version set makes the extraction
    reproducible, and the scores tell a reviewer how much to trust it. Both halves
    matter - a value without its confidence is presented as more certain than it is.
    """

    #: 0..1 model confidence in this specific extraction.
    confidence: float | None = None
    #: 0..1 share of validation rules the record satisfied.
    validation_score: float | None = None
    review_status: str | None = None
    profile_version: str | None = None
    prompt_version: str | None = None
    model_version: str | None = None
    artifact_version: str | None = None
    extraction_engine_version: str | None = None
    embedding_model: str | None = None


# =============================================================================
# Lightweight references
# =============================================================================
class UserRef(ResponseSchema):
    """Minimal user reference embedded in other responses."""

    id: uuid.UUID
    email: str
    full_name: str
    avatar_url: str | None = None


class ProjectRef(ResponseSchema):
    id: uuid.UUID
    name: str
    slug: str


class ContractRef(ResponseSchema):
    id: uuid.UUID
    title: str | None = None
    original_file_name: str
    contract_number: str | None = None
    agreement_type: str | None = None


class ClauseRef(ResponseSchema):
    id: uuid.UUID
    clause_type: str
    title: str | None = None
    page_start: int | None = None


# =============================================================================
# Generic operation results
# =============================================================================
class MessageResponse(ResponseSchema):
    message: str
    detail: str | None = None


class IdResponse(ResponseSchema):
    id: uuid.UUID
    message: str | None = None


class BulkResult(ResponseSchema):
    """Result of an operation over many items - partial success is expressible.

    A 100-file upload where three files are duplicates is neither a success nor a
    failure; the client needs the breakdown.
    """

    total: int
    succeeded: int
    failed: int
    skipped: int = 0
    errors: list[dict[str, Any]] = Field(default_factory=list)


class CountResponse(ResponseSchema):
    count: int


class HealthStatus(ResponseSchema):
    status: str = Field(description="ok | degraded | error")
    version: str
    environment: str
    checks: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime


# =============================================================================
# Filter primitives
# =============================================================================
class DateRange(BaseSchema):
    """Inclusive date range filter."""

    from_date: date | None = Field(default=None, alias="from")
    to_date: date | None = Field(default=None, alias="to")

    @model_validator(mode="after")
    def _validate_order(self) -> Self:
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("'from' date must not be after 'to' date")
        return self

    @property
    def is_set(self) -> bool:
        return self.from_date is not None or self.to_date is not None


class NumberRange(BaseSchema):
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _validate_order(self) -> Self:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("'min' must not be greater than 'max'")
        return self


class FacetValue(ResponseSchema):
    """One bucket in a search facet."""

    value: str
    label: str | None = None
    count: int


class Facets(ResponseSchema):
    """Aggregations returned alongside search results."""

    agreement_type: list[FacetValue] = Field(default_factory=list)
    clause_type: list[FacetValue] = Field(default_factory=list)
    risk_band: list[FacetValue] = Field(default_factory=list)
    vendor: list[FacetValue] = Field(default_factory=list)
    department: list[FacetValue] = Field(default_factory=list)
    governing_law: list[FacetValue] = Field(default_factory=list)
    status: list[FacetValue] = Field(default_factory=list)


__all__ = [
    "BaseSchema",
    "BoundingBox",
    "BulkResult",
    "Citation",
    "ClauseRef",
    "ContractRef",
    "CountResponse",
    "CursorPage",
    "DateRange",
    "ErrorDetail",
    "ErrorResponse",
    "Evidence",
    "FacetValue",
    "Facets",
    "FieldError",
    "HealthStatus",
    "IdResponse",
    "MessageResponse",
    "NumberRange",
    "Page",
    "PageMeta",
    "PageSize",
    "Paginated",
    "PaginationParams",
    "ProjectRef",
    "ProvenanceInfo",
    "ResponseSchema",
    "SortParams",
    "UserRef",
]

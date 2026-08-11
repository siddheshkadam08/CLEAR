"""Contract, upload and repository-filter contracts.

The upload response is the shape that makes the ``< 5s`` requirement honest: the
endpoint persists the file and creates a queued job per contract, then returns
immediately. Nothing is parsed, extracted or embedded inside the request.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import Field, model_validator

from app.core.enums import (
    AgreementType,
    ContractStatus,
    FileType,
    JobPriority,
    RiskBand,
)
from app.schemas.common import (
    BaseSchema,
    DateRange,
    NumberRange,
    ProjectRef,
    ResponseSchema,
    UserRef,
)


# =============================================================================
# Upload
# =============================================================================
class UploadOptions(BaseSchema):
    """Options sent alongside a multi-file upload (as a form field)."""

    priority: JobPriority = JobPriority.NORMAL
    #: Force a profile instead of letting classification choose. For a batch of
    #: known-identical documents this skips a wrong-profile risk entirely.
    profile_key: str | None = None
    #: Pre-set the agreement type when the uploader already knows it; classification
    #: still runs and disagreement is recorded rather than silently overridden.
    agreement_type: AgreementType | None = None
    tags: list[str] = Field(default_factory=list)
    #: Treat a matching SHA-256 as a new version of the existing contract instead of
    #: rejecting it as a duplicate.
    replace_existing: bool = False
    notes: str | None = None


class UploadedFileResult(ResponseSchema):
    """Per-file outcome. A batch can partially succeed."""

    file_name: str
    #: Set when the file was accepted.
    contract_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    #: ``expanded`` is the archive's own row: the ZIP is not a document, so it is
    #: never accepted or rejected on its own terms - it either yielded documents
    #: or it did not. Deliberately not counted in accepted/duplicates/rejected,
    #: which tally documents; the documents it produced are counted individually.
    status: str = Field(description="accepted | duplicate | rejected | expanded")
    size: int | None = None
    sha256: str | None = None
    #: Populated for ``duplicate`` so the client can link to the existing contract.
    existing_contract_id: uuid.UUID | None = None
    error_code: str | None = None
    message: str | None = None


class UploadResponse(ResponseSchema):
    """Result of a multi-file upload.

    Returns in under five seconds regardless of file count: the work happens on the
    queue, and the client polls the job ids.
    """

    project_id: uuid.UUID
    total: int
    accepted: int
    duplicates: int
    rejected: int
    files: list[UploadedFileResult]
    job_ids: list[uuid.UUID] = Field(default_factory=list)
    message: str | None = None


# =============================================================================
# Contract metadata
# =============================================================================
class ContractMetadataResponse(ResponseSchema):
    """Extracted business metadata - the filterable projection (§7.4)."""

    effective_date: date | None = None
    execution_date: date | None = None
    expiration_date: date | None = None
    renewal_date: date | None = None
    notice_deadline: date | None = None
    term_months: int | None = None

    governing_law: str | None = None
    jurisdiction: str | None = None
    country: str | None = None
    language: str | None = None

    currency: str | None = None
    contract_value: Decimal | None = None
    payment_terms_days: int | None = None
    payment_terms: str | None = None

    #: Contract Details shows these as "Party A" / "Party B".
    party_a: str | None = None
    party_b: str | None = None
    vendor: str | None = None
    customer: str | None = None

    category: str | None = None
    department: str | None = None
    business_unit: str | None = None
    owner: str | None = None
    status: str | None = None

    #: 0-100. Rendered as the gauge on AI Insights and "Medium 62/100" on Details.
    risk_score: int | None = None
    risk_band: str | None = None
    risk_level: str | None = None
    #: Why the score is what it is, so the number is explainable.
    risk_factors: list[dict[str, Any]] = Field(default_factory=list)

    auto_renewal: bool | None = None
    auto_renewal_notice_days: int | None = None
    renewal_term_months: int | None = None

    #: Clause types the profile requires that extraction did not find. Powers the
    #: "Missing Clauses" KPI and its alert.
    missing_mandatory_clauses: list[str] = Field(default_factory=list)
    has_unlimited_liability: bool | None = None
    has_liability_cap: bool | None = None
    liability_cap_amount: Decimal | None = None
    has_termination_for_convenience: bool | None = None
    has_data_protection_clause: bool | None = None
    termination_notice_days: int | None = None

    clause_count: int = 0
    obligation_count: int = 0
    risk_count: int = 0
    high_risk_count: int = 0

    summary: str | None = None
    key_topics: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def days_to_expiry(self) -> int | None:
        if self.expiration_date is None:
            return None
        return (self.expiration_date - date.today()).days


class ContractMetadataUpdate(BaseSchema):
    """Manual correction of extracted metadata.

    Recorded in ``contract_history`` with ``source='user'`` so a human override is
    always distinguishable from an AI extraction.
    """

    effective_date: date | None = None
    expiration_date: date | None = None
    governing_law: str | None = Field(default=None, max_length=150)
    jurisdiction: str | None = Field(default=None, max_length=150)
    currency: str | None = Field(default=None, max_length=8)
    contract_value: Decimal | None = Field(default=None, ge=0)
    party_a: str | None = Field(default=None, max_length=255)
    party_b: str | None = Field(default=None, max_length=255)
    vendor: str | None = Field(default=None, max_length=255)
    customer: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=150)
    owner: str | None = Field(default=None, max_length=255)
    auto_renewal: bool | None = None
    auto_renewal_notice_days: int | None = Field(default=None, ge=0, le=3650)


# =============================================================================
# Processing status (embedded in contract responses)
# =============================================================================
class ProcessingSummary(ResponseSchema):
    """Enough job state for the repository row and the details header."""

    job_id: uuid.UUID | None = None
    state: str | None = None
    current_stage: str | None = None
    progress: int = 0
    retry_count: int = 0
    error_message: str | None = None
    error_stage: str | None = None
    is_retryable: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None


# =============================================================================
# Contract responses
# =============================================================================
class ContractListItem(ResponseSchema):
    """Row shape for the Contract Repository table."""

    id: uuid.UUID
    project_id: uuid.UUID
    title: str | None = None
    original_file_name: str
    contract_number: str | None = None
    agreement_type: str | None = None
    file_type: str
    file_size: int
    page_count: int | None = None
    status: str
    needs_review: bool = False

    # Denormalised for the table - avoids a join per row.
    party_a: str | None = None
    party_b: str | None = None
    vendor: str | None = None
    effective_date: date | None = None
    expiration_date: date | None = None
    contract_value: Decimal | None = None
    currency: str | None = None
    risk_score: int | None = None
    risk_band: str | None = None
    clause_count: int = 0
    missing_clause_count: int = 0

    processing: ProcessingSummary | None = None
    uploaded_by: UserRef | None = None
    created_at: datetime
    processed_at: datetime | None = None

    @property
    def display_title(self) -> str:
        return self.title or self.original_file_name


class ContractVersionResponse(ResponseSchema):
    id: uuid.UUID
    version: int
    original_file_name: str
    file_size: int
    sha256_hash: str
    change_note: str | None = None
    uploaded_by: uuid.UUID | None = None
    created_at: datetime


class ContractResponse(ResponseSchema):
    """Full contract detail (Contract Details screen)."""

    id: uuid.UUID
    project: ProjectRef
    title: str | None = None
    original_file_name: str
    contract_number: str | None = None
    agreement_type: str | None = None
    agreement_subtype: str | None = None
    #: The type of the file being *processed*, which is always PDF. What the user
    #: uploaded is `original_file_type` - the two differ for a Word upload.
    file_type: str
    file_size: int
    page_count: int | None = None
    sha256_hash: str
    mime_type: str | None = None
    language: str | None = None

    # --- source provenance ---------------------------------------------------
    #: What was uploaded: pdf, doc or docx. Equal to `file_type` for a PDF.
    original_file_type: str | None = None
    #: True when a Word original was converted, so the UI can offer both files and
    #: explain that the viewer is showing a conversion rather than the original.
    has_converted_pdf: bool = False
    #: The archive this document came out of, so the UI can group and link back.
    source_archive_id: uuid.UUID | None = None
    source_archive_name: str | None = None

    status: str
    needs_review: bool = False
    current_version: int = 1

    #: The exact profile version that processed this contract - it stays pinned even
    #: after the profile is revised (§11).
    profile_id: uuid.UUID | None = None
    profile_version: str | None = None
    profile_name: str | None = None
    classification_confidence: Decimal | None = None

    contract_metadata: ContractMetadataResponse | None = None
    processing: ProcessingSummary | None = None

    tags: list[str] = Field(default_factory=list)
    notes: str | None = None

    uploaded_by: UserRef | None = None
    created_at: datetime
    updated_at: datetime
    processed_at: datetime | None = None

    #: Counts for the tab badges on Contract Details.
    counts: dict[str, int] = Field(default_factory=dict)


class ContractUpdateRequest(BaseSchema):
    """Editable contract fields. Extracted knowledge is corrected via review."""

    title: str | None = Field(default=None, max_length=512)
    contract_number: str | None = Field(default=None, max_length=128)
    agreement_type: AgreementType | None = None
    tags: list[str] | None = None
    notes: str | None = None
    status: ContractStatus | None = None


# =============================================================================
# Repository filters
# =============================================================================
class ContractFilterParams(BaseSchema):
    """Contract Repository filters.

    These map onto indexed columns of ``contract_metadata``, which is what keeps
    the repository responsive at millions of contracts (§14 metadata-first).
    """

    search: str | None = Field(default=None, max_length=300)
    status: list[ContractStatus] | None = None
    agreement_type: list[str] | None = None
    file_type: FileType | None = None

    party: str | None = Field(default=None, max_length=255)
    vendor: str | None = None
    customer: str | None = None
    contract_number: str | None = None

    effective_date: DateRange | None = None
    expiration_date: DateRange | None = None
    #: Convenience filter for the "expiring soon" KPI drill-down.
    expiring_within_days: int | None = Field(default=None, ge=0, le=3650)

    risk_band: list[RiskBand] | None = None
    risk_score: NumberRange | None = None
    contract_value: NumberRange | None = None
    currency: str | None = None

    governing_law: str | None = None
    country: str | None = None
    category: str | None = None
    department: str | None = None
    owner: str | None = None
    language: str | None = None

    auto_renewal: bool | None = None
    has_unlimited_liability: bool | None = None
    #: Filter to contracts missing any of these clause types (JSONB containment).
    missing_clause_types: list[str] | None = None
    #: Filter to contracts missing *any* mandatory clause, without naming which.
    #: `missing_clause_types` answers "which contracts lack an indemnity clause";
    #: this answers "which contracts are incomplete", the question the dashboard
    #: tile counts.
    missing_mandatory: bool | None = None
    needs_review: bool | None = None

    tags: list[str] | None = None
    uploaded_by: uuid.UUID | None = None
    uploaded_after: datetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.expiring_within_days is not None and self.expiration_date is not None:
            raise ValueError(
                "Use either 'expiring_within_days' or an 'expiration_date' range, not both"
            )
        return self


class ContractBulkAction(BaseSchema):
    """Bulk operation over selected repository rows."""

    contract_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    action: str = Field(pattern="^(delete|reprocess|retry|tag|untag|export)$")
    tags: list[str] | None = None
    #: For ``reprocess``: which stage to restart from. Omit to re-run everything.
    from_stage: str | None = None


class FileAccessResponse(ResponseSchema):
    """How the viewer should fetch the source document."""

    contract_id: uuid.UUID
    file_name: str
    file_type: str
    file_size: int
    page_count: int | None = None
    #: Time-limited direct URL. The browser fetches bytes from storage, not through
    #: the API, so the viewer does not make the API a bandwidth bottleneck.
    url: str
    expires_in: int
    #: True when ``url`` proxies through the API (local storage in development).
    is_proxied: bool = False


__all__ = [
    "ContractBulkAction",
    "ContractFilterParams",
    "ContractListItem",
    "ContractMetadataResponse",
    "ContractMetadataUpdate",
    "ContractResponse",
    "ContractUpdateRequest",
    "ContractVersionResponse",
    "FileAccessResponse",
    "ProcessingSummary",
    "UploadOptions",
    "UploadResponse",
    "UploadedFileResult",
]

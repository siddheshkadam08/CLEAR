"""Export request/response schemas (§23, FR-6)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.core.enums import ExportFormat, ExportStatus, SearchScope
from app.export.base import ExportEntity
from app.schemas.common import BaseSchema, ResponseSchema


class ExportCreateRequest(BaseSchema):
    """Ask for an export.

    ``filters`` takes the same shape the contract list endpoint accepts, which is
    what makes "export what I am looking at" exact rather than approximate - the
    screen and the workbook run through the same filter builder.
    """

    export_format: ExportFormat = Field(
        default=ExportFormat.XLSX, description="Only formats with a registered exporter."
    )
    scope: SearchScope = Field(
        default=SearchScope.APPLICATION,
        description=(
            "application = every project the caller belongs to; project = one "
            "project; contract = a single contract."
        ),
    )
    project_id: uuid.UUID | None = Field(
        default=None, description="Required when scope is 'project'."
    )
    scope_ref: uuid.UUID | None = Field(
        default=None, description="The contract id when scope is 'contract'."
    )
    entities: list[ExportEntity] = Field(
        default_factory=list,
        description="One worksheet each. Empty means the default set.",
    )
    #: Column selection per entity, e.g. ``{"clauses": ["clause_type", "text"]}``.
    fields: dict[str, list[str]] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("entities")
    @classmethod
    def _dedupe(cls, value: list[ExportEntity]) -> list[ExportEntity]:
        seen: list[ExportEntity] = []
        for entity in value:
            if entity not in seen:
                seen.append(entity)
        return seen


class ExportResponse(ResponseSchema):
    """An export job and, once finished, the file it produced."""

    id: uuid.UUID
    project_id: uuid.UUID | None = None
    requested_by: uuid.UUID
    scope: SearchScope
    scope_ref: uuid.UUID | None = None
    export_format: ExportFormat
    status: ExportStatus
    progress: int = 0

    entities: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)

    file_name: str | None = None
    file_size: int | None = None
    row_count: int | None = None
    #: SHA-256 of the stored bytes, so a download can be verified.
    checksum: str | None = None

    error: dict[str, Any] | None = None

    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: After this the file is purged; the row remains as the audit record.
    expires_at: datetime | None = None
    download_count: int = 0

    @property
    def is_ready(self) -> bool:
        return self.status == ExportStatus.COMPLETED.value


class ExportDownloadResponse(ResponseSchema):
    """A short-lived URL for a finished export."""

    export_id: uuid.UUID
    url: str
    file_name: str | None = None
    file_size: int | None = None
    #: Seconds. The file outlives the URL - request another when this one lapses.
    expires_in: int


class ExportCapabilitiesResponse(ResponseSchema):
    """What this deployment can export, so the UI does not offer a dead option."""

    formats: list[ExportFormat]
    entities: list[ExportEntity]
    default_entities: list[ExportEntity]
    retention_hours: int


__all__ = [
    "ExportCapabilitiesResponse",
    "ExportCreateRequest",
    "ExportDownloadResponse",
    "ExportResponse",
]

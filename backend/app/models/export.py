"""Export jobs (§23, FR-6).

Export runs on the queue rather than in the request: a project-wide export of
clauses across thousands of contracts is not a 30-second HTTP response. The
endpoint returns a job id; the worker writes a file to object storage and the
client polls for a signed download URL.

XLSX ships now; ``ExportFormat`` already carries CSV/JSON/PDF so adding one is a
new :class:`~app.export.base.IExporter` implementation and nothing else.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ExportFormat, ExportStatus, SearchScope
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import pg_enum

if TYPE_CHECKING:
    from app.models.identity import User


class ExportJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A background export request and its result."""

    __tablename__ = "export_jobs"

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    scope: Mapped[SearchScope] = mapped_column(pg_enum(SearchScope, "search_scope"), nullable=False)
    scope_ref: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    export_format: Mapped[ExportFormat] = mapped_column(
        pg_enum(ExportFormat, "export_format"), nullable=False
    )
    status: Mapped[ExportStatus] = mapped_column(
        pg_enum(ExportStatus, "export_status"),
        nullable=False,
        default=ExportStatus.QUEUED,
        server_default=ExportStatus.QUEUED.value,
        index=True,
    )

    #: Which entity types to include - one worksheet each in XLSX:
    #: ``["contracts","clauses","obligations","risks","timeline","entities"]``
    entities: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: Column selection per entity. Empty means "all columns for that entity".
    fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: The same filter shape the repository and search endpoints accept, so
    #: "export what I am looking at" is exact rather than approximate.
    filters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # --- result --------------------------------------------------------------
    storage_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)

    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Exports hold contract data, so the artifact is not kept indefinitely; the
    #: scheduler purges expired files and marks the row ``expired``.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    download_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    requester: Mapped[User] = relationship("User", lazy="joined")

    __table_args__ = (
        Index("ix_export_jobs_user_created", "requested_by", "created_at"),
        Index("ix_export_jobs_project_status", "project_id", "status"),
        # Purge sweep: only completed exports have a file to expire.
        Index(
            "ix_export_jobs_expiring",
            "expires_at",
            postgresql_where=text("status = 'completed' AND expires_at IS NOT NULL"),
        ),
    )


__all__ = ["ExportJob"]

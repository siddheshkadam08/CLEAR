"""Contract, contract versions and the denormalised metadata used for filtering.

:class:`ContractMetadata` is the metadata-first retrieval surface (§14): a flat,
heavily indexed row per contract so a query like "high-risk vendor agreements
expiring in the next 90 days" is answered by the relational planner **before** any
vector search runs. That is what keeps retrieval fast as the repository grows to
millions of contracts.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ContractStatus, FileType, RiskBand
from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import extensible_enum, pg_enum

if TYPE_CHECKING:
    from app.models.identity import User
    from app.models.processing import ProcessingJob
    from app.models.project import Project


class Contract(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """An uploaded contract document. Belongs to exactly one project."""

    __tablename__ = "contracts"

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # --- source file ---------------------------------------------------------
    #
    # Four paths, because "the file" stopped being one thing once Word documents
    # were accepted. A DOC is stored as uploaded *and* as the PDF it was converted
    # into, and different callers want different ones: the pipeline and the
    # evidence viewer need the PDF, the download button needs what the user
    # actually gave us.
    #
    # `storage_path` is kept as the pipeline's path and always equals
    # `processing_file_path`. It is read in a dozen places - the parser stage,
    # version rows, reprocessing - and the requirement was that no downstream
    # service change because of this feature. Keeping it authoritative for
    # "the bytes to process" means none had to.
    original_file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    #: Where the bytes the user uploaded live, whatever their format.
    original_file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: The PDF produced from a Word original. NULL for a PDF upload - that is the
    #: flag for "no conversion happened", not an absent value.
    converted_file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: The PDF the pipeline reads. Equals `original_file_path` for a PDF upload and
    #: `converted_file_path` for a Word one.
    processing_file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: What was uploaded, before any conversion. `file_type` stays as the type of
    #: the file being *processed*, which is always PDF now, so existing filters and
    #: parser selection keep working unchanged.
    original_file_type: Mapped[FileType | None] = mapped_column(
        pg_enum(FileType, "file_type"), nullable=True
    )
    file_type: Mapped[FileType] = mapped_column(pg_enum(FileType, "file_type"), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # --- archive provenance --------------------------------------------------
    #: Set when this contract came out of a ZIP, so the UI can group everything
    #: that arrived in one upload. The archive itself is never a contract.
    source_archive_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True
    )
    #: The archive's own file name, carried so the UI can label the group without
    #: another lookup - there is no archive table to join to.
    source_archive_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: Content hash of the file *the user uploaded*. Unique per project, so
    #: re-uploading the same document to the same project is rejected while the
    #: same document in two projects is fine.
    #:
    #: Deliberately the original's, not the processed file's: LibreOffice does not
    #: produce byte-identical output from one run to the next, so hashing the
    #: converted PDF would let the same Word document in repeatedly.
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Content hash of the file at ``processing_file_path``, which the validation
    #: stage re-computes to prove storage has not corrupted or swapped it.
    #:
    #: Equal to ``sha256_hash`` for a PDF upload; different for a converted one,
    #: because the two columns answer different questions - "is this the same
    #: document?" and "are these the same bytes?". Collapsing them made validation
    #: hash the PDF and compare it against the DOCX's hash, halting every Word
    #: upload at the first stage with an integrity error. NULL on rows predating
    #: conversion, where the two were necessarily the same.
    processing_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- business identity ---------------------------------------------------
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    contract_number: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    #: Extensible: classification may emit a type the seeded taxonomy lacks.
    agreement_type: Mapped[str | None] = mapped_column(
        extensible_enum(64), nullable=True, index=True
    )
    agreement_subtype: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[ContractStatus] = mapped_column(
        pg_enum(ContractStatus, "contract_status"),
        nullable=False,
        default=ContractStatus.UPLOADED,
        server_default=ContractStatus.UPLOADED.value,
        index=True,
    )
    current_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    #: The Document Intelligence Profile that processed this contract. Pinned to
    #: the exact version used, so a later profile revision never rewrites how an
    #: already-processed contract is interpreted (§11).
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("document_profiles.id", ondelete="SET NULL"), nullable=True
    )
    profile_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Classification confidence, kept here for quick "was this classified well?"
    #: filtering on the repository screen.
    classification_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)

    #: True when any extracted item on this contract awaits human review.
    needs_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Free-form user annotations (tags, notes). Not AI-derived.
    tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- relationships -------------------------------------------------------
    project: Mapped[Project] = relationship("Project", back_populates="contracts")
    uploader: Mapped[User | None] = relationship("User", foreign_keys=[uploaded_by], lazy="joined")
    versions: Mapped[list[ContractVersion]] = relationship(
        "ContractVersion",
        back_populates="contract",
        cascade="all, delete-orphan",
        order_by="ContractVersion.version",
        lazy="noload",
    )
    contract_metadata: Mapped[ContractMetadata | None] = relationship(
        "ContractMetadata",
        back_populates="contract",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="joined",
    )
    jobs: Mapped[list[ProcessingJob]] = relationship(
        "ProcessingJob",
        back_populates="contract",
        cascade="all, delete-orphan",
        order_by="desc(ProcessingJob.created_at)",
        lazy="noload",
    )

    __table_args__ = (
        # Duplicate detection is per project (§7.2).
        UniqueConstraint("project_id", "sha256_hash", name="uq_contracts_project_id_sha256_hash"),
        # Repository listing: project + status, newest first.
        Index("ix_contracts_project_status_created", "project_id", "status", "created_at"),
        Index(
            "ix_contracts_project_live",
            "project_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_contracts_project_agreement_type", "project_id", "agreement_type"),
        Index(
            "ix_contracts_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index(
            "ix_contracts_filename_trgm",
            "original_file_name",
            postgresql_using="gin",
            postgresql_ops={"original_file_name": "gin_trgm_ops"},
        ),
        Index(
            "ix_contracts_needs_review",
            "project_id",
            postgresql_where=text("needs_review = true AND deleted_at IS NULL"),
        ),
        CheckConstraint("file_size > 0", name="file_size_positive"),
    )

    @property
    def display_title(self) -> str:
        """What the UI shows - falls back to the filename before extraction runs."""
        return self.title or self.original_file_name


class ContractVersion(Base, UUIDPrimaryKeyMixin):
    """An immutable snapshot of a contract's source file.

    Re-uploading a revised document adds a version rather than overwriting, so
    prior extractions remain traceable to the bytes they came from.
    """

    __tablename__ = "contract_versions"

    contract_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    change_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    contract: Mapped[Contract] = relationship("Contract", back_populates="versions")

    __table_args__ = (
        UniqueConstraint("contract_id", "version", name="uq_contract_versions_contract_id_version"),
    )


class ContractMetadata(Base, TimestampMixin):
    """Flat, filterable projection of a contract's extracted business metadata.

    Written by the AI Extraction stage. Read by: the repository filters, every
    dashboard KPI, the Alerts evaluator, and the metadata pre-filter in front of
    vector search. Denormalised on purpose - these columns are indexed for
    selectivity, not normalised for storage.
    """

    __tablename__ = "contract_metadata"

    contract_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # --- dates ---------------------------------------------------------------
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    execution_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    renewal_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notice_deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    term_months: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- legal ---------------------------------------------------------------
    governing_law: Mapped[str | None] = mapped_column(String(150), nullable=True)
    jurisdiction: Mapped[str | None] = mapped_column(String(150), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # --- commercial ----------------------------------------------------------
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    contract_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    payment_terms_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- parties -------------------------------------------------------------
    #: Contract Details shows these as "Party A" / "Party B".
    party_a: Mapped[str | None] = mapped_column(String(255), nullable=True)
    party_b: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- classification / ownership -----------------------------------------
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    department: Mapped[str | None] = mapped_column(String(150), nullable=True)
    business_unit: Mapped[str | None] = mapped_column(String(150), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- risk ----------------------------------------------------------------
    #: 0..100, computed by the risk engine from detected risks weighted by
    #: severity. Rendered as the gauge on AI Insights and "Medium 62/100" on
    #: Contract Details.
    risk_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_band: Mapped[RiskBand | None] = mapped_column(
        pg_enum(RiskBand, "risk_band"), nullable=True, index=True
    )
    risk_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Snapshot of the risk factors behind the score, so the UI can explain it
    #: without re-running the engine.
    risk_factors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    # --- renewal -------------------------------------------------------------
    auto_renewal: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    auto_renewal_notice_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    renewal_term_months: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- AI insight flags ----------------------------------------------------
    #: Clause types the profile marks mandatory that extraction did not find.
    #: Powers the "Missing Clauses" KPI and the corresponding alert.
    missing_mandatory_clauses: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    has_unlimited_liability: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_liability_cap: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    liability_cap_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    has_termination_for_convenience: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_data_protection_clause: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    termination_notice_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- counts (dashboard cheap reads) --------------------------------------
    clause_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    obligation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    risk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    high_risk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    #: One-paragraph AI summary shown on cards and the details header.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_topics: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: Anything the profile extracts that has no dedicated column.
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    contract: Mapped[Contract] = relationship("Contract", back_populates="contract_metadata")

    __table_args__ = (
        # Every index below leads with project_id: project isolation is applied on
        # every read, so the planner needs it first to use the index at all.
        Index("ix_contract_metadata_project_expiration", "project_id", "expiration_date"),
        Index("ix_contract_metadata_project_effective", "project_id", "effective_date"),
        Index("ix_contract_metadata_project_risk", "project_id", "risk_band", "risk_score"),
        Index("ix_contract_metadata_project_vendor", "project_id", "vendor"),
        Index("ix_contract_metadata_project_customer", "project_id", "customer"),
        Index("ix_contract_metadata_project_category", "project_id", "category"),
        Index("ix_contract_metadata_project_value", "project_id", "contract_value"),
        # Partial index for the "expiring soon" KPI - the only rows that matter
        # are the ones with an expiry date at all.
        Index(
            "ix_contract_metadata_expiring",
            "expiration_date",
            "project_id",
            postgresql_where=text("expiration_date IS NOT NULL"),
        ),
        # Partial index for the auto-renewal alert.
        Index(
            "ix_contract_metadata_auto_renewal",
            "project_id",
            "notice_deadline",
            postgresql_where=text("auto_renewal = true"),
        ),
        # Containment queries: "which contracts are missing a liability cap?"
        Index(
            "ix_contract_metadata_missing_clauses",
            "missing_mandatory_clauses",
            postgresql_using="gin",
        ),
        Index("ix_contract_metadata_extra", "extra", postgresql_using="gin"),
        CheckConstraint(
            "risk_score IS NULL OR (risk_score >= 0 AND risk_score <= 100)",
            name="risk_score_range",
        ),
    )


__all__ = ["Contract", "ContractMetadata", "ContractVersion"]

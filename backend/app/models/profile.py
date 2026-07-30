"""Document Intelligence Profile (DIP) - the configuration-driven brain (§11).

A profile defines **how** a classified document is processed: which prompts and
mandatory clauses apply, how it is chunked, which embedding levels are built,
how risk is scored, which compliance packs run, and what triggers human review.

Two rules make this table load-bearing rather than decorative:

* **No document rules are hardcoded.** A new contract type is a new profile row -
  zero code changes.
* **Profiles are versioned and immutable in effect.** Editing a profile creates a
  new version; a contract stays linked to the version that processed it, so a
  policy change never silently rewrites how existing contracts were interpreted.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ChunkStrategy
from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import extensible_enum, pg_enum


class DocumentProfile(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """A versioned Document Intelligence Profile."""

    __tablename__ = "document_profiles"

    # --- identity ------------------------------------------------------------
    #: Stable key across versions, e.g. ``commercial_msa``. Classification
    #: resolves to a key; the active version for that key is then selected.
    key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    category: Mapped[str] = mapped_column(String(100), nullable=False)
    contract_type: Mapped[str] = mapped_column(extensible_enum(64), nullable=False, index=True)
    contract_subtype: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supported_languages: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=lambda: ["en"], server_default=text("'[\"en\"]'::jsonb")
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: Fallback profile used when classification is inconclusive. Exactly one
    #: profile should carry this.
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Higher priority wins when several profiles match a classification.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: Optional narrowing to one project; NULL means available platform-wide.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )

    # --- classification hints ------------------------------------------------
    #: Signals the classifier scores against: title patterns, required phrases,
    #: negative phrases, weights. Keeps classification configurable rather than
    #: a hardcoded rule cascade.
    #: ``{"title_patterns": [...], "required_phrases": [...],
    #:    "negative_phrases": [...], "min_score": 0.4}``
    classification_hints: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # --- extraction strategy -------------------------------------------------
    #: ``{"prompt_templates": {"clauses": "extraction.clauses", ...},
    #:    "categories": ["metadata","parties","clauses","obligations","risks",...],
    #:    "examples": [...], "validation_hints": [...]}``
    extraction_strategy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Clause types this document type MUST contain. The gap against extracted
    #: clauses becomes ``contract_metadata.missing_mandatory_clauses``, which
    #: drives the "Missing Clauses" KPI and its alert.
    mandatory_clauses: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    optional_clauses: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    confidence_threshold: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, default=0.85, server_default="0.85"
    )
    #: Conditions that route an extraction to a human (§13).
    #: ``{"low_confidence": true, "missing_mandatory_clause": true,
    #:    "conflicting_dates": true, "high_risk_clause": true,
    #:    "validation_failure": true}``
    review_rules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # --- chunking strategy ---------------------------------------------------
    chunk_strategy: Mapped[ChunkStrategy] = mapped_column(
        pg_enum(ChunkStrategy, "chunk_strategy"),
        nullable=False,
        default=ChunkStrategy.HYBRID,
        server_default=ChunkStrategy.HYBRID.value,
    )
    #: ``{"max_tokens": 900, "min_tokens": 40, "overlap_tokens": 80,
    #:    "preserve_tables": true, "preserve_lists": true,
    #:    "merge_cross_page_clauses": true}``
    chunk_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # --- embedding strategy --------------------------------------------------
    #: ``{"levels": ["document_summary","clause","chunk"],
    #:    "metadata_weighting": {"agreement_type": 0.2, "vendor": 0.1},
    #:    "similarity_threshold": 0.25,
    #:    "summary_includes": ["summary","key_topics","parties"]}``
    embedding_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # --- risk engine ---------------------------------------------------------
    #: Maps detected conditions to risk types and severities, plus the weights
    #: used for the 0-100 score. Lets one document type treat auto-renewal as
    #: high risk while another treats it as routine.
    risk_mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # --- compliance ----------------------------------------------------------
    #: Named packs to evaluate: procurement, HIPAA, SOX, GDPR, country packs.
    compliance_rules: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    # --- validation ----------------------------------------------------------
    #: Business validation beyond schema: date ordering, monetary sanity,
    #: cross-reference resolution.
    validation_rules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # --- workflow extensions -------------------------------------------------
    #: Optional stages the Workflow Engine inserts for this document type:
    #: ``["ocr_enhance","translation","human_review","legal_approval",
    #:    "compliance_review","risk_assessment","external_api"]``
    workflow_extensions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    # --- retention -----------------------------------------------------------
    #: ``{"retain_years": 7, "purge_artifacts_after_days": null,
    #:    "legal_hold": false}``
    retention_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Previous version of this profile key - the edit history chain.
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("document_profiles.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("key", "version", name="uq_document_profiles_key_version"),
        # One active version per key (per project scope).
        Index(
            "uq_document_profiles_active_key",
            "key",
            unique=True,
            postgresql_where=text("is_active = true AND project_id IS NULL AND deleted_at IS NULL"),
        ),
        Index("ix_document_profiles_type_active", "contract_type", "is_active"),
        Index(
            "uq_document_profiles_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default = true AND deleted_at IS NULL"),
        ),
    )

    # --- convenience accessors ----------------------------------------------
    def chunk_setting(self, key: str, default: Any = None) -> Any:
        return (self.chunk_config or {}).get(key, default)

    def embedding_levels(self) -> list[str]:
        return list(
            (self.embedding_config or {}).get("levels") or ["document_summary", "clause", "chunk"]
        )

    def prompt_template(self, category: str) -> str | None:
        templates = (self.extraction_strategy or {}).get("prompt_templates") or {}
        value = templates.get(category)
        return str(value) if value else None

    def extraction_categories(self) -> list[str]:
        return list((self.extraction_strategy or {}).get("categories") or [])

    def review_trigger_enabled(self, trigger: str) -> bool:
        return bool((self.review_rules or {}).get(trigger, False))


__all__ = ["DocumentProfile"]

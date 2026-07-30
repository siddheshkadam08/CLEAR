"""Clause Master - administrator-managed clause taxonomy and extraction rules (FR-7).

This is the configuration surface behind AI extraction. An administrator defines
*what* a clause category is, *how* to find it (prompt template, synonyms,
patterns), *how confident* the model must be, and whether it is mandatory. The
extraction engine reads these rows; it has no hardcoded clause list.

Rules are versioned and the standard language is stored, so:

* a change to a category's prompt creates a new rule version rather than silently
  altering how past extractions would be read, and
* ``standard_text`` gives the deviation score a baseline - "this termination
  clause differs from our standard" is measurable rather than a vibe.
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import RiskSeverity
from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import extensible_enum, pg_enum


class ClauseMasterCategory(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """A clause category in the master taxonomy."""

    __tablename__ = "clause_master_categories"

    #: Machine key used as ``clauses.clause_type``. Stable; the display name can
    #: change without invalidating existing extractions.
    key: Mapped[str] = mapped_column(extensible_enum(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Grouping for the UI: Commercial, Legal, Risk, Compliance, Operational.
    group_name: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)

    #: Whether a contract missing this category is flagged. Profiles can override
    #: per document type; this is the platform-wide default.
    mandatory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Minimum confidence for an extraction of this category to be accepted
    #: without review.
    confidence_threshold: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, default=0.85, server_default="0.85"
    )
    #: Severity assigned when this clause is missing or non-standard.
    default_risk_severity: Mapped[RiskSeverity | None] = mapped_column(
        pg_enum(RiskSeverity, "risk_severity"), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: Seeded categories cannot be deleted, only deactivated - existing clauses
    #: reference their keys.
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    #: Business priority rank, 1 = highest. Drives extraction ordering (the
    #: highest-value clauses are extracted first, so a partially-failed job still
    #: yields the terms that matter most) and the default sort of every clause
    #: list in the UI.
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=999, server_default="999", index=True
    )
    display_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )

    #: How the frontend surfaces this category. Config rather than hardcoded UI,
    #: so promoting a clause to its own tab is an admin change:
    #: ``{"placement": "dedicated_tab" | "list" | "summary_card",
    #:    "tab_label": "Limitation of Liability",
    #:    "primary_fields": ["cap_basis", "has_carve_outs"],
    #:    "highlight_when": {"cap_basis": "uncapped"}}``
    ui_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    rules: Mapped[list[ClauseMasterRule]] = relationship(
        "ClauseMasterRule",
        back_populates="category",
        cascade="all, delete-orphan",
        order_by="desc(ClauseMasterRule.version)",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_clause_master_categories_active", "is_active", "priority", "display_order"),
        Index(
            "ix_clause_master_categories_mandatory",
            "mandatory",
            postgresql_where=text("is_active = true"),
        ),
    )

    @property
    def has_dedicated_tab(self) -> bool:
        """True when the UI should give this category its own tab."""
        return (self.ui_config or {}).get("placement") == "dedicated_tab"

    @property
    def active_rule(self) -> ClauseMasterRule | None:
        """Highest-version active rule for this category."""
        return next((rule for rule in self.rules if rule.is_active), None)


class ClauseMasterRule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A versioned extraction rule for one clause category."""

    __tablename__ = "clause_master_rules"

    category_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("clause_master_categories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    #: Deterministic pre-filter applied before the LLM: heading patterns, required
    #: keywords, proximity windows, negative patterns. Cuts LLM cost by not asking
    #: the model about chunks that cannot contain this clause.
    #: ``{"heading_patterns": [...], "keywords": [...], "must_not_contain": [...],
    #:    "min_tokens": 20, "search_scope": "clause|section"}``
    extraction_rule: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Prompt for this category. Either an inline template or a reference into the
    #: prompt registry (``extraction.clauses``); the registry keeps the version.
    prompt_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_template_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    #: Alternative headings the same clause appears under across contracts
    #: ("Term and Termination", "Duration", "Cancellation").
    synonyms: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: JSON Schema fragment for the attributes this clause type must yield
    #: (notice days, cap amount). Validated by the extraction validator.
    output_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Few-shot examples supplied to the model.
    examples: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: Baseline language for deviation scoring.
    standard_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Business validation specific to this clause type.
    validation_rules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    change_note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    category: Mapped[ClauseMasterCategory] = relationship(
        "ClauseMasterCategory", back_populates="rules"
    )

    __table_args__ = (
        UniqueConstraint("category_id", "version", name="uq_clause_master_rules_category_version"),
        # One active rule per category.
        Index(
            "uq_clause_master_rules_active",
            "category_id",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )


class AISettings(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Runtime-adjustable AI configuration (``/admin/ai-settings``).

    A thin, audited override layer over environment configuration: an
    administrator can retune thresholds or switch model routing without a
    redeploy. Values are read through the settings service, which falls back to
    the environment when a key is absent - so a bad edit degrades to the
    deployed default rather than breaking the pipeline.

    Single-row table (enforced by the partial unique index on ``is_current``);
    history is retained for audit.
    """

    __tablename__ = "ai_settings"

    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    #: ``{"llm": {"provider": "...", "model": "...", "temperature": 0.0},
    #:    "embedding": {...}, "reranker": {...}}``
    providers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: ``{"review_confidence": 0.85, "retrieval_min_similarity": 0.25,
    #:    "context_token_budget": 24000, "rerank_top_k": 20}``
    thresholds: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Model routing: which model handles simple vs complex requests (§17 cost
    #: optimisation).
    model_routing: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Organisational policies injected into every prompt by the orchestrator.
    policies: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Feature switches: streaming, graph retrieval, human review, OCR.
    feature_flags: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    change_note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        Index(
            "uq_ai_settings_current",
            "is_current",
            unique=True,
            postgresql_where=text("is_current = true"),
        ),
    )


__all__ = ["AISettings", "ClauseMasterCategory", "ClauseMasterRule"]

"""Extracted legal knowledge: clauses, entities, obligations, risks, key dates
and cross-references.

Every row here is AI-derived and therefore carries provenance
(:class:`~app.db.base.EvidenceMixin` +
:class:`~app.db.base.ExtractionProvenanceMixin`): the page range, the bounding
boxes the PDF viewer highlights, the source chunk, the confidence, and the exact
profile/prompt/model versions that produced it. An extraction without evidence
is not deliverable (§12 explainable AI), so the columns are structural rather
than optional.

Idempotency: the AI Extraction stage replaces a contract's knowledge rows
wholesale inside one transaction (delete-then-insert keyed by ``contract_id``),
so re-running the stage never accumulates duplicates (§10.1).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    DateType,
    EntityType,
    GraphRelation,
    ObligationStatus,
    RiskSeverity,
)
from app.db.base import (
    Base,
    EvidenceMixin,
    ExtractionProvenanceMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from app.db.types import extensible_enum, pg_enum

if TYPE_CHECKING:
    from app.models.chunk import Chunk


class Clause(Base, UUIDPrimaryKeyMixin, TimestampMixin, EvidenceMixin, ExtractionProvenanceMixin):
    """An extracted contract clause.

    The unit the UI navigates: clicking a clause opens the viewer at
    ``page_start`` and draws ``bounding_boxes``. Also the L2 embedding subject,
    so clause-level similarity search ("show me every termination clause like
    this one") works without descending to chunks.
    """

    __tablename__ = "clauses"

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
    #: The semantic chunk this clause was extracted from - the audit link back to
    #: the exact text the model saw.
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True
    )

    #: Extensible taxonomy: administrators add clause categories through the
    #: Clause Master without a migration (§7.8).
    clause_type: Mapped[str] = mapped_column(extensible_enum(64), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    text_content: Mapped[str] = mapped_column("text", Text, nullable=False)
    #: Short AI paraphrase shown in list views where the full clause is too long.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    section_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    section_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    clause_number: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Clause-level risk flag, set when the risk engine attributes a risk here.
    is_risk_flagged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Whether this clause satisfies one of the profile's mandatory clause types.
    is_mandatory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Deviation from the Clause Master's standard language, 0..1. Feeds the
    #: "non-standard clause" insight.
    deviation_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)

    #: Anything profile-specific the extraction produced for this clause type
    #: (notice periods, cap amounts, carve-outs).
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    chunk: Mapped[Chunk | None] = relationship("Chunk", lazy="noload")
    obligations: Mapped[list[Obligation]] = relationship(
        "Obligation", back_populates="clause", lazy="noload"
    )
    risks: Mapped[list[Risk]] = relationship("Risk", back_populates="clause", lazy="noload")

    __table_args__ = (
        Index("ix_clauses_project_type", "project_id", "clause_type"),
        Index("ix_clauses_contract_type", "contract_id", "clause_type"),
        Index("ix_clauses_project_confidence", "project_id", "confidence"),
        # Review queue: which clauses are waiting on a human?
        Index(
            "ix_clauses_review_pending",
            "project_id",
            postgresql_where=text("review_status = 'pending'"),
        ),
        Index(
            "ix_clauses_flagged",
            "project_id",
            "clause_type",
            postgresql_where=text("is_risk_flagged = true"),
        ),
        # Keyword search over clause text.
        Index(
            "ix_clauses_text_fts",
            text("to_tsvector('english', text)"),
            postgresql_using="gin",
        ),
        Index("ix_clauses_attributes", "attributes", postgresql_using="gin"),
    )


class Entity(Base, UUIDPrimaryKeyMixin, TimestampMixin, EvidenceMixin, ExtractionProvenanceMixin):
    """A party, organisation or person named in the contract."""

    __tablename__ = "entities"

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
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True
    )

    entity_type: Mapped[EntityType] = mapped_column(
        pg_enum(EntityType, "entity_type"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Full registered name where the contract states one, e.g.
    #: "Acme Corporation Inc." for the defined term "Acme".
    legal_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: Defined terms and short forms the contract uses for this party. Resolving
    #: these is what lets "the Supplier shall..." be attributed correctly.
    aliases: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    role: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    jurisdiction: Mapped[str | None] = mapped_column(String(150), nullable=True)
    registration_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Marks the primary two signatories, surfaced as Party A / Party B.
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    __table_args__ = (
        Index("ix_entities_project_name", "project_id", "name"),
        Index("ix_entities_project_type_role", "project_id", "entity_type", "role"),
        # Cross-contract party lookup ("every agreement with this vendor") relies
        # on fuzzy name matching, since legal names vary between documents.
        Index(
            "ix_entities_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        Index("ix_entities_aliases", "aliases", postgresql_using="gin"),
    )


class Obligation(
    Base, UUIDPrimaryKeyMixin, TimestampMixin, EvidenceMixin, ExtractionProvenanceMixin
):
    """A duty the contract imposes: who must do what, by when, triggered by what."""

    __tablename__ = "obligations"

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
    clause_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("clauses.id", ondelete="SET NULL"), nullable=True
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True
    )

    responsible_party: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    #: Resolved link to the party entity when the alias could be matched.
    responsible_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: Free-text deadline when no absolute date exists ("within 30 days of
    #: termination"). Kept alongside ``due_date`` rather than forcing a guess.
    due_description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: The event that starts the clock.
    trigger_event: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: Another obligation or clause this one depends on.
    dependency: Mapped[str | None] = mapped_column(String(512), nullable=True)

    frequency: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_recurring: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    status: Mapped[ObligationStatus] = mapped_column(
        pg_enum(ObligationStatus, "obligation_status"),
        nullable=False,
        default=ObligationStatus.UNKNOWN,
        server_default=ObligationStatus.UNKNOWN.value,
    )
    penalty: Mapped[str | None] = mapped_column(Text, nullable=True)

    clause: Mapped[Clause | None] = relationship("Clause", back_populates="obligations")

    __table_args__ = (
        Index("ix_obligations_project_due", "project_id", "due_date"),
        Index("ix_obligations_contract_party", "contract_id", "responsible_party"),
        # Upcoming-obligations panel: only dated obligations are schedulable.
        Index(
            "ix_obligations_upcoming",
            "project_id",
            "due_date",
            postgresql_where=text("due_date IS NOT NULL AND status <> 'fulfilled'"),
        ),
    )


class Risk(Base, UUIDPrimaryKeyMixin, TimestampMixin, EvidenceMixin, ExtractionProvenanceMixin):
    """A detected contractual risk, attributed to the clause that creates it."""

    __tablename__ = "risks"

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
    clause_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("clauses.id", ondelete="SET NULL"), nullable=True
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True
    )

    risk_type: Mapped[str] = mapped_column(extensible_enum(64), nullable=False, index=True)
    severity: Mapped[RiskSeverity] = mapped_column(
        pg_enum(RiskSeverity, "risk_severity"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    #: What to do about it - shown in the risk report.
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Contribution this risk made to the contract's 0-100 score, so the score is
    #: explainable rather than opaque.
    score_contribution: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: True when the risk is the *absence* of something (no liability cap), which
    #: has no bounding box to highlight.
    is_omission: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    clause: Mapped[Clause | None] = relationship("Clause", back_populates="risks")

    __table_args__ = (
        Index("ix_risks_project_severity", "project_id", "severity"),
        Index("ix_risks_contract_severity", "contract_id", "severity"),
        Index("ix_risks_project_type", "project_id", "risk_type"),
        Index(
            "ix_risks_high_severity",
            "project_id",
            "contract_id",
            postgresql_where=text("severity IN ('critical','high')"),
        ),
    )


class KeyDate(Base, UUIDPrimaryKeyMixin, TimestampMixin, EvidenceMixin, ExtractionProvenanceMixin):
    """A date or milestone on the contract timeline."""

    __tablename__ = "key_dates"

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
    clause_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("clauses.id", ondelete="SET NULL"), nullable=True
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True
    )

    date_type: Mapped[DateType] = mapped_column(
        pg_enum(DateType, "date_type"), nullable=False, index=True
    )
    date_value: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    #: Relative dates that cannot be resolved to a calendar date are preserved
    #: verbatim rather than discarded or guessed.
    date_expression: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_recurring: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    __table_args__ = (
        Index("ix_key_dates_project_type_value", "project_id", "date_type", "date_value"),
        Index("ix_key_dates_contract_type", "contract_id", "date_type"),
    )


class KnowledgeRelationship(Base, UUIDPrimaryKeyMixin, TimestampMixin, ExtractionProvenanceMixin):
    """A cross-reference, definition link or dependency found during extraction.

    **This table is the knowledge graph.** Rows carry an ``attributes.origin`` of
    either ``extracted`` - what the model read out of the document - or
    ``derived``, which the Indexing stage resolves from the extracted rows.
    ``RetrievalEngine._expand_graph`` traverses both.

    The docstring here used to say the Indexing stage projected these into
    ``app.models.graph`` for traversal. It never did: those tables were created,
    indexed and never written to, and have now been dropped. What misled was that
    ``app.ai.graph.builder`` defines in-memory dataclasses with the same names as
    the models had.
    """

    __tablename__ = "knowledge_relationships"

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

    relation: Mapped[GraphRelation] = mapped_column(
        pg_enum(GraphRelation, "graph_relation"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Resolved ids where the reference could be matched to a real row; NULL when
    #: the contract references something not present in this document.
    source_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    label: Mapped[str | None] = mapped_column(String(512), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    is_resolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    __table_args__ = (
        Index("ix_knowledge_relationships_contract_relation", "contract_id", "relation"),
        Index("ix_knowledge_relationships_project_relation", "project_id", "relation"),
    )


class ContractSummary(Base, UUIDPrimaryKeyMixin, TimestampMixin, ExtractionProvenanceMixin):
    """Structured AI summaries for a contract (FR-5).

    One row per format so an executive summary and a risk summary coexist without
    one overwriting the other, and each keeps its own provenance.
    """

    __tablename__ = "contract_summaries"

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

    summary_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: Section-wise breakdown when the format has one (executive summary).
    sections: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    key_points: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: Citations backing the summary - a summary is an AI answer and obeys the
    #: same grounding rules as the Copilot (§17).
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    __table_args__ = (
        Index(
            "uq_contract_summaries_contract_type",
            "contract_id",
            "summary_type",
            unique=True,
        ),
    )


__all__ = [
    "Clause",
    "ContractSummary",
    "Entity",
    "KeyDate",
    "KnowledgeRelationship",
    "Obligation",
    "Risk",
]

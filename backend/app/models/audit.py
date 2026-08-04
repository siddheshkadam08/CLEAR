"""Audit log, history tables and search/answer audit records (§7.9).

Three separate concerns, deliberately not merged:

* :class:`AuditLog` - append-only record of **every mutating request**: who, what,
  before, after, from where. The compliance artifact. Never updated, never
  deleted by application code.
* :class:`ContractHistory` - field-level change trail
  for the two entities that carry legal weight, including human review decisions.
  A reviewer's correction of an AI extraction must be attributable years later.
* :class:`RetrievalAudit` - what was retrieved and answered. Required by §17
  ("audit everything: session, prompt version, model version, response time,
  tokens, retrieved evidence, validation, user identity") and separated from the
  audit log because its volume and retention differ.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import AuditAction
from app.db.base import Base, UUIDPrimaryKeyMixin
from app.db.types import pg_enum


class AuditLog(Base, UUIDPrimaryKeyMixin):
    """Immutable audit record. One row per mutating operation."""

    __tablename__ = "audit_log"

    #: NULL for platform-level actions (user management, AI settings).
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: ``SET NULL`` rather than cascade: deleting a user must not erase the record
    #: of what they did.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Denormalised actor identity, kept because the user row may later be
    #: deleted or renamed.
    user_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    action: Mapped[AuditAction] = mapped_column(
        pg_enum(AuditAction, "audit_action"), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True
    )
    #: Human-readable identifier of the target (contract title, project name), so
    #: the audit trail stays legible after the row is gone.
    entity_label: Mapped[str | None] = mapped_column(String(512), nullable=True)

    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The endpoint that produced this record.
    route: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: False when the operation was rejected - a denied access attempt is exactly
    #: the kind of event an auditor wants to see.
    succeeded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True
    )

    __table_args__ = (
        Index("ix_audit_log_project_created", "project_id", "created_at"),
        Index("ix_audit_log_user_created", "user_id", "created_at"),
        Index("ix_audit_log_entity", "entity_type", "entity_id", "created_at"),
        Index("ix_audit_log_action_created", "action", "created_at"),
        # Security review: failed operations across the platform.
        Index(
            "ix_audit_log_failures",
            "created_at",
            postgresql_where=text("succeeded = false"),
        ),
    )


class ContractHistory(Base, UUIDPrimaryKeyMixin):
    """Field-level change trail for a contract."""

    __tablename__ = "contract_history"

    contract_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    change_type: Mapped[str] = mapped_column(String(64), nullable=False)
    field_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ``user`` | ``ai_extraction`` | ``human_review`` | ``system`` - lets the UI
    #: distinguish "the model said this" from "a lawyer decided this".
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="user", server_default="user"
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (Index("ix_contract_history_contract_created", "contract_id", "created_at"),)


class RetrievalAudit(Base, UUIDPrimaryKeyMixin):
    """What a search or Copilot answer retrieved, and how it was validated.

    Kept for reproducibility and for the retrieval-quality metrics: strategy mix,
    candidate counts, re-rank effect, citation coverage, cost.
    """

    __tablename__ = "retrieval_audit"

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )

    #: ``search`` | ``copilot`` | ``summary`` | ``report``
    operation: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    query_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    query_intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    strategy: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    filters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    #: ``{"documents": 12, "clauses": 30, "chunks": 45, "graph_nodes": 4}``
    candidate_counts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Ids of what actually reached the context package, so an answer can be
    #: reconstructed exactly.
    evidence_refs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    graph_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)

    retrieval_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rerank_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inference_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    result_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    citation_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    citation_coverage: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)

    #: Full version set (planner, reranking, context engine, prompt, model, policy).
    versions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    token_usage: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    validation_result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    cache_hit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True
    )

    __table_args__ = (
        Index("ix_retrieval_audit_project_created", "project_id", "created_at"),
        Index("ix_retrieval_audit_operation_created", "operation", "created_at"),
    )


__all__ = ["AuditLog", "ContractHistory", "RetrievalAudit"]

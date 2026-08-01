"""AI Copilot sessions and messages (§7.7).

Conversation state is **externalised**: a session stores the scope, the active
filters and the messages, and each assistant message stores the *citations and
evidence references* it was grounded in - not the raw model output as truth. A
follow-up question therefore re-retrieves against the current index instead of
inheriting a stale context window, which is what keeps answers correct after a
contract is reprocessed.

Every message records the prompt and model version that produced it, so an
answer given today remains explainable after the models move on (§17 audit).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ChatRole, ConfidenceBand, ResponseFormat, SearchScope
from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import pg_enum

if TYPE_CHECKING:
    from app.models.identity import User


class ChatSession(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """A Copilot conversation, scoped to the application, a project or a contract."""

    __tablename__ = "chat_sessions"

    #: Always set, even for an application-wide session: it records which project
    #: context the session was opened from and keeps the isolation filter simple.
    #: Application scope is only reachable by a System Admin (§1.1).
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    scope: Mapped[SearchScope] = mapped_column(
        pg_enum(SearchScope, "search_scope"),
        nullable=False,
        default=SearchScope.PROJECT,
        server_default=SearchScope.PROJECT.value,
    )
    #: Contract id when ``scope == contract``; project id when ``scope == project``.
    scope_ref: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    title: Mapped[str] = mapped_column(String(255), nullable=False, default="New conversation")
    #: Metadata filters pinned for the conversation (agreement type, date range,
    #: vendor). Re-applied on every turn so the user does not restate them.
    active_filters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    user: Mapped[User] = relationship("User", lazy="joined")
    messages: Mapped[list[ChatMessage]] = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
        lazy="noload",
    )

    __table_args__ = (
        Index("ix_chat_sessions_user_updated", "user_id", "updated_at"),
        Index("ix_chat_sessions_project_user", "project_id", "user_id"),
    )


class ChatMessage(Base, UUIDPrimaryKeyMixin):
    """One turn in a conversation.

    Assistant messages carry the full evidence trail: ``citations`` for rendering
    clickable references, ``evidence`` for the retrieval package that produced
    them, and the confidence breakdown behind the High/Medium/Low badge.
    """

    __tablename__ = "chat_messages"

    session_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )

    role: Mapped[ChatRole] = mapped_column(pg_enum(ChatRole, "chat_role"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    #: ``[{document_id, contract_title, clause_id, chunk_id, page_number,
    #:    bounding_boxes, confidence, artifact_version, snippet}]``
    #: Exactly what the viewer needs to jump to a page and draw the highlight.
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: The evidence package summary: strategy used, candidate counts, documents
    #: and clauses considered. Answers "why did it say that?".
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    confidence_band: Mapped[ConfidenceBand | None] = mapped_column(
        pg_enum(ConfidenceBand, "confidence_band"), nullable=True
    )
    #: Set when the answer needs a human to check it before it is relied on: a
    #: fabricated citation, an uncited factual answer, or low grounding confidence.
    #:
    #: Persisted rather than recomputed. The flag is a statement about what the
    #: system knew *at the time it answered* - the evidence it was shown and the
    #: validation it ran - and none of that is reconstructable later. Without the
    #: column a reloaded conversation silently presented every flagged answer as
    #: clean, which is a worse failure than never flagging them.
    needs_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Component scores behind ``confidence``: retrieval, evidence quality,
    #: citation coverage, model, validation (§17).
    confidence_breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    response_format: Mapped[ResponseFormat | None] = mapped_column(
        pg_enum(ResponseFormat, "response_format"), nullable=True
    )
    retrieval_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    query_intent: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- provenance ----------------------------------------------------------
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Full version set (planner, context engine, policy, output schema).
    versions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    token_usage: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Set when the answer was regenerated after failing validation, and why.
    was_regenerated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    validation_result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: True when the engine declared the evidence insufficient rather than
    #: guessing - a first-class outcome, not an error (§16 grounding rules).
    insufficient_evidence: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    #: Thumbs up/down from the user, feeding the satisfaction metric.
    user_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True
    )

    session: Mapped[ChatSession] = relationship("ChatSession", back_populates="messages")

    __table_args__ = (Index("ix_chat_messages_session_created", "session_id", "created_at"),)


__all__ = ["ChatMessage", "ChatSession"]

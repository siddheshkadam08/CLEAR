"""Project and project membership - the platform's security boundary.

A Project is *the* isolation unit (§1.1). Every contract-derived row carries
``project_id``, every read filters on it, and cross-project retrieval is
prohibited outside an explicitly authorised System Admin query. There is exactly
one organisation, so ``organization_id`` is a label rather than a boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ProjectStatus
from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import pg_enum

if TYPE_CHECKING:
    from app.models.contract import Contract
    from app.models.identity import Role, User


class Project(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """A project: the container for contracts and every derived artifact."""

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ProjectStatus] = mapped_column(
        pg_enum(ProjectStatus, "project_status"),
        nullable=False,
        default=ProjectStatus.ACTIVE,
        server_default=ProjectStatus.ACTIVE.value,
    )

    #: Single-organisation deployment: retained for reporting and future
    #: partitioning, never used as an authorisation check.
    organization_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    client_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    department: Mapped[str | None] = mapped_column(String(150), nullable=True)
    business_unit: Mapped[str | None] = mapped_column(String(150), nullable=True)
    default_language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="en", server_default="en"
    )

    #: Per-project overrides: default profile, review thresholds, alert windows,
    #: retention. Read by the Workflow Engine when planning a job, so a project
    #: can tighten review rules without touching global configuration.
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Denormalised counters maintained on ingestion completion. Dashboards read
    #: these instead of counting millions of rows on every page load; the
    #: scheduler reconciles them so drift is self-healing.
    contract_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    ready_contract_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    members: Mapped[list[ProjectMember]] = relationship(
        "ProjectMember",
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    contracts: Mapped[list[Contract]] = relationship(
        "Contract", back_populates="project", lazy="noload"
    )
    creator: Mapped[User | None] = relationship("User", foreign_keys=[created_by], lazy="joined")

    __table_args__ = (
        Index(
            "ix_projects_status_active",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_projects_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    def setting(self, key: str, default: Any = None) -> Any:
        """Read a project setting with a fallback."""
        return self.settings.get(key, default) if self.settings else default


class ProjectMember(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """``(user, project, role)`` - the authorisation triple.

    One row per user per project (enforced by the unique constraint), so a user's
    permissions on a project are unambiguous.
    """

    __tablename__ = "project_members"

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Narrow a role for one member without inventing a new role.
    permission_overrides: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    is_favourite: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    project: Mapped[Project] = relationship("Project", back_populates="members")
    user: Mapped[User] = relationship(
        "User", back_populates="memberships", foreign_keys=[user_id], lazy="joined"
    )
    role: Mapped[Role] = relationship("Role", back_populates="members", lazy="joined")

    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_members_project_id_user_id"),
        Index("ix_project_members_user_project", "user_id", "project_id"),
    )


class ProjectActivity(Base, UUIDPrimaryKeyMixin):
    """Human-readable activity feed backing the "Recent Activities" panel.

    Distinct from :class:`~app.models.audit.AuditLog`: the audit log is a
    complete, immutable compliance record; this is a small, presentation-shaped
    feed that can be pruned without losing the audit trail.
    """

    __tablename__ = "project_activities"

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    activity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True
    )

    user: Mapped[User | None] = relationship("User", lazy="joined")

    __table_args__ = (
        # Feed query is always "this project, newest first".
        Index("ix_project_activities_project_created", "project_id", "created_at"),
    )


__all__ = ["Project", "ProjectActivity", "ProjectMember"]

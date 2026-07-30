"""Identity and access models: users, roles, refresh tokens.

RBAC is ``(user, project, role)``. A user with no ``project_members`` row for a
project cannot see it at all - System Admin is the only bypass. Permissions live
as a JSONB list on the role so an administrator can adjust a role without a
migration, and they are resolved per request rather than baked into the JWT, so
revoking access takes effect immediately.
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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import AuthProvider, Permission, RoleName
from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import pg_enum

if TYPE_CHECKING:
    from app.models.project import ProjectMember


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """A platform user - local password account or Microsoft SSO identity."""

    __tablename__ = "users"

    # CITEXT: email uniqueness must be case-insensitive without every lookup
    # having to remember to lower() both sides.
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: NULL for SSO-only accounts - they authenticate against the identity
    #: provider and have no local credential to steal.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: Application-wide administrator. The only role that crosses project
    #: boundaries, and the only one that may run cross-project queries (§1.1).
    is_system_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    auth_provider: Mapped[AuthProvider] = mapped_column(
        pg_enum(AuthProvider, "auth_provider"),
        nullable=False,
        default=AuthProvider.LOCAL,
        server_default=AuthProvider.LOCAL.value,
    )
    #: Immutable subject claim from the IdP; the join key for SSO logins, since
    #: an email address can be reassigned but the object id cannot.
    external_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)

    job_title: Mapped[str | None] = mapped_column(String(150), nullable=True)
    department: Mapped[str | None] = mapped_column(String(150), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    locale: Mapped[str] = mapped_column(
        String(16), nullable=False, default="en", server_default="en"
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )
    #: UI preferences (theme, default project, table density). Never security data.
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Lockout counters. Reset on success; drive temporary lockout on abuse.
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    memberships: Mapped[list[ProjectMember]] = relationship(
        "ProjectMember",
        back_populates="user",
        foreign_keys="ProjectMember.user_id",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        "RefreshToken",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="noload",
    )

    __table_args__ = (
        UniqueConstraint("auth_provider", "external_subject", name="uq_users_provider_subject"),
        Index("ix_users_active", "is_active", postgresql_where=text("deleted_at IS NULL")),
        Index(
            "ix_users_full_name_trgm",
            "full_name",
            postgresql_using="gin",
            postgresql_ops={"full_name": "gin_trgm_ops"},
        ),
    )

    @property
    def is_locked(self) -> bool:
        if self.locked_until is None:
            return False
        from datetime import UTC

        return self.locked_until > datetime.now(UTC)


class Role(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A named permission set.

    Seeded with the four platform roles. ``permissions`` holds
    :class:`~app.core.enums.Permission` values; System Admin's list is complete
    but the flag on :class:`User` is what actually grants the bypass, so an
    accidental edit here cannot lock every administrator out.
    """

    __tablename__ = "roles"

    name: Mapped[RoleName] = mapped_column(
        pg_enum(RoleName, "role_name"), nullable=False, unique=True, index=True
    )
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    permissions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: Seeded roles cannot be deleted or renamed through the admin API.
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: Higher rank wins when a user somehow holds two roles on one project.
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    members: Mapped[list[ProjectMember]] = relationship(
        "ProjectMember", back_populates="role", lazy="noload"
    )

    def has_permission(self, permission: Permission | str) -> bool:
        value = permission.value if isinstance(permission, Permission) else permission
        return value in self.permissions


class RefreshToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A rotated refresh token.

    Only the SHA-256 digest is stored. Rotation revokes the old row and records
    the successor in ``replaced_by_id``, so replaying a stolen token hits a
    revoked row and the chain identifies the compromised session.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("refresh_tokens.id", ondelete="SET NULL"), nullable=True
    )
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship("User", back_populates="refresh_tokens")

    __table_args__ = (
        Index(
            "ix_refresh_tokens_user_active",
            "user_id",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    @property
    def is_active(self) -> bool:
        from datetime import UTC

        return self.revoked_at is None and self.expires_at > datetime.now(UTC)


__all__ = ["RefreshToken", "Role", "User"]

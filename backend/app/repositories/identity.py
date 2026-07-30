"""Repositories for users, roles and refresh tokens."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.orm import selectinload

from app.core.enums import AuthProvider, RoleName
from app.models.identity import RefreshToken, Role, User
from app.models.project import ProjectMember
from app.repositories.base import BaseRepository, affected_rows

#: Consecutive failures before an account is temporarily locked.
_MAX_FAILED_LOGINS = 8
_LOCKOUT_MINUTES = 15


class UserRepository(BaseRepository[User]):
    model = User
    sortable_fields = frozenset(
        {"email", "full_name", "created_at", "last_login_at", "is_active", "department"}
    )
    default_order_by = "created_at"

    # ------------------------------------------------------------------ lookup
    async def get_by_email(self, email: str, *, include_deleted: bool = False) -> User | None:
        """Find by email. ``CITEXT`` makes this case-insensitive in the database."""
        stmt = self.query() if include_deleted else self.live()
        return (
            await self.db.execute(stmt.where(User.email == email).limit(1))
        ).scalar_one_or_none()

    async def get_by_external_subject(self, provider: AuthProvider, subject: str) -> User | None:
        """Find by identity-provider subject claim.

        The subject is the stable SSO join key: an email address can be reassigned
        inside a tenant, but the object id cannot.
        """
        stmt = self.live().where(
            User.auth_provider == provider,
            User.external_subject == subject,
        )
        return (await self.db.execute(stmt.limit(1))).scalar_one_or_none()

    async def get_with_memberships(self, user_id: uuid.UUID) -> User | None:
        """Load a user with memberships, roles and projects in one round trip."""
        stmt = (
            self.live()
            .where(User.id == user_id)
            .options(
                selectinload(User.memberships).selectinload(ProjectMember.role),
                selectinload(User.memberships).selectinload(ProjectMember.project),
            )
        )
        return (await self.db.execute(stmt)).unique().scalar_one_or_none()

    # -------------------------------------------------------------------- list
    def search_query(
        self,
        *,
        search: str | None = None,
        is_active: bool | None = None,
        is_system_admin: bool | None = None,
        role: RoleName | None = None,
        project_id: uuid.UUID | None = None,
        department: str | None = None,
        auth_provider: str | None = None,
    ) -> Select[tuple[User]]:
        """Build the filtered user query used by the admin list."""
        stmt = self.live()

        if search:
            pattern = f"%{search.strip()}%"
            # The trigram index on full_name serves the ILIKE; email is CITEXT so
            # its comparison is already case-insensitive.
            stmt = stmt.where(
                or_(
                    User.full_name.ilike(pattern),
                    User.email.ilike(pattern),
                    func.coalesce(User.job_title, "").ilike(pattern),
                )
            )
        if is_active is not None:
            stmt = stmt.where(User.is_active.is_(is_active))
        if is_system_admin is not None:
            stmt = stmt.where(User.is_system_admin.is_(is_system_admin))
        if department:
            stmt = stmt.where(User.department == department)
        if auth_provider:
            stmt = stmt.where(User.auth_provider == auth_provider)

        # Role and project filters both need the membership table.
        if role is not None or project_id is not None:
            stmt = stmt.join(ProjectMember, ProjectMember.user_id == User.id)
            if project_id is not None:
                stmt = stmt.where(ProjectMember.project_id == project_id)
            if role is not None:
                stmt = stmt.join(Role, Role.id == ProjectMember.role_id).where(Role.name == role)
            stmt = stmt.distinct()

        return stmt

    async def project_counts(self, user_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Membership count per user - one query for a whole page of rows."""
        if not user_ids:
            return {}
        stmt = (
            select(ProjectMember.user_id, func.count(ProjectMember.id))
            .where(ProjectMember.user_id.in_(list(user_ids)))
            .group_by(ProjectMember.user_id)
        )
        rows = await self.db.execute(stmt)
        return {row[0]: int(row[1]) for row in rows}

    async def primary_roles(self, user_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Highest-ranked role each user holds anywhere.

        Shown as the user's effective role in the admin table, where one column
        must summarise memberships that may differ per project.
        """
        if not user_ids:
            return {}
        stmt = (
            select(ProjectMember.user_id, Role.name, Role.rank)
            .join(Role, Role.id == ProjectMember.role_id)
            .where(ProjectMember.user_id.in_(list(user_ids)))
        )
        best: dict[uuid.UUID, tuple[int, str]] = {}
        for user_id, role_name, rank in await self.db.execute(stmt):
            current = best.get(user_id)
            if current is None or rank > current[0]:
                best[user_id] = (int(rank), str(role_name))
        return {user_id: value[1] for user_id, value in best.items()}

    # ------------------------------------------------------------ login state
    async def record_successful_login(self, user: User) -> None:
        """Stamp the login and clear the lockout counters."""
        user.last_login_at = datetime.now(UTC)
        user.failed_login_count = 0
        user.locked_until = None
        await self.db.flush()

    async def record_failed_login(self, user: User) -> bool:
        """Increment the failure counter, locking the account past the threshold.

        Returns ``True`` when this failure caused a lockout. Throttles credential
        stuffing per account, complementing the IP-based rate limiter.
        """
        user.failed_login_count = (user.failed_login_count or 0) + 1
        locked = False
        if user.failed_login_count >= _MAX_FAILED_LOGINS:
            user.locked_until = datetime.now(UTC) + timedelta(minutes=_LOCKOUT_MINUTES)
            user.failed_login_count = 0
            locked = True
        await self.db.flush()
        return locked

    async def set_password(
        self, user: User, password_hash: str, *, must_change: bool = False
    ) -> None:
        user.password_hash = password_hash
        user.must_change_password = must_change
        await self.db.flush()


class RoleRepository(BaseRepository[Role]):
    model = Role
    sortable_fields = frozenset({"name", "rank", "created_at"})
    default_order_by = "rank"

    async def get_by_name(self, name: RoleName | str) -> Role | None:
        return (
            await self.db.execute(self.query().where(Role.name == name).limit(1))
        ).scalar_one_or_none()

    async def all_roles(self) -> Sequence[Role]:
        return (await self.db.execute(self.query().order_by(Role.rank.desc()))).scalars().all()

    async def by_name_map(self) -> dict[str, Role]:
        """``{role name: Role}`` - avoids one lookup per assignment in bulk work."""
        return {str(role.name): role for role in await self.all_roles()}


class RefreshTokenRepository(BaseRepository[RefreshToken]):
    model = RefreshToken
    default_order_by = "created_at"

    async def get_active_by_hash(self, token_hash: str) -> RefreshToken | None:
        """Find a usable token by digest.

        Only matches unrevoked, unexpired rows, so a replayed token simply is not
        found and the caller can treat it as invalid.
        """
        stmt = self.query().where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > datetime.now(UTC),
        )
        return (await self.db.execute(stmt.limit(1))).scalar_one_or_none()

    async def get_any_by_hash(self, token_hash: str) -> RefreshToken | None:
        """Find a token regardless of state.

        Used to detect reuse of an already-revoked token, which indicates the
        token was captured and is worth logging as a security event.
        """
        stmt = self.query().where(RefreshToken.token_hash == token_hash)
        return (await self.db.execute(stmt.limit(1))).scalar_one_or_none()

    async def issue(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> RefreshToken:
        return await self.create(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            user_agent=(user_agent or "")[:512] or None,
            ip_address=ip_address,
        )

    async def rotate(
        self,
        old: RefreshToken,
        *,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> RefreshToken:
        """Revoke ``old`` and issue its successor, recording the chain.

        Linking successor to predecessor is what makes a stolen token detectable:
        if the old digest is presented again, the chain identifies the compromised
        session rather than silently issuing a fresh pair.
        """
        replacement = await self.issue(
            user_id=old.user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        old.revoked_at = datetime.now(UTC)
        old.replaced_by_id = replacement.id
        await self.db.flush()
        return replacement

    async def revoke(self, token: RefreshToken) -> None:
        token.revoked_at = datetime.now(UTC)
        await self.db.flush()

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        """Revoke every active session for a user.

        Called on "sign out everywhere", on password change, and when an account
        is deactivated - an access token expires in minutes, but a live refresh
        token would otherwise keep the session alive for days.
        """
        result = await self.db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        await self.db.flush()
        return affected_rows(result)

    async def active_sessions(self, user_id: uuid.UUID) -> Sequence[RefreshToken]:
        stmt = (
            self.query()
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > datetime.now(UTC),
            )
            .order_by(RefreshToken.created_at.desc())
        )
        return (await self.db.execute(stmt)).scalars().all()

    async def purge_expired(self, *, older_than_days: int = 30) -> int:
        """Delete long-expired tokens. Hard delete: no audit value in a dead digest."""
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        return await self.delete_where(RefreshToken.expires_at < cutoff)


__all__ = ["RefreshTokenRepository", "RoleRepository", "UserRepository"]

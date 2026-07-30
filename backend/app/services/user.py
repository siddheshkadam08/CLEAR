"""User administration service (User Management screen).

Account creation is administrator-only - there is no self-service signup, which is
why the Login screen says "Contact Admin". Two invariants are enforced here
because losing either one is unrecoverable through the API:

* the last active System Administrator cannot be demoted or deactivated;
* nobody can change their own privilege flags.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AuditAction, RoleName
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.security import hash_password, validate_password_strength
from app.models.identity import User
from app.repositories.identity import RefreshTokenRepository, RoleRepository, UserRepository
from app.repositories.project import ProjectMemberRepository, ProjectRepository
from app.schemas.common import Paginated, ProjectRef, UserRef
from app.schemas.user import (
    AdminSetPasswordRequest,
    ProfileUpdateRequest,
    RoleResponse,
    UserCreateRequest,
    UserFilterParams,
    UserListItem,
    UserMembershipSummary,
    UserResponse,
    UserUpdateRequest,
)
from app.services.audit import AuditService, snapshot

logger = get_logger(__name__)


class UserService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.users = UserRepository(db)
        self.roles = RoleRepository(db)
        self.members = ProjectMemberRepository(db)
        self.projects = ProjectRepository(db)
        self.tokens = RefreshTokenRepository(db)
        self.audit = AuditService(db)

    # =========================================================================
    # Read
    # =========================================================================
    async def list_users(
        self,
        *,
        filters: UserFilterParams,
        page: int = 1,
        size: int = 25,
        sort_by: str | None = None,
        sort_dir: str = "desc",
    ) -> Paginated[UserListItem]:
        stmt = self.users.search_query(
            search=filters.search,
            is_active=filters.is_active,
            is_system_admin=filters.is_system_admin,
            role=filters.role,
            project_id=filters.project_id,
            department=filters.department,
            auth_provider=filters.auth_provider,
        )
        rows, total = await self.users.paginate(
            stmt, page=page, size=size, sort_by=sort_by, sort_dir=sort_dir
        )

        # Two aggregate queries for the whole page rather than two per row.
        user_ids = [row.id for row in rows]
        counts = await self.users.project_counts(user_ids)
        primary_roles = await self.users.primary_roles(user_ids)

        items = [
            UserListItem(
                id=row.id,
                email=row.email,
                full_name=row.full_name,
                is_active=row.is_active,
                is_system_admin=row.is_system_admin,
                auth_provider=str(row.auth_provider),
                job_title=row.job_title,
                department=row.department,
                primary_role=str(RoleName.SYSTEM_ADMIN)
                if row.is_system_admin
                else primary_roles.get(row.id),
                project_count=counts.get(row.id, 0),
                last_login_at=row.last_login_at,
                created_at=row.created_at,
            )
            for row in rows
        ]
        return Paginated.build(items, page=page, size=size, total=total)

    async def get_user(self, user_id: uuid.UUID) -> UserResponse:
        user = await self.users.get_with_memberships(user_id)
        if user is None:
            raise NotFoundError("User", user_id)
        return self._to_response(user)

    def _to_response(self, user: User) -> UserResponse:
        memberships: list[UserMembershipSummary] = []
        for member in user.memberships or []:
            project = member.project
            if project is None or project.deleted_at is not None:
                continue
            memberships.append(
                UserMembershipSummary(
                    project=ProjectRef(id=project.id, name=project.name, slug=project.slug),
                    role=str(member.role.name),
                    role_display_name=member.role.display_name,
                    added_at=member.created_at,
                )
            )

        return UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            is_active=user.is_active,
            is_system_admin=user.is_system_admin,
            must_change_password=user.must_change_password,
            auth_provider=str(user.auth_provider),
            job_title=user.job_title,
            department=user.department,
            avatar_url=user.avatar_url,
            locale=user.locale,
            timezone=user.timezone,
            last_login_at=user.last_login_at,
            created_at=user.created_at,
            updated_at=user.updated_at,
            is_locked=user.is_locked,
            project_count=len(memberships),
            memberships=memberships,
        )

    async def list_roles(self) -> list[RoleResponse]:
        return [
            RoleResponse(
                id=role.id,
                name=str(role.name),
                display_name=role.display_name,
                description=role.description,
                permissions=list(role.permissions or []),
                is_system=role.is_system,
                rank=role.rank,
            )
            for role in await self.roles.all_roles()
        ]

    # =========================================================================
    # Create
    # =========================================================================
    async def create_user(
        self,
        payload: UserCreateRequest,
        *,
        actor: User,
        ip: str | None = None,
    ) -> UserResponse:
        """Create an account, optionally with initial project memberships."""
        existing = await self.users.get_by_email(payload.email, include_deleted=True)
        if existing is not None:
            if existing.deleted_at is not None:
                raise ConflictError(
                    "An archived account already uses this email address. "
                    "Restore it instead of creating a duplicate.",
                    details={"user_id": str(existing.id)},
                )
            raise ConflictError("An account with this email address already exists.")

        password_hash: str | None = None
        force_change = payload.must_change_password
        if payload.password:
            validate_password_strength(payload.password)
            password_hash = hash_password(payload.password)
        elif payload.use_default_password:
            from app.core.config import get_settings

            # Not validated for strength: it is a deployment-configured starting
            # credential, and the account cannot do anything with it beyond reaching
            # the change-password screen (see `require_password_current`).
            password_hash = hash_password(get_settings().security.new_user_default_password)
            force_change = True
        elif not payload.project_assignments and not payload.is_system_admin:
            # No password and no project access means an account that can neither
            # sign in locally nor do anything after SSO - almost certainly a mistake.
            logger.info("user_created_without_password_or_projects", email=payload.email)

        user = await self.users.create(
            email=payload.email.lower(),
            full_name=payload.full_name,
            password_hash=password_hash,
            is_active=payload.is_active,
            is_system_admin=payload.is_system_admin,
            # A user given a password by an admin must change it on first sign-in;
            # an SSO-only account has nothing to change.
            must_change_password=force_change if password_hash else False,
            job_title=payload.job_title,
            department=payload.department,
            locale=payload.locale,
            timezone=payload.timezone,
        )

        role_map = await self.roles.by_name_map()
        for assignment in payload.project_assignments:
            project = await self.projects.get(assignment.project_id)
            if project is None:
                raise NotFoundError("Project", assignment.project_id)
            role = role_map.get(str(assignment.role))
            if role is None:
                raise ValidationError(f"Role '{assignment.role}' is not configured.")
            await self.members.add_member(
                project_id=project.id,
                user_id=user.id,
                role_id=role.id,
                added_by=actor.id,
            )

        await self.audit.record(
            action=AuditAction.CREATE,
            entity_type="user",
            entity_id=user.id,
            entity_label=user.email,
            user_id=actor.id,
            user_email=actor.email,
            after=snapshot(user),
            ip=ip,
        )
        logger.info("user_created", user_id=str(user.id), created_by=str(actor.id))

        return await self.get_user(user.id)

    # =========================================================================
    # Update
    # =========================================================================
    async def update_user(
        self,
        user_id: uuid.UUID,
        payload: UserUpdateRequest,
        *,
        actor: User,
        ip: str | None = None,
    ) -> UserResponse:
        user = await self.users.get(user_id)
        if user is None:
            raise NotFoundError("User", user_id)

        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        if not changes:
            return await self.get_user(user_id)

        # Self-privilege changes are refused outright: an administrator must not be
        # able to escalate or lock themselves out through the same endpoint.
        if user.id == actor.id and ("is_system_admin" in changes or "is_active" in changes):
            raise ForbiddenError("You cannot change your own administrator status or active state.")

        if changes.get("is_system_admin") is False or changes.get("is_active") is False:
            await self._guard_last_admin(user)

        before = snapshot(user)
        await self.users.update(user, **changes)

        # Deactivation must also end live sessions; a refresh token would otherwise
        # keep the account usable for days.
        if changes.get("is_active") is False:
            revoked = await self.tokens.revoke_all_for_user(user.id)
            logger.info("user_deactivated", user_id=str(user.id), sessions_revoked=revoked)

        await self.audit.record(
            action=AuditAction.PERMISSION_CHANGE
            if {"is_system_admin", "is_active"} & changes.keys()
            else AuditAction.UPDATE,
            entity_type="user",
            entity_id=user.id,
            entity_label=user.email,
            user_id=actor.id,
            user_email=actor.email,
            before=before,
            after=snapshot(user),
            ip=ip,
        )
        return await self.get_user(user_id)

    async def update_own_profile(
        self,
        user: User,
        payload: ProfileUpdateRequest,
        *,
        ip: str | None = None,
    ) -> UserResponse:
        """Self-service profile update. Cannot touch privileges by construction."""
        changes = payload.model_dump(exclude_unset=True)
        changes = {k: v for k, v in changes.items() if v is not None}
        if not changes:
            return await self.get_user(user.id)

        before = snapshot(user)
        await self.users.update(user, **changes)
        await self.audit.record(
            action=AuditAction.UPDATE,
            entity_type="user",
            entity_id=user.id,
            entity_label=user.email,
            user_id=user.id,
            user_email=user.email,
            before=before,
            after=snapshot(user),
            ip=ip,
        )
        return await self.get_user(user.id)

    async def set_password(
        self,
        user_id: uuid.UUID,
        payload: AdminSetPasswordRequest,
        *,
        actor: User,
        ip: str | None = None,
    ) -> None:
        """Administrative password reset."""
        user = await self.users.get(user_id)
        if user is None:
            raise NotFoundError("User", user_id)

        validate_password_strength(payload.new_password)
        await self.users.set_password(
            user, hash_password(payload.new_password), must_change=payload.must_change_password
        )
        revoked = await self.tokens.revoke_all_for_user(user.id)

        await self.audit.record(
            action=AuditAction.UPDATE,
            entity_type="user",
            entity_id=user.id,
            entity_label=user.email,
            user_id=actor.id,
            user_email=actor.email,
            after={"password_reset_by_admin": True, "sessions_revoked": revoked},
            ip=ip,
        )
        logger.info(
            "admin_password_reset",
            user_id=str(user.id),
            by=str(actor.id),
            sessions_revoked=revoked,
        )

    # =========================================================================
    # Delete
    # =========================================================================
    async def delete_user(
        self,
        user_id: uuid.UUID,
        *,
        actor: User,
        ip: str | None = None,
    ) -> None:
        """Soft-delete an account and revoke its sessions.

        Soft delete, not hard: the audit log references this user, and uploaded
        contracts record them as uploader. Removing the row would break that trail.
        """
        user = await self.users.get(user_id)
        if user is None:
            raise NotFoundError("User", user_id)
        if user.id == actor.id:
            raise ForbiddenError("You cannot delete your own account.")

        await self._guard_last_admin(user)

        before = snapshot(user)
        await self.users.soft_delete(user)
        await self.users.update(user, is_active=False)
        revoked = await self.tokens.revoke_all_for_user(user.id)

        await self.audit.record(
            action=AuditAction.DELETE,
            entity_type="user",
            entity_id=user.id,
            entity_label=user.email,
            user_id=actor.id,
            user_email=actor.email,
            before=before,
            after={"deleted": True, "sessions_revoked": revoked},
            ip=ip,
        )
        logger.info("user_deleted", user_id=str(user.id), by=str(actor.id))

    async def _guard_last_admin(self, user: User) -> None:
        """Refuse to remove the last active System Administrator.

        Without this, a single careless update can leave the platform with nobody
        able to manage users, projects or master data - unrecoverable through the
        API.
        """
        if not user.is_system_admin:
            return
        remaining = (
            await self.db.execute(
                select(func.count(User.id)).where(
                    User.is_system_admin.is_(True),
                    User.is_active.is_(True),
                    User.deleted_at.is_(None),
                    User.id != user.id,
                )
            )
        ).scalar() or 0
        if remaining == 0:
            raise ForbiddenError(
                "This is the only active System Administrator. Promote another "
                "administrator before changing this account."
            )

    # =========================================================================
    # Helpers
    # =========================================================================
    async def find_assignable(self, *, search: str | None = None, limit: int = 50) -> list[UserRef]:
        """Active users, for the "add member" picker."""
        stmt = self.users.search_query(search=search, is_active=True).limit(limit)
        rows: Sequence[User] = (await self.db.execute(stmt)).scalars().all()
        return [
            UserRef(
                id=row.id,
                email=row.email,
                full_name=row.full_name,
                avatar_url=row.avatar_url,
            )
            for row in rows
        ]

    async def update_role_permissions(
        self,
        role_name: RoleName,
        *,
        permissions: list[str] | None,
        description: str | None,
        actor: User,
        ip: str | None = None,
    ) -> RoleResponse:
        """Adjust a role's permission set.

        The System Admin role is not editable here: its authority comes from the
        ``is_system_admin`` flag, and letting it be narrowed would create a
        confusing half-privileged state.
        """
        role = await self.roles.get_by_name(role_name)
        if role is None:
            raise NotFoundError("Role", role_name)
        if role.name == RoleName.SYSTEM_ADMIN:
            raise ForbiddenError("The System Administrator role cannot be modified.")

        from app.core.enums import Permission

        before: dict[str, Any] = {"permissions": list(role.permissions or [])}
        if permissions is not None:
            valid = {p.value for p in Permission}
            unknown = [p for p in permissions if p not in valid]
            if unknown:
                raise ValidationError("Unknown permissions supplied.", details={"unknown": unknown})
            role.permissions = permissions
        if description is not None:
            role.description = description
        await self.db.flush()

        await self.audit.record(
            action=AuditAction.PERMISSION_CHANGE,
            entity_type="role",
            entity_id=role.id,
            entity_label=str(role.name),
            user_id=actor.id,
            user_email=actor.email,
            before=before,
            after={"permissions": list(role.permissions or [])},
            ip=ip,
        )
        return RoleResponse(
            id=role.id,
            name=str(role.name),
            display_name=role.display_name,
            description=role.description,
            permissions=list(role.permissions or []),
            is_system=role.is_system,
            rank=role.rank,
        )


__all__ = ["UserService"]

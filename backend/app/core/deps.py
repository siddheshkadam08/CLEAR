"""FastAPI dependencies: authentication, authorization and project scoping.

This module is where the platform's security boundary is enforced. Three layers,
each depending on the previous:

1. :func:`get_current_user` - who is calling.
2. :func:`get_project_context` - which project, and may they see it at all?
   Non-members get 404, not 403: confirming that a project id exists is itself a
   leak across the isolation boundary.
3. :func:`require_permission` - may they do *this*?

Permissions are resolved from the database on every request rather than read from
the JWT, so revoking a membership takes effect immediately (see
:mod:`app.services.auth`).

Endpoints declare their requirements in the signature, which makes the check
impossible to forget and visible in the OpenAPI schema:

    @router.post("/projects/{project_id}/contracts/upload")
    async def upload(
        ctx: ProjectContext = Depends(require_permission(Permission.CONTRACT_UPLOAD)),
    ) -> ...:
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Header, Path, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import Settings, get_settings
from app.core.enums import Permission, RoleName
from app.core.errors import (
    ForbiddenError,
    NotFoundError,
    PermissionDeniedError,
    ProjectAccessDeniedError,
    UnauthenticatedError,
)
from app.core.logging import bind_context, get_logger
from app.core.security import decode_token, verify_internal_token
from app.core.telemetry import set_span_attributes
from app.db.session import get_db
from app.models.identity import User
from app.models.project import Project, ProjectMember
from app.schemas.common import PaginationParams, SortParams

logger = get_logger(__name__)

# auto_error=False so a missing header raises our own envelope rather than
# FastAPI's default 403 shape.
_bearer = HTTPBearer(auto_error=False, description="JWT access token")

DbSession = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


# =============================================================================
# Authentication
# =============================================================================
async def get_current_user(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> User:
    """Resolve the authenticated user from the bearer token.

    The user row is loaded on every request: a token issued before an account was
    deactivated must stop working immediately, not at expiry.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthenticatedError()

    payload = decode_token(credentials.credentials, expected_type="access")

    try:
        user_id = uuid.UUID(str(payload.get("sub")))
    except (TypeError, ValueError) as exc:
        raise UnauthenticatedError("Malformed authentication token.") from exc

    from app.repositories.identity import UserRepository

    user = await UserRepository(db).get(user_id)
    if user is None:
        # Token is validly signed but the account is gone.
        raise UnauthenticatedError("This account no longer exists.")
    if not user.is_active:
        metrics.authorization_denied_total.labels(reason="inactive_user").inc()
        raise ForbiddenError("This account has been deactivated.")

    bind_context(user_id=str(user.id))
    set_span_attributes(**{"enduser.id": str(user.id)})
    request.state.user = user
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


async def get_current_user_optional(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> User | None:
    """Like :func:`get_current_user` but tolerates an anonymous caller."""
    if credentials is None or not credentials.credentials:
        return None
    try:
        return await get_current_user(request, db, credentials)
    except (UnauthenticatedError, ForbiddenError):
        return None


async def require_password_current(user: CurrentUserDep) -> User:
    """Block normal API use while a forced password change is outstanding.

    Applied to everything except the change-password and logout endpoints, so a
    seeded admin cannot keep operating on the default credential.
    """
    if user.must_change_password:
        raise ForbiddenError(
            "You must change your password before continuing.",
            details={"must_change_password": True},
        )
    return user


async def require_system_admin(user: CurrentUserDep) -> User:
    """Platform administration. The only role that crosses project boundaries."""
    if not user.is_system_admin:
        metrics.authorization_denied_total.labels(reason="not_system_admin").inc()
        logger.warning("system_admin_required", user_id=str(user.id))
        raise PermissionDeniedError("This action requires System Administrator access.")
    return user


SystemAdminDep = Annotated[User, Depends(require_system_admin)]


#: Capabilities a System Administrator does **not** inherit.
#:
#: The administrator is an oversight and governance role: it reads every project,
#: creates projects and provisions users, but it does not author contract content.
#: Someone who can both grant themselves access to any project *and* put documents
#: into it has no separation of duties left, and the audit trail stops answering
#: "who brought this contract into the repository".
#:
#: The exclusion lives here rather than at each call site so that an endpoint added
#: later, which merely declares ``require_permission(Permission.CONTRACT_UPLOAD)``,
#: is covered on the day it is written.
ADMIN_EXCLUDED_PERMISSIONS: frozenset[str] = frozenset({Permission.CONTRACT_UPLOAD.value})


async def require_internal_caller(
    x_internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> bool:
    """Guard the ``/internal/*`` stage endpoints.

    These are called by the queue shim inside the cluster, never by a browser.
    A shared secret compared in constant time keeps them off the public surface;
    in Kubernetes they are additionally unreachable from outside the namespace.
    """
    if not verify_internal_token(x_internal_token):
        logger.warning("internal_token_rejected")
        raise UnauthenticatedError("Invalid internal service token.")
    return True


InternalCallerDep = Annotated[bool, Depends(require_internal_caller)]


# =============================================================================
# Project context
# =============================================================================
@dataclass(slots=True)
class ProjectContext:
    """A validated (user, project, permissions) triple.

    Handlers receive this instead of a bare project id, so the fact that access
    was checked is structural rather than a convention.
    """

    user: User
    project: Project
    membership: ProjectMember | None
    permissions: frozenset[str] = field(default_factory=frozenset)
    role: str | None = None

    @property
    def project_id(self) -> uuid.UUID:
        return self.project.id

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def is_system_admin(self) -> bool:
        return self.user.is_system_admin

    def has(self, permission: Permission | str) -> bool:
        """Does the caller hold this permission on this project?

        System Administrators hold everything except
        :data:`ADMIN_EXCLUDED_PERMISSIONS`, which they are never granted - see the
        note on that constant. The exclusion is applied by leaving those values out
        of ``permissions`` in :func:`get_project_context`, so this single membership
        test is correct for every caller.
        """
        value = permission.value if isinstance(permission, Permission) else permission
        if self.user.is_system_admin and value not in ADMIN_EXCLUDED_PERMISSIONS:
            return True
        return value in self.permissions

    def require(self, permission: Permission | str) -> None:
        """Assert a permission mid-handler, for conditional operations."""
        if not self.has(permission):
            value = permission.value if isinstance(permission, Permission) else permission
            metrics.authorization_denied_total.labels(reason="permission").inc()
            logger.warning(
                "permission_denied",
                user_id=str(self.user.id),
                project_id=str(self.project.id),
                permission=value,
            )
            raise PermissionDeniedError(value)


async def get_project_context(
    project_id: Annotated[uuid.UUID, Path(description="Project id")],
    user: CurrentUserDep,
    db: DbSession,
) -> ProjectContext:
    """Load a project and verify the caller may see it.

    A user with no membership row cannot see the project at all (§7.1). The
    response is 404 rather than 403 so an outsider cannot use the API to confirm
    which project ids exist.
    """
    from app.repositories.project import ProjectMemberRepository, ProjectRepository

    project = await ProjectRepository(db).get(project_id)
    if project is None:
        raise NotFoundError("Project", project_id)

    bind_context(project_id=str(project.id))
    set_span_attributes(**{"cip.project_id": str(project.id)})

    if user.is_system_admin:
        # Admins bypass membership but still get an explicit permission set, so
        # `ctx.has(...)` behaves uniformly for every caller. The set is built by
        # subtraction rather than addition: a permission added to the enum later is
        # granted automatically, and one that must stay out of admin hands has to be
        # named in ADMIN_EXCLUDED_PERMISSIONS on purpose.
        #
        # The membership row is deliberately not consulted. An administrator who is
        # also a project member must not pick up an excluded capability through that
        # membership, or the restriction would be one "add me to the project" away
        # from being undone by the person it constrains.
        return ProjectContext(
            user=user,
            project=project,
            membership=None,
            permissions=frozenset(p.value for p in Permission) - ADMIN_EXCLUDED_PERMISSIONS,
            role=str(RoleName.SYSTEM_ADMIN),
        )

    membership = await ProjectMemberRepository(db).get_membership(project_id, user.id)
    if membership is None:
        metrics.authorization_denied_total.labels(reason="project_access").inc()
        logger.warning(
            "project_access_denied",
            user_id=str(user.id),
            project_id=str(project_id),
        )
        # Same shape as a genuine miss - see docstring.
        raise NotFoundError("Project", project_id)

    role = membership.role
    permissions = set(role.permissions or [])
    if membership.permission_overrides:
        # Overrides narrow a role for one member; they never widen it.
        permissions &= set(membership.permission_overrides)

    return ProjectContext(
        user=user,
        project=project,
        membership=membership,
        permissions=frozenset(permissions),
        role=str(role.name),
    )


ProjectContextDep = Annotated[ProjectContext, Depends(get_project_context)]


def require_permission(
    *permissions: Permission,
    require_all: bool = True,
) -> Callable[..., Awaitable[ProjectContext]]:
    """Dependency factory: project access **plus** the named permissions.

    >>> ctx: ProjectContext = Depends(require_permission(Permission.CONTRACT_UPLOAD))

    ``require_all=False`` accepts any one of several permissions, for endpoints
    reachable by more than one role.
    """

    async def dependency(ctx: ProjectContextDep) -> ProjectContext:
        # No early return for administrators. They already hold every permission
        # except the excluded ones (see get_project_context), so the ordinary check
        # below gives them the same answer - and, unlike a bypass, it still says no
        # to the handful of things an administrator is not meant to do.
        held = [ctx.has(p) for p in permissions]
        ok = all(held) if require_all else any(held)
        if not ok:
            missing = [p.value for p, granted in zip(permissions, held, strict=True) if not granted]
            metrics.authorization_denied_total.labels(reason="permission").inc()
            logger.warning(
                "permission_denied",
                user_id=str(ctx.user_id),
                project_id=str(ctx.project_id),
                missing=missing,
                role=ctx.role,
                is_system_admin=ctx.is_system_admin,
            )
            if ctx.is_system_admin:
                # Generic "your role does not grant this" is actively confusing for
                # an account that holds every other permission on the platform.
                raise PermissionDeniedError(
                    ", ".join(missing),
                    message=(
                        "System Administrators cannot upload contracts. Sign in as a "
                        "project member, or ask one to upload on your behalf."
                    ),
                )
            raise PermissionDeniedError(", ".join(missing))
        return ctx

    return dependency


def require_project_role(*roles: RoleName) -> Callable[..., Awaitable[ProjectContext]]:
    """Require one of several roles on the project.

    Prefer :func:`require_permission`; use this only where the rule genuinely is
    about the role rather than a capability.
    """

    async def dependency(ctx: ProjectContextDep) -> ProjectContext:
        if ctx.is_system_admin:
            return ctx
        if ctx.role not in {str(r) for r in roles}:
            metrics.authorization_denied_total.labels(reason="role").inc()
            raise PermissionDeniedError(
                details={"required_roles": [str(r) for r in roles], "actual_role": ctx.role}
            )
        return ctx

    return dependency


# =============================================================================
# Cross-project scope
# =============================================================================
@dataclass(slots=True)
class ContractContext:
    """A validated (user, contract, project) triple.

    Contract-scoped routes do not carry ``project_id`` in the path, so the project is
    resolved *from the contract* and the identical membership check applied. Handlers
    receive this rather than a bare contract id, so the fact that access was verified
    is structural rather than a convention each route has to remember.
    """

    contract_id: uuid.UUID
    project: ProjectContext

    @property
    def project_id(self) -> uuid.UUID:
        return self.project.project_id

    @property
    def user(self) -> User:
        return self.project.user

    def require(self, permission: Permission | str) -> None:
        self.project.require(permission)

    def has(self, permission: Permission | str) -> bool:
        return self.project.has(permission)


async def get_contract_context(
    contract_id: Annotated[uuid.UUID, Path(description="Contract id")],
    user: CurrentUserDep,
    db: DbSession,
) -> ContractContext:
    """Resolve a contract's project and verify the caller may access it.

    Returns 404 when the caller is not a member of the owning project - the same
    shape as a genuine miss, so an outsider cannot probe the API for valid contract
    ids by comparing 403s against 404s.
    """
    from sqlalchemy import select

    from app.models.contract import Contract

    project_id = (
        await db.execute(
            select(Contract.project_id).where(
                Contract.id == contract_id, Contract.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()

    if project_id is None:
        raise NotFoundError("Contract", contract_id)

    project = await get_project_context(project_id, user, db)
    return ContractContext(contract_id=contract_id, project=project)


ContractContextDep = Annotated[ContractContext, Depends(get_contract_context)]


@dataclass(slots=True)
class AccessScope:
    """The set of projects a request may read.

    Used by application-wide endpoints (overall dashboard, cross-project search).
    "All projects" always means *this list* - the projects the caller is a member
    of - never the whole table. A System Admin's list is every project, which is
    the only sanctioned cross-project path (§1.1).
    """

    user: User
    project_ids: list[uuid.UUID]

    @property
    def is_system_admin(self) -> bool:
        return self.user.is_system_admin

    @property
    def is_empty(self) -> bool:
        return not self.project_ids

    def contains(self, project_id: uuid.UUID) -> bool:
        return project_id in self.project_ids

    def require(self, project_id: uuid.UUID) -> None:
        if not self.contains(project_id):
            raise ProjectAccessDeniedError()


async def get_access_scope(user: CurrentUserDep, db: DbSession) -> AccessScope:
    """Resolve every project the caller may read."""
    from app.repositories.project import ProjectRepository

    project_ids = await ProjectRepository(db).accessible_ids(
        user.id, is_system_admin=user.is_system_admin
    )
    return AccessScope(user=user, project_ids=project_ids)


AccessScopeDep = Annotated[AccessScope, Depends(get_access_scope)]


async def resolve_scope_for_project(
    project_id: uuid.UUID | None,
    scope: AccessScope,
) -> list[uuid.UUID]:
    """Narrow an access scope to one project, or keep it application-wide.

    Shared by the dashboard, search and export endpoints, which all accept an
    optional project filter over the caller's accessible set.
    """
    if project_id is None:
        return scope.project_ids
    scope.require(project_id)
    return [project_id]


# =============================================================================
# Query parameter dependencies
# =============================================================================
async def pagination(
    page: Annotated[int, Query(ge=1, le=100_000, description="1-based page number")] = 1,
    size: Annotated[int, Query(ge=1, le=200, description="Items per page")] = 25,
) -> PaginationParams:
    return PaginationParams(page=page, size=size)


PaginationDep = Annotated[PaginationParams, Depends(pagination)]


async def sorting(
    sort_by: Annotated[str | None, Query(description="Field to sort by")] = None,
    sort_dir: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
) -> SortParams:
    return SortParams(sort_by=sort_by, sort_dir=sort_dir)


SortingDep = Annotated[SortParams, Depends(sorting)]


# =============================================================================
# Request metadata
# =============================================================================
@dataclass(slots=True)
class RequestInfo:
    """Client metadata for audit records."""

    ip: str | None
    user_agent: str | None
    route: str | None
    request_id: str | None


async def request_info(request: Request) -> RequestInfo:
    from app.core.middleware import client_ip

    route = request.scope.get("route")
    return RequestInfo(
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        route=getattr(route, "path", None),
        request_id=getattr(request.state, "request_id", None),
    )


RequestInfoDep = Annotated[RequestInfo, Depends(request_info)]


__all__ = [
    "ADMIN_EXCLUDED_PERMISSIONS",
    "AccessScope",
    "AccessScopeDep",
    "AppSettings",
    "ContractContext",
    "ContractContextDep",
    "CurrentUserDep",
    "DbSession",
    "InternalCallerDep",
    "PaginationDep",
    "ProjectContext",
    "ProjectContextDep",
    "RequestInfo",
    "RequestInfoDep",
    "SortingDep",
    "SystemAdminDep",
    "get_access_scope",
    "get_contract_context",
    "get_current_user",
    "get_current_user_optional",
    "get_project_context",
    "pagination",
    "request_info",
    "require_internal_caller",
    "require_password_current",
    "require_permission",
    "require_project_role",
    "require_system_admin",
    "resolve_scope_for_project",
    "sorting",
]

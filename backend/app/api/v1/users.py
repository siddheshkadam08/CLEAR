"""User, role and profile endpoints.

User management is System Admin only. ``/profile`` is the self-service subset any
authenticated user may call, and it cannot touch privilege flags by construction
(see :class:`~app.schemas.user.ProfileUpdateRequest`).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import (
    CurrentUserDep,
    DbSession,
    PaginationDep,
    RequestInfoDep,
    SortingDep,
    SystemAdminDep,
)
from app.core.enums import RoleName
from app.schemas.common import MessageResponse, Paginated, UserRef
from app.schemas.user import (
    AdminSetPasswordRequest,
    ProfileUpdateRequest,
    RoleResponse,
    RoleUpdateRequest,
    UserCreateRequest,
    UserFilterParams,
    UserListItem,
    UserResponse,
    UserUpdateRequest,
)
from app.services.user import UserService

router = APIRouter(tags=["Users"])


# =============================================================================
# Self-service profile
# =============================================================================
@router.get("/profile", response_model=UserResponse, summary="Your own profile")
async def get_profile(user: CurrentUserDep, db: DbSession) -> UserResponse:
    return await UserService(db).get_user(user.id)


@router.patch("/profile", response_model=UserResponse, summary="Update your own profile")
async def update_profile(
    payload: ProfileUpdateRequest,
    user: CurrentUserDep,
    db: DbSession,
    info: RequestInfoDep,
) -> UserResponse:
    return await UserService(db).update_own_profile(user, payload, ip=info.ip)


# =============================================================================
# Roles
# =============================================================================
@router.get(
    "/roles",
    response_model=list[RoleResponse],
    summary="Platform roles and their permissions",
)
async def list_roles(user: CurrentUserDep, db: DbSession) -> list[RoleResponse]:
    """Readable by any authenticated user - the SPA needs role labels to render."""
    return await UserService(db).list_roles()


@router.patch(
    "/roles/{role_name}",
    response_model=RoleResponse,
    summary="Adjust a role's permission set",
    responses={403: {"description": "The System Administrator role cannot be modified"}},
)
async def update_role(
    role_name: RoleName,
    payload: RoleUpdateRequest,
    admin: SystemAdminDep,
    db: DbSession,
    info: RequestInfoDep,
) -> RoleResponse:
    return await UserService(db).update_role_permissions(
        role_name,
        permissions=payload.permissions,
        description=payload.description,
        actor=admin,
        ip=info.ip,
    )


# =============================================================================
# User administration
# =============================================================================
@router.get(
    "/users",
    response_model=Paginated[UserListItem],
    summary="List users",
)
async def list_users(
    admin: SystemAdminDep,
    db: DbSession,
    pagination: PaginationDep,
    sorting: SortingDep,
    search: Annotated[str | None, Query(max_length=200)] = None,
    is_active: Annotated[bool | None, Query()] = None,
    is_system_admin: Annotated[bool | None, Query()] = None,
    role: Annotated[RoleName | None, Query()] = None,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    department: Annotated[str | None, Query()] = None,
    auth_provider: Annotated[str | None, Query()] = None,
) -> Paginated[UserListItem]:
    filters = UserFilterParams(
        search=search,
        is_active=is_active,
        is_system_admin=is_system_admin,
        role=role,
        project_id=project_id,
        department=department,
        auth_provider=auth_provider,
    )
    return await UserService(db).list_users(
        filters=filters,
        page=pagination.page,
        size=pagination.size,
        sort_by=sorting.sort_by,
        sort_dir=sorting.sort_dir,
    )


@router.get(
    "/users/assignable",
    response_model=list[UserRef],
    summary="Active users, for the project member picker",
)
async def assignable_users(
    user: CurrentUserDep,
    db: DbSession,
    search: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[UserRef]:
    """Available to any authenticated user so a Contract Manager can invite members.

    Returns identity only - no roles, activity or privilege flags.
    """
    return await UserService(db).find_assignable(search=search, limit=limit)


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user",
    responses={409: {"description": "Email address already in use"}},
)
async def create_user(
    payload: UserCreateRequest,
    admin: SystemAdminDep,
    db: DbSession,
    info: RequestInfoDep,
) -> UserResponse:
    return await UserService(db).create_user(payload, actor=admin, ip=info.ip)


@router.get("/users/{user_id}", response_model=UserResponse, summary="Get a user")
async def get_user(
    user_id: uuid.UUID,
    admin: SystemAdminDep,
    db: DbSession,
) -> UserResponse:
    return await UserService(db).get_user(user_id)


@router.patch(
    "/users/{user_id}",
    response_model=UserResponse,
    summary="Update a user",
    responses={
        403: {"description": "Self-privilege change, or last administrator"},
        404: {"description": "User not found"},
    },
)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    admin: SystemAdminDep,
    db: DbSession,
    info: RequestInfoDep,
) -> UserResponse:
    return await UserService(db).update_user(user_id, payload, actor=admin, ip=info.ip)


@router.post(
    "/users/{user_id}/password",
    response_model=MessageResponse,
    summary="Reset a user's password",
)
async def set_user_password(
    user_id: uuid.UUID,
    payload: AdminSetPasswordRequest,
    admin: SystemAdminDep,
    db: DbSession,
    info: RequestInfoDep,
) -> MessageResponse:
    await UserService(db).set_password(user_id, payload, actor=admin, ip=info.ip)
    return MessageResponse(
        message="Password reset.",
        detail="All of the user's sessions have been signed out.",
    )


@router.delete(
    "/users/{user_id}",
    response_model=MessageResponse,
    summary="Deactivate and archive a user",
    responses={403: {"description": "Cannot delete yourself or the last administrator"}},
)
async def delete_user(
    user_id: uuid.UUID,
    admin: SystemAdminDep,
    db: DbSession,
    info: RequestInfoDep,
) -> MessageResponse:
    """Soft delete - the audit trail and contract uploader references are preserved."""
    await UserService(db).delete_user(user_id, actor=admin, ip=info.ip)
    return MessageResponse(message="User archived.")


__all__ = ["router"]

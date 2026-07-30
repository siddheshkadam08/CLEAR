"""Project and membership endpoints.

Every path parameterised by ``{project_id}`` depends on
:func:`~app.core.deps.get_project_context` (directly or through
``require_permission``), so access is verified before the handler body runs and a
non-member receives 404 rather than a hint that the project exists.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import (
    AccessScopeDep,
    CurrentUserDep,
    DbSession,
    PaginationDep,
    ProjectContext,
    ProjectContextDep,
    RequestInfoDep,
    SortingDep,
    require_permission,
)
from app.core.enums import Permission, ProjectStatus
from app.schemas.common import MessageResponse, Paginated
from app.schemas.project import (
    ProjectActivityResponse,
    ProjectCreateRequest,
    ProjectFilterParams,
    ProjectListItem,
    ProjectMemberBulkCreate,
    ProjectMemberCreate,
    ProjectMemberResponse,
    ProjectMemberUpdate,
    ProjectResponse,
    ProjectUpdateRequest,
)
from app.services.project import ProjectService

router = APIRouter(prefix="/projects", tags=["Projects"])


# =============================================================================
# Collection
# =============================================================================
@router.get(
    "",
    response_model=Paginated[ProjectListItem],
    summary="List the projects you can access",
)
async def list_projects(
    user: CurrentUserDep,
    db: DbSession,
    pagination: PaginationDep,
    sorting: SortingDep,
    search: Annotated[str | None, Query(max_length=200)] = None,
    project_status: Annotated[ProjectStatus | None, Query(alias="status")] = None,
    department: Annotated[str | None, Query()] = None,
    client_name: Annotated[str | None, Query()] = None,
    favourites_only: Annotated[bool, Query()] = False,
) -> Paginated[ProjectListItem]:
    """A System Admin sees every project; everyone else sees their memberships."""
    filters = ProjectFilterParams(
        search=search,
        status=project_status,
        department=department,
        client_name=client_name,
        favourites_only=favourites_only,
    )
    return await ProjectService(db).list_projects(
        user=user,
        filters=filters,
        page=pagination.page,
        size=pagination.size,
        sort_by=sorting.sort_by,
        sort_dir=sorting.sort_dir,
    )


@router.post(
    "",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
)
async def create_project(
    payload: ProjectCreateRequest,
    user: CurrentUserDep,
    db: DbSession,
    info: RequestInfoDep,
) -> ProjectResponse:
    """Any authenticated user may create a project and becomes its Contract Manager.

    Restricting creation to administrators would make the platform unusable for the
    teams it is for; the boundary that matters is who can see an *existing*
    project, which membership already enforces.
    """
    return await ProjectService(db).create_project(payload, actor=user, ip=info.ip)


# =============================================================================
# Single project
# =============================================================================
@router.get(
    "/{project_id}",
    response_model=ProjectResponse,
    summary="Get a project",
    responses={404: {"description": "Not found, or you are not a member"}},
)
async def get_project(
    ctx: ProjectContextDep,
    db: DbSession,
) -> ProjectResponse:
    service = ProjectService(db)
    # Record access so the switcher can order by "recently used".
    if ctx.membership is not None:
        await service.members.mark_accessed(ctx.project_id, ctx.user_id)
    return await service.get_project(
        ctx.project, role=ctx.role, permissions=sorted(ctx.permissions)
    )


@router.patch(
    "/{project_id}",
    response_model=ProjectResponse,
    summary="Update a project",
)
async def update_project(
    payload: ProjectUpdateRequest,
    db: DbSession,
    info: RequestInfoDep,
    ctx: Annotated[ProjectContext, Depends(require_permission(Permission.PROJECT_UPDATE))],
) -> ProjectResponse:
    return await ProjectService(db).update_project(
        ctx.project,
        payload,
        actor=ctx.user,
        role=ctx.role,
        permissions=sorted(ctx.permissions),
        ip=info.ip,
    )


@router.delete(
    "/{project_id}",
    response_model=MessageResponse,
    summary="Archive a project",
)
async def delete_project(
    db: DbSession,
    info: RequestInfoDep,
    ctx: Annotated[ProjectContext, Depends(require_permission(Permission.PROJECT_DELETE))],
) -> MessageResponse:
    """Soft delete. Contracts and derived data are retained and restorable."""
    await ProjectService(db).delete_project(ctx.project, actor=ctx.user, ip=info.ip)
    return MessageResponse(
        message="Project archived.",
        detail="Contracts and extracted data are retained.",
    )


@router.put(
    "/{project_id}/favourite",
    response_model=MessageResponse,
    summary="Pin or unpin a project",
)
async def set_favourite(
    ctx: ProjectContextDep,
    db: DbSession,
    is_favourite: Annotated[bool, Query()] = True,
) -> MessageResponse:
    await ProjectService(db).set_favourite(ctx.project, ctx.user_id, is_favourite=is_favourite)
    return MessageResponse(message="Pinned." if is_favourite else "Unpinned.")


# =============================================================================
# Members
# =============================================================================
@router.get(
    "/{project_id}/members",
    response_model=list[ProjectMemberResponse],
    summary="List project members",
)
async def list_members(
    ctx: ProjectContextDep,
    db: DbSession,
) -> list[ProjectMemberResponse]:
    """Any member may see the team - needed to know who to ask about a contract."""
    return await ProjectService(db).list_members(ctx.project_id)


@router.post(
    "/{project_id}/members",
    response_model=ProjectMemberResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a member",
    responses={409: {"description": "Already a member"}},
)
async def add_member(
    payload: ProjectMemberCreate,
    db: DbSession,
    info: RequestInfoDep,
    ctx: Annotated[ProjectContext, Depends(require_permission(Permission.PROJECT_MEMBER_MANAGE))],
) -> ProjectMemberResponse:
    return await ProjectService(db).add_member(ctx.project, payload, actor=ctx.user, ip=info.ip)


@router.post(
    "/{project_id}/members/bulk",
    response_model=list[ProjectMemberResponse],
    summary="Add several members with one role",
)
async def add_members_bulk(
    payload: ProjectMemberBulkCreate,
    db: DbSession,
    info: RequestInfoDep,
    ctx: Annotated[ProjectContext, Depends(require_permission(Permission.PROJECT_MEMBER_MANAGE))],
) -> list[ProjectMemberResponse]:
    """Users already on the project are skipped rather than failing the batch."""
    return await ProjectService(db).add_members_bulk(
        ctx.project, payload, actor=ctx.user, ip=info.ip
    )


@router.patch(
    "/{project_id}/members/{user_id}",
    response_model=ProjectMemberResponse,
    summary="Change a member's role",
    responses={403: {"description": "Would leave the project without a manager"}},
)
async def update_member(
    user_id: uuid.UUID,
    payload: ProjectMemberUpdate,
    db: DbSession,
    info: RequestInfoDep,
    ctx: Annotated[ProjectContext, Depends(require_permission(Permission.PROJECT_MEMBER_MANAGE))],
) -> ProjectMemberResponse:
    return await ProjectService(db).update_member(
        ctx.project, user_id, payload, actor=ctx.user, ip=info.ip
    )


@router.delete(
    "/{project_id}/members/{user_id}",
    response_model=MessageResponse,
    summary="Remove a member",
    responses={403: {"description": "Would leave the project without a manager"}},
)
async def remove_member(
    user_id: uuid.UUID,
    db: DbSession,
    info: RequestInfoDep,
    ctx: Annotated[ProjectContext, Depends(require_permission(Permission.PROJECT_MEMBER_MANAGE))],
) -> MessageResponse:
    await ProjectService(db).remove_member(ctx.project, user_id, actor=ctx.user, ip=info.ip)
    return MessageResponse(message="Member removed.")


# =============================================================================
# Activity
# =============================================================================
@router.get(
    "/{project_id}/activities",
    response_model=list[ProjectActivityResponse],
    summary="Recent activity in this project",
)
async def project_activities(
    ctx: ProjectContextDep,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ProjectActivityResponse]:
    return await ProjectService(db).recent_activity([ctx.project_id], limit=limit)


# Application-wide feed. Registered on its own router because it is not
# project-scoped, and mounted alongside the dashboard endpoints.
activity_router = APIRouter(tags=["Projects"])


@activity_router.get(
    "/activities",
    response_model=list[ProjectActivityResponse],
    summary="Recent activity across your projects",
)
async def all_activities(
    scope: AccessScopeDep,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ProjectActivityResponse]:
    """ "All projects" means the caller's accessible set, never the whole table."""
    return await ProjectService(db).recent_activity(scope.project_ids, limit=limit)


__all__ = ["activity_router", "router"]

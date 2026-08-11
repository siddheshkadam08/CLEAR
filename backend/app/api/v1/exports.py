"""Export endpoints (§23, FR-6).

Creating an export returns immediately with a job id. The work happens outside the
request: a project-wide export is not a 30-second response, and holding a request
worker and a database connection open for it starves everything else.

Execution uses a background task rather than the stage queue. The stage queue is
keyed by :class:`~app.core.enums.PipelineStage` and carries per-stage checkpointing
that an export has no use for; adding an ``export`` member to that enum would put a
non-pipeline concern into the contract pipeline's state machine and into every
stage list in the product. The durable record is the ``export_jobs`` row itself, so
a process restart mid-export leaves a row in ``running`` that
``ExportService.recover_stalled`` requeues - which is the property the queue would
have provided.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    AccessScopeDep,
    CurrentUserDep,
    DbSession,
    ProjectContextDep,
    resolve_scope_for_project,
)
from app.core.enums import ExportStatus, Permission, SearchScope
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.export import DEFAULT_ENTITIES, ExportEntity, available_formats
from app.export.service import DEFAULT_TTL_HOURS, DOWNLOAD_URL_TTL_SECONDS, ExportService
from app.models.identity import User
from app.schemas.common import Paginated
from app.schemas.export import (
    ExportCapabilitiesResponse,
    ExportCreateRequest,
    ExportDownloadResponse,
    ExportResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/exports", tags=["Exports"])


# =============================================================================
# Capabilities
# =============================================================================
@router.get(
    "/capabilities",
    response_model=ExportCapabilitiesResponse,
    summary="Formats and entities this deployment can export",
)
async def export_capabilities(user: CurrentUserDep) -> ExportCapabilitiesResponse:
    """Advertise what is actually registered.

    Read from the registry rather than from the ``ExportFormat`` enum: the enum
    lists formats the platform has *names* for, and offering the user a CSV button
    that fails on submit is worse than not offering it.
    """
    return ExportCapabilitiesResponse(
        formats=available_formats(),
        entities=list(ExportEntity),
        default_entities=list(DEFAULT_ENTITIES),
        retention_hours=DEFAULT_TTL_HOURS,
    )


# =============================================================================
# Create
# =============================================================================
@router.post(
    "",
    response_model=ExportResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request an export",
)
async def create_export(
    payload: ExportCreateRequest,
    user: CurrentUserDep,
    scope: AccessScopeDep,
    db: DbSession,
    background: BackgroundTasks,
) -> ExportResponse:
    """Queue an export and return the job.

    202, not 201: the row exists but the file does not yet, and a 201 would invite
    a client to go straight to the download.
    """
    # `use_enum_values` means these arrive as plain strings.
    requested_scope = SearchScope(payload.scope)

    project_ids = list(scope.project_ids)
    if requested_scope is SearchScope.PROJECT and payload.project_id is not None:
        # Membership is asserted here, before any row is written, so a request for
        # a project the caller does not belong to never becomes a queued job.
        scope.require(payload.project_id)
        project_ids = [payload.project_id]

    permissions = await _permissions_for(db, user, payload.project_id)
    if not user.is_system_admin and Permission.EXPORT_CREATE.value not in permissions:
        from app.core.errors import PermissionDeniedError

        raise PermissionDeniedError(Permission.EXPORT_CREATE.value)

    service = ExportService(db)
    job = await service.create(
        user_id=user.id,
        user_email=user.email,
        project_id=payload.project_id,
        scope=requested_scope,
        scope_ref=payload.scope_ref,
        export_format=payload.export_format,
        entities=[str(entity) for entity in payload.entities],
        filters=payload.filters,
        fields=payload.fields,
    )
    await db.commit()
    await db.refresh(job)

    # The scope is captured now, from the request's own resolved memberships. The
    # background task must not re-derive it: it runs after the request's identity
    # context is gone, and re-resolving there is how an export ends up scoped to
    # something other than the person who asked for it.
    background.add_task(
        _run_export,
        job.id,
        tuple(project_ids),
        frozenset(permissions),
        user.is_system_admin,
    )

    return ExportResponse.model_validate(job)


async def _run_export(
    export_id: uuid.UUID,
    project_ids: tuple[uuid.UUID, ...],
    permissions: frozenset[str],
    is_system_admin: bool,
) -> None:
    """Run one export on its own session.

    A background task outlives the request, and with it the request-scoped session,
    so it opens its own and owns the transaction boundary.
    """
    from app.db.session import session_scope

    try:
        async with session_scope() as db:
            await ExportService(db).run(
                export_id,
                project_ids=project_ids,
                permissions=permissions,
                is_system_admin=is_system_admin,
            )
    except Exception as exc:  # noqa: BLE001 - the job row already records the failure
        logger.error("export_background_failed", export_id=str(export_id), error=str(exc))


# =============================================================================
# Read
# =============================================================================
@router.get("", response_model=Paginated[ExportResponse], summary="List my exports")
async def list_exports(
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 25,
    # Typed as the enum rather than coerced in the body: `ExportStatus(value)` on an
    # unknown string raises ValueError, which leaves the handler as a 500. Declaring
    # it here makes a bad filter a 422 that names the offending field.
    export_status: Annotated[list[ExportStatus] | None, Query(alias="status")] = None,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Paginated[ExportResponse]:
    """Exports this user requested, optionally narrowed to one business unit.

    Scoped to the requester rather than to the project: an export is a copy of data
    taken by a named person, and listing other people's copies would expose what
    they have been looking at.

    `project_id` narrows *within* that, and does not loosen it - the caller still
    only ever sees their own exports. It exists because the business-unit selector
    is meant to re-scope the whole application, and a screen that ignores it shows
    rows belonging to a unit the header says you are not looking at. Resolved
    through the usual scope helper, so naming a unit you cannot see is refused
    rather than quietly ignored.

    Omitted - "All Business Units" - keeps returning everything you requested.
    """
    from sqlalchemy import func, select

    from app.models.export import ExportJob

    conditions = [ExportJob.requested_by == user.id]
    if export_status:
        conditions.append(ExportJob.status.in_(export_status))
    if project_id is not None:
        project_ids = await resolve_scope_for_project(project_id, scope)
        conditions.append(ExportJob.project_id.in_(project_ids))

    total = (
        await db.execute(select(func.count()).select_from(ExportJob).where(*conditions))
    ).scalar_one()
    rows = (
        (
            await db.execute(
                select(ExportJob)
                .where(*conditions)
                .order_by(ExportJob.created_at.desc())
                .offset((page - 1) * size)
                .limit(size)
            )
        )
        .scalars()
        .all()
    )

    return Paginated.build(
        [ExportResponse.model_validate(row) for row in rows],
        page=page,
        size=size,
        total=total,
    )


@router.get("/{export_id}", response_model=ExportResponse, summary="Export status")
async def get_export(
    export_id: uuid.UUID,
    user: CurrentUserDep,
    db: DbSession,
) -> ExportResponse:
    """Poll one export.

    A job belonging to someone else is reported as missing rather than forbidden:
    distinguishing the two would confirm that another user's export exists.
    """
    from sqlalchemy import select

    from app.models.export import ExportJob

    job = (
        await db.execute(select(ExportJob).where(ExportJob.id == export_id))
    ).scalar_one_or_none()
    if job is None or (job.requested_by != user.id and not user.is_system_admin):
        raise NotFoundError("Export", export_id)
    return ExportResponse.model_validate(job)


@router.get(
    "/{export_id}/download",
    response_model=ExportDownloadResponse,
    summary="Get a download URL for a finished export",
)
async def download_export(
    export_id: uuid.UUID,
    user: CurrentUserDep,
    db: DbSession,
) -> ExportDownloadResponse:
    """Mint a short-lived URL. Every call is audited."""
    service = ExportService(db)
    url, job = await service.download_url(export_id, user_id=user.id, user_email=user.email)
    await db.commit()

    return ExportDownloadResponse(
        export_id=job.id,
        url=url,
        file_name=job.file_name,
        file_size=job.file_size,
        expires_in=DOWNLOAD_URL_TTL_SECONDS,
    )


# =============================================================================
# Token-authenticated download
#
# Its own router because every other export route carries the bearer-auth and
# forced-password-change dependencies applied in `api/v1/__init__.py`, and this one
# cannot: it is reached by a browser navigation that sends no Authorization header,
# so those dependencies would reject it with a 401 before the token was ever looked
# at. Same reasoning that excludes `auth` from the password gate.
# =============================================================================
download_router = APIRouter(prefix="/exports", tags=["Exports"])


@download_router.get(
    "/{export_id}/content",
    summary="Stream a finished export",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/octet-stream": {}}}},
    include_in_schema=False,
)
async def stream_export(
    export_id: uuid.UUID,
    db: DbSession,
    token: Annotated[str, Query(description="Short-lived download token from /download.")],
) -> StreamingResponse:
    """Serve the export bytes to a browser navigation.

    **Authenticated by the token in the URL, not by a bearer header** - deliberately,
    and this is the only endpoint that works this way. The client reaches it by
    navigating the tab (so a large workbook streams to disk instead of through the
    page's memory), and a navigation sends no ``Authorization`` header. Object
    storage answers this with a presigned URL; with the local adapter there is
    nothing to presign, so the API mints the equivalent itself.

    The token is bound to one user, one export and five minutes - see
    ``create_download_token``. Everything that fails here returns the same 404 the
    missing-export case returns: an export id is guessable-adjacent, and a distinct
    403 would confirm which ids exist.
    """
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.core.errors import TokenExpiredError, TokenInvalidError
    from app.core.security import verify_download_token
    from app.models.export import ExportJob
    from app.storage import get_storage

    try:
        payload = verify_download_token(token, resource="export", resource_id=export_id)
    except (TokenExpiredError, TokenInvalidError) as exc:
        raise NotFoundError("Export", export_id) from exc

    job = (
        await db.execute(select(ExportJob).where(ExportJob.id == export_id))
    ).scalar_one_or_none()
    if job is None or str(job.requested_by) != payload.get("sub"):
        raise NotFoundError("Export", export_id)
    if job.status is not ExportStatus.COMPLETED or not job.storage_path:
        raise NotFoundError("Export", export_id)
    if job.expires_at is not None and job.expires_at <= datetime.now(UTC):
        raise NotFoundError("Export", export_id)

    stream = get_storage().get_stream(job.storage_path)
    return StreamingResponse(
        stream,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{job.file_name}"',
            # The token already limits the window; caching a copy of contract data
            # in a shared proxy would outlive it.
            "Cache-Control": "private, no-store",
        },
    )


# =============================================================================
# Project-scoped convenience route
# =============================================================================
project_export_router = APIRouter(prefix="/projects/{project_id}/exports", tags=["Exports"])


@project_export_router.post(
    "",
    response_model=ExportResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Export one project",
)
async def create_project_export(
    payload: ExportCreateRequest,
    ref: ProjectContextDep,
    db: DbSession,
    background: BackgroundTasks,
) -> ExportResponse:
    """Export scoped to one project, with membership already resolved by the path.

    The project comes from the URL, not the body: a body that disagreed with the
    path would otherwise decide the scope, which is the wrong half of the request
    to trust.
    """
    ref.require(Permission.EXPORT_CREATE)

    service = ExportService(db)
    job = await service.create(
        user_id=ref.user_id,
        user_email=ref.user.email,
        project_id=ref.project_id,
        scope=SearchScope.PROJECT,
        scope_ref=payload.scope_ref,
        export_format=payload.export_format,
        entities=[str(entity) for entity in payload.entities],
        filters=payload.filters,
        fields=payload.fields,
    )
    await db.commit()
    await db.refresh(job)

    background.add_task(
        _run_export,
        job.id,
        (ref.project_id,),
        frozenset(ref.permissions),
        ref.is_system_admin,
    )
    return ExportResponse.model_validate(job)


# =============================================================================
# Helpers
# =============================================================================
async def _permissions_for(db: AsyncSession, user: User, project_id: uuid.UUID | None) -> set[str]:
    """The caller's permissions, for the masking decision inside the export.

    With no project pinned the union across memberships is used, which is the
    correct reading of "export everything I can see": a reviewer on one project
    should not have that project's clause text withheld because they happen to be a
    plain viewer somewhere else. The dataset builder still scopes every row by
    project, so a wider permission set cannot widen the *data*.

    Resolution mirrors ``get_project_context``: role permissions, then overrides
    intersected in - overrides narrow a role for one member and never widen it.
    """
    from app.repositories.project import ProjectMemberRepository

    if user.is_system_admin:
        return {permission.value for permission in Permission}

    memberships = await ProjectMemberRepository(db).list_for_user(user.id)

    collected: set[str] = set()
    for membership in memberships:
        if project_id is not None and membership.project_id != project_id:
            continue
        role = membership.role
        permissions = set(getattr(role, "permissions", None) or [])
        if membership.permission_overrides:
            permissions &= set(membership.permission_overrides)
        collected |= permissions
    return collected


__all__ = ["project_export_router", "router"]

"""Contract upload, repository and document-access endpoints.

Two routers:

* ``/projects/{project_id}/contracts`` - upload and listing, project-scoped in the
  path so :func:`require_permission` guards them.
* ``/contracts/{contract_id}`` - single-contract operations. The project is
  resolved from the contract, then the same membership check is applied, so these
  are exactly as protected as the project-scoped routes.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from fastapi.responses import RedirectResponse, StreamingResponse

from app.core.deps import (
    AccessScopeDep,
    ContractContextDep,
    DbSession,
    PaginationDep,
    ProjectContext,
    RequestInfoDep,
    SortingDep,
    require_permission,
    resolve_scope_for_project,
)
from app.core.enums import AgreementType, JobPriority, Permission
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.schemas.common import DateRange, MessageResponse, NumberRange, Paginated
from app.schemas.contract import (
    ContractFilterParams,
    ContractListItem,
    ContractMetadataResponse,
    ContractMetadataUpdate,
    ContractResponse,
    ContractUpdateRequest,
    ContractVersionResponse,
    FileAccessResponse,
    UploadOptions,
    UploadResponse,
)
from app.services.contract import ContractService
from app.services.upload import UploadService

logger = get_logger(__name__)

router = APIRouter(prefix="/projects/{project_id}/contracts", tags=["Contracts"])
contract_router = APIRouter(prefix="/contracts", tags=["Contracts"])


# =============================================================================
# Upload
# =============================================================================
@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload one or more contracts",
    description=(
        "Accepts a multipart batch of PDF/DOCX files. Each accepted file is stored "
        "and gets its own queued processing job; the response returns within "
        "seconds because no parsing or extraction happens in the request.\n\n"
        "A batch may partially succeed - inspect `files[].status` "
        "(`accepted` | `duplicate` | `rejected`)."
    ),
    responses={
        202: {"description": "Batch processed; per-file outcomes in the response"},
        413: {"description": "Request body exceeds the upload limit"},
    },
)
async def upload_contracts(
    db: DbSession,
    info: RequestInfoDep,
    ctx: Annotated[ProjectContext, Depends(require_permission(Permission.CONTRACT_UPLOAD))],
    files: Annotated[list[UploadFile], File(description="PDF or DOCX files")],
    priority: Annotated[JobPriority, Form()] = JobPriority.NORMAL,
    profile_key: Annotated[str | None, Form()] = None,
    agreement_type: Annotated[AgreementType | None, Form()] = None,
    tags: Annotated[str | None, Form(description="Comma-separated tags")] = None,
    replace_existing: Annotated[bool, Form()] = False,
    notes: Annotated[str | None, Form()] = None,
) -> UploadResponse:
    options = UploadOptions(
        priority=priority,
        profile_key=profile_key,
        agreement_type=agreement_type,
        tags=[tag.strip() for tag in (tags or "").split(",") if tag.strip()],
        replace_existing=replace_existing,
        notes=notes,
    )
    return await UploadService(db).upload(
        project=ctx.project,
        files=files,
        options=options,
        actor=ctx.user,
        ip=info.ip,
    )


# =============================================================================
# Repository listing
# =============================================================================
def _filters_from_query(
    search: str | None = Query(None, max_length=300, description="Title, party or summary"),
    status_filter: list[str] | None = Query(None, alias="status"),
    agreement_type: list[str] | None = Query(None),
    file_type: str | None = Query(None, pattern="^(pdf|docx)$"),
    party: str | None = Query(None, description="Matches any party, vendor or customer"),
    vendor: str | None = Query(None),
    customer: str | None = Query(None),
    contract_number: str | None = Query(None),
    effective_from: str | None = Query(None),
    effective_to: str | None = Query(None),
    expiry_from: str | None = Query(None),
    expiry_to: str | None = Query(None),
    expiring_within_days: int | None = Query(None, ge=0, le=3650),
    risk_band: list[str] | None = Query(None),
    risk_score_min: float | None = Query(None, ge=0, le=100),
    risk_score_max: float | None = Query(None, ge=0, le=100),
    value_min: float | None = Query(None, ge=0),
    value_max: float | None = Query(None, ge=0),
    currency: str | None = Query(None, max_length=8),
    governing_law: str | None = Query(None),
    country: str | None = Query(None),
    category: str | None = Query(None),
    department: str | None = Query(None),
    owner: str | None = Query(None),
    language: str | None = Query(None),
    auto_renewal: bool | None = Query(None),
    has_unlimited_liability: bool | None = Query(None),
    missing_clause_types: list[str] | None = Query(None),
    needs_review: bool | None = Query(None),
    tags: list[str] | None = Query(None),
    uploaded_by: uuid.UUID | None = Query(None),
) -> ContractFilterParams:
    """Assemble the filter model from flat query parameters.

    Flat parameters keep the URL shareable and bookmarkable - a filtered repository
    view is something users send to each other.
    """
    from datetime import date as date_type

    def parse_date(value: str | None) -> date_type | None:
        if not value:
            return None
        try:
            return date_type.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(
                f"'{value}' is not a valid ISO date (YYYY-MM-DD).",
            ) from exc

    from app.core.enums import ContractStatus, FileType, RiskBand

    def parse_enum_list(values: list[str] | None, enum_cls: type[Enum]) -> list[Any] | None:
        if not values:
            return None
        parsed = []
        for value in values:
            try:
                parsed.append(enum_cls(value))
            except ValueError as exc:
                allowed = ", ".join(member.value for member in enum_cls)
                raise ValidationError(
                    f"'{value}' is not a valid value. Allowed: {allowed}."
                ) from exc
        return parsed

    effective = DateRange(**{"from": parse_date(effective_from), "to": parse_date(effective_to)})
    expiry = DateRange(**{"from": parse_date(expiry_from), "to": parse_date(expiry_to)})

    return ContractFilterParams(
        search=search,
        status=parse_enum_list(status_filter, ContractStatus),
        agreement_type=agreement_type,
        file_type=FileType(file_type) if file_type else None,
        party=party,
        vendor=vendor,
        customer=customer,
        contract_number=contract_number,
        effective_date=effective if effective.is_set else None,
        expiration_date=expiry if expiry.is_set else None,
        expiring_within_days=expiring_within_days,
        risk_band=parse_enum_list(risk_band, RiskBand),
        risk_score=NumberRange(min=risk_score_min, max=risk_score_max)
        if risk_score_min is not None or risk_score_max is not None
        else None,
        contract_value=NumberRange(min=value_min, max=value_max)
        if value_min is not None or value_max is not None
        else None,
        currency=currency,
        governing_law=governing_law,
        country=country,
        category=category,
        department=department,
        owner=owner,
        language=language,
        auto_renewal=auto_renewal,
        has_unlimited_liability=has_unlimited_liability,
        missing_clause_types=missing_clause_types,
        needs_review=needs_review,
        tags=tags,
        uploaded_by=uploaded_by,
    )


ContractFiltersDep = Annotated[ContractFilterParams, Depends(_filters_from_query)]


@router.get(
    "",
    response_model=Paginated[ContractListItem],
    summary="List contracts in a project",
)
async def list_project_contracts(
    db: DbSession,
    pagination: PaginationDep,
    sorting: SortingDep,
    filters: ContractFiltersDep,
    ctx: Annotated[ProjectContext, Depends(require_permission(Permission.CONTRACT_READ))],
) -> Paginated[ContractListItem]:
    return await ContractService(db).list_contracts(
        project_ids=[ctx.project_id],
        filters=filters,
        page=pagination.page,
        size=pagination.size,
        sort_by=sorting.sort_by,
        sort_dir=sorting.sort_dir,
    )


@contract_router.get(
    "",
    response_model=Paginated[ContractListItem],
    summary="List contracts across your projects",
)
async def list_all_contracts(
    db: DbSession,
    scope: AccessScopeDep,
    pagination: PaginationDep,
    sorting: SortingDep,
    filters: ContractFiltersDep,
    project_id: Annotated[uuid.UUID | None, Query(description="Narrow to one project")] = None,
) -> Paginated[ContractListItem]:
    """Application-wide listing, bounded by the caller's accessible projects."""
    project_ids = await resolve_scope_for_project(project_id, scope)
    return await ContractService(db).list_contracts(
        project_ids=project_ids,
        filters=filters,
        page=pagination.page,
        size=pagination.size,
        sort_by=sorting.sort_by,
        sort_dir=sorting.sort_dir,
    )


# =============================================================================
# Single contract
# =============================================================================


@contract_router.get(
    "/{contract_id}",
    response_model=ContractResponse,
    summary="Get a contract",
    responses={404: {"description": "Not found, or not in one of your projects"}},
)
async def get_contract(ref: ContractContextDep, db: DbSession) -> ContractResponse:
    contract_id, project_id, ctx = ref.contract_id, ref.project_id, ref.project
    ctx.require(Permission.CONTRACT_READ)
    return await ContractService(db).get_contract(contract_id, project_id)


@contract_router.patch(
    "/{contract_id}",
    response_model=ContractResponse,
    summary="Update contract details",
)
async def update_contract(
    payload: ContractUpdateRequest,
    ref: ContractContextDep,
    db: DbSession,
    info: RequestInfoDep,
) -> ContractResponse:
    contract_id, project_id, ctx = ref.contract_id, ref.project_id, ref.project
    ctx.require(Permission.CONTRACT_UPDATE)
    return await ContractService(db).update_contract(
        contract_id, project_id, payload, actor=ctx.user, ip=info.ip
    )


@contract_router.patch(
    "/{contract_id}/metadata",
    response_model=ContractMetadataResponse,
    summary="Correct extracted metadata",
    description=(
        "Manual correction of AI-extracted metadata. Every change is written to "
        "`contract_history` with `source='user'`, so a human override stays "
        "distinguishable from the model's output."
    ),
)
async def update_metadata(
    payload: ContractMetadataUpdate,
    ref: ContractContextDep,
    db: DbSession,
    info: RequestInfoDep,
) -> ContractMetadataResponse:
    contract_id, project_id, ctx = ref.contract_id, ref.project_id, ref.project
    ctx.require(Permission.KNOWLEDGE_REVIEW)
    return await ContractService(db).update_metadata(
        contract_id, project_id, payload, actor=ctx.user, ip=info.ip
    )


@contract_router.delete(
    "/{contract_id}",
    response_model=MessageResponse,
    summary="Delete a contract",
    responses={409: {"description": "Still processing - cancel the job first"}},
)
async def delete_contract(
    ref: ContractContextDep,
    db: DbSession,
    info: RequestInfoDep,
    purge_files: Annotated[bool, Query(description="Also remove stored bytes")] = False,
) -> MessageResponse:
    contract_id, project_id, ctx = ref.contract_id, ref.project_id, ref.project
    ctx.require(Permission.CONTRACT_DELETE)
    await ContractService(db).delete_contract(
        contract_id, project_id, actor=ctx.user, ip=info.ip, purge_files=purge_files
    )
    return MessageResponse(
        message="Contract deleted.",
        detail="Files purged." if purge_files else "Stored files retained.",
    )


@contract_router.get(
    "/{contract_id}/versions",
    response_model=list[ContractVersionResponse],
    summary="List a contract's versions",
)
async def list_versions(ref: ContractContextDep, db: DbSession) -> list[ContractVersionResponse]:
    contract_id, project_id, ctx = ref.contract_id, ref.project_id, ref.project
    ctx.require(Permission.CONTRACT_READ)
    return await ContractService(db).list_versions(contract_id, project_id)


# =============================================================================
# Document access
# =============================================================================
@contract_router.get(
    "/{contract_id}/file",
    response_model=FileAccessResponse,
    summary="Get a signed URL for the source document",
    description=(
        "Returns a time-limited URL the viewer fetches directly from object "
        "storage. Access is authorised here once; the API does not proxy every "
        "page render."
    ),
)
async def get_file_url(
    ref: ContractContextDep,
    db: DbSession,
    info: RequestInfoDep,
    version: Annotated[int | None, Query(ge=1)] = None,
    download: Annotated[bool, Query()] = False,
) -> FileAccessResponse:
    contract_id, project_id, ctx = ref.contract_id, ref.project_id, ref.project
    ctx.require(Permission.CONTRACT_DOWNLOAD if download else Permission.CONTRACT_READ)
    return await ContractService(db).file_access(
        contract_id,
        project_id,
        version=version,
        download=download,
        actor=ctx.user,
        ip=info.ip,
    )


@contract_router.get(
    "/{contract_id}/content",
    summary="Stream the source document",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"application/pdf": {}}, "description": "Document bytes"},
        302: {"description": "Redirect to a signed storage URL"},
    },
)
async def stream_content(
    ref: ContractContextDep,
    db: DbSession,
    inline: Annotated[bool, Query(description="Content-Disposition: inline")] = True,
    redirect: Annotated[bool, Query(description="Redirect to the signed URL instead")] = False,
) -> Response:
    """Serve the document bytes through the API.

    Kept for clients that cannot use a signed URL (local storage in development, or
    a viewer that will not follow a cross-origin redirect). ``redirect=true`` opts
    into the cheaper path.
    """
    contract_id, project_id, ctx = ref.contract_id, ref.project_id, ref.project
    ctx.require(Permission.CONTRACT_READ)
    service = ContractService(db)

    if redirect:
        access = await service.file_access(contract_id, project_id)
        if not access.is_proxied:
            return RedirectResponse(url=access.url, status_code=status.HTTP_302_FOUND)

    contract, stream = await service.stream_file(contract_id, project_id)
    disposition = "inline" if inline else "attachment"
    return StreamingResponse(
        stream,
        media_type=contract.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'{disposition}; filename="{contract.original_file_name}"',
            "Content-Length": str(contract.file_size),
            # The bytes never change for a given contract version, so allow caching.
            "Cache-Control": "private, max-age=3600",
            "Accept-Ranges": "none",
        },
    )


__all__ = ["contract_router", "router"]

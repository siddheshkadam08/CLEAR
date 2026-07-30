"""Clause Master administration, dashboards and alerts.

Three routers that share one property: they are the configuration and reporting
surface, so nothing here is hardcoded in the frontend.

* **Clause Master** is the platform's extensibility point. Adding a clause category
  is a data change; the extraction engine reads the categories, the UI reads their
  ``ui_config``, and neither needs a release. Rule edits create a new *version*
  rather than mutating - a contract extracted under version 3 must stay reproducible
  after someone edits it to version 4 (§25).
* **Dashboards** are always scoped to the caller's accessible projects. "All
  projects" means exactly the projects this user belongs to (§1.1).
* **Alerts** are how a renewal deadline surfaces before it passes rather than after.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import ColumnElement, func, or_, select

from app.core.deps import (
    AccessScopeDep,
    CurrentUserDep,
    DbSession,
    PaginationDep,
    RequestInfoDep,
    require_system_admin,
    resolve_scope_for_project,
)
from app.core.enums import (
    AlertStatus,
    AuditAction,
    JobState,
    RiskBand,
)
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.alert import Alert, AlertRule
from app.models.clause_master import ClauseMasterCategory, ClauseMasterRule
from app.models.contract import Contract, ContractMetadata
from app.schemas.admin import (
    AlertResponse,
    AlertRuleCreate,
    AlertRuleResponse,
    AlertRuleUpdate,
    AlertUpdateRequest,
    ClauseCategoryCreate,
    ClauseCategoryResponse,
    ClauseCategoryUpdate,
    ClauseRuleCreate,
    ClauseRuleResponse,
    DashboardResponse,
    DistributionBucket,
    KpiTile,
    ProcessingStatsResponse,
    TimeSeriesPoint,
)
from app.schemas.common import MessageResponse, Paginated

logger = get_logger(__name__)

clause_master_router = APIRouter(prefix="/clause-master", tags=["Clause Master"])
dashboard_router = APIRouter(prefix="/dashboard", tags=["Dashboards"])
alert_router = APIRouter(prefix="/alerts", tags=["Alerts"])

#: Window for the "expiring soon" tile. Ninety days is long enough that a renewal
#: notice period has not already lapsed by the time anyone looks.
_EXPIRING_WINDOW_DAYS = 90


# =============================================================================
# Clause Master
# =============================================================================
@clause_master_router.get(
    "",
    response_model=list[ClauseCategoryResponse],
    summary="List clause categories",
)
async def list_clause_categories(
    user: CurrentUserDep,
    db: DbSession,
    include_inactive: Annotated[bool, Query()] = False,
) -> list[ClauseCategoryResponse]:
    """Every clause category, in priority order.

    Readable by any authenticated user: the frontend needs the categories and their
    ``ui_config`` to render a contract at all. Editing requires admin.
    """
    stmt = select(ClauseMasterCategory).where(ClauseMasterCategory.deleted_at.is_(None))
    if not include_inactive:
        stmt = stmt.where(ClauseMasterCategory.is_active.is_(True))
    stmt = stmt.order_by(ClauseMasterCategory.priority, ClauseMasterCategory.key)

    categories = (await db.execute(stmt)).scalars().all()
    return [_category(row, await _current_rule(db, row.id)) for row in categories]


@clause_master_router.get(
    "/{category_id}",
    response_model=ClauseCategoryResponse,
    summary="Get a clause category",
)
async def get_clause_category(
    category_id: uuid.UUID, user: CurrentUserDep, db: DbSession
) -> ClauseCategoryResponse:
    category = await _load_category(db, category_id)
    return _category(category, await _current_rule(db, category_id))


@clause_master_router.post(
    "",
    response_model=ClauseCategoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a clause category",
    dependencies=[Depends(require_system_admin)],
)
async def create_clause_category(
    payload: ClauseCategoryCreate,
    user: CurrentUserDep,
    db: DbSession,
    info: RequestInfoDep,
) -> ClauseCategoryResponse:
    """Add a clause category.

    Zero code changes: the extraction engine picks it up on the next run, and the UI
    renders it from ``ui_config``.
    """
    existing = (
        await db.execute(
            select(ClauseMasterCategory).where(ClauseMasterCategory.key == payload.key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"A clause category with key '{payload.key}' already exists.")

    category = ClauseMasterCategory(
        key=payload.key,
        name=payload.name,
        description=payload.description,
        group_name=payload.group_name,
        priority=payload.priority,
        mandatory=payload.mandatory,
        confidence_threshold=payload.confidence_threshold,
        default_risk_severity=payload.default_risk_severity,
        ui_config=payload.ui_config or {"placement": "list"},
        is_system=False,
        created_by=user.id,
    )
    db.add(category)
    await db.flush()

    rule = ClauseMasterRule(
        category_id=category.id,
        version=1,
        extraction_rule=payload.extraction_rule,
        output_schema=payload.output_schema,
        synonyms=payload.synonyms,
        is_active=True,
        created_by=user.id,
        change_note="Initial version.",
    )
    db.add(rule)
    await db.flush()

    await _audit(db, user, info, AuditAction.CREATE, category, after=payload.model_dump())
    logger.info("clause_category_created", key=payload.key, user_id=str(user.id))
    return _category(category, rule)


@clause_master_router.patch(
    "/{category_id}",
    response_model=ClauseCategoryResponse,
    summary="Update a clause category",
    dependencies=[Depends(require_system_admin)],
)
async def update_clause_category(
    category_id: uuid.UUID,
    payload: ClauseCategoryUpdate,
    user: CurrentUserDep,
    db: DbSession,
    info: RequestInfoDep,
) -> ClauseCategoryResponse:
    """Update a category's metadata or UI placement.

    Editing ``ui_config.placement`` to ``dedicated_tab`` is how a clause is promoted
    to its own tab - no frontend change involved.
    """
    category = await _load_category(db, category_id)
    before = {
        "name": category.name,
        "priority": category.priority,
        "mandatory": category.mandatory,
        "ui_config": dict(category.ui_config or {}),
        "is_active": category.is_active,
    }

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(category, field, value)
    await db.flush()

    await _audit(
        db,
        user,
        info,
        AuditAction.UPDATE,
        category,
        before=before,
        after=payload.model_dump(exclude_unset=True),
    )
    return _category(category, await _current_rule(db, category_id))


@clause_master_router.post(
    "/{category_id}/rules",
    response_model=ClauseRuleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Publish a new rule version",
    dependencies=[Depends(require_system_admin)],
)
async def create_clause_rule(
    category_id: uuid.UUID,
    payload: ClauseRuleCreate,
    user: CurrentUserDep,
    db: DbSession,
    info: RequestInfoDep,
) -> ClauseRuleResponse:
    """Publish a new version of a category's extraction contract.

    A new row, never an edit. Contracts already extracted keep pointing at the
    version that produced them, so an earlier extraction stays reproducible and the
    two can be compared (§25).
    """
    category = await _load_category(db, category_id)

    highest = (
        await db.execute(
            select(func.coalesce(func.max(ClauseMasterRule.version), 0)).where(
                ClauseMasterRule.category_id == category_id
            )
        )
    ).scalar() or 0

    rule = ClauseMasterRule(
        category_id=category.id,
        version=int(highest) + 1,
        extraction_rule=payload.extraction_rule,
        output_schema=payload.output_schema,
        synonyms=payload.synonyms,
        prompt_template=payload.prompt_template,
        standard_text=payload.standard_text,
        validation_rules=payload.validation_rules,
        examples=payload.examples,
        is_active=True,
        created_by=user.id,
        change_note=payload.change_note,
    )
    db.add(rule)
    await db.flush()

    await _audit(
        db,
        user,
        info,
        AuditAction.CONFIG_CHANGE,
        category,
        after={"rule_version": rule.version, "note": payload.change_note},
    )
    logger.info(
        "clause_rule_published",
        category=str(category.key),
        version=rule.version,
        user_id=str(user.id),
    )
    return _rule(rule)


@clause_master_router.get(
    "/{category_id}/rules",
    response_model=list[ClauseRuleResponse],
    summary="Rule version history",
)
async def list_clause_rules(
    category_id: uuid.UUID, user: CurrentUserDep, db: DbSession
) -> list[ClauseRuleResponse]:
    await _load_category(db, category_id)
    rows = (
        (
            await db.execute(
                select(ClauseMasterRule)
                .where(ClauseMasterRule.category_id == category_id)
                .order_by(ClauseMasterRule.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_rule(row) for row in rows]


# =============================================================================
# Dashboards
# =============================================================================
@dashboard_router.get("", response_model=DashboardResponse, summary="Overview dashboard")
async def get_dashboard(
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
    project_id: Annotated[uuid.UUID | None, Query(description="Narrow to one project")] = None,
) -> DashboardResponse:
    """KPIs and distributions across the caller's accessible projects."""
    project_ids = await resolve_scope_for_project(project_id, scope)
    now = datetime.now(UTC)

    if not project_ids:
        return DashboardResponse(
            scope="project" if project_id else "application",
            project_ids=[],
            generated_at=now,
        )

    today = now.date()
    horizon = today + timedelta(days=_EXPIRING_WINDOW_DAYS)

    total = await _scalar(
        db,
        select(func.count())
        .select_from(Contract)
        .where(Contract.project_id.in_(project_ids), Contract.deleted_at.is_(None)),
    )
    expiring = await _scalar(
        db,
        select(func.count())
        .select_from(ContractMetadata)
        .where(
            ContractMetadata.project_id.in_(project_ids),
            ContractMetadata.expiration_date.is_not(None),
            ContractMetadata.expiration_date >= today,
            ContractMetadata.expiration_date <= horizon,
        ),
    )
    high_risk = await _scalar(
        db,
        select(func.count())
        .select_from(ContractMetadata)
        .where(
            ContractMetadata.project_id.in_(project_ids),
            ContractMetadata.risk_band == RiskBand.HIGH.value,
        ),
    )
    unlimited = await _scalar(
        db,
        select(func.count())
        .select_from(ContractMetadata)
        .where(
            ContractMetadata.project_id.in_(project_ids),
            ContractMetadata.has_unlimited_liability.is_(True),
        ),
    )
    missing_clauses = await _scalar(
        db,
        select(func.count())
        .select_from(ContractMetadata)
        .where(
            ContractMetadata.project_id.in_(project_ids),
            func.jsonb_array_length(ContractMetadata.missing_mandatory_clauses) > 0,
        ),
    )
    needs_review = await _scalar(
        db,
        select(func.count())
        .select_from(Contract)
        .where(
            Contract.project_id.in_(project_ids),
            Contract.needs_review.is_(True),
            Contract.deleted_at.is_(None),
        ),
    )
    total_value = await _scalar(
        db,
        select(func.coalesce(func.sum(ContractMetadata.contract_value), 0)).where(
            ContractMetadata.project_id.in_(project_ids)
        ),
    )

    kpis = [
        KpiTile(key="total_contracts", label="Contracts", value=float(total)),
        KpiTile(
            key="expiring_soon",
            label=f"Expiring in {_EXPIRING_WINDOW_DAYS} days",
            value=float(expiring),
            # The tile links to the filter that produced it, so "12 expiring" is
            # clickable rather than a number the user then has to reconstruct.
            drilldown={"expiring_before": horizon.isoformat(), "expiring_after": today.isoformat()},
        ),
        KpiTile(
            key="high_risk",
            label="High risk",
            value=float(high_risk),
            drilldown={"risk_band": "high"},
        ),
        KpiTile(
            key="unlimited_liability",
            label="Unlimited liability",
            value=float(unlimited),
            drilldown={"has_unlimited_liability": True},
        ),
        KpiTile(
            key="missing_clauses",
            label="Missing mandatory clauses",
            value=float(missing_clauses),
            drilldown={"missing_mandatory": True},
        ),
        KpiTile(
            key="needs_review",
            label="Needs review",
            value=float(needs_review),
            drilldown={"needs_review": True},
        ),
        KpiTile(key="total_value", label="Total value", value=float(total_value), unit="USD"),
    ]

    return DashboardResponse(
        scope="project" if project_id else "application",
        project_ids=project_ids,
        kpis=kpis,
        risk_distribution=await _distribution(
            db, ContractMetadata.risk_band, project_ids, ContractMetadata
        ),
        agreement_type_distribution=await _distribution(
            db, Contract.agreement_type, project_ids, Contract
        ),
        status_distribution=await _distribution(db, Contract.status, project_ids, Contract),
        expiring_soon=await _expiring(db, project_ids, today, horizon),
        top_risks=await _top_risks(db, project_ids),
        uploads_over_time=await _uploads(db, project_ids),
        generated_at=now,
    )


@dashboard_router.get(
    "/processing",
    response_model=ProcessingStatsResponse,
    summary="Pipeline throughput",
)
async def processing_stats(
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ProcessingStatsResponse:
    """Job outcomes and stage timings, for the operations view."""
    project_ids = await resolve_scope_for_project(project_id, scope)
    if not project_ids:
        return ProcessingStatsResponse()

    from app.repositories.processing import JobStageRunRepository, ProcessingJobRepository

    jobs = ProcessingJobRepository(db)
    counts = await jobs.state_counts(project_ids)
    durations = await JobStageRunRepository(db).stage_durations(project_ids)

    ready = counts.get(JobState.READY.value, 0)
    failed = counts.get(JobState.FAILED.value, 0)
    finished = ready + failed

    return ProcessingStatsResponse(
        jobs_by_state=counts,
        stage_durations_ms=durations,
        # Of jobs that finished. Including in-flight jobs would make the rate drift
        # downward during a busy period for no real reason.
        success_rate=round(ready / finished, 4) if finished else 0.0,
        average_duration_ms=round(sum(durations.values()), 2),
        in_flight=await jobs.count_in_flight(),
    )


# =============================================================================
# Alerts
# =============================================================================
@alert_router.get("", response_model=Paginated[AlertResponse], summary="List alerts")
async def list_alerts(
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
    pagination: PaginationDep,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    alert_status: Annotated[list[AlertStatus] | None, Query(alias="status")] = None,
) -> Paginated[AlertResponse]:
    project_ids = await resolve_scope_for_project(project_id, scope)
    if not project_ids:
        return Paginated.build(items=[], page=pagination.page, size=pagination.size, total=0)

    stmt = select(Alert).where(Alert.project_id.in_(project_ids))
    if alert_status:
        stmt = stmt.where(Alert.status.in_(alert_status))

    total = await _scalar(db, select(func.count()).select_from(stmt.subquery()))
    rows = (
        (
            await db.execute(
                stmt.order_by(Alert.due_date.asc().nullslast(), Alert.created_at.desc())
                .offset((pagination.page - 1) * pagination.size)
                .limit(pagination.size)
            )
        )
        .scalars()
        .all()
    )
    return Paginated.build(
        items=[_alert(row) for row in rows],
        page=pagination.page,
        size=pagination.size,
        total=total,
    )


@alert_router.patch(
    "/{alert_id}",
    response_model=AlertResponse,
    summary="Acknowledge, resolve or dismiss an alert",
)
async def update_alert(
    alert_id: uuid.UUID,
    payload: AlertUpdateRequest,
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
    info: RequestInfoDep,
) -> AlertResponse:
    alert = (await db.execute(select(Alert).where(Alert.id == alert_id))).scalar_one_or_none()
    if alert is None or not scope.contains(alert.project_id):
        raise NotFoundError("Alert", alert_id)

    before = {"status": alert.status.value if hasattr(alert.status, "value") else alert.status}

    # `BaseSchema` sets `use_enum_values`, so `payload.status` is a plain string.
    # Re-coercing is mandatory: `is` against an enum member would always be false
    # and the acknowledgement timestamps would silently never be written.
    new_status = AlertStatus(payload.status)
    alert.status = new_status
    if payload.note:
        alert.note = payload.note

    now = datetime.now(UTC)
    if new_status is AlertStatus.ACKNOWLEDGED:
        alert.acknowledged_at = now
        alert.acknowledged_by = user.id
    elif new_status in {AlertStatus.RESOLVED, AlertStatus.DISMISSED}:
        alert.resolved_at = now
    await db.flush()

    from app.services.audit import AuditService

    await AuditService(db).record(
        action=AuditAction.UPDATE,
        entity_type="alert",
        entity_id=alert_id,
        project_id=alert.project_id,
        user_id=user.id,
        user_email=user.email,
        ip=info.ip,
        user_agent=info.user_agent,
        route=info.route,
        before=before,
        after={"status": new_status.value},
    )
    return _alert(alert)


@alert_router.get(
    "/rules",
    response_model=list[AlertRuleResponse],
    summary="List alert rules",
)
async def list_alert_rules(
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[AlertRuleResponse]:
    project_ids = await resolve_scope_for_project(project_id, scope)

    # A rule with no project is a platform default and applies everywhere, so it is
    # visible even to a caller with no project membership.
    condition: ColumnElement[bool] = AlertRule.project_id.is_(None)
    if project_ids:
        condition = or_(condition, AlertRule.project_id.in_(project_ids))

    rows = (
        (await db.execute(select(AlertRule).where(condition).order_by(AlertRule.name)))
        .scalars()
        .all()
    )
    return [_alert_rule(row) for row in rows]


@alert_router.post(
    "/rules",
    response_model=AlertRuleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an alert rule",
    dependencies=[Depends(require_system_admin)],
)
async def create_alert_rule(
    payload: AlertRuleCreate,
    user: CurrentUserDep,
    db: DbSession,
    info: RequestInfoDep,
) -> AlertRuleResponse:
    rule = AlertRule(
        name=payload.name,
        alert_type=payload.alert_type,
        severity=payload.severity,
        is_enabled=payload.is_enabled,
        config=payload.config,
        escalate_after_days=payload.escalate_after_days,
        notify_channels=payload.notify_channels,
        project_id=payload.project_id,
        updated_by=user.id,
    )
    db.add(rule)
    await db.flush()

    from app.services.audit import AuditService

    await AuditService(db).record(
        action=AuditAction.CREATE,
        entity_type="alert_rule",
        entity_id=rule.id,
        entity_label=payload.name,
        project_id=payload.project_id,
        user_id=user.id,
        user_email=user.email,
        ip=info.ip,
        user_agent=info.user_agent,
        route=info.route,
        after=payload.model_dump(mode="json"),
    )
    return _alert_rule(rule)


@alert_router.patch(
    "/rules/{rule_id}",
    response_model=AlertRuleResponse,
    summary="Update an alert rule",
    dependencies=[Depends(require_system_admin)],
)
async def update_alert_rule(
    rule_id: uuid.UUID,
    payload: AlertRuleUpdate,
    user: CurrentUserDep,
    db: DbSession,
    info: RequestInfoDep,
) -> AlertRuleResponse:
    rule = (await db.execute(select(AlertRule).where(AlertRule.id == rule_id))).scalar_one_or_none()
    if rule is None:
        raise NotFoundError("Alert rule", rule_id)

    before = {"is_enabled": rule.is_enabled, "config": dict(rule.config or {})}
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)
    rule.updated_by = user.id
    await db.flush()

    from app.services.audit import AuditService

    await AuditService(db).record(
        action=AuditAction.CONFIG_CHANGE,
        entity_type="alert_rule",
        entity_id=rule_id,
        entity_label=rule.name,
        project_id=rule.project_id,
        user_id=user.id,
        user_email=user.email,
        ip=info.ip,
        user_agent=info.user_agent,
        route=info.route,
        before=before,
        after=payload.model_dump(exclude_unset=True, mode="json"),
    )
    return _alert_rule(rule)


@alert_router.delete(
    "/rules/{rule_id}",
    response_model=MessageResponse,
    summary="Delete an alert rule",
    dependencies=[Depends(require_system_admin)],
)
async def delete_alert_rule(
    rule_id: uuid.UUID, user: CurrentUserDep, db: DbSession
) -> MessageResponse:
    rule = (await db.execute(select(AlertRule).where(AlertRule.id == rule_id))).scalar_one_or_none()
    if rule is None:
        raise NotFoundError("Alert rule", rule_id)
    await db.delete(rule)
    await db.flush()
    return MessageResponse(message="The alert rule was deleted.")


# =============================================================================
# Helpers
# =============================================================================
async def _scalar(db: Any, stmt: Any) -> int:
    return int((await db.execute(stmt)).scalar() or 0)


async def _load_category(db: Any, category_id: uuid.UUID) -> ClauseMasterCategory:
    category = (
        await db.execute(
            select(ClauseMasterCategory).where(
                ClauseMasterCategory.id == category_id,
                ClauseMasterCategory.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if category is None:
        raise NotFoundError("Clause category", category_id)
    return category


async def _current_rule(db: Any, category_id: uuid.UUID) -> ClauseMasterRule | None:
    """The highest active rule version - the one extraction actually uses."""
    return (
        await db.execute(
            select(ClauseMasterRule)
            .where(
                ClauseMasterRule.category_id == category_id,
                ClauseMasterRule.is_active.is_(True),
            )
            .order_by(ClauseMasterRule.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _audit(
    db: Any,
    user: Any,
    info: Any,
    action: AuditAction,
    category: ClauseMasterCategory,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    from app.services.audit import AuditService

    await AuditService(db).record(
        action=action,
        entity_type="clause_master_category",
        entity_id=category.id,
        entity_label=str(category.key),
        user_id=user.id,
        user_email=user.email,
        ip=info.ip,
        user_agent=info.user_agent,
        route=info.route,
        before=before,
        after=after,
    )


async def _distribution(
    db: Any, column: Any, project_ids: list[uuid.UUID], model: Any
) -> list[DistributionBucket]:
    """Counts grouped by one column, with percentages."""
    stmt = select(column, func.count()).where(model.project_id.in_(project_ids))
    if hasattr(model, "deleted_at"):
        stmt = stmt.where(model.deleted_at.is_(None))
    rows = (await db.execute(stmt.group_by(column))).all()

    total = sum(int(row[1]) for row in rows) or 1
    return [
        DistributionBucket(
            label=str(row[0].value if hasattr(row[0], "value") else row[0] or "unknown"),
            value=int(row[1]),
            percentage=round(int(row[1]) / total * 100, 2),
        )
        for row in rows
    ]


async def _expiring(
    db: Any, project_ids: list[uuid.UUID], today: date, horizon: date
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(
                ContractMetadata.contract_id,
                Contract.title,
                ContractMetadata.expiration_date,
                ContractMetadata.auto_renewal,
                ContractMetadata.notice_deadline,
                ContractMetadata.risk_band,
            )
            .join(Contract, Contract.id == ContractMetadata.contract_id)
            .where(
                ContractMetadata.project_id.in_(project_ids),
                ContractMetadata.expiration_date.is_not(None),
                ContractMetadata.expiration_date >= today,
                ContractMetadata.expiration_date <= horizon,
            )
            .order_by(ContractMetadata.expiration_date)
            .limit(20)
        )
    ).all()
    return [
        {
            "contract_id": str(row.contract_id),
            "title": row.title,
            "expiration_date": row.expiration_date.isoformat() if row.expiration_date else None,
            "auto_renewal": row.auto_renewal,
            "notice_deadline": row.notice_deadline.isoformat() if row.notice_deadline else None,
            "risk_band": row.risk_band,
            "days_remaining": (row.expiration_date - today).days if row.expiration_date else None,
        }
        for row in rows
    ]


async def _top_risks(db: Any, project_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
    from app.repositories.knowledge import RiskRepository

    counts = await RiskRepository(db).severity_counts(project_ids)
    rows = (
        await db.execute(
            select(
                ContractMetadata.contract_id,
                Contract.title,
                ContractMetadata.risk_score,
                ContractMetadata.risk_band,
            )
            .join(Contract, Contract.id == ContractMetadata.contract_id)
            .where(
                ContractMetadata.project_id.in_(project_ids),
                Contract.deleted_at.is_(None),
            )
            .order_by(ContractMetadata.risk_score.desc().nullslast())
            .limit(10)
        )
    ).all()
    return [
        {
            "contract_id": str(row.contract_id),
            "title": row.title,
            "risk_score": row.risk_score,
            "risk_band": row.risk_band,
            "severity_counts": counts,
        }
        for row in rows
    ]


async def _uploads(db: Any, project_ids: list[uuid.UUID]) -> list[TimeSeriesPoint]:
    """Uploads per day over the last 30 days."""
    since = datetime.now(UTC) - timedelta(days=30)
    rows = (
        await db.execute(
            select(
                func.date(Contract.created_at).label("day"),
                func.count().label("uploads"),
            )
            .where(
                Contract.project_id.in_(project_ids),
                Contract.created_at >= since,
                Contract.deleted_at.is_(None),
            )
            .group_by(func.date(Contract.created_at))
            .order_by(func.date(Contract.created_at))
        )
    ).all()
    return [TimeSeriesPoint(period=row.day, value=float(row.uploads)) for row in rows]


def _category(row: ClauseMasterCategory, rule: ClauseMasterRule | None) -> ClauseCategoryResponse:
    return ClauseCategoryResponse(
        id=row.id,
        key=str(row.key),
        name=row.name,
        description=row.description,
        group_name=row.group_name,
        priority=row.priority,
        display_order=row.display_order,
        mandatory=bool(row.mandatory),
        confidence_threshold=float(row.confidence_threshold),
        default_risk_severity=row.default_risk_severity,
        is_active=bool(row.is_active),
        is_system=bool(row.is_system),
        ui_config=dict(row.ui_config or {}),
        current_rule=_rule(rule) if rule else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _rule(row: ClauseMasterRule) -> ClauseRuleResponse:
    return ClauseRuleResponse(
        id=row.id,
        version=row.version,
        extraction_rule=dict(row.extraction_rule or {}),
        output_schema=dict(row.output_schema or {}),
        synonyms=list(row.synonyms or []),
        prompt_template=row.prompt_template,
        standard_text=row.standard_text,
        validation_rules=dict(row.validation_rules or {}),
        examples=list(row.examples or []),
        is_active=bool(row.is_active),
        change_note=row.change_note,
        created_at=row.created_at,
    )


def _alert(row: Alert) -> AlertResponse:
    return AlertResponse(
        id=row.id,
        project_id=row.project_id,
        contract_id=row.contract_id,
        contract_title=getattr(getattr(row, "contract", None), "title", None),
        alert_type=row.alert_type,
        severity=row.severity,
        status=row.status,
        title=row.title,
        message=row.message,
        details=dict(row.details or {}),
        due_date=row.due_date,
        created_at=row.created_at,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
        resolved_at=row.resolved_at,
        note=row.note,
    )


def _alert_rule(row: AlertRule) -> AlertRuleResponse:
    return AlertRuleResponse(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        alert_type=row.alert_type,
        is_enabled=bool(row.is_enabled),
        severity=row.severity,
        config=dict(row.config or {}),
        escalate_after_days=row.escalate_after_days,
        notify_channels=list(row.notify_channels or []),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


__all__ = ["alert_router", "clause_master_router", "dashboard_router"]

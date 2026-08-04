"""Cross-contract registers: obligations, key dates, risks, counterparties.

:mod:`app.api.v1.knowledge` serves the same extracted rows one contract at a time,
which answers "what is in this agreement?". These answer the questions a repository
is actually kept for - what falls due this month, which counterparty carries the
most exposure, where the unlimited-liability clauses are - and none of them can be
asked of a single document.

**Scope is the same everywhere.** Every query is bounded by
:class:`~app.core.deps.AccessScope`, the caller's own project memberships, and
``project_id`` only ever *narrows* that set. An empty scope short-circuits to an
empty page rather than issuing an unbounded query - a user with no memberships must
not be one missing WHERE clause away from the whole estate.

**Archived and deleted contracts are excluded.** A register is a list of things
somebody may have to act on; an obligation under a contract that has been archived
is history, and mixing the two makes the live rows harder to trust.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import Select, Text, case, func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.core.deps import (
    AccessScopeDep,
    DbSession,
    PaginationDep,
    resolve_scope_for_project,
)
from app.core.enums import ContractStatus, DateType, ObligationStatus, RiskSeverity
from app.core.logging import get_logger
from app.models.contract import Contract, ContractMetadata
from app.models.knowledge import Entity, KeyDate, Obligation, Risk
from app.schemas.common import Paginated
from app.schemas.portfolio import (
    PartyDirectoryEntry,
    PortfolioKeyDate,
    PortfolioObligation,
    PortfolioRisk,
)

logger = get_logger(__name__)

router = APIRouter(tags=["Portfolio"])

#: Excluded from every register. Archived is a deliberate retirement and failed
#: means extraction never completed, so its rows are partial at best.
_HIDDEN_CONTRACT_STATES = (ContractStatus.ARCHIVED, ContractStatus.FAILED)

#: Most severe first. Postgres orders a native enum by declaration order, and
#: `RiskSeverity` happens to declare critical..low - but relying on that couples
#: this ordering to the order of an unrelated enum, so it is stated explicitly.
_SEVERITY_RANK = {
    RiskSeverity.CRITICAL: 0,
    RiskSeverity.HIGH: 1,
    RiskSeverity.MEDIUM: 2,
    RiskSeverity.LOW: 3,
}


def _live_contracts(project_ids: list[uuid.UUID]) -> list[ColumnElement[bool]]:
    """The join predicate every register shares."""
    return [
        Contract.project_id.in_(project_ids),
        Contract.deleted_at.is_(None),
        Contract.status.notin_(_HIDDEN_CONTRACT_STATES),
    ]


async def _page(
    db: Any, stmt: Select[Any], *, page: int, size: int
) -> tuple[list[Any], int]:
    """Count and fetch one page of a joined statement.

    Counting over ``stmt.subquery()`` rather than rebuilding the query keeps the
    total honest when filters change: a hand-written count is the classic way for a
    list to say "247 results" and show 12.
    """
    total = int(
        (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
    )
    if total == 0:
        return [], 0
    rows = (await db.execute(stmt.offset((page - 1) * size).limit(size))).all()
    return list(rows), total


# =============================================================================
# Obligations
# =============================================================================
@router.get(
    "/obligations",
    response_model=Paginated[PortfolioObligation],
    summary="Obligation register across your contracts",
)
async def list_obligations(
    db: DbSession,
    scope: AccessScopeDep,
    pagination: PaginationDep,
    project_id: Annotated[uuid.UUID | None, Query(description="Narrow to one project")] = None,
    obligation_status: Annotated[
        list[ObligationStatus] | None, Query(alias="status", description="Repeatable")
    ] = None,
    responsible_party: Annotated[
        str | None, Query(description="Partial match on the responsible party")
    ] = None,
    due_from: Annotated[date | None, Query(description="Due on or after this date")] = None,
    due_to: Annotated[date | None, Query(description="Due on or before this date")] = None,
    undated: Annotated[
        bool | None,
        Query(description="true = only obligations with no resolved date; false = only dated"),
    ] = None,
    q: Annotated[str | None, Query(description="Partial match on the obligation text")] = None,
) -> Paginated[PortfolioObligation]:
    """Every duty across the estate, soonest first.

    Undated obligations sort last rather than being hidden. An obligation whose
    deadline the contract states relatively ("within 30 days of invoice") is still
    a duty somebody owes, and dropping it from the register because extraction
    could not resolve a calendar date would silently shrink the very list this
    screen exists to be complete about. ``undated=true`` isolates them, which is
    the review queue for exactly that problem.
    """
    project_ids = await resolve_scope_for_project(project_id, scope)
    if not project_ids:
        return Paginated.build(items=[], page=pagination.page, size=pagination.size, total=0)

    conditions = _live_contracts(project_ids)
    if obligation_status:
        conditions.append(Obligation.status.in_(obligation_status))
    if responsible_party:
        conditions.append(Obligation.responsible_party.ilike(f"%{responsible_party}%"))
    if due_from:
        conditions.append(Obligation.due_date >= due_from)
    if due_to:
        conditions.append(Obligation.due_date <= due_to)
    if undated is True:
        conditions.append(Obligation.due_date.is_(None))
    elif undated is False:
        conditions.append(Obligation.due_date.is_not(None))
    if q:
        conditions.append(
            or_(
                Obligation.action.ilike(f"%{q}%"),
                Obligation.due_description.ilike(f"%{q}%"),
                Obligation.trigger_event.ilike(f"%{q}%"),
            )
        )

    stmt = (
        select(Obligation, Contract.title, Contract.contract_number)
        .join(Contract, Contract.id == Obligation.contract_id)
        .where(*conditions)
        .order_by(Obligation.due_date.asc().nullslast(), Obligation.created_at.desc())
    )
    rows, total = await _page(db, stmt, page=pagination.page, size=pagination.size)

    return Paginated.build(
        items=[
            PortfolioObligation(
                id=row.id,
                contract_id=row.contract_id,
                project_id=row.project_id,
                contract_title=title,
                contract_number=number,
                action=row.action,
                responsible_party=row.responsible_party,
                due_date=row.due_date,
                due_description=row.due_description,
                trigger_event=row.trigger_event,
                frequency=row.frequency,
                is_recurring=bool(row.is_recurring),
                status=row.status,
                penalty=getattr(row, "penalty", None),
                clause_id=row.clause_id,
            )
            for row, title, number in rows
        ],
        page=pagination.page,
        size=pagination.size,
        total=total,
    )


# =============================================================================
# Key dates
# =============================================================================
@router.get(
    "/key-dates",
    response_model=Paginated[PortfolioKeyDate],
    summary="Key-date timeline across your contracts",
)
async def list_key_dates(
    db: DbSession,
    scope: AccessScopeDep,
    pagination: PaginationDep,
    project_id: Annotated[uuid.UUID | None, Query(description="Narrow to one project")] = None,
    date_type: Annotated[list[DateType] | None, Query(description="Repeatable")] = None,
    date_from: Annotated[date | None, Query(description="On or after this date")] = None,
    date_to: Annotated[date | None, Query(description="On or before this date")] = None,
    unresolved: Annotated[
        bool | None, Query(description="true = only dates extraction could not resolve")
    ] = None,
) -> Paginated[PortfolioKeyDate]:
    """The calendar behind the estate, earliest first.

    Rows with only a ``date_expression`` - a relative date extraction could not pin
    to the calendar - sort last and are reachable with ``unresolved=true``. They
    are the ones most likely to matter and least likely to be noticed.
    """
    project_ids = await resolve_scope_for_project(project_id, scope)
    if not project_ids:
        return Paginated.build(items=[], page=pagination.page, size=pagination.size, total=0)

    conditions = _live_contracts(project_ids)
    if date_type:
        conditions.append(KeyDate.date_type.in_(date_type))
    if date_from:
        conditions.append(KeyDate.date_value >= date_from)
    if date_to:
        conditions.append(KeyDate.date_value <= date_to)
    if unresolved is True:
        conditions.append(KeyDate.date_value.is_(None))
    elif unresolved is False:
        conditions.append(KeyDate.date_value.is_not(None))

    stmt = (
        select(KeyDate, Contract.title, Contract.contract_number)
        .join(Contract, Contract.id == KeyDate.contract_id)
        .where(*conditions)
        .order_by(KeyDate.date_value.asc().nullslast(), KeyDate.date_type.asc())
    )
    rows, total = await _page(db, stmt, page=pagination.page, size=pagination.size)

    return Paginated.build(
        items=[
            PortfolioKeyDate(
                id=row.id,
                contract_id=row.contract_id,
                project_id=row.project_id,
                contract_title=title,
                contract_number=number,
                date_type=row.date_type,
                date_value=row.date_value,
                date_expression=row.date_expression,
                description=row.description,
                is_recurring=bool(row.is_recurring),
            )
            for row, title, number in rows
        ],
        page=pagination.page,
        size=pagination.size,
        total=total,
    )


# =============================================================================
# Risks
# =============================================================================
@router.get(
    "/risks",
    response_model=Paginated[PortfolioRisk],
    summary="Risk register across your contracts",
)
async def list_risks(
    db: DbSession,
    scope: AccessScopeDep,
    pagination: PaginationDep,
    project_id: Annotated[uuid.UUID | None, Query(description="Narrow to one project")] = None,
    severity: Annotated[list[RiskSeverity] | None, Query(description="Repeatable")] = None,
    risk_type: Annotated[str | None, Query(description="Exact risk type")] = None,
    category: Annotated[str | None, Query(description="Exact category")] = None,
    omissions: Annotated[
        bool | None, Query(description="true = only absent-clause findings")
    ] = None,
    q: Annotated[str | None, Query(description="Partial match on the description")] = None,
) -> Paginated[PortfolioRisk]:
    """Every finding across the estate, most severe first.

    Ordered by severity and then by the contribution the finding made to its
    contract's score, so the top of the list is what a reviewer should read first
    rather than whichever contract happened to be uploaded most recently.
    """
    project_ids = await resolve_scope_for_project(project_id, scope)
    if not project_ids:
        return Paginated.build(items=[], page=pagination.page, size=pagination.size, total=0)

    conditions = _live_contracts(project_ids)
    if severity:
        conditions.append(Risk.severity.in_(severity))
    if risk_type:
        conditions.append(Risk.risk_type == risk_type)
    if category:
        conditions.append(Risk.category == category)
    if omissions is True:
        conditions.append(Risk.is_omission.is_(True))
    elif omissions is False:
        conditions.append(Risk.is_omission.is_(False))
    if q:
        conditions.append(
            or_(Risk.description.ilike(f"%{q}%"), Risk.recommendation.ilike(f"%{q}%"))
        )

    # An explicit rank rather than ORDER BY severity: the column is a native enum,
    # so the database would sort it by declaration order - correct today, and
    # silently wrong the moment somebody inserts a member into the middle.
    rank = case(
        *[(Risk.severity == member, order) for member, order in _SEVERITY_RANK.items()],
        else_=len(_SEVERITY_RANK),
    )

    stmt = (
        select(Risk, Contract.title, Contract.contract_number, ContractMetadata.risk_score)
        .join(Contract, Contract.id == Risk.contract_id)
        .join(ContractMetadata, ContractMetadata.contract_id == Risk.contract_id, isouter=True)
        .where(*conditions)
        .order_by(
            rank.asc(),
            Risk.score_contribution.desc().nullslast(),
            Risk.created_at.desc(),
        )
    )
    rows, total = await _page(db, stmt, page=pagination.page, size=pagination.size)

    return Paginated.build(
        items=[
            PortfolioRisk(
                id=row.id,
                contract_id=row.contract_id,
                project_id=row.project_id,
                contract_title=title,
                contract_number=number,
                risk_type=row.risk_type,
                severity=row.severity,
                description=row.description,
                recommendation=row.recommendation,
                category=row.category,
                score_contribution=row.score_contribution,
                is_omission=bool(row.is_omission),
                clause_id=row.clause_id,
                contract_risk_score=score,
            )
            for row, title, number, score in rows
        ],
        page=pagination.page,
        size=pagination.size,
        total=total,
    )


# =============================================================================
# Parties
# =============================================================================
@router.get(
    "/parties",
    response_model=Paginated[PartyDirectoryEntry],
    summary="Counterparty directory across your contracts",
)
async def list_parties(
    db: DbSession,
    scope: AccessScopeDep,
    pagination: PaginationDep,
    project_id: Annotated[uuid.UUID | None, Query(description="Narrow to one project")] = None,
    q: Annotated[str | None, Query(description="Partial match on the party name")] = None,
    primary_only: Annotated[
        bool, Query(description="Only parties that sign somewhere, not names in passing")
    ] = False,
) -> Paginated[PartyDirectoryEntry]:
    """Who you contract with, and how often.

    Aggregated by lower-cased name. This is grouping, not entity resolution: "Acme
    Corp" and "Acme Corporation Inc." are one counterparty to a lawyer and two rows
    here. Merging them needs the trigram index on ``entities.name`` plus a
    confirmable match, and a directory that guessed would under-report exposure -
    which is the one number this screen exists to give.

    ``contract_count`` counts distinct contracts, not entity rows: a party named
    six times in one agreement is one contract, and counting mentions would make
    a verbose document look like a major relationship.
    """
    project_ids = await resolve_scope_for_project(project_id, scope)
    if not project_ids:
        return Paginated.build(items=[], page=pagination.page, size=pagination.size, total=0)

    key = func.lower(func.trim(Entity.name))
    conditions = _live_contracts(project_ids)
    conditions.append(func.length(func.trim(Entity.name)) > 0)
    if q:
        conditions.append(Entity.name.ilike(f"%{q}%"))
    if primary_only:
        conditions.append(Entity.is_primary.is_(True))

    grouped = (
        select(
            key.label("key"),
            func.count(func.distinct(Entity.contract_id)).label("contract_count"),
            func.bool_or(Entity.is_primary).label("is_primary_anywhere"),
            # `max` over the raw names picks a stable representative; the longest
            # legal name is chosen below where one exists, since that is the more
            # complete spelling.
            func.max(func.coalesce(Entity.legal_name, Entity.name)).label("display_name"),
            func.array_agg(func.distinct(Entity.entity_type)).label("entity_types"),
            func.array_agg(func.distinct(func.coalesce(Entity.role, ""))).label("roles"),
            func.array_agg(func.distinct(func.coalesce(Entity.jurisdiction, ""))).label(
                "jurisdictions"
            ),
            func.max(ContractMetadata.expiration_date).label("next_expiry"),
            # Postgres has no min(uuid) aggregate, and the value here is only a
            # "somewhere to click through to" - so it goes via text for a stable,
            # arbitrary representative rather than a second query.
            func.min(func.cast(Entity.contract_id, Text)).label("sample_contract_id"),
        )
        .join(Contract, Contract.id == Entity.contract_id)
        .join(ContractMetadata, ContractMetadata.contract_id == Entity.contract_id, isouter=True)
        .where(*conditions)
        .group_by(key)
        .order_by(func.count(func.distinct(Entity.contract_id)).desc(), key.asc())
    )

    total = int(
        (await db.execute(select(func.count()).select_from(grouped.subquery()))).scalar() or 0
    )
    if total == 0:
        return Paginated.build(items=[], page=pagination.page, size=pagination.size, total=0)

    rows = (
        await db.execute(
            grouped.offset((pagination.page - 1) * pagination.size).limit(pagination.size)
        )
    ).all()

    keys = [row.key for row in rows]
    values = await _values_by_party(db, keys, project_ids)

    return Paginated.build(
        items=[
            PartyDirectoryEntry(
                key=row.key,
                name=row.display_name or row.key,
                entity_types=[value for value in (row.entity_types or []) if value],
                roles=sorted(value for value in (row.roles or []) if value),
                jurisdictions=sorted(value for value in (row.jurisdictions or []) if value),
                contract_count=int(row.contract_count or 0),
                is_primary_anywhere=bool(row.is_primary_anywhere),
                total_value=values.get(row.key, {}),
                next_expiry=row.next_expiry,
                sample_contract_id=uuid.UUID(row.sample_contract_id)
                if row.sample_contract_id
                else None,
            )
            for row in rows
        ],
        page=pagination.page,
        size=pagination.size,
        total=total,
    )


async def _values_by_party(
    db: Any, keys: list[str], project_ids: list[uuid.UUID]
) -> dict[str, dict[str, float]]:
    """Contract value per party, per currency.

    Split by currency rather than summed. Adding 40,000 GBP to 50,000 USD produces
    a number that is wrong in every currency, and a directory that reports one is
    worse than one that reports neither.

    Distinct on contract id: a party named several times in one agreement must not
    have its value counted several times.
    """
    if not keys:
        return {}

    key = func.lower(func.trim(Entity.name))
    inner = (
        select(
            key.label("key"),
            ContractMetadata.currency.label("currency"),
            Entity.contract_id.label("contract_id"),
            func.max(ContractMetadata.contract_value).label("value"),
        )
        .join(Contract, Contract.id == Entity.contract_id)
        .join(ContractMetadata, ContractMetadata.contract_id == Entity.contract_id)
        .where(
            *_live_contracts(project_ids),
            key.in_(keys),
            ContractMetadata.contract_value.is_not(None),
        )
        .group_by(key, ContractMetadata.currency, Entity.contract_id)
        .subquery()
    )

    rows = (
        await db.execute(
            select(inner.c.key, inner.c.currency, func.sum(inner.c.value)).group_by(
                inner.c.key, inner.c.currency
            )
        )
    ).all()

    totals: dict[str, dict[str, float]] = {}
    for party_key, currency, total in rows:
        if total is None:
            continue
        totals.setdefault(party_key, {})[currency or "unknown"] = float(total)
    return totals


__all__ = ["router"]

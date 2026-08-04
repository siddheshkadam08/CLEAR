"""The Clause Master, grouped by agreement type.

Two things live behind one screen, and keeping them straight is what makes the
screen simple:

* **A clause** is a row in ``clause_master_categories`` - global, keyed on
  ``key``, and the thing ``clauses.clause_type`` points at on every extracted
  contract.
* **A mapping** is a row in ``agreement_type_clauses`` - "an MSA is checked for
  Indemnification, and right now that check is on".

Every button on the screen is one of those two. Toggling active, marking
mandatory and reordering are mappings. Renaming a clause or changing its synonyms
is the clause. Removing a clause from a type deletes a mapping; retiring a clause
everywhere soft-deletes the category.

**Deactivating applies to new uploads only.** The extraction stage reads this
mapping once, while a document is being processed. A contract extracted last week
keeps the clauses it was extracted with - those rows are evidence of what the
document says, not of what the configuration would look for today. Re-process the
contract if you want it re-judged.

**Deletes are soft.** ``clauses.clause_type`` on every already-extracted contract
references the key, so a hard delete would leave historical extractions pointing
at nothing. Setting ``deleted_at`` removes the clause from the screen and from
future extractions while old contracts keep rendering. That also replaces the
``is_system`` restriction, which existed to prevent exactly the damage soft
deletion makes impossible.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import CurrentUserDep, DbSession, RequestInfoDep, require_system_admin
from app.core.enums import AgreementType, AuditAction
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.clause_master import AgreementTypeClause, ClauseMasterCategory, ClauseMasterRule
from app.models.profile import DocumentProfile
from app.schemas.admin import (
    AgreementClause,
    AgreementClauseUpsert,
    AgreementTypeClauses,
    ClauseDefinitionUpsert,
    ClauseImportRequest,
    ClauseImportResult,
    ClauseImportRow,
)
from app.schemas.common import MessageResponse

logger = get_logger(__name__)

router = APIRouter(prefix="/clause-master", tags=["Clause Master"])

_ADMIN = [Depends(require_system_admin)]


# =============================================================================
# Read
# =============================================================================
@router.get(
    "/by-agreement-type",
    response_model=list[AgreementTypeClauses],
    summary="Every clause, grouped by agreement type",
)
async def list_by_agreement_type(
    user: CurrentUserDep,
    db: DbSession,
    include_unmapped: Annotated[
        bool, Query(description="Also list clauses not yet attached to each type")
    ] = True,
) -> list[AgreementTypeClauses]:
    """The screen, in one call.

    Types come from the configured document profiles, so the list is the same set
    the classifier can produce - a group here that the classifier can never emit
    would be a configuration nobody can reach.

    ``include_unmapped`` returns the rest of the Clause Master alongside, flagged
    ``is_mapped: false``. Without it, adding a clause to a type means already
    knowing which clauses exist, which is exactly what this screen is for.
    """
    categories = await _all_categories(db)
    by_key = {str(category.key): category for category in categories}

    labels = await _type_labels(db)
    mappings = await _mappings(db)

    # Every configured type, plus any type that has mappings but lost its profile.
    types = sorted(set(labels) | set(mappings))

    groups: list[AgreementTypeClauses] = []
    for agreement_type in types:
        rows = mappings.get(agreement_type, {})
        clauses = [
            _clause(by_key[key], row)
            for key, row in sorted(
                rows.items(), key=lambda item: (item[1].display_order, item[0])
            )
            if key in by_key
        ]
        if include_unmapped:
            clauses.extend(
                _clause(category, None)
                for category in categories
                if str(category.key) not in rows
            )
        groups.append(
            AgreementTypeClauses(
                agreement_type=agreement_type,
                label=labels.get(agreement_type) or _humanise(agreement_type),
                clauses=clauses,
            )
        )
    return groups


@router.get(
    "/clauses",
    response_model=list[AgreementClause],
    summary="The clause taxonomy itself, ungrouped",
)
async def list_clauses(user: CurrentUserDep, db: DbSession) -> list[AgreementClause]:
    """Every clause in the Clause Master, independent of any agreement type."""
    return [_clause(category, None) for category in await _all_categories(db)]


# =============================================================================
# The clause taxonomy
# =============================================================================
@router.post(
    "/clauses",
    response_model=AgreementClause,
    status_code=status.HTTP_201_CREATED,
    summary="Add a clause to the taxonomy",
    dependencies=_ADMIN,
)
async def create_clause(
    payload: ClauseDefinitionUpsert,
    user: CurrentUserDep,
    db: DbSession,
    info: RequestInfoDep,
) -> AgreementClause:
    if not payload.key:
        raise ValidationError("A clause key is required.", details={"field": "key"})

    existing = await _find_category(db, payload.key, include_deleted=True)
    if existing is not None and existing.deleted_at is None:
        raise ConflictError(f"A clause with the key '{payload.key}' already exists.")

    if existing is not None:
        # Restoring a soft-deleted clause rather than refusing. The key is unique
        # and a delete is reversible, so "create the one you deleted" should mean
        # what the user expects instead of a conflict they cannot resolve.
        existing.deleted_at = None
        existing.is_active = True
        category = existing
    else:
        category = ClauseMasterCategory(key=payload.key, priority=999, created_by=user.id)
        db.add(category)

    category.name = payload.name
    category.description = payload.description
    category.group_name = payload.group_name
    await db.flush()
    await _set_synonyms(db, category, payload.synonyms, user_id=user.id)

    await _audit(db, user, info, AuditAction.CREATE, category, after=payload.model_dump(mode="json"))
    return await _reload(db, category)


@router.patch(
    "/clauses/{clause_key}",
    response_model=AgreementClause,
    summary="Edit a clause",
    dependencies=_ADMIN,
)
async def update_clause(
    clause_key: str,
    payload: ClauseDefinitionUpsert,
    user: CurrentUserDep,
    db: DbSession,
    info: RequestInfoDep,
) -> AgreementClause:
    category = await _require_category(db, clause_key)
    before = {
        "name": category.name,
        "description": category.description,
        "group_name": category.group_name,
    }

    category.name = payload.name
    category.description = payload.description
    category.group_name = payload.group_name
    await db.flush()
    await _set_synonyms(db, category, payload.synonyms, user_id=user.id)

    await _audit(
        db,
        user,
        info,
        AuditAction.UPDATE,
        category,
        before=before,
        after=payload.model_dump(mode="json"),
    )
    return await _reload(db, category)


@router.delete(
    "/clauses/{clause_key}",
    response_model=MessageResponse,
    summary="Retire a clause everywhere",
    dependencies=_ADMIN,
)
async def delete_clause(
    clause_key: str,
    user: CurrentUserDep,
    db: DbSession,
    info: RequestInfoDep,
) -> MessageResponse:
    """Soft delete. Historical extractions keep rendering; new ones skip it.

    The mappings go with it - a clause nobody can extract should not still be
    listed under six agreement types - but the extracted `clauses` rows do not.
    Those are what the document said, and they remain true.
    """
    category = await _require_category(db, clause_key)

    removed = len(
        (
            await db.execute(
                delete(AgreementTypeClause)
                .where(AgreementTypeClause.clause_key == clause_key)
                .returning(AgreementTypeClause.id)
            )
        )
        .scalars()
        .all()
    )
    category.deleted_at = func.now()
    category.is_active = False
    await db.flush()

    await _audit(
        db,
        user,
        info,
        AuditAction.DELETE,
        category,
        before={"key": clause_key, "mappings": removed},
    )
    return MessageResponse(
        message=(
            f"'{category.name}' was retired and removed from {removed} agreement "
            "type(s). Contracts already extracted keep their clauses."
        )
    )


# =============================================================================
# The mapping
# =============================================================================
@router.put(
    "/by-agreement-type/{agreement_type}/clauses",
    response_model=AgreementClause,
    summary="Attach a clause to an agreement type, or change how it applies",
    dependencies=_ADMIN,
)
async def upsert_mapping(
    agreement_type: str,
    payload: AgreementClauseUpsert,
    user: CurrentUserDep,
    db: DbSession,
    info: RequestInfoDep,
) -> AgreementClause:
    """One call for attach, toggle active, mark mandatory and reorder.

    A PUT rather than a POST/PATCH pair: the screen's controls all describe the
    desired end state of one ``(agreement_type, clause)`` pair, and an upsert says
    that directly. It is also what makes the toggle idempotent under a double
    click.
    """
    category = await _require_category(db, payload.clause_key)

    values = {
        "agreement_type": agreement_type,
        "clause_key": payload.clause_key,
        "is_active": payload.is_active,
        "is_mandatory": payload.is_mandatory,
        "display_order": payload.display_order
        if payload.display_order is not None
        else int(category.priority or 100),
        "updated_by": user.id,
    }
    statement = (
        pg_insert(AgreementTypeClause)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["agreement_type", "clause_key"],
            set_={
                "is_active": values["is_active"],
                "is_mandatory": values["is_mandatory"],
                "display_order": values["display_order"],
                "updated_by": user.id,
            },
        )
        .returning(AgreementTypeClause)
    )
    row = (await db.execute(statement)).scalar_one()
    await db.flush()

    await _audit(
        db,
        user,
        info,
        AuditAction.CONFIG_CHANGE,
        category,
        after={"agreement_type": agreement_type, **payload.model_dump(mode="json")},
    )
    return _clause(category, row)


@router.delete(
    "/by-agreement-type/{agreement_type}/clauses/{clause_key}",
    response_model=MessageResponse,
    summary="Remove a clause from an agreement type",
    dependencies=_ADMIN,
)
async def remove_mapping(
    agreement_type: str,
    clause_key: str,
    user: CurrentUserDep,
    db: DbSession,
    info: RequestInfoDep,
) -> MessageResponse:
    """Detaches only. The clause itself stays in the taxonomy for other types.

    To stop applying a clause *without* forgetting it was configured, set
    ``is_active: false`` instead - that is the difference the mapping row exists
    to record.
    """
    removed = (
        await db.execute(
            delete(AgreementTypeClause)
            .where(
                AgreementTypeClause.agreement_type == agreement_type,
                AgreementTypeClause.clause_key == clause_key,
            )
            .returning(AgreementTypeClause.id)
        )
    ).scalars().first()
    if removed is None:
        raise NotFoundError("Clause mapping", f"{agreement_type}/{clause_key}")
    await db.flush()

    category = await _find_category(db, clause_key)
    if category is not None:
        await _audit(
            db,
            user,
            info,
            AuditAction.DELETE,
            category,
            before={"agreement_type": agreement_type, "clause_key": clause_key},
        )
    return MessageResponse(message=f"Removed from {_humanise(agreement_type)}.")


# =============================================================================
# Bulk
# =============================================================================
@router.get(
    "/export",
    response_model=list[ClauseImportRow],
    summary="Every mapping as flat rows, for CSV or XLSX",
)
async def export_rows(user: CurrentUserDep, db: DbSession) -> list[ClauseImportRow]:
    """The same columns the import accepts, so a round trip is lossless.

    Returned as JSON and rendered to a file by the browser. The sheet format is a
    presentation concern, and doing it client-side means one code path produces
    both CSV and XLSX from data the screen already holds.
    """
    categories = {str(c.key): c for c in await _all_categories(db)}
    mappings = await _mappings(db)

    rows: list[ClauseImportRow] = []
    for agreement_type in sorted(mappings):
        for key, mapping in sorted(
            mappings[agreement_type].items(), key=lambda item: (item[1].display_order, item[0])
        ):
            category = categories.get(key)
            if category is None:
                continue
            rows.append(
                ClauseImportRow(
                    agreement_type=agreement_type,
                    clause_key=key,
                    name=str(category.name),
                    description=category.description,
                    group_name=category.group_name,
                    synonyms=_synonyms(category),
                    is_active=bool(mapping.is_active),
                    is_mandatory=bool(mapping.is_mandatory),
                    display_order=int(mapping.display_order),
                )
            )
    return rows


@router.post(
    "/import",
    response_model=ClauseImportResult,
    summary="Apply an uploaded sheet",
    dependencies=_ADMIN,
)
async def import_rows(
    payload: ClauseImportRequest,
    user: CurrentUserDep,
    db: DbSession,
    info: RequestInfoDep,
) -> ClauseImportResult:
    """Upsert clauses and mappings from a parsed sheet.

    The file is parsed in the browser - CSV and XLSX both reduce to the same rows,
    and doing it there avoids a server-side spreadsheet dependency. Everything
    that decides what is *written* is validated here regardless: a client parse is
    a convenience, never the authority on what becomes extraction configuration.

    A row that cannot be applied is skipped and reported. Failing the whole import
    on one bad row would make a 400-row sheet unusable because of a typo in one.
    """
    result = ClauseImportResult()
    seen: set[tuple[str, str]] = set()
    valid_types = {member.value for member in AgreementType}

    for index, row in enumerate(payload.rows, start=2):  # row 1 is the header
        agreement_type = row.agreement_type.strip()
        clause_key = row.clause_key.strip().lower()

        if agreement_type not in valid_types:
            result.skipped.append(
                {
                    "row": str(index),
                    "clause_key": clause_key,
                    "reason": f"'{agreement_type}' is not a known agreement type",
                }
            )
            continue

        category = await _find_category(db, clause_key)
        if category is None:
            if not payload.create_missing_clauses:
                result.skipped.append(
                    {"row": str(index), "clause_key": clause_key, "reason": "no such clause"}
                )
                continue
            if not row.name:
                result.skipped.append(
                    {
                        "row": str(index),
                        "clause_key": clause_key,
                        "reason": "new clause needs a name",
                    }
                )
                continue
            category = ClauseMasterCategory(
                key=clause_key,
                name=row.name,
                description=row.description,
                group_name=row.group_name,
                priority=999,
                created_by=user.id,
            )
            db.add(category)
            await db.flush()
            await _set_synonyms(db, category, row.synonyms, user_id=user.id)
            result.clauses_created += 1
        elif row.name and (
            category.name != row.name
            or category.description != row.description
            or category.group_name != row.group_name
        ):
            category.name = row.name
            category.description = row.description
            category.group_name = row.group_name
            if row.synonyms:
                await _set_synonyms(db, category, row.synonyms, user_id=user.id)
            result.clauses_updated += 1

        existed = await db.scalar(
            select(func.count())
            .select_from(AgreementTypeClause)
            .where(
                AgreementTypeClause.agreement_type == agreement_type,
                AgreementTypeClause.clause_key == clause_key,
            )
        )
        await db.execute(
            pg_insert(AgreementTypeClause)
            .values(
                agreement_type=agreement_type,
                clause_key=clause_key,
                is_active=row.is_active,
                is_mandatory=row.is_mandatory,
                display_order=row.display_order
                if row.display_order is not None
                else int(category.priority or 100),
                updated_by=user.id,
            )
            .on_conflict_do_update(
                index_elements=["agreement_type", "clause_key"],
                set_={
                    "is_active": row.is_active,
                    "is_mandatory": row.is_mandatory,
                    "display_order": row.display_order
                    if row.display_order is not None
                    else int(category.priority or 100),
                    "updated_by": user.id,
                },
            )
        )
        if existed:
            result.mappings_updated += 1
        else:
            result.mappings_created += 1
        seen.add((agreement_type, clause_key))

    if payload.deactivate_missing and seen:
        # Only within the types the sheet actually mentions. A file covering one
        # agreement type must not switch off the other twelve.
        touched_types = {agreement_type for agreement_type, _ in seen}
        rows = (
            await db.execute(
                select(AgreementTypeClause).where(
                    AgreementTypeClause.agreement_type.in_(touched_types),
                    AgreementTypeClause.is_active.is_(True),
                )
            )
        ).scalars().all()
        for mapping in rows:
            if (str(mapping.agreement_type), str(mapping.clause_key)) in seen:
                continue
            mapping.is_active = False
            mapping.updated_by = user.id
            result.mappings_deactivated += 1

    await db.flush()
    logger.info(
        "clause_master_imported",
        user_id=str(user.id),
        **result.model_dump(exclude={"skipped"}),
        skipped=len(result.skipped),
    )
    return result


# =============================================================================
# Helpers
# =============================================================================
async def _all_categories(db: AsyncSession) -> list[ClauseMasterCategory]:
    rows = await db.execute(
        select(ClauseMasterCategory)
        .options(selectinload(ClauseMasterCategory.rules))
        .where(ClauseMasterCategory.deleted_at.is_(None))
        .order_by(ClauseMasterCategory.priority, ClauseMasterCategory.key)
    )
    return list(rows.scalars().all())


async def _mappings(db: AsyncSession) -> dict[str, dict[str, AgreementTypeClause]]:
    rows = (await db.execute(select(AgreementTypeClause))).scalars().all()
    grouped: dict[str, dict[str, AgreementTypeClause]] = {}
    for row in rows:
        grouped.setdefault(str(row.agreement_type), {})[str(row.clause_key)] = row
    return grouped


async def _type_labels(db: AsyncSession) -> dict[str, str]:
    """Agreement type -> the profile's display name, for every configured type."""
    rows = await db.execute(
        select(DocumentProfile.agreement_type, DocumentProfile.name).where(
            DocumentProfile.is_active.is_(True),
            DocumentProfile.deleted_at.is_(None),
        )
    )
    return {str(agreement_type): str(name) for agreement_type, name in rows.all() if agreement_type}


async def _find_category(
    db: AsyncSession, clause_key: str, *, include_deleted: bool = False
) -> ClauseMasterCategory | None:
    statement = (
        select(ClauseMasterCategory)
        .options(selectinload(ClauseMasterCategory.rules))
        .where(ClauseMasterCategory.key == clause_key)
    )
    if not include_deleted:
        statement = statement.where(ClauseMasterCategory.deleted_at.is_(None))
    return (await db.execute(statement)).scalars().first()


async def _require_category(db: AsyncSession, clause_key: str) -> ClauseMasterCategory:
    category = await _find_category(db, clause_key)
    if category is None:
        raise NotFoundError("Clause", clause_key)
    return category


async def _set_synonyms(
    db: AsyncSession,
    category: ClauseMasterCategory,
    synonyms: list[str],
    *,
    user_id: uuid.UUID,
) -> None:
    """Write synonyms onto the category's active rule, creating one if needed.

    Synonyms live on the *rule*, not the category, because they are extraction
    behaviour. The screen presents them as a property of the clause because that
    is what they are to a reader - so this hides the indirection rather than
    exposing rule versioning on a form that has no use for it.
    """
    cleaned = [value.strip() for value in synonyms if value and value.strip()]

    # Queried, not `category.rules`. That relationship is lazy, and on a category
    # created moments ago it has never been loaded - traversing it here is IO from
    # a sync context, which is `MissingGreenlet` rather than a slow query.
    rule = (
        await db.execute(
            select(ClauseMasterRule).where(
                ClauseMasterRule.category_id == category.id,
                ClauseMasterRule.is_active.is_(True),
            )
        )
    ).scalars().first()

    if rule is None:
        db.add(
            ClauseMasterRule(
                category_id=category.id,
                version=1,
                synonyms=cleaned,
                is_active=True,
                created_by=user_id,
                change_note="Created from the Clause Master screen.",
            )
        )
        await db.flush()
        return

    if list(rule.synonyms or []) != cleaned:
        rule.synonyms = cleaned
        await db.flush()


async def _reload(db: AsyncSession, category: ClauseMasterCategory) -> AgreementClause:
    """Re-read the category with its rules eagerly loaded, then serialise.

    `_clause` reads `category.rules` for the synonyms, and after a write the
    in-session object may have that relationship unloaded - which would be lazy IO
    from the sync response builder. One extra read is cheaper than the class of bug
    that causes.
    """
    fresh = await _find_category(db, str(category.key), include_deleted=True)
    return _clause(fresh or category, None)


def _synonyms(category: ClauseMasterCategory) -> list[str]:
    rule = next((r for r in (category.rules or []) if r.is_active), None)
    return [str(value) for value in (rule.synonyms if rule else []) or []]


def _clause(
    category: ClauseMasterCategory, mapping: AgreementTypeClause | None
) -> AgreementClause:
    return AgreementClause(
        clause_key=str(category.key),
        name=str(category.name),
        description=category.description,
        group_name=category.group_name,
        synonyms=_synonyms(category),
        is_active=bool(mapping.is_active) if mapping else False,
        is_mandatory=bool(mapping.is_mandatory) if mapping else False,
        display_order=int(mapping.display_order) if mapping else int(category.priority or 100),
        is_mapped=mapping is not None,
    )


def _humanise(value: str) -> str:
    return value.replace("_", " ").title()


async def _audit(
    db: AsyncSession,
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
        entity_type="clause_master",
        entity_id=category.id,
        entity_label=str(category.name),
        user_id=user.id,
        user_email=user.email,
        ip=info.ip,
        user_agent=info.user_agent,
        route=info.route,
        before=before,
        after=after,
    )


__all__ = ["router"]

"""Insights over what the document pipeline actually produced.

This used to read `cip_DocMaster`, `cip_DocContentMaster` and `cip_docMapping` -
three tables owned by another team, created outside this repository, and living
only on a database that has been decommissioned. Every column duplicated one the
platform already keeps, so the screen now reads the platform's own tables:
`contracts` for the documents, `clauses` for what was extracted from them,
`embeddings` for what was vectorised, and `document_profiles` for how many clauses
each document type is expected to have.

That is a change in *source*, not in meaning, and it removes the reason the old
module gave for its isolation - that mixing two pipelines would sum two different
definitions of "clause". There is now one definition.

Coverage is still the number worth looking at. A document's clause count on its
own says nothing - twelve clauses is good for an NDA and poor for an MSA - so
every figure is reported against the count its profile expects.

**Scoped to the caller's projects.** The cip tables carried no project column, so
the old endpoint could not scope and did not try. `contracts` and `clauses` do,
and an unscoped read here would be a cross-project leak on a screen that has no
business being one.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AccessScopeDep, CurrentUserDep, DbSession, resolve_scope_for_project
from app.core.enums import ContractStatus, EmbeddingLevel
from app.core.logging import get_logger
from app.models.clause_master import ClauseMasterCategory
from app.models.contract import Contract
from app.models.embedding import Embedding
from app.models.knowledge import Clause
from app.models.profile import DocumentProfile

logger = get_logger(__name__)

router = APIRouter(prefix="/docpipeline", tags=["Document pipeline"])

#: Excluded from the figures. A failed document never finished extraction, so its
#: clause count is not a coverage signal, it is noise.
_HIDDEN = (ContractStatus.ARCHIVED, ContractStatus.FAILED)


@router.get("", summary="Document pipeline insights")
async def get_docpipeline_dashboard(
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
    limit: Annotated[int, Query(ge=1, le=200, description="Documents to list")] = 50,
    project_id: Annotated[uuid.UUID | None, Query(description="Narrow to one project")] = None,
) -> dict[str, Any]:
    """Everything the pipeline has produced, summarised."""
    project_ids = await resolve_scope_for_project(project_id, scope)
    if not project_ids:
        return _empty()

    expected = await _expected_clause_counts(db)
    documents = await _documents(db, project_ids, expected, limit)
    totals = await _totals(db, project_ids)

    return {
        "totals": {
            **totals,
            "average_coverage": _mean([d["coverage"] for d in documents]),
        },
        "documents": documents,
        "by_doc_type": await _by_doc_type(db, project_ids, expected),
        "clause_frequency": await _clause_frequency(db, project_ids),
        "missing_clauses": await _missing_clauses(db, project_ids, expected),
        "expected_by_doc_type": expected,
    }


def _empty() -> dict[str, Any]:
    """A caller with no projects sees an empty dashboard, not an error."""
    return {
        "totals": {
            "documents": 0,
            "clauses": 0,
            "embedded": 0,
            "clause_page_regions": 0,
            "clauses_per_document": 0.0,
            "average_coverage": None,
        },
        "documents": [],
        "by_doc_type": [],
        "clause_frequency": [],
        "missing_clauses": [],
        "expected_by_doc_type": {},
    }


def _live(project_ids: list[uuid.UUID]) -> list[Any]:
    return [
        Contract.project_id.in_(project_ids),
        Contract.deleted_at.is_(None),
        Contract.status.notin_(_HIDDEN),
    ]


def _clauses_of(project_ids: list[uuid.UUID]) -> Select[Any]:
    """Clauses belonging to a document this caller can see."""
    return select(Clause).join(Contract, Contract.id == Clause.contract_id).where(*_live(project_ids))


# ------------------------------------------------------------------ queries
async def _expected_clause_counts(db: AsyncSession) -> dict[str, int]:
    """How many clauses each document type's profile expects.

    Mandatory plus optional, which is the profile's full target list - the same
    set `load_clauses` hands the detector. Counting only the mandatory ones would
    report coverage above 100% the moment an optional clause was found.
    """
    rows = await db.execute(
        select(
            DocumentProfile.agreement_type,
            DocumentProfile.mandatory_clauses,
            DocumentProfile.optional_clauses,
        ).where(
            DocumentProfile.is_active.is_(True),
            DocumentProfile.deleted_at.is_(None),
        )
    )
    expected: dict[str, int] = {}
    for agreement_type, mandatory, optional in rows.all():
        if not agreement_type:
            continue
        keys = {str(key) for key in (mandatory or [])} | {str(key) for key in (optional or [])}
        # Several profiles can share an agreement_type; the widest target wins, so
        # coverage is never flattered by picking the narrowest.
        expected[str(agreement_type)] = max(expected.get(str(agreement_type), 0), len(keys))
    return expected


async def _totals(db: AsyncSession, project_ids: list[uuid.UUID]) -> dict[str, Any]:
    documents = await db.scalar(
        select(func.count()).select_from(Contract).where(*_live(project_ids))
    )
    clauses = await db.scalar(
        select(func.count()).select_from(_clauses_of(project_ids).subquery())
    )
    embedded = await db.scalar(
        select(func.count())
        .select_from(Embedding)
        .join(Contract, Contract.id == Embedding.contract_id)
        .where(*_live(project_ids), Embedding.level == EmbeddingLevel.CLAUSE)
    )
    # A clause spanning two pages contributes two, which is the number that
    # matters here: it is how many page-regions can be highlighted.
    regions = await db.scalar(
        select(func.coalesce(func.sum(func.jsonb_array_length(Clause.bounding_boxes)), 0))
        .select_from(Clause)
        .join(Contract, Contract.id == Clause.contract_id)
        .where(*_live(project_ids))
    )
    return {
        "documents": int(documents or 0),
        "clauses": int(clauses or 0),
        "embedded": int(embedded or 0),
        "clause_page_regions": int(regions or 0),
        "clauses_per_document": round((clauses or 0) / (documents or 1), 1),
    }


async def _documents(
    db: AsyncSession,
    project_ids: list[uuid.UUID],
    expected: dict[str, int],
    limit: int,
) -> list[dict[str, Any]]:
    """One row per processed document, with its clause coverage."""
    counts = (
        select(Clause.contract_id.label("contract_id"), func.count().label("found"))
        .group_by(Clause.contract_id)
        .subquery()
    )
    rows = await db.execute(
        select(
            Contract.id,
            Contract.agreement_type,
            Contract.title,
            Contract.original_file_name,
            Contract.storage_path,
            func.coalesce(counts.c.found, 0).label("found"),
        )
        .outerjoin(counts, counts.c.contract_id == Contract.id)
        .where(*_live(project_ids))
        .order_by(Contract.created_at.desc())
        .limit(limit)
    )

    documents: list[dict[str, Any]] = []
    for contract_id, doc_type, title, file_name, storage_path, found in rows.all():
        target = expected.get(doc_type or "", 0)
        documents.append(
            {
                # The frontend labels this column "docid" and renders it as an
                # identifier; the contract id is the identifier that now exists.
                "docid": str(contract_id),
                "doc_type": doc_type,
                "doc_path": title or file_name or storage_path,
                "json_path": None,
                "clauses_found": int(found),
                "clauses_expected": target,
                "coverage": round(int(found) / target, 3) if target else None,
            }
        )
    return documents


async def _by_doc_type(
    db: AsyncSession, project_ids: list[uuid.UUID], expected: dict[str, int]
) -> list[dict[str, Any]]:
    """Document-type mix, with the clause target for each."""
    rows = await db.execute(
        select(Contract.agreement_type, func.count())
        .where(*_live(project_ids))
        .group_by(Contract.agreement_type)
        .order_by(func.count().desc())
    )
    return [
        {
            "doc_type": doc_type,
            "documents": int(count),
            "clauses_expected": expected.get(doc_type or "", 0),
        }
        for doc_type, count in rows.all()
    ]


async def _clause_frequency(
    db: AsyncSession, project_ids: list[uuid.UUID]
) -> list[dict[str, Any]]:
    """Which clauses are actually being found, across every document.

    Counted as *documents*, not rows: a contract with three indemnity clauses is
    one document that has indemnity, and counting rows would make a verbose
    contract look like broad coverage.
    """
    rows = await db.execute(
        select(Clause.clause_type, func.count(func.distinct(Clause.contract_id)))
        .join(Contract, Contract.id == Clause.contract_id)
        .where(*_live(project_ids))
        .group_by(Clause.clause_type)
        .order_by(func.count(func.distinct(Clause.contract_id)).desc(), Clause.clause_type)
    )
    labels = await _clause_labels(db)
    return [
        {"clause": labels.get(str(clause_type), str(clause_type)), "documents": int(count)}
        for clause_type, count in rows.all()
    ]


async def _missing_clauses(
    db: AsyncSession, project_ids: list[uuid.UUID], expected: dict[str, int]
) -> list[dict[str, Any]]:
    """Clauses a profile expects that no document of that type has.

    The inverse of clause frequency, and the more actionable half: a clause the
    pipeline has never once found is either genuinely rare in these contracts or
    a detection gap, and both are worth knowing.
    """
    found = await db.execute(
        select(Contract.agreement_type, Clause.clause_type)
        .join(Contract, Contract.id == Clause.contract_id)
        .where(*_live(project_ids))
        .distinct()
    )
    seen: dict[str, set[str]] = {}
    for doc_type, clause_type in found.all():
        seen.setdefault(doc_type or "", set()).add(str(clause_type))

    processed = await db.execute(
        select(Contract.agreement_type)
        .where(*_live(project_ids), Contract.agreement_type.is_not(None))
        .distinct()
    )
    processed_types = [str(value) for (value,) in processed.all() if value]
    if not processed_types:
        return []

    targets = await _profile_clause_keys(db, processed_types)
    labels = await _clause_labels(db)

    missing: list[dict[str, Any]] = []
    for doc_type in processed_types:
        for key in targets.get(doc_type, []):
            if key in seen.get(doc_type, set()):
                continue
            missing.append(
                {
                    "doc_type": doc_type,
                    "clause": labels.get(key, key),
                    "description": "",
                    "expected_in": expected.get(doc_type, 0),
                }
            )
    return missing


async def _profile_clause_keys(
    db: AsyncSession, agreement_types: list[str]
) -> dict[str, list[str]]:
    """The clause keys each document type's profile targets, in priority order."""
    rows = await db.execute(
        select(
            DocumentProfile.agreement_type,
            DocumentProfile.mandatory_clauses,
            DocumentProfile.optional_clauses,
        ).where(
            DocumentProfile.is_active.is_(True),
            DocumentProfile.deleted_at.is_(None),
            DocumentProfile.agreement_type.in_(agreement_types),
        )
    )
    order = await _clause_priority(db)
    targets: dict[str, list[str]] = {}
    for agreement_type, mandatory, optional in rows.all():
        keys = {str(key) for key in (mandatory or [])} | {str(key) for key in (optional or [])}
        targets[str(agreement_type)] = sorted(keys, key=lambda key: order.get(key, 9_999))
    return targets


async def _clause_labels(db: AsyncSession) -> dict[str, str]:
    """Clause key -> the name a reader recognises."""
    rows = await db.execute(select(ClauseMasterCategory.key, ClauseMasterCategory.name))
    return {str(key): str(name) for key, name in rows.all()}


async def _clause_priority(db: AsyncSession) -> dict[str, int]:
    rows = await db.execute(
        select(ClauseMasterCategory.key, ClauseMasterCategory.priority)
    )
    return {str(key): int(priority or 9_999) for key, priority in rows.all()}


def _mean(values: list[Any]) -> float | None:
    numbers = [value for value in values if isinstance(value, (int, float))]
    return round(sum(numbers) / len(numbers), 3) if numbers else None

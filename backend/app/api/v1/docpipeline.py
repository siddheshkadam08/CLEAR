"""Insights over what the document pipeline actually produced.

Everything here reads `cip_DocMaster`, `cip_DocContentMaster` and the taxonomy
in `cip_docMapping`, and nothing else. That is deliberate: the older pipeline
writes to `contracts`, `clauses` and `embeddings`, and mixing the two on one
screen would show a clause count that is the sum of two different definitions of
"clause" produced by two different processes.

Coverage is the number worth looking at. A document's clause count on its own
says nothing - twelve clauses is good for an NDA and poor for an MSA - so every
figure is reported against the number of clauses `cip_docMapping` says that
document type should have.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.docpipeline.tables import (
    cip_doc_content_master,
    cip_doc_mapping,
    cip_doc_master,
)
from app.core.deps import CurrentUserDep, DbSession
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/docpipeline", tags=["Document pipeline"])


@router.get("", summary="Document pipeline insights")
async def get_docpipeline_dashboard(
    user: CurrentUserDep,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=200, description="Documents to list")] = 50,
) -> dict[str, Any]:
    """Everything the document pipeline has recorded, summarised.

    These tables carry no project column, so there is nothing to scope by; the
    endpoint still requires an authenticated user rather than being public.
    """
    expected = await _expected_clause_counts(db)
    documents = await _documents(db, expected, limit)
    totals = await _totals(db)

    return {
        "totals": {
            **totals,
            "average_coverage": _mean([d["coverage"] for d in documents]),
        },
        "documents": documents,
        "by_doc_type": await _by_doc_type(db, expected),
        "clause_frequency": await _clause_frequency(db),
        "missing_clauses": await _missing_clauses(db, expected),
        "expected_by_doc_type": expected,
    }


# ------------------------------------------------------------------ queries
async def _expected_clause_counts(db: AsyncSession) -> dict[str, int]:
    """How many clauses the taxonomy says each document type should have."""
    rows = await db.execute(
        select(cip_doc_mapping.c["docType"], func.count())
        .where(cip_doc_mapping.c["docType"].is_not(None))
        .group_by(cip_doc_mapping.c["docType"])
    )
    return {doc_type: int(count) for doc_type, count in rows.all()}


async def _totals(db: AsyncSession) -> dict[str, Any]:
    documents = await db.scalar(select(func.count()).select_from(cip_doc_master))
    clauses = await db.scalar(select(func.count()).select_from(cip_doc_content_master))
    embedded = await db.scalar(
        select(func.count())
        .select_from(cip_doc_content_master)
        .where(cip_doc_content_master.c.embeddings.is_not(None))
    )
    pages = await db.scalar(
        select(
            func.coalesce(func.sum(func.array_length(cip_doc_content_master.c["pageNumber"], 1)), 0)
        )
    )
    return {
        "documents": int(documents or 0),
        "clauses": int(clauses or 0),
        "embedded": int(embedded or 0),
        # A clause spanning two pages contributes two, which is the number that
        # matters here: it is how many page-regions can be highlighted.
        "clause_page_regions": int(pages or 0),
        "clauses_per_document": round((clauses or 0) / (documents or 1), 1),
    }


async def _documents(
    db: AsyncSession, expected: dict[str, int], limit: int
) -> list[dict[str, Any]]:
    """One row per processed document, with its clause coverage."""
    counts = (
        select(
            cip_doc_content_master.c.docid.label("docid"),
            func.count().label("found"),
        )
        .group_by(cip_doc_content_master.c.docid)
        .subquery()
    )
    rows = await db.execute(
        select(
            cip_doc_master.c.docid,
            cip_doc_master.c.doc_type,
            cip_doc_master.c.doc_path,
            cip_doc_master.c["jsonPath"],
            func.coalesce(counts.c.found, 0).label("found"),
        )
        # docid is bigint on the master and text on the content table, so the
        # join casts rather than relying on Postgres to guess.
        .outerjoin(
            counts,
            counts.c.docid
            == func.cast(cip_doc_master.c.docid, cip_doc_content_master.c.docid.type),
        )
        .order_by(cip_doc_master.c.docid.desc())
        .limit(limit)
    )

    documents: list[dict[str, Any]] = []
    for docid, doc_type, doc_path, json_path, found in rows.all():
        target = expected.get(doc_type or "", 0)
        documents.append(
            {
                "docid": docid,
                "doc_type": doc_type,
                "doc_path": doc_path,
                "json_path": json_path,
                "clauses_found": int(found),
                "clauses_expected": target,
                "coverage": round(int(found) / target, 3) if target else None,
            }
        )
    return documents


async def _by_doc_type(db: AsyncSession, expected: dict[str, int]) -> list[dict[str, Any]]:
    """Document-type mix, with the clause target for each."""
    rows = await db.execute(
        select(cip_doc_master.c.doc_type, func.count())
        .group_by(cip_doc_master.c.doc_type)
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


async def _clause_frequency(db: AsyncSession) -> list[dict[str, Any]]:
    """Which clauses are actually being found, across every document."""
    rows = await db.execute(
        select(cip_doc_content_master.c.clause, func.count())
        .group_by(cip_doc_content_master.c.clause)
        .order_by(func.count().desc(), cip_doc_content_master.c.clause)
    )
    return [{"clause": clause, "documents": int(count)} for clause, count in rows.all()]


async def _missing_clauses(db: AsyncSession, expected: dict[str, int]) -> list[dict[str, Any]]:
    """Clauses the taxonomy expects that no document of that type has.

    The inverse of clause frequency, and the more actionable half: a clause the
    pipeline has never once found is either genuinely rare in these contracts or
    a detection gap, and both are worth knowing.
    """
    found = await db.execute(
        select(
            cip_doc_master.c.doc_type,
            cip_doc_content_master.c.clause,
        )
        .select_from(cip_doc_content_master)
        .join(
            cip_doc_master,
            func.cast(cip_doc_master.c.docid, cip_doc_content_master.c.docid.type)
            == cip_doc_content_master.c.docid,
        )
        .distinct()
    )
    seen: dict[str, set[str]] = {}
    for doc_type, clause in found.all():
        seen.setdefault(doc_type or "", set()).add(clause)

    processed_types = await db.execute(
        select(cip_doc_master.c.doc_type).distinct().where(cip_doc_master.c.doc_type.is_not(None))
    )

    missing: list[dict[str, Any]] = []
    for (doc_type,) in processed_types.all():
        rows = await db.execute(
            select(cip_doc_mapping.c.clause, cip_doc_mapping.c.description)
            .where(cip_doc_mapping.c["docType"] == doc_type)
            .order_by(cip_doc_mapping.c.id)
        )
        for clause, description in rows.all():
            if clause not in seen.get(doc_type, set()):
                missing.append(
                    {
                        "doc_type": doc_type,
                        "clause": clause,
                        "description": (description or "")[:160],
                        "expected_in": expected.get(doc_type, 0),
                    }
                )
    return missing


@router.get("/documents/{docid}", summary="One document's clauses")
async def get_document_clauses(
    docid: str,
    user: CurrentUserDep,
    db: DbSession,
) -> dict[str, Any]:
    """The clauses recorded for one document, with their geometry.

    ``polygon`` is one box per page: eight floats per entry in ``page_number``
    order, so a viewer pairs them rather than assuming a single box.
    """
    rows = await db.execute(
        select(
            cip_doc_content_master.c.id,
            cip_doc_content_master.c.clause,
            cip_doc_content_master.c["pageNumber"],
            cip_doc_content_master.c.polygon,
            cip_doc_content_master.c["jsonfilePath"],
            cip_doc_content_master.c.textcontent,
        )
        .where(cip_doc_content_master.c.docid == docid)
        .order_by(cip_doc_content_master.c.id)
    )

    clauses = [
        {
            "id": row.id,
            "clause": row.clause,
            "page_numbers": list(row[2] or []),
            "polygon": [float(v) for v in (row.polygon or [])],
            "json_file": row[4],
            "text": row.textcontent,
            "chars": len(row.textcontent or ""),
        }
        for row in rows.all()
    ]

    master = await db.execute(
        select(cip_doc_master.c.doc_type, cip_doc_master.c.doc_path, cip_doc_master.c["jsonPath"])
        .where(func.cast(cip_doc_master.c.docid, cip_doc_content_master.c.docid.type) == docid)
        .limit(1)
    )
    row = master.first()

    return {
        "docid": docid,
        "doc_type": row[0] if row else None,
        "doc_path": row[1] if row else None,
        "json_path": row[2] if row else None,
        "clauses": clauses,
    }


def _mean(values: list[float | None]) -> float | None:
    usable = [v for v in values if v is not None]
    return round(sum(usable) / len(usable), 3) if usable else None


__all__ = ["router"]

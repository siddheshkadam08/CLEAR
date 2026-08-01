"""Stage 4: write the document and its clauses to the ``cip_*`` tables.

One document is one transaction. A half-written document - master row present,
half its clauses missing - would be indistinguishable from a document that
genuinely has few clauses, and the next run would have no way to tell the
difference.

Re-running the same source directory replaces the previous rows rather than
adding a second copy, matching the delete-then-insert idempotency the rest of
the codebase uses for stage output.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.docpipeline.clauses import DetectedClause
from app.ai.docpipeline.tables import cip_doc_content_master, cip_doc_master
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PersistedDocument:
    docid: int
    clause_rows: int
    replaced_previous: bool


async def persist_document(
    db: AsyncSession,
    *,
    doc_type: str,
    json_dir: Path,
    pdf_path: str | None,
    clauses: Sequence[DetectedClause],
    vectors: Sequence[Sequence[float]],
) -> PersistedDocument:
    """Write one ``cip_DocMaster`` row and one ``cip_DocContentMaster`` row per clause."""
    if vectors and len(vectors) != len(clauses):
        raise ValueError(
            f"{len(vectors)} vectors for {len(clauses)} clauses. They are paired by "
            "position; a mismatch would file each clause under another's embedding."
        )

    json_path = str(json_dir)
    replaced = await _delete_previous(db, json_path)

    docid = await _insert_master(db, doc_type=doc_type, json_path=json_path, pdf_path=pdf_path)

    for clause in clauses:
        # The two arrays are read together - box N belongs to page N - so a
        # length disagreement silently attributes geometry to the wrong page.
        # Cheaper to refuse the write than to debug a highlight drawn on page 17
        # for a clause on page 16.
        if clause.polygon and len(clause.polygon) != 8 * len(clause.page_numbers):
            raise ValueError(
                f"{clause.clause}: {len(clause.polygon)} polygon floats for "
                f"{len(clause.page_numbers)} pages. Expected 8 per page - the "
                "reader pairs polygon[8n:8n+8] with pageNumber[n]."
            )

    rows = [
        {
            # cip_DocMaster.docid is bigint, cip_DocContentMaster.docid is text.
            # The cast is explicit so the mismatch stays visible to the next
            # person who writes a join across these two tables.
            "docid": str(docid),
            "clause": clause.clause,
            "polygon": clause.polygon or None,
            "pageNumber": clause.page_numbers or None,
            "jsonfilePath": _clause_json_file(clause, json_dir),
            "textcontent": clause.textcontent,
            "embeddings": list(vectors[index]) if vectors else None,
        }
        for index, clause in enumerate(clauses)
    ]
    if rows:
        await db.execute(insert(cip_doc_content_master), rows)

    logger.info(
        "docpipeline_document_persisted",
        docid=docid,
        doc_type=doc_type,
        clause_rows=len(rows),
        replaced_previous=replaced,
        embedded=bool(vectors),
    )
    return PersistedDocument(docid=docid, clause_rows=len(rows), replaced_previous=replaced)


async def _delete_previous(db: AsyncSession, json_path: str) -> bool:
    """Drop any earlier run over the same source directory."""
    existing = await db.execute(
        select(cip_doc_master.c.docid).where(cip_doc_master.c["jsonPath"] == json_path)
    )
    docids = [str(value) for (value,) in existing.all() if value is not None]
    if not docids:
        return False

    await db.execute(
        delete(cip_doc_content_master).where(cip_doc_content_master.c.docid.in_(docids))
    )
    await db.execute(delete(cip_doc_master).where(cip_doc_master.c["jsonPath"] == json_path))
    return True


async def _insert_master(
    db: AsyncSession, *, doc_type: str, json_path: str, pdf_path: str | None
) -> int:
    """Insert the master row and return its docid.

    ``docid`` has no sequence of its own, so it is derived from the identity
    primary key: unique without inventing a second counter that could drift.
    """
    result = await db.execute(
        insert(cip_doc_master)
        .values(doc_path=pdf_path, doc_type=doc_type, **{"jsonPath": json_path})
        .returning(cip_doc_master.c.id)
    )
    row_id = int(result.scalar_one())

    await db.execute(
        cip_doc_master.update().where(cip_doc_master.c.id == row_id).values(docid=row_id)
    )
    return row_id


def _clause_json_file(clause: DetectedClause, json_dir: Path) -> str:
    """The page file the clause starts on - the most precise pointer available."""
    pages = clause.page_numbers
    if not pages:
        return str(json_dir)
    return str(json_dir / f"page_{pages[0]}.json")

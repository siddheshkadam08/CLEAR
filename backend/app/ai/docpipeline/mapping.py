"""Reading the clause taxonomy out of ``cip_docMapping``.

The document types and the clause list for each are read from the database at
runtime rather than hardcoded. That is not ceremony: the taxonomy is data owned
by another system, and a seventh document type - or a twenty-fourth MSA clause -
should take effect without a deploy here. It also means the classifier's label
set and the detector's target list are, by construction, the same vocabulary the
lookup joins on.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.docpipeline.tables import cip_doc_mapping
from app.core.logging import get_logger

logger = get_logger(__name__)

#: The label used when no type is identified. Present in the table as a real
#: document type with its own clause list, so the pipeline still has targets.
FALLBACK_DOC_TYPE = "Others"


@dataclass(frozen=True, slots=True)
class ClauseSpec:
    """One clause to look for, and what distinguishes it."""

    clause: str
    description: str

    def as_prompt_line(self) -> str:
        if self.description:
            return f"- {self.clause}: {self.description}"
        return f"- {self.clause}"


async def load_doc_types(db: AsyncSession) -> list[str]:
    """Every document type in the mapping table, alphabetically."""
    rows = await db.execute(
        select(distinct(cip_doc_mapping.c["docType"]))
        .where(cip_doc_mapping.c["docType"].is_not(None))
        .order_by(cip_doc_mapping.c["docType"])
    )
    types = [value for (value,) in rows.all() if value]
    if not types:
        raise LookupError(
            "cip_docMapping holds no document types. The clause taxonomy must be "
            "populated before this pipeline can classify anything."
        )
    return types


async def load_clauses(db: AsyncSession, doc_type: str) -> list[ClauseSpec]:
    """The clauses expected in ``doc_type``, in the table's own order.

    Order is by ``id``, which is the order a human curated them in - roughly
    most to least important - so a truncated report still leads with what
    matters.
    """
    rows = await db.execute(
        select(cip_doc_mapping.c.clause, cip_doc_mapping.c.description)
        .where(cip_doc_mapping.c["docType"] == doc_type)
        .order_by(cip_doc_mapping.c.id)
    )
    clauses = [
        ClauseSpec(clause=clause.strip(), description=(description or "").strip())
        for clause, description in rows.all()
        if clause and clause.strip()
    ]
    logger.info("docpipeline_clauses_loaded", doc_type=doc_type, clauses=len(clauses))
    return clauses

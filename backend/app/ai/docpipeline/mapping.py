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

from app.ai.docpipeline import taxonomy
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


@dataclass(frozen=True, slots=True)
class ResolvedDocumentType:
    """A document type the taxonomy recognises, in both vocabularies.

    ``label`` is what ``cip_docMapping`` calls it and what a user sees;
    ``agreement_type`` is the ``AgreementType`` value that the contract row and
    every vector's ``filter_metadata`` are keyed on, and therefore the only one of
    the two that can actually be used as a retrieval filter.

    There is no numeric document-type id to carry: ``cip_docMapping.id`` is the
    primary key of a *clause* row, not of a type - the same type appears across as
    many rows as it has clauses. The distinct label is the type's identity.
    """

    label: str
    agreement_type: str
    subtype: str | None = None


async def resolve_document_type(db: AsyncSession, label: str) -> ResolvedDocumentType | None:
    """Match a document-type label against ``cip_docMapping``.

    Returns ``None`` for anything the table does not contain. That is the whole
    point of the function: an unmatched label is a signal to search *without* a
    type filter, and coercing it to the nearest-looking type - or to ``Others`` -
    would silently exclude the documents the question was about.

    Matching is via :func:`taxonomy.normalise`, so case, punctuation and the
    trailing whitespace some rows carry do not decide the outcome.
    """
    wanted = taxonomy.normalise(label or "")
    if not wanted:
        return None

    try:
        available = await load_doc_types(db)
    except LookupError:
        # An empty taxonomy is a deployment problem, not a query problem. The
        # question still gets answered, just without a type filter.
        logger.warning(
            "document_type_taxonomy_empty",
            detail="cip_docMapping has no rows; the type filter is skipped.",
        )
        return None

    if wanted == taxonomy.normalise(FALLBACK_DOC_TYPE):
        # "Others" is the taxonomy's own unknown bucket. Treating it as a resolved
        # type would turn "I could not tell" into a filter that excludes every
        # document that *was* classified.
        logger.info(
            "document_type_fallback_ignored",
            label=label,
            reason="the fallback type is not a filter; retrieval runs unfiltered",
        )
        return None

    matched = next((name for name in available if taxonomy.normalise(name) == wanted), None)
    if matched is None:
        logger.info(
            "document_type_unresolved",
            label=label,
            normalised=wanted,
            reason="no such document type in cip_docMapping; retrieval runs unfiltered",
        )
        return None

    agreement_type, subtype = taxonomy.agreement_type_for(matched)
    return ResolvedDocumentType(label=matched, agreement_type=agreement_type, subtype=subtype)


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

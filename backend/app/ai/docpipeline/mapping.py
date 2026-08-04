"""The clause taxonomy: which clauses matter for which document type.

This used to read ``cip_docMapping``, a table owned by another team and created
outside this repository. That table lived only on the shared Hackathon Postgres,
which is decommissioned, so it now exists nowhere - and because it gates
:class:`~app.orchestrator.stages.docpipeline.DocPipelineStage`, and extraction
depends on that stage, *no document could be processed at all*.

It is not needed. ``cip_docMapping`` held document types mapped to clause names,
which is precisely what ``document_profiles`` (``agreement_type`` ->
``mandatory_clauses`` + ``optional_clauses``) and the Clause Master already store,
against data this application owns and seeds. Two facts make the swap lossless
rather than approximate:

* **The clause vocabularies are the same strings.** ``cip_docMapping.clause``
  carried names like "IP ownership", "Renewal", "Term/duration" - which are
  ``clause_master_categories.name`` verbatim. ``taxonomy.EXPLICIT`` existed only
  to bridge the punctuation, and :func:`taxonomy.normalise` still collapses it.
* **Nothing joins on the clause list.** It is prompt material: the detector puts
  it in a system prompt and in a JSON-schema enum, and matches the model's echo
  back against it. No row references it.

Two things improve as a result, neither of them incidental:

* Seven Clause Master categories that ``cip_docMapping`` had no equivalent for -
  ``scope_of_work``, ``data_protection``, ``compliance``, ``service_level``,
  ``publicity``, ``definitions``, ``entire_agreement`` - become detectable.
* The label set grows from six types to every configured profile. Under the old
  six labels only *five* of the thirteen seeded profiles were reachable, so
  ``vendor_agreement``, ``employment_agreement``, ``lease``,
  ``consulting_agreement``, ``government_contract``, ``healthcare_agreement``,
  ``insurance_policy`` and ``research_collaboration`` - each with its own
  mandatory clauses, risk weights and thresholds - could never be selected.

The taxonomy is still read from the database at runtime rather than hardcoded,
for the same reason as before: adding a profile or moving a clause between types
is configuration, and should take effect without a deploy.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai.docpipeline import taxonomy
from app.core.enums import AgreementType
from app.core.logging import get_logger
from app.models.clause_master import AgreementTypeClause, ClauseMasterCategory
from app.models.profile import DocumentProfile

logger = get_logger(__name__)

#: The label used when no type is identified.
#:
#: ``AgreementType.OTHER``'s own value, so it needs no translation - the label a
#: document is classified as *is* the value written to ``contracts.agreement_type``.
#: The old taxonomy spelled this "Others" and mapped it; the mapping is retained
#: in :mod:`app.ai.docpipeline.taxonomy` for historical rows.
FALLBACK_DOC_TYPE = AgreementType.OTHER.value


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
    """Every document type the platform is configured to recognise.

    The distinct ``agreement_type`` of every active profile, plus the fallback.
    These are ``AgreementType`` values, which matters twice over: the classifier's
    answer becomes ``contracts.agreement_type`` directly, and that column is what
    the Copilot document-type filter and the Contracts facets key on.

    The fallback is appended rather than assumed present. No profile carries
    ``agreement_type = 'other'``, and without an explicit entry the classifier would
    have no way to say "I could not tell" - it would be forced to pick the
    nearest-looking type, which is worse than an honest miss.
    """
    rows = await db.execute(
        select(DocumentProfile.agreement_type)
        .where(
            DocumentProfile.is_active.is_(True),
            DocumentProfile.deleted_at.is_(None),
            DocumentProfile.agreement_type.is_not(None),
        )
        .distinct()
        .order_by(DocumentProfile.agreement_type)
    )
    types = [value for (value,) in rows.all() if value]

    if not types:
        raise LookupError(
            "No active document profiles exist, so there is nothing to classify "
            "into. Seed the document profiles before processing a document."
        )

    if FALLBACK_DOC_TYPE not in types:
        types.append(FALLBACK_DOC_TYPE)
    return types


@dataclass(frozen=True, slots=True)
class ResolvedDocumentType:
    """A document type the taxonomy recognises.

    ``label`` is what the classifier answers with and what a user sees;
    ``agreement_type`` is the value ``contracts.agreement_type`` and every vector's
    ``filter_metadata`` are keyed on, and therefore the only one of the two that
    can actually be used as a retrieval filter.

    The two are now the same string for every profile-derived type. They are kept
    separate because the six legacy ``cip_docMapping`` labels ("Contract cum Order
    Form") still resolve through :func:`taxonomy.agreement_type_for`, and a
    question asked in those words should still find its documents.
    """

    label: str
    agreement_type: str
    subtype: str | None = None


async def resolve_document_type(db: AsyncSession, label: str) -> ResolvedDocumentType | None:
    """Match a document-type label against the configured taxonomy.

    Returns ``None`` for anything not recognised. That is the whole point of the
    function: an unmatched label is a signal to search *without* a type filter, and
    coercing it to the nearest-looking type - or to the fallback - would silently
    exclude the documents the question was about.

    Matching is via :func:`taxonomy.normalise`, so case and punctuation do not
    decide the outcome, and a legacy label is tried before giving up.
    """
    wanted = taxonomy.normalise(label or "")
    if not wanted:
        return None

    if _is_fallback(label):
        # The taxonomy's own unknown bucket, in either the current spelling or the
        # retired "Others". Treating it as a resolved type would turn "I could not
        # tell" into a filter that excludes every document that *was* classified.
        #
        # Checked before the profile lookup so it costs no query - which is also
        # what lets the resolver tests exercise it without a database.
        logger.info(
            "document_type_fallback_ignored",
            label=label,
            reason="the fallback type is not a filter; retrieval runs unfiltered",
        )
        return None

    try:
        available = await load_doc_types(db)
    except LookupError:
        # No profiles configured is a deployment problem, not a query problem. The
        # question still gets answered, just without a type filter.
        logger.warning(
            "document_type_taxonomy_empty",
            detail="no active document profiles; the type filter is skipped.",
        )
        return None

    matched = next((name for name in available if taxonomy.normalise(name) == wanted), None)
    if matched is not None:
        agreement_type, subtype = taxonomy.agreement_type_for(matched)
        return ResolvedDocumentType(label=matched, agreement_type=agreement_type, subtype=subtype)

    # Not a configured type. It may still be one of the six labels the retired
    # `cip_docMapping` used - "Contract cum Order Form" reads far more naturally in
    # a question than `purchase_order`, and documents filed under it are still here.
    legacy = taxonomy.legacy_agreement_type(label)
    if legacy is not None:
        agreement_type, subtype = legacy
        if agreement_type in available:
            logger.info("document_type_resolved_via_legacy_label", label=label, mapped=agreement_type)
            return ResolvedDocumentType(
                label=agreement_type, agreement_type=agreement_type, subtype=subtype
            )

    logger.info(
        "document_type_unresolved",
        label=label,
        normalised=wanted,
        reason="no such document type is configured; retrieval runs unfiltered",
    )
    return None


async def load_clauses(db: AsyncSession, doc_type: str) -> list[ClauseSpec]:
    """The clauses expected in ``doc_type``, most important first.

    Sourced from the profile for that ``agreement_type``: its ``mandatory_clauses``
    and ``optional_clauses`` are Clause Master keys, and the Clause Master supplies
    the name the detector prompts with.

    Ordered by ``priority``, which is the order the taxonomy was curated in -
    roughly most to least important - so a truncated report still leads with what
    matters.

    The fallback type, and any type whose profile names no clauses, gets the whole
    active Clause Master. A document we could not classify is exactly the one worth
    looking hardest at; returning nothing would report "0 clauses found" for a
    document nobody examined.
    """
    wanted = await _clause_keys_for(db, doc_type)

    statement = (
        select(ClauseMasterCategory)
        # Eager, not lazy: `_describe` reads `category.rules`, and a lazy load
        # there would be IO from a sync context - `MissingGreenlet`, not a slow
        # query. Same option `AIExtractionStage` uses for the same reason.
        .options(selectinload(ClauseMasterCategory.rules))
        .where(
            ClauseMasterCategory.is_active.is_(True),
            ClauseMasterCategory.deleted_at.is_(None),
        )
        .order_by(ClauseMasterCategory.priority, ClauseMasterCategory.key)
    )
    # `is not None` rather than truthiness: an empty set means "every clause for
    # this type is switched off", and `.in_(set())` is the correct - and intended
    # - way to say that. Testing truthiness would silently widen it to everything.
    if wanted is not None:
        statement = statement.where(ClauseMasterCategory.key.in_(wanted))

    categories = (await db.execute(statement)).scalars().all()

    clauses = [
        ClauseSpec(clause=str(category.name).strip(), description=_describe(category))
        for category in categories
        if str(category.name or "").strip()
    ]
    logger.info(
        "docpipeline_clauses_loaded",
        doc_type=doc_type,
        clauses=len(clauses),
        scoped_to_type=wanted is not None,
    )
    return clauses


def _is_fallback(label: str) -> bool:
    """Is this the "I could not tell" bucket, in any spelling it has had?

    Matched by meaning, not by string: the retired vocabulary spelled it
    ``"Others"`` and mapped it to ``AgreementType.OTHER``, and documents filed
    under that label are still in the database.
    """
    candidate = (label or "").strip()
    if not candidate:
        return False
    if taxonomy.normalise(candidate) == taxonomy.normalise(FALLBACK_DOC_TYPE):
        return True
    legacy = taxonomy.legacy_agreement_type(candidate)
    return legacy is not None and legacy[0] == AgreementType.OTHER.value


async def _clause_keys_for(db: AsyncSession, doc_type: str) -> set[str] | None:
    """The Clause Master keys this document type expects.

    Read from ``agreement_type_clauses`` - the same table the extraction stage
    reads, so the clauses the detector looks for and the clauses the engine
    extracts cannot drift apart. Only ``is_active`` rows count: a clause switched
    off for this type is configured and deliberately not applied.

    ``None`` means "nothing configured, use the whole Clause Master". An *empty
    set* means "configured, and every clause switched off" - a state the Clause
    Master screen can produce, and one that must be honoured rather than quietly
    turned back into a full extraction.
    """
    if _is_fallback(doc_type):
        return None

    rows = await db.execute(
        select(AgreementTypeClause.clause_key).where(
            AgreementTypeClause.agreement_type == doc_type,
            AgreementTypeClause.is_active.is_(True),
        )
    )
    keys = {str(key) for (key,) in rows.all()}
    if keys:
        return keys

    configured = await db.scalar(
        select(func.count())
        .select_from(AgreementTypeClause)
        .where(AgreementTypeClause.agreement_type == doc_type)
    )
    if configured:
        logger.info("docpipeline_all_clauses_inactive", doc_type=doc_type)
        return set()

    logger.warning(
        "docpipeline_no_clause_mapping",
        doc_type=doc_type,
        detail="no clauses configured for this type; falling back to the whole Clause Master",
    )
    return None


def _describe(category: ClauseMasterCategory) -> str:
    """A one-line description for the detector's prompt.

    Prefers the active rule's synonyms over ``description``. The seeded
    descriptions are boilerplate - "Confidentiality / NDA clause - extracted,
    validated and indexed for retrieval." - which tells a model nothing it cannot
    read off the name. The synonyms are hand-curated alternative headings, which
    is exactly what a heading-matching prompt needs.
    """
    for rule in getattr(category, "rules", None) or []:
        if not getattr(rule, "is_active", False):
            continue
        synonyms = [str(value).strip() for value in (rule.synonyms or []) if str(value).strip()]
        if synonyms:
            return "also called " + ", ".join(synonyms)
        break

    description = str(getattr(category, "description", "") or "").strip()
    return description

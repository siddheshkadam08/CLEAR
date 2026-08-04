"""The document pipeline, as a pipeline stage.

Runs directly after the parser. It classifies the document, resolves the profile
that governs it, and locates the clauses that profile expects.

The input is the parser's per-page JSON, already fetched and cached under
``parser-cache/{parser}/{sha256}``. Reading it back from there rather than
re-deriving it from the canonical document is deliberate: the canonical model has
already thrown away the page-local paragraph geometry this pipeline pins clauses
to, and the cache is keyed on the file hash so nothing is re-parsed.

Why this stage exists, concretely. A 13-page "IRIS CARBON® License and Services
Agreement" was classified `nda` by the rules classifier - it contains the phrase
"confidential information", which was enough - and the `nda` profile declares
four mandatory clauses. Four categories ran against 61 chunks, none errored, none
found anything, and the job failed with "Extraction produced no clauses". An LLM
reading the first five pages calls it a licence agreement, which selects the
right profile and therefore the right clause list.

It writes no rows of its own. Its outputs are the classification - promoted onto
``contracts.agreement_type`` and ``processing_jobs.profile_id`` by
``context_updates`` - and the DOC_PIPELINE artifact. It previously also wrote
``cip_DocMaster`` / ``cip_DocContentMaster``, two tables owned by another team
whose every column duplicated one the platform already keeps; see the note beside
the detection call below.
"""

from __future__ import annotations

from typing import Any

from app.ai.docpipeline.classification import DocumentTypeClassifier
from app.ai.docpipeline.clauses import ClauseDetector
from app.ai.docpipeline.mapping import load_clauses, load_doc_types
from app.ai.docpipeline.source import pages_from_payloads
from app.ai.docpipeline.taxonomy import agreement_type_for
from app.ai.parsers.fixtures import ObjectCache
from app.core.config import get_settings
from app.core.enums import ArtifactKind, PipelineStage
from app.core.errors import PipelineError
from app.core.logging import get_logger
from app.core.versions import ComponentVersions, current_versions_for_stage
from app.orchestrator.stages.base import (
    StageArtifact,
    StageContext,
    StageHandler,
    StageResult,
    register_stage,
)

logger = get_logger(__name__)


class DocPipelineStage(StageHandler):
    """Classify the document, locate its clauses, embed and record them."""

    stage = PipelineStage.DOCPIPELINE
    requires = (ArtifactKind.NORMALIZED_DOCUMENT,)
    cacheable = True
    retryable = True

    def versions_for(self, ctx: StageContext) -> ComponentVersions:
        return current_versions_for_stage(self.stage)

    async def cleanup(self, ctx: StageContext) -> None:
        """Nothing to do: this stage writes no rows of its own.

        Its output is the DOC_PIPELINE artifact plus the classification promoted
        onto the contract by `context_updates`. The artifact is superseded by
        `invalidate_from_stage` and the contract columns are overwritten on the
        next run, so a retry replaces rather than accumulates without this stage
        deleting anything.
        """

    async def run(self, ctx: StageContext) -> StageResult:
        settings = get_settings()

        payloads = await ObjectCache(settings.parser.active_parser).load(ctx.contract.sha256_hash)
        if not payloads:
            # The parser stage is a hard dependency, so its cache should be warm.
            # An empty cache means the parse came from a fixture replay or a
            # pre-cache run, and re-deriving the page JSON here would mean
            # calling the vendor again from a stage that is not supposed to.
            raise PipelineError(
                "No cached page JSON for this document. The document pipeline reads "
                "the parser's cache, which is keyed on the file hash; re-run the "
                "parser stage to populate it.",
                stage=self.stage.value,
                retryable=False,
                details={"file_hash": ctx.contract.sha256_hash},
            )

        pages = pages_from_payloads(payloads)
        await ctx.report_progress(40, f"{len(pages)} pages")

        doc_types = await load_doc_types(ctx.db)
        classification = await DocumentTypeClassifier().classify(pages, doc_types)
        agreement_type, agreement_subtype = agreement_type_for(classification.doc_type)
        if classification.fell_back:
            # The type is a guess, and every clause this stage looks for comes
            # from it. Flag it here rather than letting "0 clauses found" be the
            # only symptom of a classification miss.
            ctx.contract.needs_review = True
        profile_updates = await _resolve_profile(ctx, agreement_type)
        await ctx.report_progress(55, f"classified as {classification.doc_type}")

        clause_specs = await load_clauses(ctx.db, classification.doc_type)
        if not clause_specs:
            raise PipelineError(
                f"No clauses are defined for document type '{classification.doc_type}'. "
                "Its document profile names no clause keys that exist in the Clause "
                "Master, and the Clause Master itself is empty.",
                stage=self.stage.value,
                retryable=False,
                details={"doc_type": classification.doc_type},
            )

        detection = await ClauseDetector().detect(pages, clause_specs)
        await ctx.report_progress(
            80, f"{len(detection.detected)}/{detection.total_targets} clauses"
        )

        # Unlike the extraction stage this replaced, finding nothing is a
        # result, not a failure. A short addendum legitimately contains few of
        # the clauses its type expects, and failing the job would leave the
        # document with no record at all - which is strictly less useful than a
        # record saying "these were looked for and not found".
        #
        # The detections used to be written to `cip_DocMaster` /
        # `cip_DocContentMaster`, and embedded on the way. Both are gone:
        #
        # * those tables were owned by another team, existed only on a database
        #   that has been decommissioned, and every column duplicated one the
        #   platform already keeps - `clauses.text_content`,
        #   `clauses.bounding_boxes`, `contracts.agreement_type`;
        # * the embedding filled `cip_DocContentMaster.embeddings`, a second
        #   vector store *no query ever searched*. Retrieval reads the
        #   `embeddings` table, which the embedding stage fills. It was an LLM
        #   call per document for a column nobody read.
        #
        # What the detections are actually for survives untouched: the
        # DOC_PIPELINE artifact below, which is what the checkpoint replays from
        # and what the Doc Pipeline screen reports on.
        await ctx.report_progress(95, f"detected {len(detection.detected)} clauses")

        logger.info(
            "docpipeline_stage_completed",
            contract_id=str(ctx.contract_id),
            doc_type=classification.doc_type,
            pages=len(pages),
            targets=detection.total_targets,
            detected=len(detection.detected),
            not_found=len(detection.not_found),
        )

        return StageResult(
            artifacts=[
                StageArtifact(
                    kind=ArtifactKind.DOC_PIPELINE,
                    payload={
                        # The contract is the identity. `docid` was a bigint
                        # minted by `cip_DocMaster`, which no longer exists.
                        "contract_id": str(ctx.contract_id),
                        "doc_type": classification.doc_type,
                        "confidence": classification.confidence,
                        "reason": classification.reason,
                        "clauses": [
                            {
                                "clause": item.clause,
                                "method": item.method,
                                "pages": item.page_numbers,
                                "chars": len(item.textcontent),
                            }
                            for item in detection.detected
                        ],
                        "not_found": detection.not_found,
                    },
                    summary={
                        "doc_type": classification.doc_type,
                        "detected": len(detection.detected),
                        "targets": detection.total_targets,
                    },
                )
            ],
            stats={
                "doc_type": classification.doc_type,
                "classification_confidence": classification.confidence,
                "pages": len(pages),
                "clause_targets": detection.total_targets,
                "clauses_detected": len(detection.detected),
                "clauses_not_found": len(detection.not_found),
                "heading_matches": detection.heading_exact + detection.heading_llm,
                "chunk_calls": detection.llm_chunk_calls,
                "pages_never_read": len(detection.pages_never_read),
            },
            # Promoted onto the contract row by `_apply_context_updates`, which
            # already knows these keys. This is now the *only* record of the
            # classification - without it the Contracts list renders "-" in the
            # Type column for every document this stage has just classified, and
            # the Copilot document-type filter has nothing to match on.
            context_updates=profile_updates
            | {
                "agreement_type": agreement_type,
                "agreement_subtype": agreement_subtype,
                "classification_confidence": classification.confidence,
            },
            warnings=(
                [
                    f"{len(detection.not_found)} of {detection.total_targets} expected "
                    f"clauses were not found: {', '.join(detection.not_found[:5])}"
                    + ("..." if len(detection.not_found) > 5 else "")
                ]
                if detection.not_found
                else []
            )
            + (
                [
                    f"The document type could not be determined, so it was filed as "
                    f"'{classification.doc_type}'. Clause detection used that type's "
                    f"clause list, which may not be the right one."
                ]
                if classification.fell_back
                else []
            ),
        )


async def _resolve_profile(ctx: StageContext, agreement_type: Any) -> dict[str, Any]:
    """Pick the Document Intelligence Profile for this document type.

    This is the one piece of plumbing that went missing when CLASSIFICATION was
    retired. `"profile_id"` appeared in exactly one `context_updates` in the whole
    codebase - that stage's - and everything downstream of it survived intact:
    `runner._apply_context_updates` still promotes the key, `_load_profile` still
    reads `job.profile_id`, and `ctx.profile_setting` still returns defaults when
    there is none. So `ctx.profile` was simply `None` everywhere, and eleven
    consumers quietly fell back to defaults: mandatory-clause detection ran off
    the Clause Master's global flags instead of the document type's list, risk
    weighting used severity defaults instead of `risk_mapping`, and every
    extracted row recorded a null `profile_version`.

    Resolved by `agreement_type` rather than by profile key. `agreement_type_for`
    already maps the vendor's `docType` onto an `AgreementType`, and
    `DocumentProfile.agreement_type` stores exactly those values behind the
    `ix_document_profiles_type_active` index - so this is the join the schema was
    built for, and no new mapping table has to be kept in step with the taxonomy.

    Falls back to the default profile rather than to nothing: a document type with
    no profile of its own is better served by the platform default than by the
    silent no-profile behaviour this replaces. Returns an empty dict only when
    there is no default either, which leaves the previous behaviour exactly as it
    was.
    """
    from sqlalchemy import select

    from app.models.profile import DocumentProfile

    wanted = getattr(agreement_type, "value", agreement_type)

    statement = (
        select(DocumentProfile)
        .where(
            DocumentProfile.is_active.is_(True),
            DocumentProfile.deleted_at.is_(None),
            DocumentProfile.agreement_type == wanted,
        )
        # Project-scoped profiles win over platform-wide ones, then higher
        # priority - the same precedence DocumentClassifier._load_profiles used.
        .order_by(
            DocumentProfile.project_id.is_(None),
            DocumentProfile.priority.desc(),
        )
        .limit(1)
    )
    profile = (await ctx.db.execute(statement)).scalars().first()

    if profile is None:
        profile = (
            await ctx.db.execute(
                select(DocumentProfile)
                .where(
                    DocumentProfile.is_active.is_(True),
                    DocumentProfile.deleted_at.is_(None),
                    DocumentProfile.is_default.is_(True),
                )
                .limit(1)
            )
        ).scalars().first()

    if profile is None:
        logger.warning("docpipeline_no_profile", agreement_type=str(wanted))
        return {}

    logger.info(
        "docpipeline_profile_selected",
        agreement_type=str(wanted),
        profile_key=profile.key,
        profile_version=profile.version,
        matched=profile.agreement_type == wanted,
    )
    return {
        "profile_id": str(profile.id),
        "profile_version": profile.version,
        "profile_key": profile.key,
    }


register_stage(DocPipelineStage())

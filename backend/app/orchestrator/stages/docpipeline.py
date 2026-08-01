"""The document pipeline, as a pipeline stage.

Runs directly after the parser and replaces everything the old pipeline did
after it: enrichment, classification, chunking, extraction, embedding and
indexing. Those six produced `clauses`, `chunks` and `embeddings`; this one
produces rows in `cip_DocMaster` and `cip_DocContentMaster`.

The input is the vendor's per-page JSON, which the parser has already fetched
and cached under ``parser-cache/{parser}/{sha256}``. Reading it back from there
rather than re-deriving it from the canonical document is deliberate: the
canonical model has already thrown away the page-local paragraph geometry this
pipeline pins clauses to, and the cache is keyed on the file hash so nothing is
re-fetched from the vendor.

Why this replaced the old path, concretely. A 13-page "IRIS CARBON® License and
Services Agreement" was classified `nda` by the rules classifier - it contains
the phrase "confidential information", which was enough - and the `nda` profile
declares four mandatory clauses. Four categories ran against 61 chunks, none
errored, none found anything, and the job failed with "Extraction produced no
clauses". There is no `license_agreement` profile for it to have chosen
instead. This pipeline reads its document types from `cip_docMapping`, where
`License Agreement` is one of six, and looks for the twenty clauses that
taxonomy lists for it.
"""

from __future__ import annotations

from typing import Any

from app.ai.docpipeline.classification import DocumentTypeClassifier
from app.ai.docpipeline.clauses import ClauseDetector
from app.ai.docpipeline.mapping import load_clauses, load_doc_types
from app.ai.docpipeline.persistence import persist_document
from app.ai.docpipeline.source import pages_from_payloads
from app.ai.docpipeline.taxonomy import agreement_type_for
from app.ai.docpipeline.vectors import embed_texts
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
        """Nothing to do: `persist_document` replaces a prior run in place.

        It deletes by `jsonPath` before inserting, so a retry supersedes rather
        than duplicates, and doing it there keeps the delete in the same
        transaction as the insert.
        """

    async def run(self, ctx: StageContext) -> StageResult:
        settings = get_settings()

        payloads = await ObjectCache(settings.parser.active_parser).load(
            ctx.contract.sha256_hash
        )
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
        await ctx.report_progress(
            55, f"classified as {classification.doc_type}"
        )

        clause_specs = await load_clauses(ctx.db, classification.doc_type)
        if not clause_specs:
            raise PipelineError(
                f"No clauses are defined for document type '{classification.doc_type}'. "
                "Add them to cip_docMapping.",
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
        vectors: list[list[float]] = []
        if detection.detected:
            outcome = await embed_texts([item.textcontent for item in detection.detected])
            vectors = outcome.vectors

        persisted = await persist_document(
            ctx.db,
            doc_type=classification.doc_type,
            json_dir=_source_label(ctx),
            pdf_path=ctx.contract.storage_path,
            clauses=detection.detected,
            vectors=vectors,
        )
        await ctx.report_progress(95, f"stored {persisted.clause_rows} clauses")

        logger.info(
            "docpipeline_stage_completed",
            contract_id=str(ctx.contract_id),
            doc_type=classification.doc_type,
            pages=len(pages),
            targets=detection.total_targets,
            detected=len(detection.detected),
            not_found=len(detection.not_found),
            docid=persisted.docid,
        )

        return StageResult(
            artifacts=[
                StageArtifact(
                    kind=ArtifactKind.DOC_PIPELINE,
                    payload={
                        "docid": persisted.docid,
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
                "docid": persisted.docid,
            },
            # Promoted onto the contract row by `_apply_context_updates`, which
            # already knows these three keys. Without it the classification lives
            # only in `cip_DocMaster` and the Contracts list renders "-" in the
            # Type column for every document this stage has just classified.
            context_updates={
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


def _source_label(ctx: StageContext) -> Any:
    """What goes in `jsonPath`, and the key a re-run replaces itself on.

    The page JSON never lands on a disk here, so there is no directory to point
    at. The parser cache key is the honest answer: it identifies exactly the
    bytes this run read, and it is stable across re-uploads of the same file.
    """
    from pathlib import PurePosixPath

    from app.storage import StorageKey

    return PurePosixPath(
        StorageKey.parser_cache(get_settings().parser.active_parser, ctx.contract.sha256_hash)
    )


register_stage(DocPipelineStage())

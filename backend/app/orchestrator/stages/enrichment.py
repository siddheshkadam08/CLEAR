"""Stage 3 - Enrichment.

Turns the parser's normalized output into the immutable Canonical Document Model.
All the structural work - global reading order, cross-page clause reassembly,
section hierarchy repair, cross-reference detection, honest quality metrics - lives
in :mod:`app.ai.cdm.builder`; this stage is the pipeline wiring around it.

From here on, **no downstream stage reads parser output**. Chunking, extraction,
embedding and retrieval consume only the CDM (§8 frozen rules).
"""

from __future__ import annotations

from app.ai.cdm.builder import build_canonical_document
from app.ai.cdm.models import NormalizedDocument
from app.core.enums import ArtifactKind, PipelineStage
from app.core.errors import PipelineError
from app.core.logging import get_logger
from app.orchestrator.stages.base import (
    StageArtifact,
    StageContext,
    StageHandler,
    StageResult,
    register_stage,
)
from app.repositories.processing import DocumentArtifactRepository

logger = get_logger(__name__)


class EnrichmentStage(StageHandler):
    stage = PipelineStage.ENRICHMENT
    requires = (ArtifactKind.NORMALIZED_DOCUMENT,)
    cacheable = True
    retryable = True

    async def run(self, ctx: StageContext) -> StageResult:
        await ctx.report_progress(36, "building canonical document")

        normalized = await self._load_normalized(ctx)
        result = build_canonical_document(normalized)
        document = result.document

        # The CDM is the contract every later stage relies on. If it carries no body
        # text there is nothing to chunk, extract or embed, so stop here with a clear
        # reason rather than producing empty artifacts through four more stages.
        if not document.paragraphs and not document.tables:
            return StageResult(
                artifacts=[
                    StageArtifact(
                        kind=ArtifactKind.CANONICAL_DOCUMENT,
                        payload=document.model_dump(mode="json"),
                        summary={"paragraphs": 0, "tables": 0, "usable": False},
                    )
                ],
                stats={"cdm_empty": 1},
                halt=True,
                halt_reason=(
                    "No usable content could be recovered from this document, so it "
                    "cannot be analysed."
                ),
            )

        await ctx.report_progress(43, "canonical document built")

        statistics = document.statistics
        quality = document.quality_metrics

        logger.info(
            "enrichment_completed",
            contract_id=str(ctx.contract_id),
            pages=statistics.total_pages,
            sections=statistics.section_count,
            paragraphs=statistics.paragraph_count,
            tables=statistics.table_count,
            reading_order=len(document.reading_order),
            merged_paragraphs=result.merged_paragraphs,
            references=result.references_found,
            coordinate_coverage=quality.coordinate_coverage,
        )

        return StageResult(
            artifacts=[
                StageArtifact(
                    kind=ArtifactKind.CANONICAL_DOCUMENT,
                    payload=document.model_dump(mode="json"),
                    summary={
                        "pages": statistics.total_pages,
                        "sections": statistics.section_count,
                        "paragraphs": statistics.paragraph_count,
                        "tables": statistics.table_count,
                        "lists": statistics.list_count,
                        "words": statistics.word_count,
                        "references": result.references_found,
                        "coordinate_coverage": quality.coordinate_coverage,
                        "degraded": quality.is_degraded,
                    },
                ),
                StageArtifact(
                    kind=ArtifactKind.STATISTICS,
                    payload={
                        "statistics": statistics.model_dump(mode="json"),
                        "quality": quality.model_dump(mode="json"),
                        "enrichment": {
                            "merged_paragraphs": result.merged_paragraphs,
                            "inferred_section_parents": result.inferred_parents,
                            "cleared_section_parents": result.cleared_parents,
                            "cross_references": result.references_found,
                        },
                    },
                    summary={"word_count": statistics.word_count},
                ),
            ],
            stats={
                "cdm_pages": statistics.total_pages,
                "cdm_sections": statistics.section_count,
                "cdm_paragraphs": statistics.paragraph_count,
                "cdm_words": statistics.word_count,
                "cdm_merged_paragraphs": result.merged_paragraphs,
                "cdm_references": result.references_found,
                "cdm_coordinate_coverage": quality.coordinate_coverage,
            },
            context_updates={
                "page_count": statistics.total_pages,
                "language": document.metadata.language,
            },
            warnings=result.warnings,
        )

    async def _load_normalized(self, ctx: StageContext) -> NormalizedDocument:
        """Read the parser artifact.

        Reading it back from storage rather than passing it through the queue is
        deliberate: queue messages stay small and safe to log, and a re-delivered
        message always reads the current artifact instead of a stale copy.
        """
        artifacts = DocumentArtifactRepository(ctx.db)
        pointer = await artifacts.current(ctx.contract_id, ArtifactKind.NORMALIZED_DOCUMENT)

        if pointer is None:
            raise PipelineError(
                "The parser artifact is missing. Re-run the parser stage.",
                stage=self.stage.value,
                details={"contract_id": str(ctx.contract_id)},
            )

        payload = await ctx.storage.get_json(pointer.storage_path)
        try:
            return NormalizedDocument.model_validate(payload)
        except Exception as exc:
            raise PipelineError(
                f"The parser artifact is not a valid normalized document: {exc}",
                stage=self.stage.value,
                details={"artifact": pointer.storage_path},
            ) from exc


register_stage(EnrichmentStage())

__all__ = ["EnrichmentStage"]

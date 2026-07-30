"""Stage 5 - Semantic chunking.

Turns the canonical document into the retrieval substrate. The strategy is not a
code decision: it comes from the Document Intelligence Profile the classifier
selected, so a lease is chunked table-first and an NDA clause-first without a
branch anywhere in this file (§11, §12).

Two invariants this stage is responsible for:

* **Idempotency.** ``cleanup`` deletes the contract's existing chunks before a
  re-run, so a retry replaces the chunk set rather than accumulating a second copy
  alongside it (§10.1). Embeddings are deleted too - they are keyed to chunk ids
  that are about to stop existing, and an embedding pointing at a deleted chunk is
  a retrieval result with no evidence behind it.
* **Version gating.** ``versions_for`` includes the chunk strategy *and* its
  version, so changing a profile's strategy re-chunks, while an unrelated profile
  edit does not (§25).
"""

from __future__ import annotations

from typing import Any

from app.ai.cdm.models import CanonicalDocument
from app.ai.chunking import ChunkConfig, ChunkingEngine
from app.core import metrics
from app.core.enums import ArtifactKind, ChunkStrategy, EmbeddingLevel, PipelineStage
from app.core.errors import PipelineError
from app.core.logging import get_logger
from app.core.versions import (
    CDM_VERSION,
    CHUNK_ENGINE_VERSION,
    CHUNK_STRATEGY_VERSIONS,
    ComponentVersions,
)
from app.orchestrator.stages.base import (
    StageArtifact,
    StageContext,
    StageHandler,
    StageResult,
    register_stage,
)
from app.repositories.chunk import ChunkRepository
from app.repositories.embedding import EmbeddingRepository
from app.repositories.processing import DocumentArtifactRepository

logger = get_logger(__name__)

#: Below this, chunking produced too little to analyse - almost always a parse that
#: yielded no usable text rather than a genuinely tiny contract.
_MIN_VIABLE_CHUNKS = 1


class ChunkingStage(StageHandler):
    stage = PipelineStage.CHUNKING
    requires = (ArtifactKind.CANONICAL_DOCUMENT, ArtifactKind.CLASSIFICATION)
    cacheable = True
    retryable = True

    def versions_for(self, ctx: StageContext) -> ComponentVersions:
        strategy = self._config(ctx).strategy.value
        return ComponentVersions(
            cdm_version=CDM_VERSION,
            profile_id=str(ctx.profile.id) if ctx.profile else None,
            profile_version=ctx.profile.version if ctx.profile else None,
            chunk_engine_version=CHUNK_ENGINE_VERSION,
            chunk_strategy=strategy,
            chunk_strategy_version=CHUNK_STRATEGY_VERSIONS.get(strategy, "unknown"),
        )

    async def cleanup(self, ctx: StageContext) -> None:
        """Drop the previous chunk set - and the L3 vectors that pointed at it.

        Only the chunk level is cleared: the L1 document summary and L2 clause
        vectors reference contract and clause rows this stage does not touch, and
        deleting them would force an unnecessary re-embed of text that has not
        changed (§25 selective regeneration).
        """
        embeddings = await EmbeddingRepository(ctx.db).delete_for_contract_level(
            ctx.contract_id, ctx.project_id, EmbeddingLevel.CHUNK
        )
        chunks = await ChunkRepository(ctx.db).delete_for_contract(ctx.contract_id, ctx.project_id)
        if chunks or embeddings:
            logger.info(
                "chunking_cleanup",
                contract_id=str(ctx.contract_id),
                chunks_deleted=chunks,
                embeddings_deleted=embeddings,
            )

    async def run(self, ctx: StageContext) -> StageResult:
        await ctx.report_progress(52, "chunking document")

        document = await self._load_cdm(ctx)
        config = self._config(ctx)

        result = ChunkingEngine().chunk(document, config)

        if len(result.chunks) < _MIN_VIABLE_CHUNKS:
            # Not retryable as-is: re-running the same strategy over the same CDM
            # produces the same nothing. The remedy is a different parser or profile.
            raise PipelineError(
                "Chunking produced no usable chunks. The document parsed but yielded "
                "no analysable text - check the parser output and the profile's "
                "chunking configuration.",
                stage=self.stage.value,
                retryable=False,
                details={
                    "strategy": config.strategy.value,
                    "sections": document.statistics.section_count,
                    "paragraphs": document.statistics.paragraph_count,
                    "rejected": result.validation.rejection_count,
                },
            )

        version = await self._next_version(ctx)
        repository = ChunkRepository(ctx.db)
        id_map = await repository.persist(
            contract_id=ctx.contract_id,
            project_id=ctx.project_id,
            chunks=result.chunks,
            strategy=config.strategy.value,
            engine_version=CHUNK_ENGINE_VERSION,
            strategy_version=CHUNK_STRATEGY_VERSIONS.get(config.strategy.value, "unknown"),
            version=version,
            language=document.metadata.language,
            agreement_type=ctx.contract.agreement_type,
        )

        stats = result.statistics
        for chunk_type, count in stats.by_type.items():
            metrics.chunks_created_total.labels(
                strategy=config.strategy.value, chunk_type=chunk_type
            ).inc(count)
        for rejection in result.validation.rejected:
            metrics.chunk_validation_failures_total.labels(reason=rejection.reason).inc()

        await ctx.report_progress(59, f"{len(result.chunks)} chunks")

        warnings = list(result.validation.warnings)
        coverage = stats.as_dict()["coordinate_coverage"]
        if coverage < 1.0:
            # Not fatal, but it bounds what the evidence viewer can highlight, so the
            # reviewer is told rather than left to discover it on a blank page.
            warnings.append(
                f"{stats.count - stats.with_coordinates} of {stats.count} chunks have no "
                "page coordinates and cannot be highlighted in the document viewer."
            )

        logger.info(
            "chunking_completed",
            contract_id=str(ctx.contract_id),
            strategy=config.strategy.value,
            chunks=stats.count,
            version=version,
            by_type=stats.by_type,
            rejected=result.validation.rejection_count,
            coordinate_coverage=coverage,
            max_depth=stats.max_depth,
        )

        return StageResult(
            # Three artifacts, as the stage contract declares (§7.3). The chunk set is
            # primary; statistics and validation are split out because they are read
            # on their own - by the contract detail screen and by the profile-tuning
            # view - and neither should require loading every chunk to answer.
            artifacts=[
                StageArtifact(
                    kind=ArtifactKind.CHUNKS,
                    payload={
                        "strategy": config.strategy.value,
                        "config": config.as_dict(),
                        "engine_version": CHUNK_ENGINE_VERSION,
                        "chunk_version": version,
                        # Engine id → database id, so the artifact can be replayed
                        # against the rows it produced.
                        "chunk_ids": {engine_id: str(db_id) for engine_id, db_id in id_map.items()},
                        "chunks": [chunk.as_dict() for chunk in result.chunks],
                    },
                    summary={
                        "strategy": config.strategy.value,
                        "count": stats.count,
                        "by_type": stats.by_type,
                        "chunk_version": version,
                    },
                ),
                StageArtifact(
                    kind=ArtifactKind.CHUNK_STATISTICS,
                    payload=stats.as_dict(),
                    summary={
                        "count": stats.count,
                        "total_tokens": stats.total_tokens,
                        "mean_tokens": stats.mean_tokens,
                        "max_tokens": stats.max_tokens,
                        "cross_page": stats.cross_page_count,
                        "coordinate_coverage": coverage,
                        "max_depth": stats.max_depth,
                    },
                ),
                StageArtifact(
                    kind=ArtifactKind.CHUNK_VALIDATION,
                    payload=result.validation.as_dict(),
                    summary={
                        "accepted": result.validation.accepted,
                        "rejected": result.validation.rejection_count,
                        "healthy": result.validation.is_healthy,
                    },
                ),
            ],
            stats={
                "chunk_count": stats.count,
                "chunk_strategy": config.strategy.value,
                "chunk_tokens_total": stats.total_tokens,
                "chunk_tokens_mean": stats.mean_tokens,
                "chunks_rejected": result.validation.rejection_count,
                "chunks_cross_page": stats.cross_page_count,
            },
            context_updates={
                "chunk_version": version,
                "chunk_strategy": config.strategy.value,
                "chunk_count": stats.count,
            },
            warnings=warnings,
        )

    # =========================================================================
    # Helpers
    # =========================================================================
    def _config(self, ctx: StageContext) -> ChunkConfig:
        """Resolve the chunk configuration: profile first, run options second.

        Run options override the profile so a reviewer can re-chunk one contract with
        a different strategy from the UI without editing - and thereby re-versioning -
        the profile every other contract of that type depends on.
        """
        config = ChunkConfig.from_profile(ctx.profile)

        override = ctx.options.get("chunk_strategy")
        if override:
            try:
                config.strategy = ChunkStrategy(str(override))
            except ValueError:
                logger.warning(
                    "chunk_strategy_override_invalid",
                    contract_id=str(ctx.contract_id),
                    requested=str(override),
                    using=config.strategy.value,
                )

        overrides: dict[str, Any] = ctx.options.get("chunk_config") or {}
        for key in ("max_tokens", "min_tokens", "overlap_tokens"):
            if key in overrides:
                try:
                    setattr(config, key, int(overrides[key]))
                except (TypeError, ValueError):
                    logger.warning(
                        "chunk_config_override_invalid",
                        contract_id=str(ctx.contract_id),
                        field=key,
                        value=overrides[key],
                    )
        return config

    async def _next_version(self, ctx: StageContext) -> int:
        """Next chunk-set version for this contract.

        ``cleanup`` removes the previous set, so in the normal path this returns 1
        again. It is read from the table rather than assumed because a partial
        rebuild that keeps prior chunks must not collide with the unique constraint
        on ``(contract_id, version, reading_order)``.
        """
        latest = await ChunkRepository(ctx.db).latest_version(ctx.contract_id, ctx.project_id)
        return latest + 1

    async def _load_cdm(self, ctx: StageContext) -> CanonicalDocument:
        pointer = await DocumentArtifactRepository(ctx.db).current(
            ctx.contract_id, ArtifactKind.CANONICAL_DOCUMENT
        )
        if pointer is None:
            raise PipelineError(
                "The canonical document artifact is missing. Re-run enrichment.",
                stage=self.stage.value,
            )
        payload = await ctx.storage.get_json(pointer.storage_path)
        try:
            return CanonicalDocument.model_validate(payload)
        except Exception as exc:
            raise PipelineError(
                f"The canonical document artifact is invalid: {exc}",
                stage=self.stage.value,
            ) from exc


register_stage(ChunkingStage())

__all__ = ["ChunkingStage"]

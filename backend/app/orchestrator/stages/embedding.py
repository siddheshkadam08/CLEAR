"""Stage 7 - Embedding.

Builds the three-level vector hierarchy that makes the contract findable: one L1
vector for the document, one L2 vector per extracted clause, one L3 vector per chunk.

This handler is the only part of the embedding path that may touch the database. It
loads what needs embedding, asks
:class:`~app.ai.embedding.engine.EmbeddingEngine` to compose and generate, and writes
the rows. The engine holds no session, so it stays testable against fixtures.

**Selective regeneration is the point of the version columns.** Before generating
anything, the handler asks the vector store which content hashes it already holds
*under the current version set*. Unchanged text reuses its vector and costs nothing.
That is what makes a re-run after a prompt-only change nearly free, and what stops a
re-parse of a 300-page agreement re-billing every chunk.

Which levels run is configuration: ``profile.embedding_config.levels``. A document
type with no clause extraction can skip L2 without a code change.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.ai.embedding import EmbeddingEngine, EmbeddingPlan, get_embedding_provider
from app.ai.embedding.engine import EmbeddingItem, LevelOutcome
from app.core import metrics
from app.core.config import get_settings
from app.core.enums import ArtifactKind, EmbeddingLevel, PipelineStage
from app.core.errors import PipelineError
from app.core.logging import get_logger
from app.core.versions import (
    EMBEDDING_STRATEGY_VERSION,
    ComponentVersions,
    current_versions_for_stage,
)
from app.orchestrator.stages.base import (
    StageArtifact,
    StageContext,
    StageHandler,
    StageResult,
    register_stage,
)
from app.repositories.chunk import ChunkRepository
from app.repositories.contract import ContractMetadataRepository
from app.repositories.embedding import EmbeddingRepository
from app.repositories.knowledge import ClauseRepository, EntityRepository

logger = get_logger(__name__)

#: Below this many characters a chunk is not worth a vector: a two-word fragment
#: retrieves noisily and dilutes the index. Chunk validation already rejects most of
#: these; this is the backstop.
_MIN_CHUNK_CHARS = 40


class EmbeddingStage(StageHandler):
    stage = PipelineStage.EMBEDDING
    requires = (ArtifactKind.CHUNKS,)
    cacheable = True
    retryable = True

    def versions_for(self, ctx: StageContext) -> ComponentVersions:
        return current_versions_for_stage(
            ctx.stage,
            profile_id=str(ctx.profile.id) if ctx.profile else None,
            profile_version=ctx.profile.version if ctx.profile else None,
        )

    async def cleanup(self, ctx: StageContext) -> None:
        """No blanket delete.

        Deliberately empty, unlike the other row-writing stages. Deleting every vector
        before a re-run would discard exactly the rows that duplicate detection is
        about to reuse, turning a free re-run into a full re-embed. Instead, the
        levels this stage is about to write are cleared inside ``run``, after the
        reusable vectors have been read out of them.
        """
        return None

    async def run(self, ctx: StageContext) -> StageResult:
        await ctx.report_progress(90, "preparing embeddings")

        levels = self._levels(ctx)
        if not levels:
            raise PipelineError(
                "The profile's embedding configuration enables no levels, so nothing "
                "could be embedded and the contract would not be searchable.",
                stage=self.stage.value,
                retryable=False,
            )

        plan = await self._build_plan(ctx, levels)
        if not plan.items:
            raise PipelineError(
                "Nothing embeddable was found for this contract. Chunking and "
                "extraction produced no text long enough to embed.",
                stage=self.stage.value,
                retryable=False,
                details={"levels": [level.value for level in levels]},
            )

        provider = get_embedding_provider()
        repository = EmbeddingRepository(ctx.db)

        # Read the reusable set *before* clearing anything.
        existing, reusable = await self._load_reusable(ctx, repository, plan, levels)

        # Now clear the levels being rewritten. Scoped per level so a run that only
        # rebuilds L3 leaves the L1 and L2 vectors - and their reuse - intact.
        for level in levels:
            await repository.delete_for_contract_level(ctx.contract_id, ctx.project_id, level)

        await ctx.report_progress(92, f"embedding {len(plan.items)} items")

        engine = EmbeddingEngine(provider=provider)

        async def on_level(level: EmbeddingLevel, outcome: LevelOutcome) -> None:
            await ctx.report_progress(
                93, f"{level.value}: {outcome.generated} new, {outcome.reused} reused"
            )

        run = await engine.run(
            plan,
            contract_id=ctx.contract_id,
            project_id=ctx.project_id,
            existing=existing,
            reusable_vectors=reusable,
            profile_version=getattr(ctx.profile, "version", None),
            levels=levels,
            on_progress=on_level,
        )

        if not run.rows:
            raise PipelineError(
                "No vectors were produced. Every embedding level failed - the contract "
                "would be invisible to search.",
                stage=self.stage.value,
                retryable=True,
                details={"failed_levels": run.failed_levels, "warnings": run.warnings},
            )

        await repository.insert_many(run.rows)

        counts = await repository.counts_by_level(ctx.contract_id, ctx.project_id)
        # `level_name` rather than `level`: reusing the loop variable would shadow the
        # EmbeddingLevel values used below, and these keys are already strings.
        for level_name, count in counts.items():
            metrics.vector_count.labels(level=level_name).set(count)

        stats = run.statistics()
        logger.info(
            "embedding_completed",
            contract_id=str(ctx.contract_id),
            provider=provider.name,
            model=provider.model,
            dim=provider.dim,
            **{f"level_{key}": value for key, value in counts.items()},
            generated=run.generated,
            reused=run.reused,
            reuse_rate=stats["reuse_rate"],
            tokens=run.total_tokens,
            cost_usd=run.total_cost_usd,
            failed_levels=run.failed_levels,
        )

        # A partially embedded contract is searchable but incompletely, so the gap is
        # surfaced rather than left for someone to notice as poor recall.
        warnings = list(run.warnings)
        missing = [level.value for level in levels if not counts.get(level.value)]
        if missing:
            warnings.append(
                "No vectors were stored for: "
                + ", ".join(missing)
                + ". Retrieval at those levels will return nothing for this contract."
            )

        return StageResult(
            artifacts=self._artifacts(run, counts, provider),
            stats={
                "vectors_total": len(run.rows),
                "vectors_generated": run.generated,
                "vectors_reused": run.reused,
                "embedding_reuse_rate": stats["reuse_rate"],
                "embedding_tokens": run.total_tokens,
                "embedding_cost_usd": run.total_cost_usd,
                "embedding_provider": provider.name,
                "embedding_model": provider.model,
                "embedding_dim": provider.dim,
                "embedding_failed_levels": run.failed_levels,
                **{f"vectors_{key}": value for key, value in counts.items()},
            },
            context_updates={
                "embedding_model": provider.model,
                "embedding_dim": provider.dim,
                "vectors_total": len(run.rows),
            },
            warnings=warnings,
        )

    # =========================================================================
    # Planning
    # =========================================================================
    def _levels(self, ctx: StageContext) -> list[EmbeddingLevel]:
        """Which levels to build, from the profile.

        An unrecognised level in configuration is skipped with a warning rather than
        failing the job: the profile is administrator-editable data, and a typo should
        not stop a contract being indexed at the levels that are valid.
        """
        configured = ctx.profile_setting("embedding_config.levels", None)
        if not configured:
            return list(EmbeddingLevel)

        levels: list[EmbeddingLevel] = []
        for raw in configured:
            try:
                levels.append(EmbeddingLevel(str(raw)))
            except ValueError:
                logger.warning(
                    "unknown_embedding_level",
                    level=str(raw),
                    contract_id=str(ctx.contract_id),
                )
        # Preserve the canonical order regardless of how the profile listed them, so
        # L1 is always built first and a failure part-way leaves the document
        # discoverable.
        return [level for level in EmbeddingLevel if level in levels]

    async def _build_plan(self, ctx: StageContext, levels: list[EmbeddingLevel]) -> EmbeddingPlan:
        engine = EmbeddingEngine(provider=get_embedding_provider())
        plan = EmbeddingPlan()
        base_metadata = await self._filter_metadata(ctx)

        if EmbeddingLevel.DOCUMENT_SUMMARY in levels:
            item = await self._document_item(ctx, engine, base_metadata)
            if item is not None:
                plan.items.append(item)

        if EmbeddingLevel.CLAUSE in levels:
            plan.items.extend(await self._clause_items(ctx, engine, base_metadata))

        if EmbeddingLevel.CHUNK in levels:
            plan.items.extend(await self._chunk_items(ctx, engine, base_metadata))

        logger.info(
            "embedding_plan_built",
            contract_id=str(ctx.contract_id),
            counts=plan.counts(),
            levels=[level.value for level in levels],
        )
        return plan

    async def _filter_metadata(self, ctx: StageContext) -> dict[str, Any]:
        """Attributes copied onto every vector row for metadata-first retrieval.

        Duplicated onto the vector deliberately (§14): the pre-filter has to resolve
        before the ANN scan, and joining ``contract_metadata`` inside the vector
        query's hot path would defeat that.
        """
        metadata = await ContractMetadataRepository(ctx.db).get_for_contract(ctx.contract_id)
        payload: dict[str, Any] = {
            "contract_id": str(ctx.contract_id),
            "project_id": str(ctx.project_id),
            "agreement_type": ctx.contract.agreement_type,
        }
        if metadata is None:
            return {key: value for key, value in payload.items() if value is not None}

        payload.update(
            {
                "risk_band": metadata.risk_band,
                "category": metadata.category,
                "vendor": metadata.vendor,
                "customer": metadata.customer,
                "governing_law": metadata.governing_law,
                "party_a": metadata.party_a,
                "party_b": metadata.party_b,
                "effective_date": metadata.effective_date.isoformat()
                if metadata.effective_date
                else None,
                "expiration_date": metadata.expiration_date.isoformat()
                if metadata.expiration_date
                else None,
                "has_unlimited_liability": metadata.has_unlimited_liability,
            }
        )
        return {key: value for key, value in payload.items() if value is not None}

    async def _document_item(
        self, ctx: StageContext, engine: EmbeddingEngine, base_metadata: dict[str, Any]
    ) -> EmbeddingItem | None:
        metadata = await ContractMetadataRepository(ctx.db).get_for_contract(ctx.contract_id)
        parties = [
            entity.name
            for entity in await EntityRepository(ctx.db).list_for_contract(
                ctx.contract_id, ctx.project_id
            )
            if entity.is_primary
        ]

        return engine.compose_document_summary(
            contract_id=ctx.contract_id,
            title=ctx.contract.title,
            agreement_type=ctx.contract.agreement_type,
            summary=metadata.summary if metadata else None,
            key_topics=list(metadata.key_topics) if metadata else [],
            parties=parties,
            filter_metadata=base_metadata,
            includes=ctx.profile_setting("embedding_config.summary_includes", None),
        )

    async def _clause_items(
        self, ctx: StageContext, engine: EmbeddingEngine, base_metadata: dict[str, Any]
    ) -> list[EmbeddingItem]:
        clauses = await ClauseRepository(ctx.db).list_for_contract(ctx.contract_id, ctx.project_id)
        items: list[EmbeddingItem] = []
        for clause in clauses:
            item = engine.compose_clause(
                clause_id=clause.id,
                clause_type=clause.clause_type,
                title=clause.title,
                clause_number=clause.clause_number,
                text=clause.text_content,
                attributes=dict(clause.attributes or {}),
                filter_metadata=base_metadata,
            )
            if item is not None:
                items.append(item)
        return items

    async def _chunk_items(
        self, ctx: StageContext, engine: EmbeddingEngine, base_metadata: dict[str, Any]
    ) -> list[EmbeddingItem]:
        chunks = await ChunkRepository(ctx.db).list_for_contract(ctx.contract_id, ctx.project_id)
        items: list[EmbeddingItem] = []
        for chunk in chunks:
            if len(chunk.text_content.strip()) < _MIN_CHUNK_CHARS:
                continue
            item = engine.compose_chunk(
                chunk_id=chunk.id,
                text=chunk.text_content,
                section_title=chunk.section_title,
                clause_number=chunk.clause_number,
                chunk_type=chunk.chunk_type.value
                if hasattr(chunk.chunk_type, "value")
                else str(chunk.chunk_type),
                filter_metadata=base_metadata,
            )
            if item is not None:
                item.token_estimate = chunk.token_count or 0
                items.append(item)
        return items

    # =========================================================================
    # Reuse
    # =========================================================================
    async def _load_reusable(
        self,
        ctx: StageContext,
        repository: EmbeddingRepository,
        plan: EmbeddingPlan,
        levels: list[EmbeddingLevel],
    ) -> tuple[dict[EmbeddingLevel, dict[str, uuid.UUID]], dict[uuid.UUID, Any]]:
        """Find vectors that can be reused, and fetch them.

        Keyed on the content hash *and* the full version set, so a vector is only
        reused when the text and every version that shaped it are unchanged. Reusing
        across a model change would silently mix two vector spaces in one index, and
        distances between them are meaningless.
        """
        provider = get_embedding_provider()
        embedding_settings = get_settings().embedding

        existing: dict[EmbeddingLevel, dict[str, uuid.UUID]] = {}
        wanted_ids: set[uuid.UUID] = set()

        for level in levels:
            hashes = await repository.existing_hashes(
                project_id=ctx.project_id,
                level=level,
                model=provider.model,
                embedding_version=embedding_settings.version,
                strategy_version=EMBEDDING_STRATEGY_VERSION,
            )
            if not hashes:
                continue
            # Only the hashes this plan actually needs; the project may hold millions.
            needed = {item.hash for item in plan.by_level(level)}
            matched = {digest: row_id for digest, row_id in hashes.items() if digest in needed}
            if matched:
                existing[level] = matched
                wanted_ids.update(matched.values())

        reusable: dict[uuid.UUID, Any] = {}
        for row_id in wanted_ids:
            vector = await repository.get_vector(row_id, ctx.project_id)
            if vector is not None:
                reusable[row_id] = vector

        if reusable:
            logger.info(
                "embedding_reuse_available",
                contract_id=str(ctx.contract_id),
                candidates=len(reusable),
                by_level={level.value: len(rows) for level, rows in existing.items()},
            )
        return existing, reusable

    # =========================================================================
    # Artifacts
    # =========================================================================
    def _artifacts(self, run: Any, counts: dict[str, int], provider: Any) -> list[StageArtifact]:
        """One artifact per level plus statistics, as the stage contract declares."""
        by_level = {
            EmbeddingLevel.DOCUMENT_SUMMARY: ArtifactKind.SUMMARY_EMBEDDINGS,
            EmbeddingLevel.CLAUSE: ArtifactKind.CLAUSE_EMBEDDINGS,
            EmbeddingLevel.CHUNK: ArtifactKind.CHUNK_EMBEDDINGS,
        }

        artifacts: list[StageArtifact] = []
        for level, kind in by_level.items():
            rows = [row for row in run.rows if row["level"] is level]
            if not rows:
                continue
            artifacts.append(
                StageArtifact(
                    kind=kind,
                    # Vectors are deliberately excluded: they are large, opaque, and
                    # already in the database. What is worth keeping is the mapping
                    # from source row to content hash, which is what makes a later
                    # reuse decision auditable.
                    payload={
                        "level": level.value,
                        "model": provider.model,
                        "dim": provider.dim,
                        "vectors": [
                            {
                                "ref_id": str(row["ref_id"]),
                                "content_hash": row["content_hash"],
                                "token_count": row["token_count"],
                            }
                            for row in rows
                        ],
                    },
                    summary={"level": level.value, "count": len(rows)},
                )
            )

        artifacts.append(
            StageArtifact(
                kind=ArtifactKind.EMBEDDING_STATISTICS,
                payload={
                    "statistics": run.statistics(),
                    "levels": [outcome.as_dict() for outcome in run.outcomes],
                    "stored_by_level": counts,
                    "provider": provider.metadata(),
                    "warnings": run.warnings,
                },
                summary={
                    "vectors": len(run.rows),
                    "generated": run.generated,
                    "reused": run.reused,
                    "cost_usd": run.total_cost_usd,
                    "failed_levels": run.failed_levels,
                },
            )
        )
        return artifacts


register_stage(EmbeddingStage())

__all__ = ["EmbeddingStage"]

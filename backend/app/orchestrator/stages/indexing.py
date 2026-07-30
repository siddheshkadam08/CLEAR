"""Stage 8 - Indexing.

The last stage. Makes everything the pipeline produced actually reachable:

* **Keyword search vectors.** ``chunks.search_vector`` is maintained by a database
  trigger, so this stage verifies coverage rather than computing it - a chunk with a
  null vector is invisible to the keyword leg of hybrid search, and finding that out
  here beats finding it out from a user's failed search.
* **The knowledge graph.** Extraction recorded what the text *says*; this resolves
  those references into edges between real rows, and records the ones that resolve to
  nothing.
* **Readiness.** The contract is marked READY only once its vectors and its graph
  exist. Until then it is processing, not searchable-but-incomplete.

Nothing here calls a model. Indexing is deterministic bookkeeping over rows the
earlier stages produced, which is why it is cheap enough to re-run freely.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.ai.graph import KnowledgeGraphBuilder
from app.core import metrics
from app.core.enums import ArtifactKind, ContractStatus, PipelineStage
from app.core.errors import PipelineError
from app.core.logging import get_logger
from app.core.versions import (
    GRAPH_VERSION,
    INDEX_VERSION,
    ComponentVersions,
    current_versions_for_stage,
)
from app.models.chunk import Chunk
from app.orchestrator.stages.base import (
    StageArtifact,
    StageContext,
    StageHandler,
    StageResult,
    register_stage,
)
from app.repositories.contract import ContractMetadataRepository
from app.repositories.embedding import EmbeddingRepository
from app.repositories.knowledge import (
    ClauseRepository,
    EntityRepository,
    KnowledgeRelationshipRepository,
    ObligationRepository,
    RiskRepository,
)

logger = get_logger(__name__)


class IndexingStage(StageHandler):
    stage = PipelineStage.INDEXING
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
        """Drop the resolved graph edges this stage derived.

        Only the derived ones: the extracted relationships belong to the extraction
        stage and are its to replace. Deleting them here would lose what the document
        said whenever indexing alone was re-run.
        """
        deleted = await KnowledgeRelationshipRepository(ctx.db).delete_derived_for_contract(
            ctx.contract_id, ctx.project_id
        )
        if deleted:
            logger.info("indexing_cleanup", contract_id=str(ctx.contract_id), edges_deleted=deleted)

    async def run(self, ctx: StageContext) -> StageResult:
        started = time.perf_counter()
        await ctx.report_progress(95, "building search index")

        coverage = await self._search_vector_coverage(ctx)
        vectors = await EmbeddingRepository(ctx.db).counts_by_level(ctx.contract_id, ctx.project_id)

        if not vectors:
            raise PipelineError(
                "No vectors exist for this contract, so it would not be retrievable. "
                "Re-run the embedding stage.",
                stage=self.stage.value,
                details={"search_vector_coverage": coverage},
            )

        await ctx.report_progress(97, "building knowledge graph")
        graph = await self._build_graph(ctx)

        elapsed = time.perf_counter() - started
        metrics.index_build_duration_seconds.observe(elapsed)
        metrics.graph_nodes_total.set(len(graph.nodes))
        metrics.graph_edges_total.set(len(graph.edges))

        # The contract becomes searchable here, and only here. A contract flagged for
        # review is still fully indexed - the flag is about trusting the extraction,
        # not about whether the document can be found.
        ctx.contract.status = (
            ContractStatus.NEEDS_REVIEW if ctx.contract.needs_review else ContractStatus.READY
        )
        ctx.contract.processed_at = datetime.now(UTC)
        await ctx.db.flush()

        warnings = list(graph.warnings)
        if coverage["missing"]:
            warnings.append(
                f"{coverage['missing']} of {coverage['total']} chunks have no keyword "
                "search vector; those chunks are reachable by semantic search only."
            )

        stats = graph.statistics()
        logger.info(
            "indexing_completed",
            contract_id=str(ctx.contract_id),
            status=ctx.contract.status.value,
            vectors=vectors,
            search_vector_coverage=coverage["coverage"],
            duration_seconds=round(elapsed, 3),
            **stats,
        )

        return StageResult(
            artifacts=[
                StageArtifact(
                    kind=ArtifactKind.RELATIONSHIPS,
                    payload={
                        "nodes": [node.as_dict() for node in graph.nodes],
                        "edges": [edge.as_dict() for edge in graph.edges],
                        "dangling": graph.dangling,
                        "graph_version": GRAPH_VERSION,
                    },
                    summary={
                        "nodes": len(graph.nodes),
                        "edges": len(graph.edges),
                        "resolved": graph.resolved_edges,
                        "dangling": len(graph.dangling),
                    },
                ),
                StageArtifact(
                    kind=ArtifactKind.INDEX_STATISTICS,
                    payload={
                        "vectors_by_level": vectors,
                        "search_vector": coverage,
                        "graph": stats,
                        "index_version": INDEX_VERSION,
                        "graph_version": GRAPH_VERSION,
                        "duration_seconds": round(elapsed, 3),
                        "warnings": warnings,
                    },
                    summary={
                        "vectors": sum(vectors.values()),
                        "graph_nodes": len(graph.nodes),
                        "graph_edges": len(graph.edges),
                        "searchable": True,
                    },
                ),
            ],
            stats={
                "index_duration_seconds": round(elapsed, 3),
                "search_vector_coverage": coverage["coverage"],
                "graph_nodes": len(graph.nodes),
                "graph_edges": len(graph.edges),
                "graph_resolved_edges": graph.resolved_edges,
                "graph_dangling_references": len(graph.dangling),
                **{f"vectors_{level}": count for level, count in vectors.items()},
            },
            context_updates={
                "indexed": True,
                "contract_status": ctx.contract.status.value,
            },
            warnings=warnings,
        )

    # =========================================================================
    # Search vectors
    # =========================================================================
    async def _search_vector_coverage(self, ctx: StageContext) -> dict[str, Any]:
        """How many chunks have a keyword search vector.

        Verified rather than computed: ``chunks.search_vector`` is maintained by a
        database trigger so it cannot drift from the text. Checking it here turns a
        missing trigger - the kind of migration slip that produces silently degraded
        search for months - into a visible warning on the job.
        """
        total = await ctx.db.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.contract_id == ctx.contract_id, Chunk.project_id == ctx.project_id)
        )
        indexed = await ctx.db.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(
                Chunk.contract_id == ctx.contract_id,
                Chunk.project_id == ctx.project_id,
                Chunk.search_vector.is_not(None),
            )
        )
        total = int(total or 0)
        indexed = int(indexed or 0)
        return {
            "total": total,
            "indexed": indexed,
            "missing": total - indexed,
            "coverage": round(indexed / total, 4) if total else 0.0,
        }

    # =========================================================================
    # Graph
    # =========================================================================
    async def _build_graph(self, ctx: StageContext) -> Any:
        contract_id, project_id = ctx.contract_id, ctx.project_id

        parties = await EntityRepository(ctx.db).list_for_contract(contract_id, project_id)
        clauses = await ClauseRepository(ctx.db).list_for_contract(contract_id, project_id)
        obligations = await ObligationRepository(ctx.db).list_for_contract(contract_id, project_id)
        risks = await RiskRepository(ctx.db).list_for_contract(contract_id, project_id)
        relationships = await KnowledgeRelationshipRepository(ctx.db).list_for_contract(
            contract_id, project_id
        )
        metadata = await ContractMetadataRepository(ctx.db).get_for_contract(contract_id)

        graph = KnowledgeGraphBuilder().build(
            contract_id=contract_id,
            contract_title=ctx.contract.title,
            agreement_type=ctx.contract.agreement_type,
            parties=list(parties),
            clauses=list(clauses),
            obligations=list(obligations),
            risks=list(risks),
            relationships=list(relationships),
            metadata=metadata,
        )

        await self._persist_edges(ctx, graph)
        return graph

    async def _persist_edges(self, ctx: StageContext, graph: Any) -> None:
        """Store the derived edges.

        Extracted edges already exist as rows from the extraction stage; only the
        edges this stage *derived* are inserted, and each records the node ids it
        resolved so a traversal is a join rather than a string match.
        """
        repository = KnowledgeRelationshipRepository(ctx.db)
        rows = [
            {
                "id": uuid.uuid4(),
                "contract_id": ctx.contract_id,
                "project_id": ctx.project_id,
                "relation": edge.relation,
                "source_type": edge.source_type,
                "source_ref": edge.source_ref[:255],
                "target_type": edge.target_type,
                "target_ref": edge.target_ref[:255],
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "label": edge.label[:512] if edge.label else None,
                "attributes": {**edge.attributes, "origin": edge.origin},
                "is_resolved": edge.is_resolved,
                "review_status": "not_required",
                "profile_version": getattr(ctx.profile, "version", None),
            }
            for edge in graph.edges
            if edge.origin == "derived"
        ]
        if rows:
            await repository.insert_many(rows)


register_stage(IndexingStage())

__all__ = ["IndexingStage"]

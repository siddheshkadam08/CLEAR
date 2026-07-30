"""Retrieval engine - executes a plan (§15).

Takes a :class:`~app.ai.retrieval.planner.RetrievalPlan` and returns ranked evidence.
It makes no decisions: what to search, how deep, and within which projects were all
settled by the planner.

The execution order is the hierarchy, and it is not optional:

1. **Metadata pre-filter.** Narrow to candidate contracts using the projection.
   Applied first because it is an indexed lookup and it shrinks everything after it.
2. **L1 document summary.** Rank candidate *documents*. Skipped when the plan already
   names contracts - there is nothing to narrow.
3. **L2 clause / L3 chunk.** Search only within the surviving candidates.
4. **Hybrid fusion.** Vector and keyword results are combined by reciprocal rank
   fusion, then optionally re-ranked.
5. **Expansion.** Retrieved chunks are widened with their neighbours and their
   parents, so a clause arrives with the context it depends on.

**Every query is bounded by ``plan.project_ids``.** An unscoped plan returns nothing
rather than everything - the fail-safe direction, because the alternative is a
cross-project leak (§1.1).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embedding import get_embedding_provider
from app.ai.retrieval.planner import RetrievalPlan
from app.core import metrics
from app.core.config import get_settings
from app.core.enums import EmbeddingLevel, RetrievalStrategy, SearchMode
from app.core.logging import get_logger
from app.models.chunk import Chunk
from app.models.contract import Contract, ContractMetadata
from app.models.knowledge import Clause
from app.repositories.chunk import ChunkRepository
from app.repositories.embedding import EmbeddingRepository
from app.repositories.knowledge import KnowledgeRelationshipRepository

logger = get_logger(__name__)

#: Reciprocal-rank-fusion constant. 60 is the value from the original RRF paper and
#: is deliberately not tuned per query: RRF's value is that it combines rankings
#: without needing the two scores to be comparable, and a tuned k quietly
#: reintroduces that coupling.
_RRF_K = 60

#: Cap on candidate contracts carried from the metadata pre-filter into vector
#: search. Beyond this the pre-filter has not actually narrowed anything, and a huge
#: `IN` list is slower than letting the ANN index do its job.
_MAX_CANDIDATE_CONTRACTS = 200


@dataclass(slots=True)
class Evidence:
    """One retrieved item, with everything a citation needs."""

    #: Which level produced it - the reader wants to know whether this is a whole
    #: clause or a fragment.
    level: EmbeddingLevel
    ref_id: uuid.UUID
    contract_id: uuid.UUID
    project_id: uuid.UUID
    text: str
    score: float
    #: How it was found: ``vector``, ``keyword``, ``fused``, ``neighbour``, ``parent``,
    #: ``graph``. Kept because "why is this in my answer" is a real question.
    source: str = "vector"
    rank: int = 0
    contract_title: str | None = None
    clause_type: str | None = None
    clause_number: str | None = None
    section_title: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    bounding_boxes: list[dict[str, Any]] = field(default_factory=list)
    chunk_id: uuid.UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.level.value, str(self.ref_id))

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "ref_id": str(self.ref_id),
            "contract_id": str(self.contract_id),
            "contract_title": self.contract_title,
            "text": self.text,
            "score": round(self.score, 6),
            "source": self.source,
            "rank": self.rank,
            "clause_type": self.clause_type,
            "clause_number": self.clause_number,
            "section_title": self.section_title,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "bounding_boxes": self.bounding_boxes,
            "chunk_id": str(self.chunk_id) if self.chunk_id else None,
        }


@dataclass(slots=True)
class RetrievalResult:
    """Everything the plan retrieved, plus how it got there."""

    evidence: list[Evidence] = field(default_factory=list)
    #: Contracts that survived the metadata pre-filter.
    candidate_contracts: list[uuid.UUID] = field(default_factory=list)
    #: Rows the metadata-only strategy answers from directly.
    metadata_rows: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.evidence and not self.metadata_rows

    def by_level(self, level: EmbeddingLevel) -> list[Evidence]:
        return [item for item in self.evidence if item.level is level]

    def contract_ids(self) -> list[uuid.UUID]:
        seen: list[uuid.UUID] = []
        for item in self.evidence:
            if item.contract_id not in seen:
                seen.append(item.contract_id)
        return seen

    def statistics(self) -> dict[str, Any]:
        return {
            "evidence": len(self.evidence),
            "contracts": len(self.contract_ids()),
            "candidates": len(self.candidate_contracts),
            "metadata_rows": len(self.metadata_rows),
            "duration_ms": self.duration_ms,
            "by_source": self.counts,
            "truncated": self.truncated,
        }


class RetrievalEngine:
    """Executes retrieval plans."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._settings = get_settings().retrieval

    async def retrieve(self, plan: RetrievalPlan) -> RetrievalResult:
        started = time.perf_counter()
        result = RetrievalResult()

        if not plan.is_scoped:
            # The fail-safe direction. An unscoped plan must return nothing rather
            # than fall through to an unbounded query (§1.1).
            result.warnings.append(
                "You are not a member of any project in scope, so there is nothing to search."
            )
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            return result

        # ---- 1. metadata pre-filter -----------------------------------------
        candidates, metadata_rows = await self._metadata_prefilter(plan)
        result.candidate_contracts = candidates
        result.metadata_rows = metadata_rows

        if plan.strategy is RetrievalStrategy.METADATA_ONLY:
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            metrics.retrieval_duration_seconds.labels(scope=plan.scope.value).observe(
                result.duration_ms / 1000
            )
            metrics.retrieval_strategy_total.labels(
                strategy=plan.strategy.value, intent=plan.intent.value
            ).inc()
            return result

        if not candidates and not plan.filters.is_empty:
            # The filters matched nothing. Searching anyway would answer a question
            # the user did not ask, so it stops here and says so.
            result.warnings.append("No contracts matched the filters implied by this question.")
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            return result

        # ---- 2. vector + keyword per level ----------------------------------
        query_vector = await self._embed_query(plan, result)

        for budget in plan.levels:
            vector_hits = (
                await self._vector_search(plan, budget, candidates, query_vector)
                if query_vector
                else []
            )
            keyword_hits = (
                await self._keyword_search(plan, budget, candidates)
                if plan.mode in {SearchMode.HYBRID, SearchMode.KEYWORD}
                else []
            )

            if plan.mode is SearchMode.SEMANTIC:
                merged = vector_hits
            elif plan.mode is SearchMode.KEYWORD:
                merged = keyword_hits
            else:
                merged = _fuse(vector_hits, keyword_hits, limit=budget.limit)

            result.evidence.extend(merged)

            # L1 narrows the candidate set for the levels below it: that is the whole
            # point of the hierarchy, and without this the descent is three
            # independent searches rather than one.
            if budget.level is EmbeddingLevel.DOCUMENT_SUMMARY and merged:
                narrowed = [item.contract_id for item in merged]
                candidates = narrowed[:_MAX_CANDIDATE_CONTRACTS]
                result.candidate_contracts = candidates

        # ---- 3. expansion ----------------------------------------------------
        if plan.neighbour_window > 0:
            result.evidence.extend(await self._expand_neighbours(plan, result))
        if plan.graph_depth > 0:
            result.evidence.extend(await self._expand_graph(plan, result))

        # ---- 4. finalise -----------------------------------------------------
        result.evidence = self._deduplicate(result.evidence)
        result.counts = _count_by_source(result.evidence)
        result.duration_ms = int((time.perf_counter() - started) * 1000)

        metrics.retrieval_duration_seconds.labels(scope=plan.scope.value).observe(
            result.duration_ms / 1000
        )
        metrics.retrieval_strategy_total.labels(
            strategy=plan.strategy.value, intent=plan.intent.value
        ).inc()
        for level_name, count in _count_by_level(result.evidence).items():
            metrics.retrieval_candidates.labels(level=level_name).observe(count)

        logger.info(
            "retrieval_completed",
            intent=plan.intent.value,
            strategy=plan.strategy.value,
            **result.statistics(),
        )
        return result

    # =========================================================================
    # Metadata
    # =========================================================================
    async def _metadata_prefilter(
        self, plan: RetrievalPlan
    ) -> tuple[list[uuid.UUID], list[dict[str, Any]]]:
        """Narrow to candidate contracts from the projection.

        Runs first for every strategy. Even when the answer needs document content,
        knowing the twelve contracts that can possibly be relevant turns the vector
        search from a repository-wide scan into a bounded one.
        """
        filters = plan.filters
        stmt: Select[Any] = (
            select(
                ContractMetadata.contract_id,
                Contract.title,
                Contract.agreement_type,
                ContractMetadata.risk_score,
                ContractMetadata.risk_band,
                ContractMetadata.effective_date,
                ContractMetadata.expiration_date,
                ContractMetadata.contract_value,
                ContractMetadata.currency,
                ContractMetadata.party_a,
                ContractMetadata.party_b,
                ContractMetadata.has_unlimited_liability,
                ContractMetadata.missing_mandatory_clauses,
            )
            .join(Contract, Contract.id == ContractMetadata.contract_id)
            # The isolation boundary, and the first predicate the planner sees.
            .where(ContractMetadata.project_id.in_(plan.project_ids))
            .where(Contract.deleted_at.is_(None))
        )

        if plan.contract_ids:
            stmt = stmt.where(ContractMetadata.contract_id.in_(plan.contract_ids))
        if filters.agreement_types:
            stmt = stmt.where(Contract.agreement_type.in_(filters.agreement_types))
        if filters.risk_bands:
            stmt = stmt.where(ContractMetadata.risk_band.in_(filters.risk_bands))
        if filters.has_unlimited_liability is not None:
            stmt = stmt.where(
                ContractMetadata.has_unlimited_liability.is_(filters.has_unlimited_liability)
            )
        if filters.missing_mandatory:
            stmt = stmt.where(
                func.jsonb_array_length(ContractMetadata.missing_mandatory_clauses) > 0
            )
        if filters.expiring_after:
            stmt = stmt.where(ContractMetadata.expiration_date >= filters.expiring_after)
        if filters.expiring_before:
            stmt = stmt.where(ContractMetadata.expiration_date <= filters.expiring_before)
        if filters.effective_after:
            stmt = stmt.where(ContractMetadata.effective_date >= filters.effective_after)
        if filters.parties:
            conditions = [
                or_(
                    ContractMetadata.party_a.ilike(f"%{party}%"),
                    ContractMetadata.party_b.ilike(f"%{party}%"),
                )
                for party in filters.parties
            ]
            stmt = stmt.where(or_(*conditions))

        stmt = stmt.order_by(
            ContractMetadata.risk_score.desc().nullslast(),
            ContractMetadata.expiration_date.asc().nullslast(),
        ).limit(_MAX_CANDIDATE_CONTRACTS)

        rows = (await self.db.execute(stmt)).all()
        contract_ids = [row.contract_id for row in rows]
        payload = [
            {
                "contract_id": str(row.contract_id),
                "title": row.title,
                "agreement_type": row.agreement_type,
                "risk_score": row.risk_score,
                "risk_band": row.risk_band,
                "effective_date": row.effective_date.isoformat() if row.effective_date else None,
                "expiration_date": row.expiration_date.isoformat() if row.expiration_date else None,
                "contract_value": float(row.contract_value) if row.contract_value else None,
                "currency": row.currency,
                "party_a": row.party_a,
                "party_b": row.party_b,
                "has_unlimited_liability": row.has_unlimited_liability,
                "missing_mandatory_clauses": list(row.missing_mandatory_clauses or []),
            }
            for row in rows
        ]
        return contract_ids, payload

    # =========================================================================
    # Vector
    # =========================================================================
    async def _embed_query(
        self, plan: RetrievalPlan, result: RetrievalResult
    ) -> list[float] | None:
        if plan.mode is SearchMode.KEYWORD:
            return None
        try:
            provider = get_embedding_provider()
            # `embed_query`, not `embed`: the active model is asymmetric and expects
            # a `query:` prefix here and `passage:` on the indexed side. Using the
            # document path for a search string returns a valid vector that simply
            # sits in the wrong part of the space, so recall drops with nothing in
            # the logs to explain it.
            return await provider.embed_query(plan.query)
        except Exception as exc:  # noqa: BLE001 - degrade to keyword, never fail
            logger.warning("query_embedding_failed", error=str(exc))
            result.warnings.append(
                "Semantic search was unavailable, so results are keyword-based only."
            )
            return None

    async def _vector_search(
        self,
        plan: RetrievalPlan,
        budget: Any,
        candidates: list[uuid.UUID],
        query_vector: list[float],
    ) -> list[Evidence]:
        repository = EmbeddingRepository(self.db)
        provider = get_embedding_provider()

        matches = await repository.search(
            query_vector=query_vector,
            project_ids=plan.project_ids,
            level=budget.level,
            limit=budget.limit,
            contract_ids=candidates or None,
            metadata_filters=plan.filters.vector_metadata() or None,
            # Cosine distance, so the threshold is expressed as a distance ceiling
            # rather than a similarity floor.
            max_distance=1.0 - budget.min_similarity,
            # Never compare vectors across models: the distances are not in the same
            # space and the ranking would be meaningless.
            model=provider.model,
        )

        evidence = [
            Evidence(
                level=match.level,
                ref_id=match.ref_id,
                contract_id=match.contract_id,
                project_id=uuid.UUID(str(match.filter_metadata.get("project_id")))
                if match.filter_metadata.get("project_id")
                else plan.project_ids[0],
                text=match.source_text or "",
                score=match.score,
                source="vector",
                rank=index + 1,
                metadata=match.filter_metadata,
            )
            for index, match in enumerate(matches)
        ]
        return await self._hydrate(evidence)

    # =========================================================================
    # Keyword
    # =========================================================================
    async def _keyword_search(
        self, plan: RetrievalPlan, budget: Any, candidates: list[uuid.UUID]
    ) -> list[Evidence]:
        """Full-text search over chunk text.

        The other half of hybrid retrieval. It exists because embeddings are poor at
        exact tokens - a party name, a clause number, "net 30" - which is precisely
        what people type into a contract search box.
        """
        if budget.level is EmbeddingLevel.DOCUMENT_SUMMARY:
            # There is no lexical index over composed summaries; L1 is a semantic
            # narrowing step and keyword search adds nothing there.
            return []

        stmt = (
            select(
                Chunk.id,
                Chunk.contract_id,
                Chunk.project_id,
                Chunk.text_content,
                Chunk.section_title,
                Chunk.clause_number,
                Chunk.page_start,
                Chunk.page_end,
                Chunk.bounding_boxes,
                func.ts_rank(
                    Chunk.search_vector, func.plainto_tsquery("english", plan.query)
                ).label("rank_score"),
            )
            .where(
                Chunk.project_id.in_(plan.project_ids),
                Chunk.search_vector.op("@@")(func.plainto_tsquery("english", plan.query)),
            )
            .order_by(
                func.ts_rank(
                    Chunk.search_vector, func.plainto_tsquery("english", plan.query)
                ).desc()
            )
            .limit(budget.limit)
        )
        if candidates:
            stmt = stmt.where(Chunk.contract_id.in_(candidates))

        try:
            rows = (await self.db.execute(stmt)).all()
        except Exception as exc:  # noqa: BLE001 - a missing FTS index must not fail search
            logger.warning("keyword_search_failed", error=str(exc))
            return []

        return [
            Evidence(
                level=EmbeddingLevel.CHUNK,
                ref_id=row.id,
                contract_id=row.contract_id,
                project_id=row.project_id,
                text=row.text_content,
                score=float(row.rank_score or 0.0),
                source="keyword",
                rank=index + 1,
                section_title=row.section_title,
                clause_number=row.clause_number,
                page_start=row.page_start,
                page_end=row.page_end,
                bounding_boxes=list(row.bounding_boxes or []),
                chunk_id=row.id,
            )
            for index, row in enumerate(rows)
        ]

    # =========================================================================
    # Hydration and expansion
    # =========================================================================
    async def _hydrate(self, evidence: list[Evidence]) -> list[Evidence]:
        """Fill in the citation fields a vector row does not carry.

        A vector match knows its ``ref_id`` and its source text but not the page,
        the clause number, or the boxes to highlight. Without this the answer would
        cite evidence the viewer cannot show.
        """
        chunk_refs = [item for item in evidence if item.level is EmbeddingLevel.CHUNK]
        clause_refs = [item for item in evidence if item.level is EmbeddingLevel.CLAUSE]

        if chunk_refs:
            chunk_rows = (
                (
                    await self.db.execute(
                        select(Chunk).where(Chunk.id.in_([item.ref_id for item in chunk_refs]))
                    )
                )
                .scalars()
                .all()
            )
            chunks_by_id = {row.id: row for row in chunk_rows}
            for item in chunk_refs:
                chunk = chunks_by_id.get(item.ref_id)
                if chunk is None:
                    continue
                item.text = item.text or chunk.text_content
                item.section_title = chunk.section_title
                item.clause_number = chunk.clause_number
                item.page_start = chunk.page_start
                item.page_end = chunk.page_end
                item.bounding_boxes = list(chunk.bounding_boxes or [])
                item.chunk_id = chunk.id

        if clause_refs:
            clause_rows = (
                (
                    await self.db.execute(
                        select(Clause).where(Clause.id.in_([item.ref_id for item in clause_refs]))
                    )
                )
                .scalars()
                .all()
            )
            clauses_by_id = {row.id: row for row in clause_rows}
            for item in clause_refs:
                clause = clauses_by_id.get(item.ref_id)
                if clause is None:
                    continue
                item.text = item.text or clause.text_content
                item.clause_type = clause.clause_type
                item.clause_number = clause.clause_number
                item.section_title = clause.section_title
                item.page_start = clause.page_start
                item.page_end = clause.page_end
                item.bounding_boxes = list(clause.bounding_boxes or [])
                # The chunk the clause was read from, so a clause citation can still
                # be highlighted in the document viewer.
                item.chunk_id = clause.chunk_id

        await self._attach_titles(evidence)
        return evidence

    async def _attach_titles(self, evidence: list[Evidence]) -> None:
        """Attach contract titles, so a citation names the document."""
        contract_ids = {item.contract_id for item in evidence}
        if not contract_ids:
            return
        rows = (
            await self.db.execute(
                select(Contract.id, Contract.title).where(Contract.id.in_(contract_ids))
            )
        ).all()
        titles = {row.id: row.title for row in rows}
        for item in evidence:
            item.contract_title = titles.get(item.contract_id)

    async def _expand_neighbours(
        self, plan: RetrievalPlan, result: RetrievalResult
    ) -> list[Evidence]:
        """Widen each retrieved chunk with its immediate neighbours.

        A clause that reads "the foregoing limitation shall not apply" is actively
        misleading without the limitation it refers to, so the neighbours come with
        it - scored below the hit itself so they inform without displacing it.
        """
        repository = ChunkRepository(self.db)
        seen = {item.chunk_id for item in result.evidence if item.chunk_id}
        expanded: list[Evidence] = []

        for item in result.by_level(EmbeddingLevel.CHUNK)[: self._settings.max_chunks]:
            if item.chunk_id is None:
                continue
            chunk = await repository.get_scoped(item.chunk_id, item.project_id)
            if chunk is None:
                continue
            for neighbour in await repository.neighbours(
                chunk, item.project_id, window=plan.neighbour_window
            ):
                if neighbour.id in seen:
                    continue
                seen.add(neighbour.id)
                expanded.append(
                    Evidence(
                        level=EmbeddingLevel.CHUNK,
                        ref_id=neighbour.id,
                        contract_id=neighbour.contract_id,
                        project_id=neighbour.project_id,
                        text=neighbour.text_content,
                        # Deliberately below the hit that pulled it in: context, not
                        # a result in its own right.
                        score=item.score * 0.5,
                        source="neighbour",
                        section_title=neighbour.section_title,
                        clause_number=neighbour.clause_number,
                        page_start=neighbour.page_start,
                        page_end=neighbour.page_end,
                        bounding_boxes=list(neighbour.bounding_boxes or []),
                        chunk_id=neighbour.id,
                        contract_title=item.contract_title,
                    )
                )
        return expanded

    async def _expand_graph(self, plan: RetrievalPlan, result: RetrievalResult) -> list[Evidence]:
        """Follow knowledge-graph edges from the clauses already retrieved.

        Traversal stays inside one project on every hop. A graph walk that crossed the
        boundary would be the easiest possible way to leak another project's terms,
        so the project is re-asserted per edge rather than assumed from the seed.
        """
        repository = KnowledgeRelationshipRepository(self.db)
        related: list[Evidence] = []
        seen_refs: set[str] = set()

        for item in result.by_level(EmbeddingLevel.CLAUSE)[:10]:
            if not item.clause_number:
                continue
            edges = await repository.neighbours(
                item.project_id, node_ref=item.clause_number, limit=plan.graph_depth * 5
            )
            for edge in edges:
                target = (
                    edge.target_ref if edge.source_ref == item.clause_number else edge.source_ref
                )
                if not target or target in seen_refs:
                    continue
                seen_refs.add(target)
                clause = (
                    (
                        await self.db.execute(
                            select(Clause).where(
                                Clause.project_id == item.project_id,
                                Clause.contract_id == item.contract_id,
                                Clause.clause_number == target,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if clause is None:
                    continue
                related.append(
                    Evidence(
                        level=EmbeddingLevel.CLAUSE,
                        ref_id=clause.id,
                        contract_id=clause.contract_id,
                        project_id=clause.project_id,
                        text=clause.text_content,
                        score=item.score * 0.4,
                        source="graph",
                        clause_type=clause.clause_type,
                        clause_number=clause.clause_number,
                        section_title=clause.section_title,
                        page_start=clause.page_start,
                        page_end=clause.page_end,
                        bounding_boxes=list(clause.bounding_boxes or []),
                        contract_title=item.contract_title,
                        metadata={"via": edge.relation.value, "from": item.clause_number},
                    )
                )
        return related

    # =========================================================================
    # Finalisation
    # =========================================================================
    @staticmethod
    def _deduplicate(evidence: list[Evidence]) -> list[Evidence]:
        """Collapse duplicates, keeping the highest-scoring occurrence.

        The same chunk legitimately arrives from vector search, keyword search and
        neighbour expansion. Presenting it three times would waste context budget and
        make the answer look better-evidenced than it is.
        """
        best: dict[tuple[str, str], Evidence] = {}
        for item in evidence:
            existing = best.get(item.key)
            if existing is None or item.score > existing.score:
                # Keep the stronger provenance: a real hit outranks a neighbour.
                if existing is not None and existing.source in {"vector", "keyword", "fused"}:
                    item.source = existing.source
                best[item.key] = item

        ordered = sorted(best.values(), key=lambda item: -item.score)
        for index, item in enumerate(ordered, start=1):
            item.rank = index
        return ordered


# =============================================================================
# Fusion
# =============================================================================
def _fuse(
    vector_hits: list[Evidence], keyword_hits: list[Evidence], *, limit: int
) -> list[Evidence]:
    """Reciprocal rank fusion of the two legs.

    RRF combines *rankings* rather than scores, which is the point: a cosine
    similarity and a ``ts_rank`` are not on the same scale, and normalising them
    against each other would be inventing a relationship that does not exist. RRF
    only needs each list to be ordered.
    """
    scores: dict[tuple[str, str], float] = {}
    items: dict[tuple[str, str], Evidence] = {}

    for leg in (vector_hits, keyword_hits):
        for rank, item in enumerate(leg, start=1):
            key = item.key
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            # Keep the richer record: a hydrated vector hit carries coordinates that
            # a keyword row may not.
            if key not in items or (not items[key].bounding_boxes and item.bounding_boxes):
                items[key] = item

    # Computed once, not per iteration: an item found by *both* legs is the strongest
    # signal hybrid search produces, and it is worth labelling as such.
    in_both = {item.key for item in vector_hits} & {item.key for item in keyword_hits}

    fused: list[Evidence] = []
    for key, score in sorted(scores.items(), key=lambda entry: -entry[1])[:limit]:
        item = items[key]
        item.score = score
        if key in in_both:
            item.source = "fused"
        fused.append(item)

    for index, item in enumerate(fused, start=1):
        item.rank = index
    return fused


def _count_by_level(evidence: list[Evidence]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in evidence:
        counts[item.level.value] = counts.get(item.level.value, 0) + 1
    return counts


def _count_by_source(evidence: list[Evidence]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in evidence:
        counts[item.source] = counts.get(item.source, 0) + 1
    return counts


__all__ = ["Evidence", "RetrievalEngine", "RetrievalResult"]

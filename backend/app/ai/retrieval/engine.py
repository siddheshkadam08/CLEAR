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
from app.ai.retrieval.rerank import apply_reranker
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

#: Levels whose similarity means "this text can answer the question". L1 ranks
#: documents and is deliberately excluded - see ``RetrievalResult.answerable_similarity``.
_ANSWERING_LEVELS = frozenset({EmbeddingLevel.CLAUSE.value, EmbeddingLevel.CHUNK.value})

#: Shingle containment above which two passages are the same text. High enough
#: that a clause and its neighbour survive as separate evidence; low enough to
#: catch a clause against the chunk it was read from.
_NEAR_DUPLICATE_CONTAINMENT = 0.8


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
    #: Cosine similarity as the vector search reported it, before fusion turned
    #: ``score`` into a reciprocal-rank value. Kept because the two are not
    #: interchangeable: a user-facing "similarity 0.91" and the answer-level
    #: guardrail both mean the cosine figure, and an RRF score (~0.016) would be
    #: nonsense in either place.
    similarity: float | None = None
    #: Relevance as the re-ranker judged it, when one ran. A different measurement
    #: from ``similarity``, so it gets a different field.
    rerank_score: float | None = None

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
            "similarity": round(self.similarity, 6) if self.similarity is not None else None,
            "rerank_score": (
                round(self.rerank_score, 6) if self.rerank_score is not None else None
            ),
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
    #: Time spent re-ranking, when a re-ranker ran. Reported separately from
    #: ``duration_ms`` so a slow answer can be attributed to the right stage.
    rerank_ms: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: True when the metadata pre-filter matched more contracts than it can carry.
    #: The consequence is not cosmetic - see ``RetrievalEngine.retrieve``.
    truncated: bool = False
    #: Best cosine similarity per level, taken before fusion overwrote ``score``.
    #: Per level, not one number: an L1 document summary is long and topical and
    #: scores 0.55-0.70 against almost any question about that contract, so a
    #: single maximum would let a document that is merely *about* the subject
    #: vouch for clause evidence that scored far lower.
    top_similarity_by_level: dict[str, float] = field(default_factory=dict)

    @property
    def top_similarity(self) -> float:
        """Best similarity at any level. Reporting only - see ``answerable_similarity``."""
        return max(self.top_similarity_by_level.values(), default=0.0)

    @property
    def answerable_similarity(self) -> float:
        """Best similarity among the levels that can actually answer a question.

        L1 selects *documents*; it never contains the answer. Guardrails compare
        against this, not against ``top_similarity``.
        """
        return max(
            (
                score
                for level, score in self.top_similarity_by_level.items()
                if level in _ANSWERING_LEVELS
            ),
            default=0.0,
        )

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
            "rerank_ms": self.rerank_ms,
            "top_similarity": round(self.top_similarity, 4),
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
        candidates, metadata_rows, truncated = await self._metadata_prefilter(plan)
        result.metadata_rows = metadata_rows
        result.truncated = truncated

        if truncated:
            # The pre-filter did not narrow anything, so it must not pretend to.
            #
            # Carrying an arbitrary N contracts into `WHERE contract_id IN (...)`
            # would silently restrict every vector search to that slice - ordered
            # by risk score, which has nothing to do with the question - and
            # exclude the rest of the project without a word. The project boundary
            # is already enforced on every vector row, so dropping the list widens
            # the search back to what the user actually asked about.
            logger.info(
                "metadata_prefilter_not_narrowing",
                matched_at_least=len(candidates),
                cap=_MAX_CANDIDATE_CONTRACTS,
                detail="candidate list dropped; the project boundary bounds the scan",
            )
            result.warnings.append(
                f"More than {_MAX_CANDIDATE_CONTRACTS} contracts match this question, "
                "so results are ranked across the whole project rather than a "
                "pre-selected subset of it."
            )
            candidates = []

        result.candidate_contracts = candidates

        if plan.strategy is RetrievalStrategy.METADATA_ONLY:
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            metrics.retrieval_duration_seconds.labels(scope=plan.scope.value).observe(
                result.duration_ms / 1000
            )
            metrics.retrieval_strategy_total.labels(
                strategy=plan.strategy.value, intent=plan.intent.value
            ).inc()
            return result

        # `not truncated` is load-bearing: after a truncated pre-filter the candidate
        # list is emptied deliberately, and without this the engine would read that
        # as "the filters matched nothing" and return no results for a question that
        # matched thousands of contracts.
        if not candidates and not truncated and not plan.filters.is_empty:
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

            # Taken here, before fusion: `_fuse` replaces `score` with a
            # reciprocal-rank value, so this is the last point at which a cosine
            # similarity is still on the object.
            if vector_hits:
                level = budget.level.value
                result.top_similarity_by_level[level] = max(
                    result.top_similarity_by_level.get(level, 0.0),
                    max(item.score for item in vector_hits),
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

        # ---- 4. re-rank ------------------------------------------------------
        result.evidence = self._deduplicate(result.evidence)
        # After every path has contributed, so keyword-only hits and expansions
        # are named too. Fills only what is missing, and is a no-op when the
        # vector path already covered everything.
        await self._attach_titles(result.evidence)
        if plan.rerank and result.evidence:
            result.evidence, result.rerank_ms = await self._rerank(plan, result.evidence)

        # ---- 5. finalise -----------------------------------------------------
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
    ) -> tuple[list[uuid.UUID], list[dict[str, Any]], bool]:
        """Narrow to candidate contracts from the projection.

        Runs first for every strategy. Even when the answer needs document content,
        knowing the twelve contracts that can possibly be relevant turns the vector
        search from a repository-wide scan into a bounded one.

        Returns ``(contract_ids, rows, truncated)``. ``truncated`` is the important
        one: it says the filters matched more contracts than the cap, which means
        the returned ids are a *slice* and not the candidate set. The caller must
        not use a slice as an inclusion filter - see :meth:`retrieve`.
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
            # Tie-break so the slice is at least stable between identical queries.
            ContractMetadata.contract_id,
            # One past the cap, so "exactly at the cap" is distinguishable from
            # "more than the cap". Without the extra row the two look identical
            # and the truncation goes unnoticed.
        ).limit(_MAX_CANDIDATE_CONTRACTS + 1)

        rows = (await self.db.execute(stmt)).all()
        truncated = len(rows) > _MAX_CANDIDATE_CONTRACTS
        rows = rows[:_MAX_CANDIDATE_CONTRACTS]
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
        return contract_ids, payload, truncated

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
                # Kept alongside `score` because fusion overwrites the latter. This
                # is the number the guardrail and the reported source score mean.
                similarity=match.score,
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
        """Attach contract titles, so a citation names the document.

        Only fills the gaps. Hydration covers the vector path as it reads, but a
        keyword-only hit is built straight from `Chunk` and arrives with no title,
        as does anything expanded from one - so calling this again over the
        assembled set is what stops a cited source rendering with a blank
        document name next to siblings from the same contract that have one.
        """
        contract_ids = {item.contract_id for item in evidence if item.contract_title is None}
        if not contract_ids:
            return
        rows = (
            await self.db.execute(
                select(Contract.id, Contract.title).where(Contract.id.in_(contract_ids))
            )
        ).all()
        titles = {row.id: row.title for row in rows}
        for item in evidence:
            if item.contract_title is None:
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
        seeds = [
            item
            for item in result.by_level(EmbeddingLevel.CHUNK)[: self._settings.max_chunks]
            if item.chunk_id is not None
        ]
        if not seeds:
            return []

        # Two queries for the whole expansion, not two per chunk. This used to be a
        # loop of `get_scoped` + `neighbours` per hit - forty sequential round trips
        # for twenty chunks, all on the critical path, each holding a connection
        # from a pool of thirty.
        by_project: dict[uuid.UUID, list[uuid.UUID]] = {}
        for item in seeds:
            by_project.setdefault(item.project_id, []).append(item.chunk_id)  # type: ignore[arg-type]

        chunks_by_id: dict[uuid.UUID, Any] = {}
        neighbours_by_chunk: dict[uuid.UUID, list[Any]] = {}
        for project_id, chunk_ids in by_project.items():
            chunks = await repository.list_by_ids(chunk_ids, project_id)
            chunks_by_id.update({chunk.id: chunk for chunk in chunks})
            neighbours_by_chunk.update(
                await repository.neighbours_for_many(
                    chunks, project_id, window=plan.neighbour_window
                )
            )

        seen = {item.chunk_id for item in result.evidence if item.chunk_id}
        expanded: list[Evidence] = []

        for item in seeds:
            if item.chunk_id not in chunks_by_id:
                continue
            for neighbour in neighbours_by_chunk.get(item.chunk_id, []):
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
    # Re-ranking
    # =========================================================================
    async def _rerank(
        self, plan: RetrievalPlan, evidence: list[Evidence]
    ) -> tuple[list[Evidence], int]:
        """Re-order the primary hits, leaving expansion material behind them.

        Neighbours and graph hops are deliberately excluded from the re-ranked set.
        They were never retrieved on their own merit - they are there to give a hit
        the context it depends on - so scoring them for relevance would spend the
        budget on passages that are not candidates, and could float one above the
        hit that pulled it in.
        """
        primary = [item for item in evidence if item.source not in {"neighbour", "graph"}]
        expansion = [item for item in evidence if item.source in {"neighbour", "graph"}]
        if not primary:
            return evidence, 0

        ordered, duration_ms = await apply_reranker(
            plan.query,
            primary,
            # The ANN scan has already done the cheap narrowing; re-ranking is the
            # expensive read, so it sees a bounded list.
            limit=min(len(primary), self._settings.rerank_top_k),
        )
        kept = {item.key for item in ordered}
        # Anything the top-K cut dropped goes back at the end rather than being
        # discarded: the context assembler has its own budget and is entitled to
        # see the full retrieval, ordered worst-last.
        ordered.extend(item for item in primary if item.key not in kept)
        return ordered + expansion, duration_ms

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
        ordered = _suppress_near_duplicates(ordered)
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
    # The cosine similarity per key, so it survives the keyword record being the one
    # kept above. Without this, a passage found by both legs would come out of
    # fusion with no similarity at all and read as unscored downstream.
    similarities = {item.key: item.score for item in vector_hits}

    fused: list[Evidence] = []
    for key, score in sorted(scores.items(), key=lambda entry: -entry[1])[:limit]:
        item = items[key]
        item.score = score
        if item.similarity is None:
            item.similarity = similarities.get(key)
        if key in in_both:
            item.source = "fused"
        fused.append(item)

    for index, item in enumerate(fused, start=1):
        item.rank = index
    return fused


def _suppress_near_duplicates(evidence: list[Evidence]) -> list[Evidence]:
    """Drop passages that repeat text already admitted higher up.

    Identity dedup on ``(level, ref_id)`` is not enough. Chunking overlaps by
    design, and the same paragraph legitimately arrives as an L2 clause and again
    as the L3 chunk it was read from - different rows, different ids, the same
    words. Left alone, two or three of the eight context slots hold one paragraph:
    the evidence budget is spent on repetition, and the answer reads as though a
    term were corroborated by several sources when it has one.

    Shingle containment rather than equality, because the duplicates are rarely
    byte-identical - one copy usually carries a heading or a trailing sentence the
    other does not.
    """
    kept: list[Evidence] = []
    signatures: list[tuple[Evidence, frozenset[str]]] = []

    for item in evidence:
        shingles = _shingles(item.text)
        if shingles:
            duplicate = False
            for existing, existing_shingles in signatures:
                overlap = len(shingles & existing_shingles) / min(
                    len(shingles), len(existing_shingles)
                )
                if overlap >= _NEAR_DUPLICATE_CONTAINMENT:
                    # Keep the one already admitted - it scored higher, and if the
                    # shorter of the two is the survivor it is because it is the
                    # more precisely retrieved passage.
                    existing.metadata.setdefault("duplicates_suppressed", 0)
                    existing.metadata["duplicates_suppressed"] += 1
                    duplicate = True
                    break
            if duplicate:
                continue
            signatures.append((item, shingles))
        kept.append(item)

    return kept


def _shingles(text: str, *, size: int = 8) -> frozenset[str]:
    """Overlapping word n-grams, for containment comparison."""
    words = text.lower().split()
    if len(words) < size:
        # Too short to shingle: compared by exact text instead, which is the right
        # test for a one-line clause anyway.
        return frozenset({" ".join(words)}) if words else frozenset()
    return frozenset(
        " ".join(words[index : index + size]) for index in range(len(words) - size + 1)
    )


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

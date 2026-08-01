"""Neighbour expansion must not scale its query count with the result count.

It used to: ``_expand_neighbours`` awaited ``get_scoped`` and then ``neighbours``
once per retrieved chunk, so twenty hits meant forty sequential round trips on the
critical path of every question, each holding a connection from a pool of thirty.
That put the concurrency ceiling in the low hundreds of users for work that is two
queries.

The assertion is on the *number of calls*, not on latency: a timing test would be
flaky, and the call count is the thing that actually caused the problem.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.ai.retrieval.engine import Evidence, RetrievalEngine, RetrievalResult
from app.ai.retrieval.planner import RetrievalPlan
from app.core.enums import EmbeddingLevel, QueryIntent, RetrievalStrategy, SearchMode, SearchScope

PROJECT = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONTRACT = uuid.UUID("22222222-2222-2222-2222-222222222222")


class _Chunk:
    """The handful of attributes expansion reads off a chunk row."""

    def __init__(self, index: int) -> None:
        self.id = uuid.UUID(int=index)
        self.contract_id = CONTRACT
        self.project_id = PROJECT
        self.version = 1
        self.reading_order = index
        self.text_content = f"chunk {index}"
        self.section_title = None
        self.clause_number = None
        self.page_start = 1
        self.page_end = 1
        self.bounding_boxes: list[dict[str, Any]] = []


class _CountingRepository:
    """Records how many round trips expansion actually makes."""

    def __init__(self, chunks: dict[uuid.UUID, _Chunk]) -> None:
        self._chunks = chunks
        self.list_calls = 0
        self.neighbour_calls = 0

    async def list_by_ids(self, chunk_ids, project_id):
        self.list_calls += 1
        return [self._chunks[cid] for cid in chunk_ids if cid in self._chunks]

    async def neighbours_for_many(self, chunks, project_id, *, window=1):
        self.neighbour_calls += 1
        result = {}
        for chunk in chunks:
            found = []
            for offset in range(-window, window + 1):
                neighbour = self._chunks.get(uuid.UUID(int=chunk.reading_order + offset))
                if neighbour is not None and offset != 0:
                    found.append(neighbour)
            result[chunk.id] = found
        return result


def _plan() -> RetrievalPlan:
    return RetrievalPlan(
        query="q",
        intent=QueryIntent.GENERAL_QA,
        strategy=RetrievalStrategy.HYBRID,
        scope=SearchScope.PROJECT,
        mode=SearchMode.HYBRID,
        project_ids=[PROJECT],
        neighbour_window=1,
    )


def _result(count: int) -> RetrievalResult:
    evidence = []
    for index in range(1, count + 1):
        item = Evidence(
            level=EmbeddingLevel.CHUNK,
            ref_id=uuid.UUID(int=index),
            contract_id=CONTRACT,
            project_id=PROJECT,
            text=f"chunk {index}",
            score=0.9,
            similarity=0.9,
        )
        item.chunk_id = item.ref_id
        evidence.append(item)
    return RetrievalResult(evidence=evidence)


class TestExpansionIsBatched:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("hits", [1, 5, 20])
    async def test_the_query_count_does_not_grow_with_the_hit_count(self, hits: int) -> None:
        chunks = {uuid.UUID(int=n): _Chunk(n) for n in range(0, hits + 3)}
        repository = _CountingRepository(chunks)
        engine = RetrievalEngine.__new__(RetrievalEngine)
        engine.db = None  # type: ignore[assignment] - the fake repository ignores it
        engine._settings = type("S", (), {"max_chunks": 40})()  # type: ignore[attr-defined]

        import app.ai.retrieval.engine as engine_module

        original = engine_module.ChunkRepository
        engine_module.ChunkRepository = lambda _db: repository  # type: ignore[assignment,misc]
        try:
            expanded = await engine._expand_neighbours(_plan(), _result(hits))
        finally:
            engine_module.ChunkRepository = original  # type: ignore[assignment]

        # One project in play, so one call of each kind - whatever the hit count.
        assert repository.list_calls == 1
        assert repository.neighbour_calls == 1
        assert expanded, "expansion must still return neighbours"

    @pytest.mark.asyncio
    async def test_neighbours_are_not_duplicated_across_adjacent_hits(self) -> None:
        """Two adjacent hits share a neighbour; it must be admitted once."""
        chunks = {uuid.UUID(int=n): _Chunk(n) for n in range(0, 6)}
        repository = _CountingRepository(chunks)
        engine = RetrievalEngine.__new__(RetrievalEngine)
        engine.db = None  # type: ignore[assignment] - the fake repository ignores it
        engine._settings = type("S", (), {"max_chunks": 40})()  # type: ignore[attr-defined]

        import app.ai.retrieval.engine as engine_module

        original = engine_module.ChunkRepository
        engine_module.ChunkRepository = lambda _db: repository  # type: ignore[assignment,misc]
        try:
            expanded = await engine._expand_neighbours(_plan(), _result(3))
        finally:
            engine_module.ChunkRepository = original  # type: ignore[assignment]

        ids = [item.ref_id for item in expanded]
        assert len(ids) == len(set(ids))

    @pytest.mark.asyncio
    async def test_nothing_to_expand_makes_no_queries(self) -> None:
        repository = _CountingRepository({})
        engine = RetrievalEngine.__new__(RetrievalEngine)
        engine.db = None  # type: ignore[assignment] - the fake repository ignores it
        engine._settings = type("S", (), {"max_chunks": 40})()  # type: ignore[attr-defined]

        import app.ai.retrieval.engine as engine_module

        original = engine_module.ChunkRepository
        engine_module.ChunkRepository = lambda _db: repository  # type: ignore[assignment,misc]
        try:
            expanded = await engine._expand_neighbours(_plan(), RetrievalResult())
        finally:
            engine_module.ChunkRepository = original  # type: ignore[assignment]

        assert expanded == []
        assert repository.list_calls == 0
        assert repository.neighbour_calls == 0

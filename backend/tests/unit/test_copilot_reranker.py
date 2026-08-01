"""Re-ranking, and the ways it is allowed to fail.

Re-ranking changes the *order* of evidence and nothing else. Every failure path
here asserts the same invariant from a different angle: whatever goes wrong, the
answer still gets the evidence retrieval found. A re-ranker that is down, slow, or
returns nonsense must degrade to similarity ordering - refusing a question that
had good evidence behind it would be a worse outcome than a suboptimal order.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.ai.rag.providers import InferenceResult, StructuredResult
from app.ai.retrieval.context import ContextAssembler
from app.ai.retrieval.engine import Evidence, RetrievalResult
from app.ai.retrieval.rerank import LLMReranker, NoopReranker, get_reranker
from app.core.enums import EmbeddingLevel, QueryIntent

CONTRACT = uuid.UUID("22222222-2222-2222-2222-222222222222")
PROJECT = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _evidence(n: int, *, score: float, text: str = "") -> Evidence:
    return Evidence(
        level=EmbeddingLevel.CHUNK,
        ref_id=uuid.UUID(int=n),
        contract_id=CONTRACT,
        project_id=PROJECT,
        text=text or f"passage {n}",
        score=score,
        similarity=score,
        rank=n,
    )


class _Provider:
    def __init__(self, data: dict[str, Any] | None = None, *, error: Exception | None = None):
        self._data = data or {}
        self._error = error

    async def generate_structured(self, **_: Any) -> StructuredResult:
        if self._error is not None:
            raise self._error
        return StructuredResult(data=self._data, inference=InferenceResult(text="", model="m"))


class TestNoop:
    @pytest.mark.asyncio
    async def test_it_keeps_retrieval_order(self) -> None:
        items = [_evidence(1, score=0.9), _evidence(2, score=0.8), _evidence(3, score=0.7)]

        ordered = await NoopReranker().rerank("q", items, limit=10)

        assert [item.ref_id for item in ordered] == [item.ref_id for item in items]

    @pytest.mark.asyncio
    async def test_it_still_honours_the_limit(self) -> None:
        items = [_evidence(n, score=0.9) for n in range(1, 6)]

        assert len(await NoopReranker().rerank("q", items, limit=2)) == 2


class TestLLMReranker:
    @pytest.mark.asyncio
    async def test_it_reorders_by_judged_relevance(self) -> None:
        items = [_evidence(1, score=0.9), _evidence(2, score=0.8), _evidence(3, score=0.7)]
        provider = _Provider(
            {
                "ranking": [
                    {"index": 2, "score": 0.95},
                    {"index": 0, "score": 0.4},
                    {"index": 1, "score": 0.1},
                ]
            }
        )

        ordered = await LLMReranker(provider).rerank("q", items, limit=10)  # type: ignore[arg-type]

        assert [item.ref_id for item in ordered] == [
            uuid.UUID(int=3),
            uuid.UUID(int=1),
            uuid.UUID(int=2),
        ]

    @pytest.mark.asyncio
    async def test_it_truncates_to_the_limit(self) -> None:
        items = [_evidence(n, score=0.9) for n in range(1, 6)]
        provider = _Provider({"ranking": [{"index": n, "score": 1.0 - n / 10} for n in range(5)]})

        ordered = await LLMReranker(provider).rerank("q", items, limit=2)  # type: ignore[arg-type]

        assert len(ordered) == 2

    @pytest.mark.asyncio
    async def test_the_similarity_survives_re_ranking(self) -> None:
        """The two are different measurements, and the guardrail means the first one."""
        items = [_evidence(1, score=0.91), _evidence(2, score=0.55)]
        provider = _Provider({"ranking": [{"index": 1, "score": 0.99}, {"index": 0, "score": 0.2}]})

        ordered = await LLMReranker(provider).rerank("q", items, limit=10)  # type: ignore[arg-type]

        assert ordered[0].similarity == pytest.approx(0.55)
        assert ordered[0].rerank_score == pytest.approx(0.99)

    @pytest.mark.asyncio
    async def test_a_provider_failure_falls_back_to_similarity_order(self) -> None:
        items = [_evidence(1, score=0.9), _evidence(2, score=0.8)]

        ordered = await LLMReranker(_Provider(error=RuntimeError("down"))).rerank(  # type: ignore[arg-type]
            "q", items, limit=10
        )

        assert [item.ref_id for item in ordered] == [item.ref_id for item in items]

    @pytest.mark.asyncio
    async def test_an_unscored_passage_is_kept_not_dropped(self) -> None:
        """A skipped index means "not judged", not "irrelevant"."""
        items = [_evidence(1, score=0.9), _evidence(2, score=0.8), _evidence(3, score=0.7)]
        provider = _Provider({"ranking": [{"index": 1, "score": 0.9}]})

        ordered = await LLMReranker(provider).rerank("q", items, limit=10)  # type: ignore[arg-type]

        assert len(ordered) == 3
        assert ordered[0].ref_id == uuid.UUID(int=2)

    @pytest.mark.asyncio
    async def test_an_out_of_range_index_is_ignored(self) -> None:
        items = [_evidence(1, score=0.9)]
        provider = _Provider({"ranking": [{"index": 99, "score": 1.0}]})

        ordered = await LLMReranker(provider).rerank("q", items, limit=10)  # type: ignore[arg-type]

        assert [item.ref_id for item in ordered] == [uuid.UUID(int=1)]

    @pytest.mark.asyncio
    async def test_a_malformed_response_leaves_the_order_alone(self) -> None:
        items = [_evidence(1, score=0.9), _evidence(2, score=0.8)]
        provider = _Provider({"ranking": "not a list"})

        ordered = await LLMReranker(provider).rerank("q", items, limit=10)  # type: ignore[arg-type]

        assert [item.ref_id for item in ordered] == [item.ref_id for item in items]

    @pytest.mark.asyncio
    async def test_a_single_candidate_is_not_sent_to_the_model(self) -> None:
        """There is nothing to rank, so paying for a call would be pure waste."""
        provider = _Provider(error=AssertionError("the model must not be called"))

        ordered = await LLMReranker(provider).rerank("q", [_evidence(1, score=0.9)], limit=10)  # type: ignore[arg-type]

        assert len(ordered) == 1


class TestSelection:
    def test_it_is_off_unless_configured_on(self, settings_env) -> None:
        settings_env(RERANKER_ENABLED="false")

        assert isinstance(get_reranker(), NoopReranker)

    def test_enabling_it_selects_the_llm_reranker(self, settings_env) -> None:
        settings_env(RERANKER_ENABLED="true")

        assert isinstance(get_reranker(), LLMReranker)


class TestContextAssemblyRespectsIt:
    def test_the_assembler_does_not_undo_the_re_ranking(self) -> None:
        """The assembler used to re-sort by score, which reversed the re-ranker."""
        # Scores are ascending on purpose: sorting by score would flip this order.
        ordered = [_evidence(1, score=0.10), _evidence(2, score=0.50), _evidence(3, score=0.90)]
        result = RetrievalResult(evidence=ordered)

        package = ContextAssembler(budget_tokens=100_000).assemble(
            query="q", intent=QueryIntent.GENERAL_QA, retrieval=result
        )

        assert [citation.ref_id for citation in package.citations] == [
            uuid.UUID(int=1),
            uuid.UUID(int=2),
            uuid.UUID(int=3),
        ]

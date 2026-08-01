"""The Copilot pipeline: the guardrail, the retrieval mode, and the sources.

The guardrail is the test that matters most here. A model handed weak evidence
and a contract question will produce a fluent answer from its own knowledge of
contracts, and that answer will look exactly like a grounded one. So the assertion
is not only that the right sentence comes back - it is that **the provider is
never called at all**.
"""

from __future__ import annotations

import uuid

import pytest

from app.ai.rag.engine import Answer
from app.ai.rag.providers import TokenUsage
from app.ai.retrieval.analysis import QueryAnalysis
from app.ai.retrieval.context import Citation, ContextPackage
from app.ai.retrieval.engine import Evidence, RetrievalResult
from app.core.enums import ConfidenceBand, EmbeddingLevel, QueryIntent
from app.core.errors import ProviderError
from app.services.copilot import (
    INSUFFICIENT_CONTEXT,
    MODE_CONTRACT,
    MODE_DOCUMENT_TYPE,
    MODE_UNFILTERED,
    CopilotService,
)

PROJECT = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONTRACT = uuid.UUID("22222222-2222-2222-2222-222222222222")


# =============================================================================
# Doubles
# =============================================================================
class _Retrieval:
    """Returns a canned retrieval result and records the plan it was given."""

    def __init__(self, result: RetrievalResult) -> None:
        self._result = result
        self.plan = None

    async def retrieve(self, plan):
        self.plan = plan
        self._result.duration_ms = 1
        return self._result


class _Analysis:
    def __init__(self, analysis: QueryAnalysis) -> None:
        self._analysis = analysis

    async def analyse(self, query: str, *, doc_types: list[str]) -> QueryAnalysis:
        return self._analysis


class _RAG:
    """Fails loudly if asked to generate. The guardrail tests rely on this."""

    def __init__(self, answer: Answer | None = None) -> None:
        self._answer = answer
        self.calls = 0

    async def answer(self, package, **_):
        self.calls += 1
        if self._answer is None:
            raise AssertionError("the model must not be called without sufficient context")
        return self._answer


def _evidence(score: float) -> Evidence:
    return Evidence(
        level=EmbeddingLevel.CHUNK,
        ref_id=uuid.uuid4(),
        contract_id=CONTRACT,
        project_id=PROJECT,
        text="Either party may terminate on thirty days written notice.",
        score=score,
        similarity=score,
    )


def _service(
    *,
    top_similarity: float,
    evidence: list[Evidence] | None = None,
    analysis: QueryAnalysis | None = None,
    rag: _RAG | None = None,
    document_type=None,
    document_similarity: float | None = None,
) -> tuple[CopilotService, _RAG, _Retrieval]:
    by_level = {EmbeddingLevel.CHUNK.value: top_similarity}
    if document_similarity is not None:
        by_level[EmbeddingLevel.DOCUMENT_SUMMARY.value] = document_similarity
    result = RetrievalResult(
        evidence=evidence if evidence is not None else [_evidence(top_similarity)],
        top_similarity_by_level=by_level,
    )
    retrieval = _Retrieval(result)
    engine = rag or _RAG()
    service = CopilotService(
        db=None,  # type: ignore[arg-type] - no query runs in these tests
        analysis_service=_Analysis(analysis or QueryAnalysis()),  # type: ignore[arg-type]
        retrieval_engine=retrieval,  # type: ignore[arg-type]
        rag_engine=engine,  # type: ignore[arg-type]
    )
    # The taxonomy read and the contract load both need a session; neither is
    # under test here, so they are stubbed at the seam rather than mocked deeper.
    service._document_type = _stub_document_type(analysis or QueryAnalysis(), document_type)  # type: ignore[assignment]
    return service, engine, retrieval


def _stub_document_type(analysis: QueryAnalysis, document_type):
    async def resolve(_query, _contract):
        return analysis, document_type

    return resolve


def _answer(text: str = "Thirty days written notice. [1]") -> Answer:
    return Answer(
        text=text,
        citations=[
            Citation(
                label=1,
                contract_id=CONTRACT,
                contract_title="Acme Master Services Agreement",
                level="chunk",
                ref_id=uuid.uuid4(),
                text="Either party may terminate on thirty days written notice.",
                clause_type="termination_for_convenience",
                clause_number="12.3",
                section_title="Termination",
                page_start=18,
                score=0.0164,
                similarity=0.91,
            )
        ],
        confidence=0.82,
        confidence_band=ConfidenceBand.HIGH,
        model="test-model",
        usage=TokenUsage(),
    )


# =============================================================================
# Tests
# =============================================================================
class TestGuardrail:
    @pytest.mark.asyncio
    async def test_weak_evidence_returns_the_fixed_sentence(self) -> None:
        service, _, _ = _service(top_similarity=0.20)

        result = await service.answer("What is the notice period?", project_ids=[PROJECT])

        assert result.answer == INSUFFICIENT_CONTEXT
        assert result.insufficient_context is True
        assert result.sources == []

    @pytest.mark.asyncio
    async def test_weak_evidence_never_reaches_the_model(self) -> None:
        service, rag, _ = _service(top_similarity=0.20)

        await service.answer("What is the notice period?", project_ids=[PROJECT])

        assert rag.calls == 0

    @pytest.mark.asyncio
    async def test_no_evidence_at_all_is_the_same_outcome(self) -> None:
        service, rag, _ = _service(top_similarity=0.0, evidence=[])

        result = await service.answer("Anything", project_ids=[PROJECT])

        assert result.insufficient_context is True
        assert rag.calls == 0

    @pytest.mark.asyncio
    async def test_a_strong_document_summary_cannot_vouch_for_weak_clauses(self) -> None:
        """L1 selects documents; it never contains the answer.

        A document summary is long and topical and scores 0.55-0.70 against almost
        any question about that contract. Comparing the overall maximum against the
        threshold let it clear the bar while the best clause scored 0.31 - exactly
        the case the guardrail exists to catch.
        """
        service, rag, _ = _service(top_similarity=0.31, document_similarity=0.68)

        result = await service.answer("What is the notice period?", project_ids=[PROJECT])

        assert result.insufficient_context is True
        assert rag.calls == 0

    @pytest.mark.asyncio
    async def test_the_reported_similarity_matches_what_was_judged(self) -> None:
        """Reporting the overall maximum would show a figure above the threshold
        next to a message saying the threshold was not met."""
        service, _, _ = _service(top_similarity=0.31, document_similarity=0.68)

        result = await service.answer("What is the notice period?", project_ids=[PROJECT])

        assert result.top_similarity == pytest.approx(0.31)

    @pytest.mark.asyncio
    async def test_evidence_above_the_threshold_is_answered(self) -> None:
        service, rag, _ = _service(top_similarity=0.91, rag=_RAG(_answer()))

        result = await service.answer("What is the notice period?", project_ids=[PROJECT])

        assert result.insufficient_context is False
        assert rag.calls == 1
        assert "thirty days" in result.answer.lower()

    @pytest.mark.asyncio
    async def test_the_threshold_is_configurable(self, settings_env) -> None:
        settings_env(COPILOT_SIMILARITY_THRESHOLD="0.10")
        service, rag, _ = _service(top_similarity=0.20, rag=_RAG(_answer()))

        result = await service.answer("What is the notice period?", project_ids=[PROJECT])

        assert result.insufficient_context is False
        assert rag.calls == 1

    @pytest.mark.asyncio
    async def test_the_guardrail_answer_still_explains_the_search(self) -> None:
        """A refusal with no context is indistinguishable from a broken search."""
        service, _, _ = _service(top_similarity=0.20)

        result = await service.answer("What is the notice period?", project_ids=[PROJECT])

        assert result.retrieval_mode in {MODE_UNFILTERED, MODE_CONTRACT, MODE_DOCUMENT_TYPE}
        assert result.top_similarity == pytest.approx(0.20)
        assert result.plan is not None


class TestRetrievalMode:
    @pytest.mark.asyncio
    async def test_an_applied_type_filter_is_reported(self) -> None:
        from app.ai.docpipeline.mapping import ResolvedDocumentType

        service, _, _ = _service(
            top_similarity=0.91,
            rag=_RAG(_answer()),
            analysis=QueryAnalysis(document_type="MSA", confidence=0.94, method="llm"),
            document_type=ResolvedDocumentType(label="MSA", agreement_type="msa"),
        )

        result = await service.answer(
            "What is the notice period in the MSA?", project_ids=[PROJECT]
        )

        assert result.retrieval_mode == MODE_DOCUMENT_TYPE
        assert result.document_type_detected is True
        assert result.document_type == "MSA"
        assert result.document_type_confidence == pytest.approx(0.94)

    @pytest.mark.asyncio
    async def test_a_detected_but_discarded_type_is_not_reported_as_detected(self) -> None:
        """It narrowed nothing, so saying it was detected would describe a search
        that did not happen."""
        service, _, _ = _service(
            top_similarity=0.91,
            rag=_RAG(_answer()),
            analysis=QueryAnalysis(document_type="MSA", confidence=0.40, method="llm"),
            document_type=None,
        )

        result = await service.answer("What is the notice period?", project_ids=[PROJECT])

        assert result.retrieval_mode == MODE_UNFILTERED
        assert result.document_type_detected is False

    @pytest.mark.asyncio
    async def test_the_copilot_never_plans_a_metadata_only_search(self) -> None:
        service, _, retrieval = _service(top_similarity=0.91, rag=_RAG(_answer()))

        await service.answer("What is the notice period?", project_ids=[PROJECT])

        assert retrieval.plan is not None
        assert retrieval.plan.levels, "a plan with no vector levels can never be answered"

    @pytest.mark.asyncio
    async def test_the_configured_top_k_reaches_the_plan(self, settings_env) -> None:
        settings_env(COPILOT_TOP_K="30")
        service, _, retrieval = _service(top_similarity=0.91, rag=_RAG(_answer()))

        await service.answer("What is the notice period?", project_ids=[PROJECT])

        assert retrieval.plan is not None
        assert {budget.limit for budget in retrieval.plan.levels} == {30}


class TestSources:
    @pytest.mark.asyncio
    async def test_every_field_of_a_source_is_mapped(self) -> None:
        service, _, _ = _service(top_similarity=0.91, rag=_RAG(_answer()))

        result = await service.answer("What is the notice period?", project_ids=[PROJECT])

        source = result.sources[0]
        assert source.contract_name == "Acme Master Services Agreement"
        assert source.clause_heading == "Termination"
        assert source.section_number == "12.3"
        assert source.page_number == 18
        assert source.similarity_score == pytest.approx(0.91)

    @pytest.mark.asyncio
    async def test_the_reported_score_is_the_similarity_not_the_fusion_score(self) -> None:
        """After hybrid fusion `score` is a reciprocal-rank value around 0.016.
        Reporting it under a "similarity" label would be a wrong number."""
        service, _, _ = _service(top_similarity=0.91, rag=_RAG(_answer()))

        result = await service.answer("What is the notice period?", project_ids=[PROJECT])

        assert result.sources[0].similarity_score is not None
        assert result.sources[0].similarity_score > 0.5

    @pytest.mark.asyncio
    async def test_a_keyword_hit_reports_no_similarity_rather_than_zero(self) -> None:
        """A keyword match has no cosine similarity. "Similarity 0.00" beside what
        may be the best exact-phrase match reads as the opposite of the truth."""
        answer = _answer()
        answer.citations[0].similarity = None
        answer.citations[0].source = "keyword"

        service, _, _ = _service(top_similarity=0.91, rag=_RAG(answer))
        result = await service.answer("q", project_ids=[PROJECT])

        assert result.sources[0].similarity_score is None
        assert result.sources[0].match_type == "keyword"


class TestDegradation:
    @pytest.mark.asyncio
    async def test_a_provider_failure_returns_the_evidence_not_a_500(self) -> None:
        """Retrieval already ran and already cost money. Handing back the passages
        it found is far more use to a reviewer than an error page."""

        class _Failing(_RAG):
            async def answer(self, package, **_):
                self.calls += 1
                raise ProviderError("upstream is rate limited")

        service, rag, _ = _service(top_similarity=0.91, rag=_Failing())

        result = await service.answer("What is the notice period?", project_ids=[PROJECT])

        assert rag.calls == 1
        assert result.generation_failed is True
        assert result.insufficient_context is False
        assert result.sources, "the retrieved passages must survive a generation failure"
        assert result.needs_review is True

    @pytest.mark.asyncio
    async def test_the_degraded_message_is_distinct_from_the_guardrail(self) -> None:
        """ "We found nothing" and "we found this and could not summarise it" are
        different facts, and the sources only make sense against the second."""

        class _Failing(_RAG):
            async def answer(self, package, **_):
                raise ProviderError("down")

        service, _, _ = _service(top_similarity=0.91, rag=_Failing())
        result = await service.answer("q", project_ids=[PROJECT])

        assert result.answer != INSUFFICIENT_CONTEXT


class TestRelaxedRetry:
    @pytest.mark.asyncio
    async def test_an_empty_type_filtered_search_is_retried_unfiltered(self) -> None:
        """ "The classifier picked the wrong type" and "the answer is not in the
        corpus" are indistinguishable from here, and only one is worth reporting."""
        from app.ai.docpipeline.mapping import ResolvedDocumentType

        attempts: list[list[str]] = []

        class _EmptyThenFound(_Retrieval):
            async def retrieve(self, plan):
                attempts.append(list(plan.filters.agreement_types))
                self.plan = plan
                if len(attempts) == 1:
                    return RetrievalResult(evidence=[], top_similarity_by_level={})
                return RetrievalResult(
                    evidence=[_evidence(0.91)],
                    top_similarity_by_level={EmbeddingLevel.CHUNK.value: 0.91},
                )

        service = CopilotService(
            db=None,  # type: ignore[arg-type]
            retrieval_engine=_EmptyThenFound(RetrievalResult()),  # type: ignore[arg-type]
            rag_engine=_RAG(_answer()),  # type: ignore[arg-type]
        )
        analysis = QueryAnalysis(document_type="MSA", confidence=0.94, method="llm")
        service._document_type = _stub_document_type(  # type: ignore[assignment]
            analysis, ResolvedDocumentType(label="MSA", agreement_type="msa")
        )

        result = await service.answer("What is the notice period?", project_ids=[PROJECT])

        assert attempts == [["msa"], []], "the second attempt must drop the type filter"
        assert result.relaxed_filters is True
        assert result.insufficient_context is False

    @pytest.mark.asyncio
    async def test_it_does_not_retry_when_there_was_no_filter(self) -> None:
        """Otherwise every genuinely empty search costs two retrievals."""
        attempts: list[int] = []

        class _AlwaysEmpty(_Retrieval):
            async def retrieve(self, plan):
                attempts.append(1)
                self.plan = plan
                return RetrievalResult(evidence=[], top_similarity_by_level={})

        service = CopilotService(
            db=None,  # type: ignore[arg-type]
            analysis_service=_Analysis(QueryAnalysis()),  # type: ignore[arg-type]
            retrieval_engine=_AlwaysEmpty(RetrievalResult()),  # type: ignore[arg-type]
            rag_engine=_RAG(),  # type: ignore[arg-type]
        )
        service._document_type = _stub_document_type(QueryAnalysis(), None)  # type: ignore[assignment]

        result = await service.answer("q", project_ids=[PROJECT])

        assert len(attempts) == 1
        assert result.insufficient_context is True

    @pytest.mark.asyncio
    async def test_a_clause_type_stands_in_for_a_missing_heading(self) -> None:
        answer = _answer()
        answer.citations[0].section_title = None

        service, _, _ = _service(top_similarity=0.91, rag=_RAG(answer))
        result = await service.answer("q", project_ids=[PROJECT])

        assert result.sources[0].clause_heading == "Termination For Convenience"

    @pytest.mark.asyncio
    async def test_only_cited_passages_are_listed(self) -> None:
        """Listing everything retrieved would present passages the answer did not
        use as if they supported it."""
        answer = _answer()
        package = ContextPackage(query="q", intent=QueryIntent.CLAUSE_LOOKUP)
        package.citations = [answer.citations[0], _uncited()]

        assert len(CopilotService._sources(package, answer)) == 1


def _uncited() -> Citation:
    return Citation(
        label=2,
        contract_id=CONTRACT,
        contract_title="Another agreement",
        level="chunk",
        ref_id=uuid.uuid4(),
        text="Unrelated passage.",
        similarity=0.55,
    )

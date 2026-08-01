"""The query classifier, and what happens when it is wrong or unavailable.

The classifier's output is only ever used to *narrow* a search, so the failure
modes that matter are the ones that narrow it wrongly: a document type the
taxonomy does not contain, a confident-sounding score attached to no type at all,
and a provider that is down. Each of those must end with no filter rather than
with a guess, because a filter on the wrong type removes the answer from the
search entirely while no filter only makes it wider.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.ai.rag.providers import InferenceResult, StructuredResult
from app.ai.retrieval.analysis import QueryAnalysisService
from app.core.enums import QueryIntent

DOC_TYPES = ["MSA", "NDA", "License Agreement", "Addendum", "Others"]


class _Provider:
    """Records what it was asked and returns a canned classification."""

    def __init__(self, data: dict[str, Any] | None = None, *, error: Exception | None = None):
        self._data = data or {}
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(self, **kwargs: Any) -> StructuredResult:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return StructuredResult(
            data=self._data,
            inference=InferenceResult(text="", model="test-model"),
        )


class TestClassification:
    @pytest.mark.asyncio
    async def test_it_returns_the_intent_type_and_confidence(self) -> None:
        provider = _Provider({"intent": "clause_lookup", "documentType": "MSA", "confidence": 0.94})

        analysis = await QueryAnalysisService(provider).analyse(  # type: ignore[arg-type]
            "What is the notice period?", doc_types=DOC_TYPES
        )

        assert analysis.intent is QueryIntent.CLAUSE_LOOKUP
        assert analysis.document_type == "MSA"
        assert analysis.confidence == pytest.approx(0.94)
        assert analysis.method == "llm"

    @pytest.mark.asyncio
    async def test_it_runs_on_the_cheap_tier(self) -> None:
        """Classification precedes every question, so it must not cost what the answer does."""
        provider = _Provider({"intent": "general_qa", "documentType": None, "confidence": 0.0})

        await QueryAnalysisService(provider).analyse("anything", doc_types=DOC_TYPES)  # type: ignore[arg-type]

        assert provider.calls[0]["purpose"] == "planner"

    @pytest.mark.asyncio
    async def test_the_prompt_forbids_answering(self) -> None:
        """A model given a contract question will answer it unless told not to."""
        provider = _Provider({"intent": "general_qa", "documentType": None, "confidence": 0.0})

        await QueryAnalysisService(provider).analyse(  # type: ignore[arg-type]
            "What is the liability cap?", doc_types=DOC_TYPES
        )

        system = provider.calls[0]["system"].lower()
        assert "never answer" in system
        assert "do not answer" in system

    @pytest.mark.asyncio
    async def test_the_vocabulary_is_supplied_to_the_model(self) -> None:
        provider = _Provider({"intent": "general_qa", "documentType": None, "confidence": 0.0})

        await QueryAnalysisService(provider).analyse("anything", doc_types=DOC_TYPES)  # type: ignore[arg-type]

        prompt = provider.calls[0]["prompt"]
        for name in DOC_TYPES:
            assert name in prompt


class TestDegradation:
    @pytest.mark.asyncio
    async def test_a_provider_failure_is_not_a_failed_question(self) -> None:
        service = QueryAnalysisService(_Provider(error=RuntimeError("upstream is down")))  # type: ignore[arg-type]

        analysis = await service.analyse("What is the notice period?", doc_types=DOC_TYPES)

        assert analysis.method == "unavailable"
        assert analysis.confidence == 0.0
        assert analysis.document_type is None

    @pytest.mark.asyncio
    async def test_an_invented_document_type_is_discarded(self) -> None:
        """A label outside the supplied vocabulary is the model telling us it guessed."""
        provider = _Provider(
            {"intent": "clause_lookup", "documentType": "Vendor MSA", "confidence": 0.99}
        )

        analysis = await QueryAnalysisService(provider).analyse("anything", doc_types=DOC_TYPES)  # type: ignore[arg-type]

        assert analysis.document_type is None
        assert analysis.confidence == 0.0

    @pytest.mark.asyncio
    async def test_confidence_without_a_type_cannot_pass_a_threshold(self) -> None:
        """Otherwise a high score with a null label would gate a filter on nothing."""
        provider = _Provider({"intent": "general_qa", "documentType": None, "confidence": 0.97})

        analysis = await QueryAnalysisService(provider).analyse("anything", doc_types=DOC_TYPES)  # type: ignore[arg-type]

        assert analysis.confidence == 0.0

    @pytest.mark.asyncio
    async def test_an_unknown_intent_falls_back_rather_than_raising(self) -> None:
        provider = _Provider(
            {"intent": "what_even_is_this", "documentType": "NDA", "confidence": 0.8}
        )

        analysis = await QueryAnalysisService(provider).analyse("anything", doc_types=DOC_TYPES)  # type: ignore[arg-type]

        assert analysis.intent is QueryIntent.GENERAL_QA
        assert analysis.document_type == "NDA"

    @pytest.mark.asyncio
    async def test_an_out_of_range_confidence_is_clamped(self) -> None:
        provider = _Provider({"intent": "general_qa", "documentType": "NDA", "confidence": 4.2})

        analysis = await QueryAnalysisService(provider).analyse("anything", doc_types=DOC_TYPES)  # type: ignore[arg-type]

        assert analysis.confidence == 1.0

    @pytest.mark.asyncio
    async def test_an_empty_question_is_not_sent_to_the_model(self) -> None:
        provider = _Provider()

        analysis = await QueryAnalysisService(provider).analyse("   ", doc_types=DOC_TYPES)  # type: ignore[arg-type]

        assert analysis.method == "skipped"
        assert provider.calls == []

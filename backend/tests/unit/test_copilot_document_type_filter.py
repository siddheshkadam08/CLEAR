"""The document-type gate in the planner, and the resolver behind it.

The gate is one-way and the asymmetry is the whole design: filtering to the wrong
document type removes the answer from the search, while declining to filter only
leaves the search wider. Every test here pins one side of that - a confident,
resolvable type narrows retrieval; anything less narrows nothing.
"""

from __future__ import annotations

import uuid

import pytest

from app.ai.docpipeline import mapping, taxonomy
from app.ai.docpipeline.mapping import ResolvedDocumentType
from app.ai.retrieval.analysis import QueryAnalysis
from app.ai.retrieval.planner import RetrievalPlanner
from app.core.enums import AgreementType, QueryIntent, RetrievalStrategy

PROJECT = uuid.UUID("11111111-1111-1111-1111-111111111111")

MSA = ResolvedDocumentType(label="MSA", agreement_type=AgreementType.MSA.value)


def _plan(analysis: QueryAnalysis | None, document_type: ResolvedDocumentType | None, **kwargs):
    return RetrievalPlanner().plan(
        "What does the indemnity clause say?",
        project_ids=[PROJECT],
        analysis=analysis,
        document_type=document_type,
        **kwargs,
    )


class TestTheThreshold:
    def test_a_confident_type_becomes_a_filter(self) -> None:
        plan = _plan(QueryAnalysis(document_type="MSA", confidence=0.94, method="llm"), MSA)

        assert plan.filters.agreement_types == [AgreementType.MSA.value]
        # The vector rows carry `agreement_type` in `filter_metadata`, so the same
        # decision reaches the ANN scan and not just the metadata pre-filter.
        assert plan.filters.vector_metadata()["agreement_type"] == AgreementType.MSA.value

    def test_a_low_score_filters_nothing(self) -> None:
        plan = _plan(QueryAnalysis(document_type="MSA", confidence=0.40, method="llm"), MSA)

        assert plan.filters.agreement_types == []
        assert "agreement_type" not in plan.filters.vector_metadata()

    def test_a_low_score_is_explained_rather_than_silent(self) -> None:
        """A user whose search was deliberately widened should be able to see why."""
        plan = _plan(QueryAnalysis(document_type="MSA", confidence=0.40, method="llm"), MSA)

        reasoning = " ".join(plan.reasoning).lower()
        assert "0.40" in reasoning
        assert "threshold" in reasoning

    def test_the_threshold_is_configurable(self, settings_env) -> None:
        settings_env(COPILOT_DOCTYPE_CONFIDENCE_THRESHOLD="0.30")

        plan = _plan(QueryAnalysis(document_type="MSA", confidence=0.40, method="llm"), MSA)

        assert plan.filters.agreement_types == [AgreementType.MSA.value]

    def test_no_analysis_at_all_filters_nothing(self) -> None:
        assert _plan(None, None).filters.agreement_types == []

    def test_an_unresolved_label_filters_nothing_and_says_so(self) -> None:
        plan = _plan(QueryAnalysis(document_type="Vendor MSA", confidence=0.99), None)

        assert plan.filters.agreement_types == []
        assert "vendor msa" in " ".join(plan.reasoning).lower()

    def test_an_explicit_agreement_type_wins(self) -> None:
        """A filter the user chose is not something a classifier may overrule."""
        plan = _plan(
            QueryAnalysis(document_type="MSA", confidence=0.99, method="llm"),
            MSA,
            agreement_types=[AgreementType.NDA.value],
        )

        assert plan.filters.agreement_types == [AgreementType.NDA.value]


class TestIntentSeeding:
    def test_the_rules_keep_priority(self) -> None:
        """A matched pattern is evidence from the question's own wording."""
        plan = RetrievalPlanner().plan(
            "Compare the indemnities",
            project_ids=[PROJECT],
            analysis=QueryAnalysis(intent=QueryIntent.FINANCIAL, method="llm"),
        )

        assert plan.intent is QueryIntent.COMPARISON

    def test_the_classifier_decides_what_the_rules_did_not(self) -> None:
        plan = RetrievalPlanner().plan(
            "How does this affect us",
            project_ids=[PROJECT],
            analysis=QueryAnalysis(intent=QueryIntent.RISK_ASSESSMENT, method="llm"),
        )

        assert plan.intent is QueryIntent.RISK_ASSESSMENT
        assert plan.method == "llm+rules"


class TestTopK:
    def test_the_level_limit_overrides_every_per_level_ceiling(self) -> None:
        plan = _plan(None, None, level_limit=25)

        assert plan.levels
        assert [budget.limit for budget in plan.levels] == [25] * len(plan.levels)

    def test_without_it_the_configured_per_level_ceilings_stand(self, settings_env) -> None:
        settings = settings_env()
        plan = _plan(None, None)

        limits = {budget.level.value: budget.limit for budget in plan.levels}
        assert limits["clause"] == settings.retrieval.max_clauses


class TestPreferContent:
    """A question answered from document text must never plan a metadata-only search."""

    def test_a_timeline_question_still_retrieves_passages(self) -> None:
        """ "Notice period" reads as a timeline question and is a clause lookup."""
        plan = RetrievalPlanner().plan(
            "What is the notice period?", project_ids=[PROJECT], prefer_content=True
        )

        assert plan.strategy is not RetrievalStrategy.METADATA_ONLY
        assert plan.levels, "a Copilot question with no vector levels can never be answered"

    def test_without_it_the_planner_is_unchanged(self) -> None:
        """Search keeps answering "which contracts expire next quarter" from the projection."""
        plan = RetrievalPlanner().plan(
            "Which contracts expire next quarter?", project_ids=[PROJECT]
        )

        assert plan.strategy is RetrievalStrategy.METADATA_ONLY
        assert plan.levels == []


class TestResolver:
    """`resolve_document_type` against a stubbed taxonomy."""

    @pytest.fixture(autouse=True)
    def _taxonomy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def load(_db):
            return ["MSA", "NDA", "License Agreement", "Others"]

        monkeypatch.setattr(mapping, "load_doc_types", load)

    @pytest.mark.asyncio
    async def test_an_exact_label_resolves(self) -> None:
        resolved = await mapping.resolve_document_type(None, "MSA")  # type: ignore[arg-type]

        assert resolved is not None
        assert resolved.label == "MSA"
        assert resolved.agreement_type == AgreementType.MSA.value

    @pytest.mark.asyncio
    async def test_case_and_punctuation_do_not_decide_it(self) -> None:
        resolved = await mapping.resolve_document_type(None, "  license-agreement ")  # type: ignore[arg-type]

        assert resolved is not None
        assert resolved.label == "License Agreement"

    @pytest.mark.asyncio
    async def test_an_unknown_label_is_none_not_the_nearest_guess(self) -> None:
        assert await mapping.resolve_document_type(None, "Vendor MSA") is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_the_fallback_bucket_is_not_a_filter(self) -> None:
        """ "Others" is the taxonomy's "I could not tell", not a type to narrow to."""
        assert await mapping.resolve_document_type(None, "Others") is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_an_empty_label_resolves_to_nothing(self) -> None:
        assert await mapping.resolve_document_type(None, "") is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_an_empty_taxonomy_does_not_fail_the_question(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def empty(_db):
            raise LookupError("cip_docMapping holds no document types.")

        monkeypatch.setattr(mapping, "load_doc_types", empty)

        assert await mapping.resolve_document_type(None, "MSA") is None  # type: ignore[arg-type]

    def test_the_resolver_and_the_ingest_pipeline_agree(self) -> None:
        """Both sides must derive the same agreement type from one label.

        The filter only matches because ingest wrote the same value onto the
        contract row and onto every vector's ``filter_metadata``.
        """
        assert taxonomy.agreement_type_for("MSA")[0] == AgreementType.MSA.value
        assert taxonomy.agreement_type_for("NDA")[0] == AgreementType.NDA.value
        assert (
            taxonomy.agreement_type_for("License Agreement")[0]
            == AgreementType.LICENSE_AGREEMENT.value
        )

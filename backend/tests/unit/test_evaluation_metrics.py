"""The metrics themselves, against hand-worked examples.

A quality gate that has never been checked is a quality gate nobody should let
fail a build. Every value asserted here was computed by hand from the definition
rather than captured from a run - a snapshot test would pin whatever the code did
on the day it was written, including its bugs, which is the opposite of what a
gate needs.
"""

from __future__ import annotations

import math
import uuid

import pytest

from app.evaluation.dataset.models import GoldenCase, GoldenExpectation
from app.evaluation.metrics.calibration import (
    IsotonicCalibrator,
    PlattCalibrator,
    compute_calibration,
)
from app.evaluation.metrics.citation import compute_citation_metrics, evaluate_case_citations
from app.evaluation.metrics.performance import percentile
from app.evaluation.metrics.planner import compute_guardrail_metrics
from app.evaluation.metrics.relevance import Grade, RelevanceJudge, found_units
from app.evaluation.metrics.retrieval import (
    duplicate_rate,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from app.evaluation.runner.result import CaseResult, CitationRecord, RetrievedItem

CONTRACT_A = uuid.UUID(int=1)
CONTRACT_B = uuid.UUID(int=2)
CLAUSE_1 = uuid.UUID(int=11)
CLAUSE_2 = uuid.UUID(int=12)
CLAUSE_3 = uuid.UUID(int=13)


def _item(
    rank: int,
    *,
    ref_id: uuid.UUID = CLAUSE_1,
    contract_id: uuid.UUID = CONTRACT_A,
    page: int | None = None,
    heading: str | None = None,
    text: str = "",
    in_context: bool = False,
    cited: bool = False,
) -> RetrievedItem:
    return RetrievedItem(
        rank=rank,
        level="clause",
        ref_id=ref_id,
        contract_id=contract_id,
        page_start=page,
        section_title=heading,
        similarity=0.8,
        text=text,
        in_context=in_context,
        cited=cited,
    )


def _case(expected: GoldenExpectation, case_id: str = "c1") -> GoldenCase:
    return GoldenCase(id=case_id, question="q", expected=expected)


# =============================================================================
# Relevance
# =============================================================================
class TestRelevanceGrading:
    def test_an_exact_clause_match_is_the_top_grade(self) -> None:
        judge = RelevanceJudge(GoldenExpectation(clauses=[CLAUSE_1]))

        assert judge.grade(_item(1, ref_id=CLAUSE_1)) is Grade.EXACT

    def test_a_chunk_id_can_satisfy_a_clause_expectation(self) -> None:
        """A case may name either the clause or the chunk it was read from."""
        judge = RelevanceJudge(GoldenExpectation(clauses=[CLAUSE_2]))
        item = _item(1, ref_id=CLAUSE_1)
        item.chunk_id = CLAUSE_2

        assert judge.grade(item) is Grade.EXACT

    def test_the_named_clause_outranks_a_coarser_match(self) -> None:
        """A case naming clauses grades those clauses highest.

        The coarser expectations the same case offered still earn partial credit -
        a passage on the named page of the named contract is evidence, just not
        the specific evidence asked for - but it can never tie with the exact
        match, which is what keeps nDCG able to tell the two apart.
        """
        judge = RelevanceJudge(
            GoldenExpectation(clauses=[CLAUSE_1], contracts=[CONTRACT_A], pages=[5])
        )

        exact = judge.grade(_item(1, ref_id=CLAUSE_1))
        coarse = judge.grade(_item(2, ref_id=CLAUSE_3, contract_id=CONTRACT_A, page=5))

        assert exact is Grade.EXACT
        assert coarse is Grade.PAGE
        assert exact > coarse

    def test_a_clause_expectation_is_not_widened_by_the_contract(self) -> None:
        """Union rather than precedence would let every passage from the right
        contract count, inflating recall towards 1.0 for exactly the cases whose
        authors did the most work to be precise. `found_units` is the guard: the
        recall denominator stays the clause count."""
        judge = RelevanceJudge(GoldenExpectation(clauses=[CLAUSE_1], contracts=[CONTRACT_A]))
        items = [_item(index, ref_id=CLAUSE_3, contract_id=CONTRACT_A) for index in range(1, 6)]

        assert judge.total_relevant == 1
        assert found_units(judge, items) == 0
        assert recall_at_k(judge, items, 10) == pytest.approx(0.0)

    def test_a_clause_expectation_alone_admits_nothing_else(self) -> None:
        judge = RelevanceJudge(GoldenExpectation(clauses=[CLAUSE_1]))

        assert judge.grade(_item(1, ref_id=CLAUSE_3)) is Grade.NONE

    def test_page_beats_contract(self) -> None:
        judge = RelevanceJudge(GoldenExpectation(contracts=[CONTRACT_A], pages=[7]))

        assert judge.grade(_item(1, page=7)) is Grade.PAGE
        assert judge.grade(_item(2, page=99)) is Grade.CONTRACT

    def test_a_page_range_counts(self) -> None:
        judge = RelevanceJudge(GoldenExpectation(contracts=[CONTRACT_A], pages=[8]))
        item = _item(1, page=7)
        item.page_end = 9

        assert judge.grade(item) is Grade.PAGE

    def test_headings_ignore_case_and_punctuation(self) -> None:
        judge = RelevanceJudge(GoldenExpectation(headings=["Termination / Notice"]))

        assert judge.grade(_item(1, heading="termination - notice")) is Grade.HEADING

    def test_the_wrong_contract_is_never_relevant(self) -> None:
        judge = RelevanceJudge(GoldenExpectation(contracts=[CONTRACT_A]))

        assert judge.grade(_item(1, contract_id=CONTRACT_B)) is Grade.NONE

    def test_units_are_counted_once_however_many_passages_match(self) -> None:
        """Three chunks of one clause is one unit found, not three."""
        judge = RelevanceJudge(GoldenExpectation(clauses=[CLAUSE_1, CLAUSE_2]))
        items = [_item(1, ref_id=CLAUSE_1), _item(2, ref_id=CLAUSE_1), _item(3, ref_id=CLAUSE_2)]

        assert found_units(judge, items) == 2


# =============================================================================
# Retrieval
# =============================================================================
class TestRetrievalMetrics:
    def test_recall_is_units_found_over_units_expected(self) -> None:
        judge = RelevanceJudge(GoldenExpectation(clauses=[CLAUSE_1, CLAUSE_2, CLAUSE_3]))
        items = [_item(1, ref_id=CLAUSE_1), _item(2, ref_id=CLAUSE_2)]

        assert recall_at_k(judge, items, 5) == pytest.approx(2 / 3)

    def test_recall_respects_the_cut_off(self) -> None:
        judge = RelevanceJudge(GoldenExpectation(clauses=[CLAUSE_1, CLAUSE_2]))
        items = [_item(1, ref_id=CLAUSE_1), _item(2, ref_id=CLAUSE_3), _item(3, ref_id=CLAUSE_2)]

        assert recall_at_k(judge, items, 2) == pytest.approx(0.5)
        assert recall_at_k(judge, items, 3) == pytest.approx(1.0)

    def test_a_case_with_no_expectation_scores_none_not_zero(self) -> None:
        """A dataset half made of negative cases would otherwise report recall 0.5
        however well retrieval worked."""
        judge = RelevanceJudge(GoldenExpectation(should_answer=False))

        assert recall_at_k(judge, [_item(1)], 5) is None
        assert precision_at_k(judge, [_item(1)], 5) is None
        assert reciprocal_rank(judge, [_item(1)]) is None

    def test_precision_divides_by_what_was_returned(self) -> None:
        """Three passages, all correct, is perfect precision - not 0.3."""
        judge = RelevanceJudge(GoldenExpectation(contracts=[CONTRACT_A]))
        items = [_item(index) for index in range(1, 4)]

        assert precision_at_k(judge, items, 10) == pytest.approx(1.0)

    def test_precision_counts_irrelevant_passages(self) -> None:
        judge = RelevanceJudge(GoldenExpectation(contracts=[CONTRACT_A]))
        items = [_item(1), _item(2, contract_id=CONTRACT_B), _item(3), _item(4)]

        assert precision_at_k(judge, items, 4) == pytest.approx(0.75)

    def test_reciprocal_rank_is_one_over_the_first_hit(self) -> None:
        judge = RelevanceJudge(GoldenExpectation(clauses=[CLAUSE_2]))
        items = [_item(1, ref_id=CLAUSE_1), _item(2, ref_id=CLAUSE_3), _item(3, ref_id=CLAUSE_2)]

        assert reciprocal_rank(judge, items) == pytest.approx(1 / 3)

    def test_reciprocal_rank_is_zero_when_nothing_is_found(self) -> None:
        judge = RelevanceJudge(GoldenExpectation(clauses=[CLAUSE_2]))

        assert reciprocal_rank(judge, [_item(1, ref_id=CLAUSE_1)]) == 0.0

    def test_ndcg_is_one_for_a_perfect_ranking(self) -> None:
        judge = RelevanceJudge(GoldenExpectation(clauses=[CLAUSE_1, CLAUSE_2]))
        items = [_item(1, ref_id=CLAUSE_1), _item(2, ref_id=CLAUSE_2)]

        assert ndcg_at_k(judge, items, 10) == pytest.approx(1.0)

    def test_ndcg_penalises_a_late_hit(self) -> None:
        """Hand-computed: one exact hit at rank 3 against an ideal of one at rank 1.
        gain = 2^4-1 = 15; DCG = 15/log2(4); IDCG = 15/log2(2) = 15."""
        judge = RelevanceJudge(GoldenExpectation(clauses=[CLAUSE_1]))
        items = [_item(1, ref_id=CLAUSE_3), _item(2, ref_id=CLAUSE_3), _item(3, ref_id=CLAUSE_1)]

        assert ndcg_at_k(judge, items, 10) == pytest.approx(1 / math.log2(4))

    def test_ndcg_grades_an_exact_match_above_a_contract_match(self) -> None:
        exact = RelevanceJudge(GoldenExpectation(clauses=[CLAUSE_1], contracts=[CONTRACT_A]))
        coarse = RelevanceJudge(GoldenExpectation(contracts=[CONTRACT_A]))
        items = [_item(1, ref_id=CLAUSE_1)]

        assert ndcg_at_k(exact, items, 10) == pytest.approx(1.0)
        assert ndcg_at_k(coarse, items, 10) == pytest.approx(1.0)

    def test_duplicate_rate_catches_repeated_text(self) -> None:
        text = " ".join(f"word{index}" for index in range(40))
        items = [_item(1, text=text), _item(2, text=text), _item(3, text="something else here")]

        assert duplicate_rate(items) == pytest.approx(1 / 3)

    def test_distinct_passages_are_not_duplicates(self) -> None:
        items = [
            _item(1, text=" ".join(f"alpha{n}" for n in range(40))),
            _item(2, text=" ".join(f"beta{n}" for n in range(40))),
        ]

        assert duplicate_rate(items) == 0.0


# =============================================================================
# Citations
# =============================================================================
class TestCitationMetrics:
    @staticmethod
    def _result(**kwargs) -> CaseResult:
        expected = GoldenExpectation(clauses=[CLAUSE_1], contracts=[CONTRACT_A])
        result = CaseResult(case=_case(expected), answered=True, answer="a" * 200)
        result.retrieved = [
            _item(1, ref_id=CLAUSE_1, in_context=True),
            _item(2, ref_id=CLAUSE_3, in_context=True),
        ]
        for key, value in kwargs.items():
            setattr(result, key, value)
        return result

    def test_a_citation_to_the_expected_clause_is_correct(self) -> None:
        result = self._result(
            citations=[CitationRecord(label=1, ref_id=CLAUSE_1, contract_id=CONTRACT_A)]
        )
        result.retrieved[0].cited = True

        outcome = evaluate_case_citations(result)
        assert outcome.correct == 1
        assert outcome.precision == pytest.approx(1.0)

    def test_a_citation_to_an_unexpected_passage_is_imprecise(self) -> None:
        result = self._result(
            citations=[CitationRecord(label=2, ref_id=CLAUSE_3, contract_id=CONTRACT_A)]
        )

        outcome = evaluate_case_citations(result)
        assert outcome.imprecise == 1
        assert outcome.correct == 0

    def test_an_unresolved_label_is_a_hallucination(self) -> None:
        """Stripped from the answer before display, so only countable here."""
        result = self._result(citations=[CitationRecord(label=9, resolved=False)])

        outcome = evaluate_case_citations(result)
        assert outcome.hallucinated == 1
        assert outcome.precision is None  # nothing resolvable to judge

    def test_a_citation_outside_the_retrieved_set_is_broken(self) -> None:
        """Structurally impossible via the validator - a count here is our bug."""
        result = self._result(
            citations=[CitationRecord(label=1, ref_id=uuid.UUID(int=99), resolved=True)]
        )

        assert evaluate_case_citations(result).broken == 1

    def test_recall_counts_only_what_the_model_was_shown(self) -> None:
        """Counting units retrieval never surfaced would make this a second,
        worse measurement of retrieval recall."""
        result = CaseResult(
            case=_case(GoldenExpectation(clauses=[CLAUSE_1])),
            answered=True,
            answer="a" * 200,
            citations=[],
        )
        result.retrieved = [
            _item(1, ref_id=CLAUSE_1, in_context=True),
            # Relevant but never admitted to the prompt: outside the denominator,
            # because the model cannot cite what it was not shown.
            _item(2, ref_id=CLAUSE_1, in_context=False),
        ]

        outcome = evaluate_case_citations(result)
        assert outcome.expected_available == 1
        assert outcome.missed == 1
        assert outcome.recall == pytest.approx(0.0)

    def test_a_substantive_uncited_answer_is_counted(self) -> None:
        metrics = compute_citation_metrics([self._result(citations=[])])

        assert metrics.uncited_answers == 1
        assert metrics.hallucination_rate == pytest.approx(1.0)

    def test_a_short_answer_is_not_penalised_for_no_citation(self) -> None:
        """Below the length threshold it is a clarification, not a factual claim."""
        metrics = compute_citation_metrics([self._result(citations=[], answer="Not stated.")])

        assert metrics.uncited_answers == 0

    def test_declined_answers_are_not_scored_for_citations(self) -> None:
        """Otherwise the metric would reward answering when it should decline."""
        declined = self._result(answered=False, insufficient_context=True, citations=[])

        metrics = compute_citation_metrics([declined])
        assert metrics.answers_scored == 0


# =============================================================================
# Guardrail
# =============================================================================
class TestGuardrailMetrics:
    @staticmethod
    def _result(*, should_answer: bool, answered: bool, by_level=None) -> CaseResult:
        return CaseResult(
            case=_case(GoldenExpectation(should_answer=should_answer)),
            answered=answered,
            insufficient_context=not answered,
            similarity_by_level=by_level or {},
        )

    def test_the_confusion_matrix(self) -> None:
        results = [
            self._result(should_answer=True, answered=True),
            self._result(should_answer=True, answered=False),
            self._result(should_answer=False, answered=True),
            self._result(should_answer=False, answered=False),
        ]

        metrics = compute_guardrail_metrics(results, answer_threshold=0.45)
        assert (metrics.true_accept, metrics.false_reject) == (1, 1)
        assert (metrics.false_accept, metrics.true_reject) == (1, 1)
        assert metrics.accuracy == pytest.approx(0.5)

    def test_rates_use_the_right_denominators(self) -> None:
        results = [
            self._result(should_answer=False, answered=True),
            self._result(should_answer=False, answered=False),
            self._result(should_answer=False, answered=False),
            self._result(should_answer=True, answered=True),
        ]

        metrics = compute_guardrail_metrics(results, answer_threshold=0.45)
        assert metrics.false_accept_rate == pytest.approx(1 / 3)
        assert metrics.false_reject_rate == pytest.approx(0.0)

    def test_a_generation_failure_is_not_a_guardrail_decision(self) -> None:
        """Blaming the guardrail for the provider being down would be wrong."""
        failed = self._result(should_answer=True, answered=False)
        failed.generation_failed = True

        assert compute_guardrail_metrics([failed], answer_threshold=0.45).total == 0

    def test_it_counts_what_a_whole_result_maximum_would_have_passed(self) -> None:
        result = self._result(
            should_answer=False,
            answered=False,
            by_level={"document_summary": 0.68, "clause": 0.31},
        )

        metrics = compute_guardrail_metrics([result], answer_threshold=0.45)
        assert metrics.document_summary_would_have_passed == 1
        assert metrics.hallucinations_prevented == 1

    def test_a_strong_clause_is_not_counted_as_a_level_one_pass(self) -> None:
        result = self._result(
            should_answer=True,
            answered=True,
            by_level={"document_summary": 0.68, "clause": 0.72},
        )

        metrics = compute_guardrail_metrics([result], answer_threshold=0.45)
        assert metrics.document_summary_would_have_passed == 0


# =============================================================================
# Calibration
# =============================================================================
class TestCalibration:
    def test_perfect_calibration_has_zero_error(self) -> None:
        # Bin [0.9,1.0): confidence 0.95, nine of ten correct... use exact bins.
        samples = [(0.05, False)] * 20 + [(0.95, True)] * 20

        metrics = compute_calibration(samples, bins=10)
        assert metrics.ece == pytest.approx(0.05, abs=0.001)

    def test_systematic_over_confidence_shows_a_positive_bias(self) -> None:
        samples = [(0.9, False)] * 10 + [(0.9, True)] * 10

        metrics = compute_calibration(samples, bins=10)
        assert metrics.mean_bias == pytest.approx(0.4, abs=0.001)
        assert metrics.ece == pytest.approx(0.4, abs=0.001)

    def test_brier_is_the_mean_squared_error(self) -> None:
        samples = [(1.0, True), (0.0, False), (0.5, True), (0.5, False)]

        metrics = compute_calibration(samples, bins=10)
        assert metrics.brier == pytest.approx((0 + 0 + 0.25 + 0.25) / 4)

    def test_mce_reports_the_worst_bin_not_the_average(self) -> None:
        samples = [(0.05, False)] * 100 + [(0.95, False)] * 4

        metrics = compute_calibration(samples, bins=10)
        assert metrics.mce == pytest.approx(0.95, abs=0.01)
        assert metrics.ece < metrics.mce

    def test_no_samples_is_not_an_error(self) -> None:
        assert compute_calibration([], bins=10).samples == 0

    def test_platt_learns_to_shift_an_over_confident_score(self) -> None:
        # Everything scored 0.9; only half are correct. A fitted calibrator should
        # pull the prediction down towards the base rate.
        samples = [(0.9, index % 2 == 0) for index in range(200)]

        calibrator = PlattCalibrator.fit(samples)
        assert calibrator.predict(0.9) == pytest.approx(0.5, abs=0.05)

    def test_isotonic_is_monotonic(self) -> None:
        samples = [(0.1, False), (0.3, True), (0.2, False), (0.9, True), (0.7, True)]

        calibrator = IsotonicCalibrator.fit(samples)
        outputs = [calibrator.predict(value / 10) for value in range(11)]
        assert outputs == sorted(outputs)

    def test_isotonic_pools_a_violating_pair(self) -> None:
        """0.4 scores correct and 0.6 does not; monotonicity forces them to pool."""
        samples = [(0.4, True), (0.6, False)]

        calibrator = IsotonicCalibrator.fit(samples)
        assert calibrator.predict(0.4) == pytest.approx(0.5)
        assert calibrator.predict(0.6) == pytest.approx(0.5)

    def test_an_empty_fit_is_the_identity(self) -> None:
        assert IsotonicCalibrator.fit([]).predict(0.42) == pytest.approx(0.42)


# =============================================================================
# Performance
# =============================================================================
class TestPercentile:
    def test_it_interpolates(self) -> None:
        assert percentile([0.0, 10.0], 0.5) == pytest.approx(5.0)

    def test_the_bounds_are_exact(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0]
        assert percentile(values, 0.0) == 1.0
        assert percentile(values, 1.0) == 4.0

    def test_a_single_value_is_every_percentile(self) -> None:
        assert percentile([7.0], 0.95) == 7.0

    def test_empty_is_zero_not_an_error(self) -> None:
        assert percentile([], 0.95) == 0.0

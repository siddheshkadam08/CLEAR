"""Dataset loading, the regression gate, and report generation.

The gate tests carry the most weight here. A gate that fails a build has to be
right, and the two ways it can be wrong are opposite: passing a real regression,
or failing on noise. Both are covered explicitly, because a gate people learn to
override is a gate that has stopped existing.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.evaluation.benchmark.baseline import (
    Baseline,
    Direction,
    Gate,
    Verdict,
    compare,
    load_baseline,
    save_baseline,
)
from app.evaluation.benchmark.history import load_history, trend_series
from app.evaluation.dataset import (
    DatasetError,
    GoldenCase,
    GoldenDataset,
    GoldenExpectation,
    load_dataset,
    write_dataset,
)
from app.evaluation.metrics.scorecard import build_scorecard
from app.evaluation.reports import (
    render_markdown,
    write_csv_reports,
    write_html_reports,
    write_json_report,
)
from app.evaluation.runner.result import CaseResult, CitationRecord, RetrievedItem, RunResult

PROJECT = uuid.UUID(int=1)
CONTRACT = uuid.UUID(int=2)
CLAUSE = uuid.UUID(int=3)


# =============================================================================
# Dataset
# =============================================================================
class TestDatasetLoading:
    def test_the_bundled_smoke_set_loads(self) -> None:
        dataset = load_dataset("smoke")

        assert len(dataset) > 0
        assert dataset.name == "smoke"

    def test_the_smoke_set_can_score_the_guardrail(self) -> None:
        """A dataset with no negative cases cannot detect a guardrail regression."""
        dataset = load_dataset("smoke")

        assert dataset.statistics()["unanswerable"] >= 2

    def test_camel_and_snake_case_are_both_accepted(self) -> None:
        case = GoldenCase.from_dict(
            {
                "id": "x",
                "question": "q",
                "projectId": str(PROJECT),
                "expected": {"agreementTypes": ["msa"], "shouldAnswer": False},
            }
        )

        assert case.project_id == PROJECT
        assert case.expected.agreement_types == ["msa"]
        assert case.expected.should_answer is False

    def test_a_malformed_id_is_rejected_at_load(self) -> None:
        """A silently skipped case makes the denominator wrong, which moves every
        metric towards whichever cases survived."""
        with pytest.raises(ValueError, match="not a valid id"):
            GoldenCase.from_dict(
                {"id": "x", "question": "q", "expected": {"contracts": ["not-a-uuid"]}}
            )

    def test_a_case_without_a_question_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="no question"):
            GoldenCase.from_dict({"id": "x", "question": "  "})

    def test_duplicate_ids_are_rejected(self, tmp_path: Path) -> None:
        """Colliding ids make per-case results overwrite each other."""
        path = tmp_path / "dupes.json"
        path.write_text(
            json.dumps(
                {
                    "name": "dupes",
                    "cases": [
                        {"id": "same", "question": "a"},
                        {"id": "same", "question": "b"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(DatasetError, match="duplicate case ids"):
            load_dataset(path)

    def test_a_future_schema_version_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "future.json"
        path.write_text(
            json.dumps({"name": "f", "schemaVersion": 99, "cases": [{"id": "a", "question": "q"}]}),
            encoding="utf-8",
        )

        with pytest.raises(DatasetError, match="schema version"):
            load_dataset(path)

    def test_an_empty_dataset_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.json"
        path.write_text(json.dumps({"name": "e", "cases": []}), encoding="utf-8")

        with pytest.raises(DatasetError, match="no cases"):
            load_dataset(path)

    def test_an_unknown_name_lists_what_is_available(self) -> None:
        with pytest.raises(DatasetError, match="Available"):
            load_dataset("no-such-dataset")

    @pytest.mark.parametrize("suffix", [".json", ".jsonl"])
    def test_it_round_trips(self, tmp_path: Path, suffix: str) -> None:
        original = GoldenDataset(
            name="rt",
            version="2",
            cases=[
                GoldenCase(
                    id="a",
                    question="What is the cap?",
                    project_id=PROJECT,
                    expected=GoldenExpectation(clauses=[CLAUSE], pages=[3], should_answer=True),
                    tags=["msa"],
                )
            ],
        )

        reloaded = load_dataset(write_dataset(original, tmp_path / f"rt{suffix}"))

        assert reloaded.version == "2"
        assert reloaded.cases[0].expected.clauses == [CLAUSE]
        assert reloaded.cases[0].tags == ["msa"]

    def test_jsonl_tolerates_blank_and_comment_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "loose.jsonl"
        path.write_text(
            '{"_meta": {"name": "loose"}}\n\n// a comment\n{"id": "a", "question": "q"}\n',
            encoding="utf-8",
        )

        assert len(load_dataset(path)) == 1

    def test_filtering_by_tag_is_any_of(self) -> None:
        dataset = GoldenDataset(
            name="t",
            cases=[
                GoldenCase(id="a", question="q", tags=["nda"]),
                GoldenCase(id="b", question="q", tags=["msa"]),
                GoldenCase(id="c", question="q", tags=["lease"]),
            ],
        )

        assert len(dataset.filter(tags=["nda", "msa"])) == 2

    def test_an_expectation_with_only_should_answer_is_not_scoreable(self) -> None:
        """It still scores the guardrail; it must not enter a recall denominator."""
        assert GoldenExpectation(should_answer=False).has_relevance_signal is False
        assert GoldenExpectation(contracts=[CONTRACT]).has_relevance_signal is True


# =============================================================================
# Regression gate
# =============================================================================
@dataclass(slots=True)
class _FixedScorecard:
    """A scorecard stand-in with exactly the metrics a gate test needs.

    ``compare`` reads three things - the dataset, the label and
    ``metrics_for_comparison()`` - so a stand-in isolates the gate's arithmetic
    from the fifty metrics a real scorecard computes. Building a real one for
    each case would test the aggregation instead of the gate.
    """

    metrics: dict[str, float]
    dataset: str = "test@1"
    label: str = "current"

    def metrics_for_comparison(self) -> dict[str, float]:
        return dict(self.metrics)


def _scorecard(**metrics: float) -> _FixedScorecard:
    base = dict.fromkeys(
        (
            "recall@10",
            "mrr",
            "citation_precision",
            "hallucination_rate",
            "guardrail_accuracy",
            "latency_p95_ms",
            "mean_cost_usd",
            "composite",
        ),
        0.5,
    )
    base.update(metrics)
    return _FixedScorecard(metrics=base)


class TestRegressionGate:
    def test_a_real_drop_fails_the_build(self) -> None:
        baseline = Baseline(
            dataset="test@1", label="base", recorded_at="", metrics={"recall@10": 0.80}
        )

        report = compare(
            _scorecard(**{"recall@10": 0.60}),
            baseline,
            gates=(Gate("recall@10", Direction.HIGHER_IS_BETTER, tolerance=0.02),),
        )

        assert report.passed is False
        assert report.comparisons[0].verdict is Verdict.REGRESSED

    def test_movement_inside_the_tolerance_is_not_a_regression(self) -> None:
        """A gate that fires on noise is a gate people learn to override."""
        baseline = Baseline(
            dataset="test@1", label="base", recorded_at="", metrics={"recall@10": 0.80}
        )

        report = compare(
            _scorecard(**{"recall@10": 0.79}),
            baseline,
            gates=(Gate("recall@10", Direction.HIGHER_IS_BETTER, tolerance=0.02),),
        )

        assert report.passed is True
        assert report.comparisons[0].verdict is Verdict.UNCHANGED

    def test_direction_is_respected(self) -> None:
        """A rise in hallucination rate is a regression, not an improvement."""
        baseline = Baseline(
            dataset="test@1", label="base", recorded_at="", metrics={"hallucination_rate": 0.01}
        )

        report = compare(
            _scorecard(hallucination_rate=0.09),
            baseline,
            gates=(Gate("hallucination_rate", Direction.LOWER_IS_BETTER, tolerance=0.01),),
        )

        assert report.comparisons[0].verdict is Verdict.REGRESSED

    def test_a_relative_tolerance_applies_to_latency(self) -> None:
        """5 ms on a 4-second p95 must not fail a build."""
        baseline = Baseline(
            dataset="test@1", label="base", recorded_at="", metrics={"latency_p95_ms": 4000.0}
        )

        report = compare(
            _scorecard(latency_p95_ms=4300.0),
            baseline,
            gates=(
                Gate(
                    "latency_p95_ms",
                    Direction.LOWER_IS_BETTER,
                    tolerance=250.0,
                    relative_tolerance=0.20,
                ),
            ),
        )

        assert report.comparisons[0].verdict is Verdict.UNCHANGED

    def test_an_absolute_limit_fires_regardless_of_the_baseline(self) -> None:
        """Guards against a slow slide that never trips the per-run tolerance."""
        baseline = Baseline(
            dataset="test@1", label="base", recorded_at="", metrics={"broken_citations": 3.0}
        )

        report = compare(
            _scorecard(broken_citations=3.0),
            baseline,
            gates=(
                Gate(
                    "broken_citations",
                    Direction.LOWER_IS_BETTER,
                    tolerance=0.0,
                    absolute_limit=0.0,
                ),
            ),
        )

        assert report.comparisons[0].verdict is Verdict.REGRESSED

    def test_a_missing_metric_fails_rather_than_passing_quietly(self) -> None:
        """A report-shape change must not silently disable a gate."""
        baseline = Baseline(dataset="test@1", label="base", recorded_at="", metrics={"gone": 1.0})

        report = compare(
            _scorecard(),
            baseline,
            gates=(Gate("gone", Direction.HIGHER_IS_BETTER),),
        )

        assert report.comparisons[0].verdict is Verdict.MISSING
        assert report.passed is False

    def test_no_baseline_is_not_a_failure(self) -> None:
        """The first run on a new dataset has nothing to compare against."""
        report = compare(
            _scorecard(),
            None,
            gates=(Gate("recall@10", Direction.HIGHER_IS_BETTER),),
        )

        assert report.passed is True
        assert report.comparisons[0].verdict is Verdict.NEW

    def test_a_non_blocking_gate_reports_without_failing(self) -> None:
        baseline = Baseline(dataset="test@1", label="base", recorded_at="", metrics={"mrr": 0.9})

        report = compare(
            _scorecard(mrr=0.1),
            baseline,
            gates=(Gate("mrr", Direction.HIGHER_IS_BETTER, tolerance=0.01, blocking=False),),
        )

        assert report.comparisons[0].verdict is Verdict.REGRESSED
        assert report.passed is True

    def test_a_baseline_round_trips(self, tmp_path: Path) -> None:
        original = Baseline(
            dataset="test@1", label="v1", recorded_at="now", metrics={"recall@10": 0.8}, cases=10
        )

        reloaded = load_baseline(save_baseline(original, tmp_path / "baseline.json"))

        assert reloaded is not None
        assert reloaded.metrics["recall@10"] == pytest.approx(0.8)

    def test_a_missing_baseline_file_is_none_not_an_error(self, tmp_path: Path) -> None:
        assert load_baseline(tmp_path / "nope.json") is None


# =============================================================================
# Reports
# =============================================================================
def _run() -> RunResult:
    expected = GoldenExpectation(
        contracts=[CONTRACT], clauses=[CLAUSE], should_answer=True, intent="clause_lookup"
    )
    good = CaseResult(
        case=GoldenCase(id="ok", question="What is the cap?", expected=expected, tags=["msa"]),
        answered=True,
        answer="The cap is the fees paid. [1]" + "x" * 200,
        intent="clause_lookup",
        strategy="hybrid",
        retrieval_mode="Unfiltered",
        confidence=0.82,
        confidence_band="high",
        top_similarity=0.9,
        similarity_by_level={"clause": 0.9},
        timings={"total_ms": 3200, "retrieval_ms": 400, "inference_ms": 2600},
        tokens=1800,
        cost_usd=0.004,
        context_tokens=5200,
    )
    good.retrieved = [
        RetrievedItem(
            rank=1,
            level="clause",
            ref_id=CLAUSE,
            contract_id=CONTRACT,
            similarity=0.9,
            in_context=True,
            cited=True,
            text="The cap is the fees paid.",
        )
    ]
    good.citations = [CitationRecord(label=1, ref_id=CLAUSE, contract_id=CONTRACT)]

    declined = CaseResult(
        case=GoldenCase(
            id="neg",
            question="What is the crop insurance excess?",
            expected=GoldenExpectation(should_answer=False),
            tags=["negative"],
        ),
        answered=False,
        insufficient_context=True,
        timings={"total_ms": 900},
        similarity_by_level={"document_summary": 0.6, "clause": 0.2},
    )

    return RunResult(
        dataset="test@1",
        label="unit",
        configuration={"min_similarity_clause": 0.45},
        results=[good, declined],
        duration_seconds=4.2,
    )


class TestReports:
    def test_a_scorecard_is_built_end_to_end(self) -> None:
        scorecard = build_scorecard(_run(), answer_threshold=0.45)

        assert scorecard.cases == 2
        assert scorecard.retrieval.recall.get(10) == pytest.approx(1.0)
        assert scorecard.guardrail.true_accept == 1
        assert scorecard.guardrail.true_reject == 1
        assert 0.0 < scorecard.composite <= 1.0

    def test_the_composite_is_bounded(self) -> None:
        assert 0.0 <= build_scorecard(_run()).composite <= 1.0

    def test_json_and_csv_are_written(self, tmp_path: Path) -> None:
        run = _run()
        scorecard = build_scorecard(run)
        regression = compare(scorecard, None)

        json_path = write_json_report(scorecard, run, regression, tmp_path)
        csv_paths = write_csv_reports(scorecard, run, tmp_path)

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert payload["scorecard"]["cases"] == 2
        assert len(payload["run"]["results"]) == 2
        assert {path.name for path in csv_paths} == {"metrics.csv", "cases.csv"}
        assert "case_id" in csv_paths[1].read_text(encoding="utf-8")

    def test_a_stored_run_can_be_read_back(self, tmp_path: Path) -> None:
        """Re-scoring a stored run costs nothing; re-running it costs an hour."""
        run = _run()
        scorecard = build_scorecard(run)
        path = write_json_report(scorecard, run, compare(scorecard, None), tmp_path)

        from app.evaluation.benchmark.benchmark import load_run

        reloaded = load_run(path)
        assert len(reloaded.results) == 2
        assert reloaded.results[0].case.id == "ok"
        assert build_scorecard(reloaded).cases == 2

    def test_every_html_page_is_written_and_self_contained(self, tmp_path: Path) -> None:
        run = _run()
        scorecard = build_scorecard(run)

        paths = write_html_reports(scorecard, run, compare(scorecard, None), tmp_path)

        assert {path.name for path in paths} >= {
            "overall_report.html",
            "retrieval_report.html",
            "citation_report.html",
            "planner_report.html",
            "confidence_report.html",
            "latency_report.html",
            "cost_report.html",
        }
        for path in paths:
            html = path.read_text(encoding="utf-8")
            # No external requests: a build agent has no internet, and a report
            # whose charts are blank rectangles is not a report.
            assert "http://" not in html.replace("http://www.w3.org/2000/svg", "")
            assert "<script" not in html
            assert "cdn" not in html.lower()

    def test_html_escapes_case_text(self, tmp_path: Path) -> None:
        """Reports render text from counterparty documents."""
        run = _run()
        run.results[0].case.question = "<img src=x onerror=alert(1)>"
        run.results[0].retrieved = []
        scorecard = build_scorecard(run)

        paths = write_html_reports(scorecard, run, compare(scorecard, None), tmp_path)
        combined = "".join(path.read_text(encoding="utf-8") for path in paths)

        assert "<img src=x" not in combined

    def test_markdown_names_the_verdict(self) -> None:
        run = _run()
        scorecard = build_scorecard(run)

        rendered = render_markdown(scorecard, compare(scorecard, None))

        assert "PASS" in rendered
        assert "recall@10" in rendered


# =============================================================================
# History
# =============================================================================
class TestHistory:
    def test_it_reads_previous_summaries(self, tmp_path: Path) -> None:
        for index, composite in enumerate([0.6, 0.7, 0.8]):
            directory = tmp_path / f"run{index}"
            directory.mkdir()
            (directory / "summary.json").write_text(
                json.dumps(
                    {
                        "dataset": "test@1",
                        "label": f"run{index}",
                        "recorded_at": f"2026-01-0{index + 1}",
                        "passed": True,
                        "cases": 10,
                        "metrics": {"composite": composite},
                    }
                ),
                encoding="utf-8",
            )

        points = load_history(tmp_path, dataset="test@1")

        assert [point.metrics["composite"] for point in points] == [0.6, 0.7, 0.8]

    def test_a_missing_metric_carries_forward_rather_than_drawing_a_cliff(self) -> None:
        from app.evaluation.benchmark.history import HistoryPoint

        points = [
            HistoryPoint("a", "1", "d", True, 5, {"composite": 0.8}),
            HistoryPoint("b", "2", "d", True, 5, {}),
            HistoryPoint("c", "3", "d", True, 5, {"composite": 0.9}),
        ]

        assert trend_series(points, ("composite",))["composite"] == [0.8, 0.8, 0.9]

    def test_an_unreadable_entry_is_skipped(self, tmp_path: Path) -> None:
        directory = tmp_path / "broken"
        directory.mkdir()
        (directory / "summary.json").write_text("{not json", encoding="utf-8")

        assert load_history(tmp_path) == []

    def test_a_missing_directory_is_empty_not_an_error(self, tmp_path: Path) -> None:
        assert load_history(tmp_path / "nope") == []

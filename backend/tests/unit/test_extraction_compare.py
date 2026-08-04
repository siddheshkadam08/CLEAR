"""The evaluator must find differences, not just report agreement.

An evaluator that returns 100% is either proof the candidate matches or proof the
evaluator is blind, and those are indistinguishable from the outside. Most of
these tests therefore feed it a *known* difference and assert it is caught -
identical-input tests alone would pass on a comparator that always says yes.

The other property under test is that it never decides which side is right. The
baseline is the incumbent, not ground truth; the report says "these differ" and
stops. Anything stronger would need a labelled set.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.ai.extraction.models import (
    CategoryOutcome,
    EvidenceRef,
    ExtractedClause,
    ExtractionResult,
)
from app.evaluation.extraction_compare import Cost, EvaluationReport, evaluate


def clause(clause_type: str = "term", **overrides: object) -> ExtractedClause:
    base: dict[str, object] = {
        "clause_type": clause_type,
        "title": "Term",
        "text": "This agreement runs for 24 months.",
        "confidence": 0.91,
        "attributes": {"months": 24, "auto_renews": False},
        "evidence": [EvidenceRef(chunk_id="chunk-1", page_start=2, page_end=2)],
    }
    return ExtractedClause(**{**base, **overrides})  # type: ignore[arg-type]


def outcome(**overrides: object) -> CategoryOutcome:
    base: dict[str, object] = {
        "category": "clause:term",
        "prompt_id": "extraction.clauses",
        "status": "ok",
        "llm_calls": 1,
        "input_tokens": 1800,
        "output_tokens": 200,
        "latency_ms": 1200,
        "cost_usd": 0.004,
    }
    return CategoryOutcome(**{**base, **overrides})  # type: ignore[arg-type]


def result(clauses: list[ExtractedClause], outcomes: list[CategoryOutcome]) -> ExtractionResult:
    return ExtractionResult(clauses=clauses, outcomes=outcomes)


def pipeline_of(payload: ExtractionResult):
    async def _run(_request: object) -> ExtractionResult:
        return payload

    return _run


async def run(baseline: ExtractionResult, candidate: ExtractionResult) -> EvaluationReport:
    return await evaluate(
        pipeline_of(baseline),
        pipeline_of(candidate),
        requests=[("Contract A", None)],  # type: ignore[list-item]
    )



@pytest.mark.asyncio
class TestIdenticalPipelinesAgree:
    """The self-test. Both sides run the same extraction today, so this is the
    state the harness must report before it is trusted on anything else."""

    async def test_identical_results_are_a_perfect_match(self) -> None:
        payload = result([clause()], [outcome()])

        report = await run(payload, payload)

        assert report.regression_pct == 0.0
        assert report.contracts[0].field_accuracy.rate == 1.0
        assert report.contracts[0].diffs == []

    async def test_float_jitter_is_not_a_difference(self) -> None:
        """Confidence comes from a model; identical inputs can differ in the last
        decimal place. Reporting that as a regression would bury real findings."""
        base = result([clause(confidence=0.9100)], [outcome()])
        cand = result([clause(confidence=0.9104)], [outcome()])

        report = await run(base, cand)

        assert report.regression_pct == 0.0

    async def test_reordered_evidence_is_not_a_difference(self) -> None:
        refs = [EvidenceRef(chunk_id="a"), EvidenceRef(chunk_id="b")]
        base = result([clause(evidence=refs)], [outcome()])
        cand = result([clause(evidence=list(reversed(refs)))], [outcome()])

        report = await run(base, cand)

        assert report.contracts[0].citation_accuracy.rate == 1.0


@pytest.mark.asyncio
class TestDifferencesAreCaught:
    """Each asserts a specific regression is visible. Without these the suite
    would pass on a comparator that returns True unconditionally."""

    async def test_a_changed_field_is_reported(self) -> None:
        base = result([clause(text="Twenty four months.")], [outcome()])
        cand = result([clause(text="Twelve months.")], [outcome()])

        report = await run(base, cand)

        assert report.regression_pct > 0
        assert any(d.field == "text" for d in report.contracts[0].diffs)

    async def test_a_changed_attribute_is_reported(self) -> None:
        base = result([clause(attributes={"months": 24})], [outcome()])
        cand = result([clause(attributes={"months": 12})], [outcome()])

        report = await run(base, cand)

        assert any(d.field == "attributes.months" for d in report.contracts[0].diffs)

    async def test_a_dropped_citation_is_reported(self) -> None:
        """The failure that matters most: an answer that keeps its text but loses
        the evidence trail is worse than one that is simply wrong, because it
        still looks sourced."""
        base = result([clause(evidence=[EvidenceRef(chunk_id="a")])], [outcome()])
        cand = result([clause(evidence=[EvidenceRef(chunk_id="b")])], [outcome()])

        report = await run(base, cand)

        assert report.contracts[0].citation_accuracy.rate == 0.0
        assert any(d.field == "evidence.chunk_ids" for d in report.contracts[0].diffs)

    async def test_a_clause_the_candidate_never_produced_is_reported(self) -> None:
        """Counted apart from field accuracy: a missing clause has no fields to
        compare, so folding it in would score a total miss as 100% accurate."""
        base = result([clause("term"), clause("indemnification")], [outcome()])
        cand = result([clause("term")], [outcome()])

        report = await run(base, cand)

        assert report.contracts[0].only_in_baseline == ["indemnification"]
        assert report.contracts[0].field_accuracy.rate == 1.0, "the shared clause still matched"

    async def test_a_value_becoming_null_is_reported(self) -> None:
        base = result([clause(summary="Runs 24 months.")], [outcome()])
        cand = result([clause(summary=None)], [outcome()])

        report = await run(base, cand)

        diff = next(d for d in report.contracts[0].diffs if d.field == "summary")
        assert "no value" in diff.as_dict()["difference"]

    async def test_invalid_json_is_reported(self) -> None:
        base = result([clause()], [outcome()])
        cand = result([clause()], [outcome(status="invalid")])

        report = await run(base, cand)

        assert report.contracts[0].json_validity.rate == 0.0


@pytest.mark.asyncio
class TestCostAccounting:
    async def test_costs_come_from_the_outcomes(self) -> None:
        """The engine already records these. The harness must not re-measure
        them or the two numbers will drift."""
        base = result([clause()], [outcome(input_tokens=1800), outcome(input_tokens=1800)])
        cand = result([clause()], [outcome(input_tokens=900)])

        report = await run(base, cand)
        summary = report.summary()

        assert report.contracts[0].baseline_cost.prompt_tokens == 3600
        assert report.contracts[0].candidate_cost.prompt_tokens == 900
        assert summary["prompt_token_reduction_pct"] == 75.0

    async def test_a_cost_increase_is_reported_as_negative_reduction(self) -> None:
        """A candidate that costs more must not read as a saving."""
        base = result([clause()], [outcome(input_tokens=1000)])
        cand = result([clause()], [outcome(input_tokens=2000)])

        assert (await run(base, cand)).summary()["prompt_token_reduction_pct"] == -100.0


@pytest.mark.asyncio
class TestTheReportIsMachineReadable:
    async def test_three_files_are_written_and_parse(self, tmp_path: Path) -> None:
        base = result([clause(text="A")], [outcome()])
        cand = result([clause(text="B")], [outcome()])

        paths = (await run(base, cand)).write(tmp_path)

        assert set(paths) == {"evaluation", "summary", "diff"}
        for path in paths.values():
            assert json.loads(path.read_text(encoding="utf-8"))

    async def test_the_diff_file_carries_the_report_columns(self, tmp_path: Path) -> None:
        """Contract / Clause / Field / Current / Candidate / Match / Difference."""
        base = result([clause(text="A")], [outcome()])
        cand = result([clause(text="B")], [outcome()])

        paths = (await run(base, cand)).write(tmp_path)
        rows = json.loads(paths["diff"].read_text(encoding="utf-8"))["differences"]

        assert rows
        assert set(rows[0]) >= {
            "contract",
            "clause_type",
            "field",
            "current",
            "candidate",
            "match",
            "difference",
        }

    async def test_long_text_is_truncated_in_the_diff(self, tmp_path: Path) -> None:
        base = result([clause(text="x" * 5000)], [outcome()])
        cand = result([clause(text="y" * 5000)], [outcome()])

        paths = (await run(base, cand)).write(tmp_path)
        rows = json.loads(paths["diff"].read_text(encoding="utf-8"))["differences"]

        assert len(rows[0]["current"]) < 400, "a full clause body per row is unreadable"

    async def test_the_summary_breaks_down_by_clause_and_field(self, tmp_path: Path) -> None:
        """A candidate that is 99% accurate but wrong on every liability cap is
        not 99% good, and only this breakdown shows it."""
        base = result([clause("limitation_of_liability", text="A")], [outcome()])
        cand = result([clause("limitation_of_liability", text="B")], [outcome()])

        summary = (await run(base, cand)).summary()

        assert summary["per_clause_accuracy"]["limitation_of_liability"] == 1
        assert summary["per_field_accuracy"]["text"] == 1


class TestTheEvaluatorIsPipelineAgnostic:
    @pytest.mark.asyncio
    async def test_it_accepts_any_async_callable(self) -> None:
        """The extension point. When batched extraction exists it is passed as
        `candidate` and nothing in the evaluator changes - an evaluator that must
        be edited to understand a new pipeline can be edited to flatter it."""

        async def anything(_request: object) -> ExtractionResult:
            return result([clause()], [outcome()])

        report = await evaluate(anything, anything, requests=[("X", None)])  # type: ignore[list-item]

        assert report.regression_pct == 0.0

    def test_it_does_not_depend_on_batching(self) -> None:
        """Prose may mention what it will compare; *code* must not import it.

        An evaluator that reaches into the pipeline it judges can be made to
        agree with it. Asserted against imports and public names rather than the
        word itself, which appears legitimately in the module docstring.
        """
        import inspect

        from app.evaluation import extraction_compare

        source = inspect.getsource(extraction_compare)
        imports = [
            line.strip()
            for line in source.splitlines()
            if line.startswith(("import ", "from ")) and "batch" in line.lower()
        ]

        assert imports == [], f"evaluator imports batching machinery: {imports}"
        assert not [n for n in extraction_compare.__all__ if "batch" in n.lower()]


class TestAccuracyKeepsItsDenominator:
    def test_a_rate_is_never_reported_without_totals(self) -> None:
        """"100%" over two fields is not the same claim as over two thousand."""
        from app.evaluation.extraction_compare import Accuracy

        acc = Accuracy()
        acc.record(True)

        assert acc.as_dict() == {"matched": 1, "total": 1, "rate": 1.0}

    def test_an_empty_accuracy_is_one_not_zero(self) -> None:
        """Nothing compared is not a failure - it is nothing compared. Zero would
        make an empty run look like a total regression."""
        from app.evaluation.extraction_compare import Accuracy

        assert Accuracy().rate == 1.0

    def test_cost_tolerates_outcomes_without_the_new_fields(self) -> None:
        """`evidence_tokens` and `repeated_tokens` were added recently; an
        outcome deserialised from an older artifact will not carry them."""

        class Older:
            llm_calls = 1
            input_tokens = 100
            output_tokens = 10
            cache_read_tokens = 0
            cost_usd = 0.001
            latency_ms = 50

        cost = Cost.from_outcomes([Older()], 60)  # type: ignore[list-item]

        assert cost.prompt_tokens == 100
        assert cost.evidence_tokens == 0


def test_uuid_is_json_safe(tmp_path: Path) -> None:
    """`default=str` on the dump: chunk ids are UUIDs and would otherwise raise
    at write time, after the whole evaluation has been paid for."""
    report = EvaluationReport()
    paths = report.write(tmp_path)

    assert json.loads(paths["summary"].read_text(encoding="utf-8"))["contracts"] == 0
    assert str(uuid.uuid4())

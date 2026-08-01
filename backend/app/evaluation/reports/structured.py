"""JSON, CSV and Markdown reports.

Three formats for three readers, and each is shaped for its consumer rather than
being the same data in a different syntax:

* **JSON** - the machine's copy. Holds the full per-case detail, so a later run
  can re-score it under different thresholds without re-executing the pipeline.
  This is the file the dashboard and the regression gate read.
* **CSV** - the analyst's copy. One row per case and one per metric, because the
  first question anyone asks of a benchmark is "show me the failures in a
  spreadsheet".
* **Markdown** - the reviewer's copy. Small enough to paste into a pull request,
  which is where a regression is actually argued about.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from app.evaluation.benchmark.baseline import RegressionReport, Verdict
from app.evaluation.metrics.scorecard import Scorecard
from app.evaluation.runner.result import RunResult


def write_json_report(
    scorecard: Scorecard,
    run: RunResult,
    regression: RegressionReport,
    output_dir: Path,
) -> Path:
    """The full record: scorecard, regression verdict, and every case.

    Per-case detail is included deliberately. It is the difference between a
    report that says recall fell and one that says *which questions stopped
    working*, and it is what makes a stored run re-scoreable.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "evaluation.json"
    path.write_text(
        json.dumps(
            {
                "scorecard": scorecard.as_dict(),
                "regression": regression.as_dict(),
                "run": run.as_dict(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_summary_json(
    scorecard: Scorecard, regression: RegressionReport, output_dir: Path
) -> Path:
    """A small file for CI and the dashboard.

    Separate from the full report because the full one is megabytes on a large
    dataset, and a build step that only needs the verdict should not parse all of
    it - nor should a dashboard fetch it over the wire.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "summary.json"
    path.write_text(
        json.dumps(
            {
                "dataset": scorecard.dataset,
                "label": scorecard.label,
                "cases": scorecard.cases,
                "failures": scorecard.failures,
                "passed": regression.passed,
                "summary": regression.summary(),
                "composite": round(scorecard.composite, 4),
                "metrics": {
                    name: round(value, 6)
                    for name, value in scorecard.metrics_for_comparison().items()
                },
                "blocking_failures": [c.metric for c in regression.blocking_failures],
                "by_tag": scorecard.by_tag,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_csv_reports(scorecard: Scorecard, run: RunResult, output_dir: Path) -> list[Path]:
    """``metrics.csv`` and ``cases.csv``."""
    output_dir.mkdir(parents=True, exist_ok=True)

    metrics_path = output_dir / "metrics.csv"
    with metrics_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        for name, value in sorted(scorecard.metrics_for_comparison().items()):
            writer.writerow([name, f"{value:.6f}"])

    cases_path = output_dir / "cases.csv"
    with cases_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "case_id",
                "question",
                "tags",
                "ok",
                "error",
                "should_answer",
                "answered",
                "insufficient_context",
                "retrieval_mode",
                "intent",
                "strategy",
                "document_type",
                "document_type_confidence",
                "retrieved",
                "in_context",
                "citations",
                "confidence",
                "top_similarity",
                "latency_ms",
                "tokens",
                "cost_usd",
            ]
        )
        for result in run.results:
            writer.writerow(
                [
                    result.case.id,
                    result.case.question,
                    "|".join(result.case.tags),
                    result.ok,
                    result.error or "",
                    result.case.expected.should_answer,
                    result.answered,
                    result.insufficient_context,
                    result.retrieval_mode or "",
                    result.intent or "",
                    result.strategy or "",
                    result.document_type or "",
                    f"{result.document_type_confidence:.4f}",
                    len(result.retrieved),
                    sum(1 for item in result.retrieved if item.in_context),
                    len(result.citations),
                    f"{result.confidence:.4f}",
                    f"{result.top_similarity:.4f}",
                    result.latency_ms,
                    result.tokens,
                    f"{result.cost_usd:.6f}",
                ]
            )

    return [metrics_path, cases_path]


def write_markdown_report(
    scorecard: Scorecard, regression: RegressionReport, output_dir: Path
) -> Path:
    """A summary small enough to paste into a pull request."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "REPORT.md"
    path.write_text(render_markdown(scorecard, regression), encoding="utf-8")
    return path


def render_markdown(scorecard: Scorecard, regression: RegressionReport) -> str:
    verdict = "✅ PASS" if regression.passed else "❌ FAIL"
    metrics = scorecard.metrics_for_comparison()

    lines = [
        f"# Retrieval evaluation - {scorecard.dataset}",
        "",
        f"**{verdict}** &mdash; {regression.summary()}",
        "",
        f"- Cases: **{scorecard.cases}** ({scorecard.failures} failed to execute)",
        f"- Composite: **{scorecard.composite:.3f}**",
        f"- Duration: {scorecard.duration_seconds:.0f}s",
        "",
        "## Headline",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    headline = (
        "recall@5",
        "recall@10",
        "mrr",
        "ndcg@10",
        "citation_precision",
        "citation_recall",
        "hallucination_rate",
        "guardrail_accuracy",
        "false_accept_rate",
        "latency_p95_ms",
        "mean_cost_usd",
        "ece",
    )
    for name in headline:
        value = metrics.get(name)
        if value is None:
            continue
        formatted = f"{value:.0f} ms" if name.endswith("_ms") else f"{value:.4f}"
        lines.append(f"| `{name}` | {formatted} |")

    if regression.comparisons:
        lines += [
            "",
            "## Against baseline",
            "",
            "| Metric | Baseline | Current | Delta | Verdict |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
        for comparison in regression.comparisons:
            if comparison.verdict is Verdict.UNCHANGED:
                continue
            marker = {
                Verdict.IMPROVED: "improved",
                Verdict.REGRESSED: "**regressed**",
                Verdict.MISSING: "**missing**",
                Verdict.NEW: "new",
            }[comparison.verdict]
            lines.append(
                f"| `{comparison.metric}` "
                f"| {_cell(comparison.baseline)} "
                f"| {_cell(comparison.current)} "
                f"| {_delta(comparison.delta)} "
                f"| {marker} |"
            )

    if scorecard.by_tag:
        lines += [
            "",
            "## By segment",
            "",
            "| Tag | Cases | Recall@10 | MRR | Citation precision |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for tag, values in sorted(scorecard.by_tag.items()):
            lines.append(
                f"| {tag} | {int(values['cases'])} | {values['recall@10']:.3f} "
                f"| {values['mrr']:.3f} | {values['citation_precision']:.3f} |"
            )

    failing = scorecard.retrieval.zero_recall_case_ids
    if failing:
        lines += [
            "",
            f"## Questions retrieving nothing relevant ({len(failing)})",
            "",
            ", ".join(f"`{case_id}`" for case_id in failing[:25]),
        ]

    if scorecard.calibration:
        lines += ["", "## Confidence", "", scorecard.calibration.recommendation]

    return "\n".join(lines) + "\n"


def _cell(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def _delta(value: float | None) -> str:
    return "-" if value is None else f"{value:+.4f}"


def write_sweep_report(sweeps: list[Any], output_dir: Path) -> Path:
    """Sweep results as JSON, with the recommended configuration on top."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "sweep.json"
    path.write_text(
        json.dumps(
            {
                "recommended": {
                    sweep.parameter: (sweep.best.value if sweep.best else None) for sweep in sweeps
                },
                "recommendations": [sweep.recommendation for sweep in sweeps],
                "sweeps": [sweep.as_dict() for sweep in sweeps],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "render_markdown",
    "write_csv_reports",
    "write_json_report",
    "write_markdown_report",
    "write_summary_json",
    "write_sweep_report",
]

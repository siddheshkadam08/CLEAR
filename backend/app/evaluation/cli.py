"""Evaluation CLI.

    python -m app.evaluation.cli benchmark --dataset banking-msa
    python -m app.evaluation.cli sweep --parameter min_similarity_clause
    python -m app.evaluation.cli ablate reranker
    python -m app.evaluation.cli calibrate --run evaluation-results/latest/evaluation.json
    python -m app.evaluation.cli dataset list

Exit codes are the contract with CI: **0 when the gate passes, 1 when it fails,
2 when the run could not be executed at all**. The third matters - a build should
not read "the database was unreachable" as "quality regressed", and a two-state
exit code forces exactly that confusion.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import typer

from app.core.logging import get_logger

logger = get_logger(__name__)

app = typer.Typer(
    name="evaluation",
    help="Retrieval and answer quality benchmarking.",
    no_args_is_help=True,
    add_completion=False,
)
dataset_app = typer.Typer(help="Golden dataset management.", no_args_is_help=True)
app.add_typer(dataset_app, name="dataset")

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_COULD_NOT_RUN = 2


def _echo(message: str, *, err: bool = False) -> None:
    typer.echo(message, err=err)


# =============================================================================
# benchmark
# =============================================================================
@app.command()
def benchmark(
    dataset: str = typer.Option("smoke", help="Dataset name or path."),
    tag: list[str] = typer.Option([], "--tag", help="Only cases with these tags."),
    limit: int = typer.Option(0, help="Stop after N cases. 0 means all."),
    concurrency: int = typer.Option(4, help="Cases in flight at once."),
    retrieval_only: bool = typer.Option(
        False, "--retrieval-only", help="Skip generation. Cheaper and deterministic."
    ),
    label: str = typer.Option("", help="Run label; defaults to a UTC timestamp."),
    output: str = typer.Option("", help="Output directory."),
    baseline: str = typer.Option("", help="Baseline file to compare against."),
    update_baseline: bool = typer.Option(
        False,
        "--update-baseline",
        help="Write this run as the new baseline. Never do this on a failing run.",
    ),
    fail_on_regression: bool = typer.Option(
        True, help="Exit non-zero when a blocking gate regresses."
    ),
) -> None:
    """Run the golden dataset and produce every report."""
    from app.evaluation.benchmark import BenchmarkOptions, run_benchmark

    options = BenchmarkOptions(
        dataset=dataset,
        tags=list(tag),
        limit=limit or None,
        concurrency=concurrency,
        retrieval_only=retrieval_only,
        label=label,
        output_dir=Path(output) if output else None,
        baseline_path=Path(baseline) if baseline else None,
        update_baseline=update_baseline,
    )

    try:
        outcome = asyncio.run(run_benchmark(options))
    except Exception as exc:
        _echo(f"The benchmark could not run: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(EXIT_COULD_NOT_RUN) from exc

    scorecard = outcome.scorecard
    _echo("")
    _echo(f"  {scorecard.dataset}  -  {scorecard.cases} cases in {scorecard.duration_seconds:.0f}s")
    if scorecard.failures:
        _echo(f"  {scorecard.failures} case(s) failed to execute")
    _echo("")
    for name in (
        "recall@5",
        "recall@10",
        "mrr",
        "ndcg@10",
        "citation_precision",
        "hallucination_rate",
        "guardrail_accuracy",
        "latency_p95_ms",
        "mean_cost_usd",
    ):
        value = scorecard.metrics_for_comparison().get(name)
        if value is None:
            continue
        rendered = f"{value:.0f} ms" if name.endswith("_ms") else f"{value:.4f}"
        _echo(f"    {name:<24} {rendered}")
    _echo("")
    _echo(f"  composite {scorecard.composite:.4f}")
    _echo(f"  {outcome.regression.summary()}")
    _echo("")
    _echo(f"  Reports: {outcome.output_dir}")

    if not outcome.passed:
        for comparison in outcome.regression.blocking_failures:
            _echo(f"    {comparison.metric}: {comparison.reason}", err=True)
        if fail_on_regression:
            raise typer.Exit(EXIT_GATE_FAILED)


# =============================================================================
# sweep
# =============================================================================
@app.command()
def sweep(
    dataset: str = typer.Option("smoke", help="Dataset name or path."),
    parameter: str = typer.Option(
        "", help="One parameter to sweep. Omitted means every default sweep."
    ),
    values: str = typer.Option("", help="Comma-separated values. Omitted uses the defaults."),
    concurrency: int = typer.Option(4),
    with_generation: bool = typer.Option(
        False,
        "--with-generation",
        help="Also generate answers. Much slower; only needed when answer quality is in question.",
    ),
    output: str = typer.Option("evaluation-results/sweep"),
) -> None:
    """Sweep a threshold and report the best-performing configuration."""
    from app.evaluation.benchmark.sweep import DEFAULT_SWEEPS, sweep_all, sweep_parameter
    from app.evaluation.dataset import load_dataset
    from app.evaluation.reports import write_sweep_report

    try:
        golden = load_dataset(dataset)
        if parameter:
            points = (
                tuple(float(value) for value in values.split(",") if value.strip())
                if values
                else DEFAULT_SWEEPS.get(parameter)
            )
            if not points:
                _echo(
                    f"No default sweep for '{parameter}'. Pass --values.",
                    err=True,
                )
                raise typer.Exit(EXIT_COULD_NOT_RUN)
            results = [
                asyncio.run(
                    sweep_parameter(
                        golden,
                        parameter,
                        points,
                        retrieval_only=not with_generation,
                        concurrency=concurrency,
                    )
                )
            ]
        else:
            results = asyncio.run(
                sweep_all(golden, retrieval_only=not with_generation, concurrency=concurrency)
            )
    except typer.Exit:
        raise
    except Exception as exc:
        _echo(f"The sweep could not run: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(EXIT_COULD_NOT_RUN) from exc

    _echo("")
    for result in results:
        _echo(f"  {result.parameter}")
        for point in result.points:
            marker = "  <-- best" if result.best and point.value == result.best.value else ""
            _echo(
                f"    {point.value:<8} composite {point.scorecard.composite:.4f}"
                f"  recall@10 {point.scorecard.key_metrics['recall@10']:.4f}{marker}"
            )
        _echo(f"    {result.recommendation}")
        _echo("")

    path = write_sweep_report(results, Path(output))
    _echo(f"  Written: {path}")


# =============================================================================
# ablate
# =============================================================================
@app.command()
def ablate(
    what: str = typer.Argument("reranker", help="'reranker' or 'embedding'."),
    dataset: str = typer.Option("smoke"),
    concurrency: int = typer.Option(4),
    models: str = typer.Option(
        "", help="For 'embedding': path to a JSON array of arm definitions."
    ),
    output: str = typer.Option("evaluation-results/ablation"),
) -> None:
    """Compare two configurations on the same dataset and report the gain."""
    from app.evaluation.benchmark.sweep import embedding_ablation, reranker_ablation
    from app.evaluation.dataset import load_dataset

    try:
        golden = load_dataset(dataset)
        directory = Path(output)
        directory.mkdir(parents=True, exist_ok=True)

        if what == "reranker":
            result = asyncio.run(reranker_ablation(golden, concurrency=concurrency))
            payload: dict[str, Any] = result.as_dict()
            _echo("")
            _echo(f"  {result.verdict}")
        elif what == "embedding":
            if not models:
                _echo("--models is required for an embedding ablation.", err=True)
                raise typer.Exit(EXIT_COULD_NOT_RUN)
            definitions = json.loads(Path(models).read_text(encoding="utf-8"))
            arms = asyncio.run(embedding_ablation(golden, definitions, concurrency=concurrency))
            payload = {
                "arms": [
                    {
                        "name": arm.name,
                        "composite": round(arm.scorecard.composite, 4),
                        "metrics": arm.scorecard.metrics_for_comparison(),
                        "configuration": arm.scorecard.configuration,
                    }
                    for arm in arms
                ],
                "recommended": max(arms, key=lambda arm: arm.scorecard.composite).name
                if arms
                else None,
            }
            _echo("")
            for arm in arms:
                _echo(
                    f"    {arm.name:<28} composite {arm.scorecard.composite:.4f}"
                    f"  recall@10 {arm.scorecard.key_metrics['recall@10']:.4f}"
                )
            _echo("")
            _echo(
                "  NOTE: vectors from two models do not share a space. Unless the "
                "index was rebuilt between arms, every arm but the live one is "
                "measuring mismatched vectors."
            )
        else:
            _echo(f"Unknown ablation '{what}'. Use 'reranker' or 'embedding'.", err=True)
            raise typer.Exit(EXIT_COULD_NOT_RUN)
    except typer.Exit:
        raise
    except Exception as exc:
        _echo(f"The ablation could not run: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(EXIT_COULD_NOT_RUN) from exc

    path = directory / f"{what}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _echo(f"  Written: {path}")


# =============================================================================
# calibrate
# =============================================================================
@app.command()
def calibrate(
    run: str = typer.Option(..., help="Path to a stored evaluation.json."),
    bins: int = typer.Option(10, help="Reliability diagram buckets."),
    output: str = typer.Option("evaluation-results/calibration"),
) -> None:
    """Fit and score calibrators against a stored run. Changes nothing at runtime."""
    from app.evaluation.benchmark.benchmark import load_run
    from app.evaluation.metrics.calibration import build_calibration_report

    try:
        stored = load_run(run)
        report = build_calibration_report(stored.results, bins=bins)
    except Exception as exc:
        _echo(f"Could not calibrate: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(EXIT_COULD_NOT_RUN) from exc

    _echo("")
    _echo(f"  samples  {report.raw.samples}")
    _echo(
        f"  ECE      {report.raw.ece:.4f}   (Platt {report.platt_metrics.ece:.4f}, "
        f"isotonic {report.isotonic_metrics.ece:.4f})"
    )
    _echo(f"  MCE      {report.raw.mce:.4f}")
    _echo(f"  Brier    {report.raw.brier:.4f}   (base rate {report.raw.base_rate:.4f})")
    _echo(f"  bias     {report.raw.mean_bias:+.4f}   (positive = over-confident)")
    _echo("")
    _echo(f"  {report.recommendation}")

    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "calibration.json"
    path.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    _echo(f"  Written: {path}")


# =============================================================================
# load test
# =============================================================================
@app.command("load-test")
def load_test(
    base_url: str = typer.Option(..., help="Target, e.g. http://localhost:8000"),
    token: str = typer.Option(..., help="Bearer token for an account with project access."),
    dataset: str = typer.Option("smoke"),
    users: str = typer.Option("100,500,1000", help="Concurrency levels to step through."),
    duration: int = typer.Option(60, help="Seconds per level."),
    output: str = typer.Option("evaluation-results/load"),
) -> None:
    """Drive concurrent traffic at a running deployment and record what happens."""
    from app.evaluation.benchmark.load import LoadTestOptions, run_load_test

    try:
        levels = [int(value) for value in users.split(",") if value.strip()]
        report = asyncio.run(
            run_load_test(
                LoadTestOptions(
                    base_url=base_url,
                    token=token,
                    dataset=dataset,
                    levels=levels,
                    seconds_per_level=duration,
                )
            )
        )
    except Exception as exc:
        _echo(f"The load test could not run: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(EXIT_COULD_NOT_RUN) from exc

    _echo("")
    for level in report["levels"]:
        _echo(
            f"    {level['users']:>5} users   "
            f"{level['throughput_rps']:>6.1f} rps   "
            f"p50 {level['p50_ms']:>6.0f} ms   p95 {level['p95_ms']:>7.0f} ms   "
            f"errors {level['error_rate']:.1%}"
        )
    _echo("")
    _echo(f"  {report['verdict']}")

    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "load.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _echo(f"  Written: {path}")


# =============================================================================
# dataset
# =============================================================================
@dataset_app.command("list")
def dataset_list() -> None:
    """Every bundled dataset, with its statistics."""
    from app.evaluation.dataset import available_datasets, load_dataset

    names = available_datasets()
    if not names:
        _echo("No bundled datasets.")
        return

    for name in names:
        try:
            stats = load_dataset(name).statistics()
        except Exception as exc:  # noqa: BLE001 - one broken file must not hide the rest
            _echo(f"  {name:<24} INVALID: {exc}")
            continue
        _echo(
            f"  {name:<24} {stats['cases']:>5} cases  "
            f"{stats['answerable']:>4} answerable  "
            f"{stats['unanswerable']:>4} negative  "
            f"{stats['with_relevance_signal']:>4} scoreable"
        )


@dataset_app.command("validate")
def dataset_validate(source: str = typer.Argument(..., help="Dataset name or path.")) -> None:
    """Load a dataset and report what it can and cannot score."""
    from app.evaluation.dataset import load_dataset

    try:
        golden = load_dataset(source)
    except Exception as exc:
        _echo(f"INVALID: {exc}", err=True)
        raise typer.Exit(EXIT_COULD_NOT_RUN) from exc

    stats = golden.statistics()
    _echo(f"  {golden.identifier}: {stats['cases']} cases")
    _echo(f"    answerable          {stats['answerable']}")
    _echo(f"    negative            {stats['unanswerable']}")
    _echo(f"    retrieval-scoreable {stats['with_relevance_signal']}")
    for tag, count in stats["tags"].items():
        _echo(f"    tag {tag:<18} {count}")

    if not stats["unanswerable"]:
        _echo(
            "\n  WARNING: no negative cases. The guardrail cannot be scored, so a "
            "regression that makes the platform answer questions it should decline "
            "would be invisible."
        )
    if stats["with_relevance_signal"] < stats["cases"] / 2:
        _echo(
            "\n  WARNING: fewer than half the cases carry retrieval expectations. "
            "Recall and MRR are computed over a small subset."
        )


@dataset_app.command("generate")
def dataset_generate(
    project_id: str = typer.Option(..., help="Project to build a skeleton from."),
    output: str = typer.Option(..., help="Where to write the dataset."),
    name: str = typer.Option("generated"),
    per_contract: int = typer.Option(3, help="Question stubs per contract."),
    limit: int = typer.Option(50, help="Contracts to include."),
) -> None:
    """Bootstrap a dataset skeleton from a project's real contracts and clauses.

    Writes real ids and real headings with **placeholder questions**, which is the
    half of the work a machine can do. Someone still has to write the questions -
    a generated question tests whatever the generator understood, which is not
    what a benchmark is for.
    """
    from app.evaluation.dataset.bootstrap import generate_skeleton

    try:
        golden = asyncio.run(
            generate_skeleton(
                project_id=uuid.UUID(project_id),
                name=name,
                per_contract=per_contract,
                contract_limit=limit,
            )
        )
    except Exception as exc:
        _echo(f"Could not generate: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(EXIT_COULD_NOT_RUN) from exc

    from app.evaluation.dataset import write_dataset

    path = write_dataset(golden, output)
    _echo(f"  {len(golden)} case stubs written to {path}")
    _echo("  Every question is a placeholder. Edit them before running a benchmark.")


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(app() or 0)

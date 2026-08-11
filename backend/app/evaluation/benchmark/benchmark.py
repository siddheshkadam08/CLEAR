"""The benchmark: run, score, compare, report.

One entry point that does the whole job, because the sequence matters and
getting it wrong in a shell script is easy. In particular: **results are written
before the gate is evaluated**. A build that fails on a regression must still
leave behind the report that explains it, and a gate that exits before writing
its evidence is a gate that produces an argument instead of a diagnosis.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.evaluation.benchmark.baseline import (
    DEFAULT_GATES,
    Baseline,
    Gate,
    RegressionReport,
    compare,
    load_baseline,
    save_baseline,
)
from app.evaluation.dataset.loader import load_dataset
from app.evaluation.dataset.models import GoldenDataset
from app.evaluation.metrics.scorecard import Scorecard, build_scorecard
from app.evaluation.runner.result import RunResult
from app.evaluation.runner.runner import EvaluationRunner, RunnerOptions

logger = get_logger(__name__)

#: Where a run's artefacts land by default, when `--output` is not given.
#:
#: Read from the same setting the API reads, `EVALUATION_RESULTS_DIR`, so the
#: writer and the reader agree by construction. It used to be the bare relative
#: `Path("evaluation-results")`, resolved against whatever directory the CLI
#: happened to run in - which the API then could not find unless it had been
#: started from that same directory. The dashboard reported "no benchmark has
#: been recorded" with the files sitting one directory away, and neither side
#: had anything to log, because from each one's point of view nothing was wrong.
def default_output_root() -> Path:
    return Path(get_settings().evaluation_results_dir)


@dataclass(slots=True)
class BenchmarkOptions:
    dataset: str = "smoke"
    tags: list[str] = field(default_factory=list)
    limit: int | None = None
    concurrency: int = 4
    retrieval_only: bool = False
    label: str = ""
    output_dir: Path | None = None
    baseline_path: Path | None = None
    #: Write this run's scorecard as the new baseline. Only ever set deliberately
    #: - a build that updated the baseline on every green run would ratchet a slow
    #: decline into the reference and never fail again.
    update_baseline: bool = False
    gates: tuple[Gate, ...] = DEFAULT_GATES


@dataclass(slots=True)
class BenchmarkOutcome:
    """Everything one benchmark produced."""

    dataset: GoldenDataset
    run: RunResult
    scorecard: Scorecard
    regression: RegressionReport
    output_dir: Path
    written: list[Path] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.regression.passed

    @property
    def exit_code(self) -> int:
        return 0 if self.passed else 1


async def run_benchmark(options: BenchmarkOptions) -> BenchmarkOutcome:
    """Load, run, score, compare and write. The whole job."""
    dataset = load_dataset(options.dataset)
    if options.tags or options.limit:
        dataset = dataset.filter(tags=options.tags or None, limit=options.limit)

    label = options.label or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = options.output_dir or (default_output_root() / label)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "benchmark_started",
        dataset=dataset.identifier,
        cases=len(dataset),
        label=label,
        retrieval_only=options.retrieval_only,
    )

    runner = EvaluationRunner(
        RunnerOptions(
            concurrency=options.concurrency,
            retrieval_only=options.retrieval_only,
            label=label,
        )
    )
    run = await runner.run(dataset)
    scorecard = build_scorecard(run)

    baseline_path = options.baseline_path or _default_baseline_path(dataset)
    baseline = load_baseline(baseline_path)
    regression = compare(scorecard, baseline, gates=options.gates)

    outcome = BenchmarkOutcome(
        dataset=dataset,
        run=run,
        scorecard=scorecard,
        regression=regression,
        output_dir=output_dir,
    )

    # Written before the gate is acted on, so a failing build still explains
    # itself.
    outcome.written = _write_all(outcome)

    if options.update_baseline:
        save_baseline(Baseline.from_scorecard(scorecard, label=label), baseline_path)
        outcome.written.append(baseline_path)

    _export_metrics(scorecard, regression)

    logger.info(
        "benchmark_complete",
        dataset=dataset.identifier,
        composite=round(scorecard.composite, 4),
        passed=regression.passed,
        summary=regression.summary(),
    )
    return outcome


def _export_metrics(scorecard: Scorecard, regression: RegressionReport) -> None:
    """Publish the scorecard to the Prometheus registry.

    Gauges rather than counters: each run *replaces* the last, because a
    benchmark value is a state of the world and not an accumulating event. Only
    useful when the process scraping this is long-lived - a CLI run exits before
    the next scrape - so the real consumers are the scheduled in-cluster run and
    the admin dashboard.
    """
    from app.core import metrics

    for name, value in scorecard.metrics_for_comparison().items():
        metrics.evaluation_metric.labels(dataset=scorecard.dataset, metric=name).set(value)
    metrics.evaluation_runs_total.labels(
        dataset=scorecard.dataset, result="pass" if regression.passed else "fail"
    ).inc()


def _write_all(outcome: BenchmarkOutcome) -> list[Path]:
    """Every report format. Imported here to keep the reports layer optional."""
    from app.evaluation.reports import (
        write_csv_reports,
        write_html_reports,
        write_json_report,
        write_markdown_report,
        write_summary_json,
    )

    written = [
        write_json_report(outcome.scorecard, outcome.run, outcome.regression, outcome.output_dir),
        write_summary_json(outcome.scorecard, outcome.regression, outcome.output_dir),
        write_markdown_report(outcome.scorecard, outcome.regression, outcome.output_dir),
    ]
    written.extend(write_csv_reports(outcome.scorecard, outcome.run, outcome.output_dir))
    written.extend(
        write_html_reports(outcome.scorecard, outcome.run, outcome.regression, outcome.output_dir)
    )
    return written


def _default_baseline_path(dataset: GoldenDataset) -> Path:
    """One baseline per dataset, versioned with it.

    Keying on ``name@version`` rather than on ``name`` is deliberate: adding
    fifty questions to a dataset changes what every average means, and comparing
    across that boundary produces a regression report about the dataset rather
    than about the code.
    """
    safe = dataset.identifier.replace("@", "-").replace("/", "-")
    return Path("backend/app/evaluation/baselines") / f"{safe}.json"


def load_run(path: str | Path) -> RunResult:
    """Re-read a stored run, so metrics can be recomputed without re-running it.

    The reason the result format is serialisable at all: re-scoring a stored run
    under a different threshold costs nothing, while re-running it costs an hour
    and a bill.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return RunResult.from_dict(payload.get("run") or payload)


__all__ = [
    "default_output_root",
    "BenchmarkOptions",
    "BenchmarkOutcome",
    "load_run",
    "run_benchmark",
]

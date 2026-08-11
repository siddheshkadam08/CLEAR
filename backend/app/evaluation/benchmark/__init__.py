"""Benchmark orchestration: runs, baselines, sweeps and ablations."""

from app.evaluation.benchmark.baseline import (
    DEFAULT_GATES,
    Baseline,
    Direction,
    Gate,
    MetricComparison,
    RegressionReport,
    Verdict,
    compare,
    load_baseline,
    save_baseline,
)
from app.evaluation.benchmark.benchmark import (
    default_output_root,
    BenchmarkOptions,
    BenchmarkOutcome,
    load_run,
    run_benchmark,
)
from app.evaluation.benchmark.sweep import (
    DEFAULT_SWEEPS,
    AblationArm,
    AblationResult,
    SweepPoint,
    SweepResult,
    embedding_ablation,
    reranker_ablation,
    sweep_all,
    sweep_parameter,
)

__all__ = [
    "DEFAULT_GATES",
    "default_output_root",
    "DEFAULT_SWEEPS",
    "AblationArm",
    "AblationResult",
    "Baseline",
    "BenchmarkOptions",
    "BenchmarkOutcome",
    "Direction",
    "Gate",
    "MetricComparison",
    "RegressionReport",
    "SweepPoint",
    "SweepResult",
    "Verdict",
    "compare",
    "embedding_ablation",
    "load_baseline",
    "load_run",
    "reranker_ablation",
    "run_benchmark",
    "save_baseline",
    "sweep_all",
    "sweep_parameter",
]

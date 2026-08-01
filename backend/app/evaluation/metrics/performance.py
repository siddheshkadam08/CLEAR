"""Latency and cost.

Percentiles, not means. A mean latency hides the tail, and the tail is what a
user experiences as "the Copilot is slow" - a p50 of four seconds with a p99 of
forty is a different product from a flat six, and the mean of both is similar.

Latency here is measured **inside the process**, from the start of ``prepare`` to
the end of generation. It excludes HTTP, auth and serialisation, so it is a floor
on what a user sees rather than the figure itself. The load-test driver measures
the real thing over the wire; this measures where the time goes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.evaluation.runner.result import CaseResult

#: Stages recorded by the pipeline, in the order they run.
STAGES = ("analysis_ms", "retrieval_ms", "rerank_ms", "inference_ms", "total_ms")


def percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile.

    Interpolated rather than nearest-rank: on a fifty-case run the nearest-rank
    p95 is the second-largest value and moves in visible jumps between runs,
    which makes a latency regression gate fire on noise.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


@dataclass(slots=True)
class StageLatency:
    """Where the milliseconds went, for one pipeline stage."""

    stage: str
    mean: float = 0.0
    p50: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    max: float = 0.0
    #: Share of total_ms this stage accounts for. The number that says what to
    #: optimise next.
    share: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "mean_ms": round(self.mean, 1),
            "p50_ms": round(self.p50, 1),
            "p95_ms": round(self.p95, 1),
            "p99_ms": round(self.p99, 1),
            "max_ms": round(self.max, 1),
            "share": round(self.share, 4),
        }


@dataclass(slots=True)
class PerformanceMetrics:
    """End-to-end latency and cost over a run."""

    cases: int = 0
    latency_mean: float = 0.0
    latency_p50: float = 0.0
    latency_p95: float = 0.0
    latency_p99: float = 0.0
    latency_max: float = 0.0

    stages: list[StageLatency] = field(default_factory=list)

    total_cost_usd: float = 0.0
    mean_cost_usd: float = 0.0
    p95_cost_usd: float = 0.0
    total_tokens: int = 0
    mean_tokens: float = 0.0

    #: Cases that cost the most. Where a cost regression is diagnosed.
    most_expensive_case_ids: list[str] = field(default_factory=list)
    slowest_case_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            "latency_mean_ms": round(self.latency_mean, 1),
            "latency_p50_ms": round(self.latency_p50, 1),
            "latency_p95_ms": round(self.latency_p95, 1),
            "latency_p99_ms": round(self.latency_p99, 1),
            "latency_max_ms": round(self.latency_max, 1),
            "stages": [stage.as_dict() for stage in self.stages],
            "total_cost_usd": round(self.total_cost_usd, 6),
            "mean_cost_usd": round(self.mean_cost_usd, 6),
            "p95_cost_usd": round(self.p95_cost_usd, 6),
            "total_tokens": self.total_tokens,
            "mean_tokens": round(self.mean_tokens, 1),
            "most_expensive_cases": list(self.most_expensive_case_ids[:10]),
            "slowest_cases": list(self.slowest_case_ids[:10]),
        }


def compute_performance_metrics(results: list[CaseResult]) -> PerformanceMetrics:
    metrics = PerformanceMetrics()
    usable = [result for result in results if result.ok]
    if not usable:
        return metrics

    metrics.cases = len(usable)
    latencies = [float(result.latency_ms) for result in usable]
    metrics.latency_mean = sum(latencies) / len(latencies)
    metrics.latency_p50 = percentile(latencies, 0.50)
    metrics.latency_p95 = percentile(latencies, 0.95)
    metrics.latency_p99 = percentile(latencies, 0.99)
    metrics.latency_max = max(latencies)

    total_ms = sum(latencies) or 1.0
    for stage in STAGES:
        if stage == "total_ms":
            continue
        values = [float(result.timings.get(stage, 0)) for result in usable]
        if not any(values):
            continue
        metrics.stages.append(
            StageLatency(
                stage=stage.removesuffix("_ms"),
                mean=sum(values) / len(values),
                p50=percentile(values, 0.50),
                p95=percentile(values, 0.95),
                p99=percentile(values, 0.99),
                max=max(values),
                share=sum(values) / total_ms,
            )
        )

    costs = [result.cost_usd for result in usable]
    metrics.total_cost_usd = sum(costs)
    metrics.mean_cost_usd = metrics.total_cost_usd / len(costs)
    metrics.p95_cost_usd = percentile(costs, 0.95)
    metrics.total_tokens = sum(result.tokens for result in usable)
    metrics.mean_tokens = metrics.total_tokens / len(usable)

    metrics.most_expensive_case_ids = [
        result.case.id
        for result in sorted(usable, key=lambda r: r.cost_usd, reverse=True)
        if result.cost_usd > 0
    ]
    metrics.slowest_case_ids = [
        result.case.id for result in sorted(usable, key=lambda r: r.latency_ms, reverse=True)
    ]
    return metrics


__all__ = [
    "STAGES",
    "PerformanceMetrics",
    "StageLatency",
    "compute_performance_metrics",
    "percentile",
]

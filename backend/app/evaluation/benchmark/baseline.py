"""Baselines and regression detection - the part that fails the build.

A gate is only useful if it is hard to argue with, so three decisions here are
deliberately rigid:

**Direction is declared per metric, not inferred.** ``hallucination_rate`` going
up is bad; ``recall@10`` going up is good; ``latency_p95_ms`` going up is bad
past a tolerance. A gate that guessed from the name would eventually guess wrong
on a metric added later, and it would guess in silence.

**Tolerance is absolute, not relative.** A relative tolerance on a metric near
zero is meaningless - a hallucination rate moving from 0.001 to 0.002 is a 100%
regression and nothing at all. Latency and cost are the exceptions, where
relative movement is what people actually reason about, so they declare both.

**A missing metric fails.** If the baseline names a metric the run did not
produce, that is a report-shape change, and passing the build on the grounds that
the number is absent is how a gate quietly stops gating.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.evaluation.metrics.scorecard import Scorecard

logger = get_logger(__name__)


class Direction(StrEnum):
    HIGHER_IS_BETTER = "higher"
    LOWER_IS_BETTER = "lower"


class Verdict(StrEnum):
    IMPROVED = "improved"
    REGRESSED = "regressed"
    UNCHANGED = "unchanged"
    MISSING = "missing"
    NEW = "new"


@dataclass(frozen=True, slots=True)
class Gate:
    """One metric's regression rule."""

    metric: str
    direction: Direction
    #: Movement smaller than this is noise, not a change.
    tolerance: float = 0.01
    #: Relative tolerance, for metrics where proportional movement is the natural
    #: reading. When both are set, the *larger* allowance applies - a 5 ms move on
    #: a 4-second p95 should not fail a build.
    relative_tolerance: float | None = None
    #: A failing gate blocks the build. A non-blocking one is reported and
    #: watched, which is the right setting for a metric whose variance is not yet
    #: understood.
    blocking: bool = True
    #: Absolute floor/ceiling regardless of the baseline. Guards against a slow
    #: slide that never trips the per-run tolerance.
    absolute_limit: float | None = None


#: The default gate set. Retrieval and citation quality block; latency and cost
#: block on generous tolerances; planner and calibration are watched.
DEFAULT_GATES: tuple[Gate, ...] = (
    Gate("recall@10", Direction.HIGHER_IS_BETTER, tolerance=0.02),
    Gate("recall@5", Direction.HIGHER_IS_BETTER, tolerance=0.02),
    Gate("mrr", Direction.HIGHER_IS_BETTER, tolerance=0.02),
    Gate("ndcg@10", Direction.HIGHER_IS_BETTER, tolerance=0.02),
    Gate("citation_precision", Direction.HIGHER_IS_BETTER, tolerance=0.03),
    Gate("citation_recall", Direction.HIGHER_IS_BETTER, tolerance=0.05, blocking=False),
    Gate("hallucination_rate", Direction.LOWER_IS_BETTER, tolerance=0.01),
    Gate("broken_citations", Direction.LOWER_IS_BETTER, tolerance=0.0, absolute_limit=0.0),
    Gate("guardrail_accuracy", Direction.HIGHER_IS_BETTER, tolerance=0.03),
    Gate("false_accept_rate", Direction.LOWER_IS_BETTER, tolerance=0.02),
    Gate(
        "latency_p95_ms",
        Direction.LOWER_IS_BETTER,
        tolerance=250.0,
        relative_tolerance=0.20,
    ),
    Gate(
        "mean_cost_usd",
        Direction.LOWER_IS_BETTER,
        tolerance=0.0005,
        relative_tolerance=0.25,
    ),
    Gate("composite", Direction.HIGHER_IS_BETTER, tolerance=0.02),
    # Watched, not blocking: the planner labels are optional in most datasets, so
    # these move with dataset composition as much as with code.
    Gate("intent_accuracy", Direction.HIGHER_IS_BETTER, tolerance=0.05, blocking=False),
    Gate("document_type_accuracy", Direction.HIGHER_IS_BETTER, tolerance=0.05, blocking=False),
    Gate("false_filtering_rate", Direction.LOWER_IS_BETTER, tolerance=0.05, blocking=False),
    Gate("ece", Direction.LOWER_IS_BETTER, tolerance=0.05, blocking=False),
    Gate("duplicate_rate", Direction.LOWER_IS_BETTER, tolerance=0.05, blocking=False),
)


@dataclass(slots=True)
class MetricComparison:
    """One metric, then and now."""

    metric: str
    baseline: float | None
    current: float | None
    verdict: Verdict
    direction: Direction | None = None
    blocking: bool = False
    reason: str = ""

    @property
    def delta(self) -> float | None:
        if self.baseline is None or self.current is None:
            return None
        return self.current - self.baseline

    @property
    def failed(self) -> bool:
        return self.blocking and self.verdict in {Verdict.REGRESSED, Verdict.MISSING}

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "baseline": self.baseline,
            "current": self.current,
            "delta": round(self.delta, 6) if self.delta is not None else None,
            "verdict": self.verdict.value,
            "blocking": self.blocking,
            "reason": self.reason,
        }


@dataclass(slots=True)
class RegressionReport:
    """The verdict on a run, and why."""

    dataset: str
    baseline_label: str = ""
    current_label: str = ""
    comparisons: list[MetricComparison] = field(default_factory=list)

    @property
    def regressions(self) -> list[MetricComparison]:
        return [c for c in self.comparisons if c.verdict is Verdict.REGRESSED]

    @property
    def improvements(self) -> list[MetricComparison]:
        return [c for c in self.comparisons if c.verdict is Verdict.IMPROVED]

    @property
    def blocking_failures(self) -> list[MetricComparison]:
        return [c for c in self.comparisons if c.failed]

    @property
    def passed(self) -> bool:
        return not self.blocking_failures

    def summary(self) -> str:
        if self.passed:
            return (
                f"PASS - {len(self.improvements)} improved, "
                f"{len(self.regressions)} regressed (none blocking)."
            )
        names = ", ".join(c.metric for c in self.blocking_failures)
        return f"FAIL - blocking regressions: {names}."

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "baseline_label": self.baseline_label,
            "current_label": self.current_label,
            "passed": self.passed,
            "summary": self.summary(),
            "improved": len(self.improvements),
            "regressed": len(self.regressions),
            "blocking_failures": [c.metric for c in self.blocking_failures],
            "comparisons": [c.as_dict() for c in self.comparisons],
        }


# =============================================================================
# Storage
# =============================================================================
@dataclass(slots=True)
class Baseline:
    """The reference a run is judged against."""

    dataset: str
    label: str
    recorded_at: str
    metrics: dict[str, float] = field(default_factory=dict)
    configuration: dict[str, Any] = field(default_factory=dict)
    cases: int = 0

    @classmethod
    def from_scorecard(cls, scorecard: Scorecard, *, label: str = "") -> Baseline:
        return cls(
            dataset=scorecard.dataset,
            label=label or scorecard.label,
            recorded_at=datetime.now(UTC).isoformat(),
            metrics=dict(scorecard.metrics_for_comparison()),
            configuration=dict(scorecard.configuration),
            cases=scorecard.cases,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "label": self.label,
            "recorded_at": self.recorded_at,
            "cases": self.cases,
            "configuration": self.configuration,
            "metrics": {name: round(value, 6) for name, value in self.metrics.items()},
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Baseline:
        return cls(
            dataset=str(payload.get("dataset") or ""),
            label=str(payload.get("label") or ""),
            recorded_at=str(payload.get("recorded_at") or ""),
            metrics={str(k): float(v) for k, v in (payload.get("metrics") or {}).items()},
            configuration=dict(payload.get("configuration") or {}),
            cases=int(payload.get("cases") or 0),
        )


def save_baseline(baseline: Baseline, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(baseline.as_dict(), indent=2) + "\n", encoding="utf-8")
    logger.info("baseline_saved", dataset=baseline.dataset, path=str(target))
    return target


def load_baseline(path: str | Path) -> Baseline | None:
    """Load a baseline, or ``None`` when there is not one yet.

    Absence is not an error: the first run on a new dataset has nothing to
    compare against, and that run's job is to *become* the baseline.
    """
    source = Path(path)
    if not source.exists():
        return None
    return Baseline.from_dict(json.loads(source.read_text(encoding="utf-8")))


# =============================================================================
# Comparison
# =============================================================================
def compare(
    scorecard: Scorecard,
    baseline: Baseline | None,
    *,
    gates: tuple[Gate, ...] = DEFAULT_GATES,
) -> RegressionReport:
    """Judge a run against a baseline."""
    report = RegressionReport(
        dataset=scorecard.dataset,
        baseline_label=baseline.label if baseline else "",
        current_label=scorecard.label,
    )
    current = scorecard.metrics_for_comparison()

    for gate in gates:
        value = current.get(gate.metric)

        if value is None:
            report.comparisons.append(
                MetricComparison(
                    metric=gate.metric,
                    baseline=baseline.metrics.get(gate.metric) if baseline else None,
                    current=None,
                    verdict=Verdict.MISSING,
                    direction=gate.direction,
                    blocking=gate.blocking,
                    reason=(
                        "the run produced no value for this metric - the report shape "
                        "changed, or the dataset cannot score it"
                    ),
                )
            )
            continue

        if gate.absolute_limit is not None and _breaches_limit(gate, value):
            report.comparisons.append(
                MetricComparison(
                    metric=gate.metric,
                    baseline=baseline.metrics.get(gate.metric) if baseline else None,
                    current=value,
                    verdict=Verdict.REGRESSED,
                    direction=gate.direction,
                    blocking=gate.blocking,
                    reason=f"breaches its absolute limit of {gate.absolute_limit}",
                )
            )
            continue

        if baseline is None or gate.metric not in baseline.metrics:
            report.comparisons.append(
                MetricComparison(
                    metric=gate.metric,
                    baseline=None,
                    current=value,
                    verdict=Verdict.NEW,
                    direction=gate.direction,
                    blocking=False,
                    reason="no baseline to compare against",
                )
            )
            continue

        report.comparisons.append(_judge(gate, baseline.metrics[gate.metric], value))

    return report


def _judge(gate: Gate, before: float, after: float) -> MetricComparison:
    allowance = gate.tolerance
    if gate.relative_tolerance is not None:
        allowance = max(allowance, abs(before) * gate.relative_tolerance)

    delta = after - before
    improved = delta > 0 if gate.direction is Direction.HIGHER_IS_BETTER else delta < 0
    magnitude = abs(delta)

    if magnitude <= allowance:
        verdict = Verdict.UNCHANGED
        reason = f"moved {delta:+.4f}, within the {allowance:.4f} tolerance"
    elif improved:
        verdict = Verdict.IMPROVED
        reason = f"moved {delta:+.4f}"
    else:
        verdict = Verdict.REGRESSED
        reason = f"moved {delta:+.4f}, beyond the {allowance:.4f} tolerance"

    return MetricComparison(
        metric=gate.metric,
        baseline=before,
        current=after,
        verdict=verdict,
        direction=gate.direction,
        blocking=gate.blocking,
        reason=reason,
    )


def _breaches_limit(gate: Gate, value: float) -> bool:
    limit = gate.absolute_limit
    if limit is None:
        return False
    if gate.direction is Direction.LOWER_IS_BETTER:
        return value > limit
    return value < limit


__all__ = [
    "DEFAULT_GATES",
    "Baseline",
    "Direction",
    "Gate",
    "MetricComparison",
    "RegressionReport",
    "Verdict",
    "compare",
    "load_baseline",
    "save_baseline",
]

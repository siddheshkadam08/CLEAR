"""The scorecard: every metric for one run, plus the single number on top.

The composite score exists because a leaderboard needs one column and a sweep
needs something to sort by - not because retrieval quality is one-dimensional.
Its weights are stated in the open, in one place, so that an argument about
whether recall matters more than citation precision is an argument about a
constant rather than an argument about a black box.

The flat ``key_metrics`` mapping is what the regression gate compares. Keeping
it flat and explicit means a threshold in CI names a metric that provably exists,
rather than a dotted path that silently resolves to ``None`` when a report shape
changes and passes every build thereafter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.evaluation.metrics.calibration import CalibrationReport, build_calibration_report
from app.evaluation.metrics.citation import CitationMetrics, compute_citation_metrics
from app.evaluation.metrics.performance import (
    PerformanceMetrics,
    compute_performance_metrics,
)
from app.evaluation.metrics.planner import (
    GuardrailMetrics,
    PlannerMetrics,
    compute_guardrail_metrics,
    compute_planner_metrics,
)
from app.evaluation.metrics.retrieval import RetrievalMetrics, compute_retrieval_metrics
from app.evaluation.runner.result import RunResult

#: Composite weights. Retrieval leads because nothing downstream can recover
#: from a passage that was never retrieved; the guardrail is weighted next
#: because answering when the corpus cannot support it is the failure with legal
#: consequences. Citation precision is weighted below both: it is a property of
#: an answer that already had the right evidence in front of it.
COMPOSITE_WEIGHTS: dict[str, float] = {
    "recall@10": 0.30,
    "mrr": 0.20,
    "ndcg@10": 0.15,
    "citation_precision": 0.15,
    "guardrail_accuracy": 0.15,
    "grounding_rate": 0.05,
}


@dataclass(slots=True)
class Scorecard:
    """Every metric for one run."""

    dataset: str
    label: str = ""
    configuration: dict[str, Any] = field(default_factory=dict)

    cases: int = 0
    failures: int = 0
    duration_seconds: float = 0.0

    retrieval: RetrievalMetrics = field(default_factory=RetrievalMetrics)
    citation: CitationMetrics = field(default_factory=CitationMetrics)
    planner: PlannerMetrics = field(default_factory=PlannerMetrics)
    guardrail: GuardrailMetrics = field(default_factory=GuardrailMetrics)
    performance: PerformanceMetrics = field(default_factory=PerformanceMetrics)
    calibration: CalibrationReport | None = None

    #: Metrics sliced by dataset tag, so a regression confined to one contract
    #: family is visible rather than averaged away across the whole set.
    by_tag: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def composite(self) -> float:
        """One number in [0, 1]. For sorting, never for reassurance."""
        metrics = self.key_metrics
        total_weight = 0.0
        score = 0.0
        for name, weight in COMPOSITE_WEIGHTS.items():
            value = metrics.get(name)
            if value is None:
                continue
            score += weight * value
            total_weight += weight
        return (score / total_weight) if total_weight else 0.0

    @property
    def key_metrics(self) -> dict[str, float]:
        """The flat mapping the regression gate and the leaderboard read.

        ``higher_is_better`` is not encoded here - it lives in the gate, where a
        threshold is declared alongside its direction. Splitting them would let
        the two disagree.
        """
        retrieval = self.retrieval
        return {
            "recall@5": retrieval.recall.get(5, 0.0),
            "recall@10": retrieval.recall.get(10, 0.0),
            "recall@20": retrieval.recall.get(20, 0.0),
            "precision@5": retrieval.precision.get(5, 0.0),
            "precision@10": retrieval.precision.get(10, 0.0),
            "ndcg@10": retrieval.ndcg.get(10, 0.0),
            "mrr": retrieval.mrr,
            "mean_similarity": retrieval.mean_similarity,
            "duplicate_rate": retrieval.mean_duplicate_rate,
            "citation_precision": self.citation.precision,
            "citation_recall": self.citation.recall,
            "hallucination_rate": self.citation.hallucination_rate,
            "grounding_rate": self.citation.fully_grounded_rate,
            "broken_citations": float(self.citation.broken),
            "guardrail_accuracy": self.guardrail.accuracy,
            "false_accept_rate": self.guardrail.false_accept_rate,
            "false_reject_rate": self.guardrail.false_reject_rate,
            "intent_accuracy": self.planner.intent_accuracy or 0.0,
            "document_type_accuracy": self.planner.document_type_accuracy or 0.0,
            "false_filtering_rate": self.planner.false_filtering_rate,
            "latency_p50_ms": self.performance.latency_p50,
            "latency_p95_ms": self.performance.latency_p95,
            "mean_cost_usd": self.performance.mean_cost_usd,
            "ece": self.calibration.raw.ece if self.calibration else 0.0,
            "composite": 0.0,  # replaced below; a property cannot reference itself
        }

    def metrics_for_comparison(self) -> dict[str, float]:
        metrics = self.key_metrics
        metrics["composite"] = self.composite
        return metrics

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "label": self.label,
            "configuration": self.configuration,
            "cases": self.cases,
            "failures": self.failures,
            "duration_seconds": round(self.duration_seconds, 2),
            "composite": round(self.composite, 4),
            "metrics": {
                name: round(value, 6) for name, value in self.metrics_for_comparison().items()
            },
            "retrieval": self.retrieval.as_dict(),
            "citation": self.citation.as_dict(),
            "planner": self.planner.as_dict(),
            "guardrail": self.guardrail.as_dict(),
            "performance": self.performance.as_dict(),
            "calibration": self.calibration.as_dict() if self.calibration else None,
            "by_tag": self.by_tag,
        }


def build_scorecard(run: RunResult, *, answer_threshold: float | None = None) -> Scorecard:
    """Compute every metric for a completed run."""
    threshold = (
        answer_threshold
        if answer_threshold is not None
        else get_settings().retrieval.answer_similarity_threshold
    )
    results = run.results

    scorecard = Scorecard(
        dataset=run.dataset,
        label=run.label,
        configuration=dict(run.configuration),
        cases=len(results),
        failures=len(run.failures),
        duration_seconds=run.duration_seconds,
        retrieval=compute_retrieval_metrics(results),
        citation=compute_citation_metrics(results),
        planner=compute_planner_metrics(results),
        guardrail=compute_guardrail_metrics(results, answer_threshold=threshold),
        performance=compute_performance_metrics(results),
        calibration=build_calibration_report(results),
    )
    scorecard.by_tag = _by_tag(run, threshold)
    return scorecard


def _by_tag(run: RunResult, threshold: float) -> dict[str, dict[str, float]]:
    """Headline metrics per tag.

    Only the headline four: a full scorecard per tag on a dataset with twenty
    tags produces a report nobody reads, and the point of the slice is to answer
    "is one segment regressing" rather than to replace the whole analysis.
    """
    tags = {tag for result in run.results for tag in result.case.tags}
    sliced: dict[str, dict[str, float]] = {}

    for tag in sorted(tags):
        subset = [result for result in run.results if tag in result.case.tags]
        if len(subset) < 3:
            # Too few to average meaningfully; reporting it would invite reading
            # noise as a trend.
            continue
        retrieval = compute_retrieval_metrics(subset)
        citation = compute_citation_metrics(subset)
        guardrail = compute_guardrail_metrics(subset, answer_threshold=threshold)
        sliced[tag] = {
            "cases": float(len(subset)),
            "recall@10": round(retrieval.recall.get(10, 0.0), 4),
            "mrr": round(retrieval.mrr, 4),
            "citation_precision": round(citation.precision, 4),
            "guardrail_accuracy": round(guardrail.accuracy, 4),
        }
    return sliced


__all__ = ["COMPOSITE_WEIGHTS", "Scorecard", "build_scorecard"]

"""Metrics over recorded evaluation results.

Every function in this package is pure: recorded results in, numbers out. No
database, no services, no settings reads outside the one explicit threshold
argument. That is what lets the metrics themselves be tested against hand-worked
examples, which is the only way a quality gate earns the authority to fail a
build.
"""

from app.evaluation.metrics.calibration import (
    CalibrationMetrics,
    CalibrationReport,
    IsotonicCalibrator,
    PlattCalibrator,
    build_calibration_report,
    compute_calibration,
)
from app.evaluation.metrics.citation import CitationMetrics, compute_citation_metrics
from app.evaluation.metrics.performance import (
    PerformanceMetrics,
    compute_performance_metrics,
    percentile,
)
from app.evaluation.metrics.planner import (
    GuardrailMetrics,
    PlannerMetrics,
    compute_guardrail_metrics,
    compute_planner_metrics,
)
from app.evaluation.metrics.relevance import Grade, RelevanceJudge
from app.evaluation.metrics.retrieval import (
    RetrievalMetrics,
    compute_retrieval_metrics,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from app.evaluation.metrics.scorecard import COMPOSITE_WEIGHTS, Scorecard, build_scorecard

__all__ = [
    "COMPOSITE_WEIGHTS",
    "CalibrationMetrics",
    "CalibrationReport",
    "CitationMetrics",
    "Grade",
    "GuardrailMetrics",
    "IsotonicCalibrator",
    "PerformanceMetrics",
    "PlannerMetrics",
    "PlattCalibrator",
    "RelevanceJudge",
    "RetrievalMetrics",
    "Scorecard",
    "build_calibration_report",
    "build_scorecard",
    "compute_calibration",
    "compute_citation_metrics",
    "compute_guardrail_metrics",
    "compute_performance_metrics",
    "compute_planner_metrics",
    "compute_retrieval_metrics",
    "ndcg_at_k",
    "percentile",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
]

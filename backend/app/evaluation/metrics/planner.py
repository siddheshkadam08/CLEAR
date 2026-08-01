"""Planner and guardrail evaluation.

These two are separated from retrieval because they measure *decisions*, not
results, and a decision can be wrong while the result is fine. The document-type
filter is the clearest case: a wrong type that happens to match anyway looks
perfect in a recall figure and is one unlucky question away from excluding the
answer entirely.

The metric that matters most here is **false filtering** - how often a
confidently-applied document-type filter removed the very contract the case
expected. It is the failure mode the confidence threshold exists to prevent, and
it is invisible in every other number in this package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.evaluation.metrics.relevance import RelevanceJudge, normalise
from app.evaluation.runner.result import CaseResult


@dataclass(slots=True)
class PlannerMetrics:
    """How well the planner decided, as distinct from how well retrieval ran."""

    cases: int = 0

    intent_labelled: int = 0
    intent_correct: int = 0

    document_type_labelled: int = 0
    document_type_correct: int = 0

    #: A filter was applied and the case's expected contracts were *not* of that
    #: type. The expensive mistake: it excludes the answer.
    false_filtering: int = 0
    #: A filter was applied and matched. The cheap win.
    correct_filtering: int = 0
    #: A type was detected but the threshold rejected it. Not an error - the
    #: designed behaviour - but its rate says whether the threshold is set sanely.
    filter_declined: int = 0

    #: The classifier was unavailable and the rules ran alone.
    analysis_unavailable: int = 0
    #: The type filter matched nothing and the search was retried without it.
    filter_retries: int = 0
    #: More contracts matched than the pre-filter carries.
    scope_truncations: int = 0

    strategy_counts: dict[str, int] = field(default_factory=dict)
    mode_counts: dict[str, int] = field(default_factory=dict)

    false_filtering_case_ids: list[str] = field(default_factory=list)

    @property
    def intent_accuracy(self) -> float | None:
        if not self.intent_labelled:
            return None
        return self.intent_correct / self.intent_labelled

    @property
    def document_type_accuracy(self) -> float | None:
        if not self.document_type_labelled:
            return None
        return self.document_type_correct / self.document_type_labelled

    @property
    def false_filtering_rate(self) -> float:
        applied = self.false_filtering + self.correct_filtering
        return (self.false_filtering / applied) if applied else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            "intent_accuracy": _round(self.intent_accuracy),
            "intent_labelled": self.intent_labelled,
            "document_type_accuracy": _round(self.document_type_accuracy),
            "document_type_labelled": self.document_type_labelled,
            "filters_applied": self.false_filtering + self.correct_filtering,
            "false_filtering": self.false_filtering,
            "false_filtering_rate": round(self.false_filtering_rate, 4),
            "filter_declined": self.filter_declined,
            "filter_retries": self.filter_retries,
            "analysis_unavailable": self.analysis_unavailable,
            "scope_truncations": self.scope_truncations,
            "strategies": dict(sorted(self.strategy_counts.items())),
            "retrieval_modes": dict(sorted(self.mode_counts.items())),
            "false_filtering_cases": list(self.false_filtering_case_ids[:20]),
        }


def compute_planner_metrics(results: list[CaseResult]) -> PlannerMetrics:
    metrics = PlannerMetrics()

    for result in results:
        if not result.ok:
            continue
        metrics.cases += 1

        if result.strategy:
            metrics.strategy_counts[result.strategy] = (
                metrics.strategy_counts.get(result.strategy, 0) + 1
            )
        if result.retrieval_mode:
            metrics.mode_counts[result.retrieval_mode] = (
                metrics.mode_counts.get(result.retrieval_mode, 0) + 1
            )

        if result.analysis_method == "unavailable":
            metrics.analysis_unavailable += 1
        if result.relaxed_filters:
            metrics.filter_retries += 1
        if result.scope_truncated:
            metrics.scope_truncations += 1

        expected = result.case.expected

        if expected.intent:
            metrics.intent_labelled += 1
            if normalise(result.intent) == normalise(expected.intent):
                metrics.intent_correct += 1

        if expected.document_type:
            metrics.document_type_labelled += 1
            if normalise(result.document_type) == normalise(expected.document_type):
                metrics.document_type_correct += 1

        _score_filtering(metrics, result)

    return metrics


def _score_filtering(metrics: PlannerMetrics, result: CaseResult) -> None:
    """Was applying - or declining to apply - the type filter the right call?

    Correctness is judged against the case's ``agreementTypes`` when it names
    them, and otherwise against whether the expected contracts were actually
    retrieved. The second is a proxy, but a sound one: a filter that excluded the
    answer shows up as zero relevant results, and that is precisely the harm the
    metric is trying to count.
    """
    applied = result.applied_agreement_types
    expected = result.case.expected

    if not applied:
        if result.document_type and result.document_type_confidence > 0:
            metrics.filter_declined += 1
        return

    if expected.agreement_types:
        wanted = {normalise(value) for value in expected.agreement_types}
        if wanted & {normalise(value) for value in applied}:
            metrics.correct_filtering += 1
        else:
            metrics.false_filtering += 1
            metrics.false_filtering_case_ids.append(result.case.id)
        return

    if not expected.has_relevance_signal:
        return

    judge = RelevanceJudge(expected)
    if any(judge.is_relevant(item) for item in result.retrieved):
        metrics.correct_filtering += 1
    else:
        # A filter was applied and nothing the case expected came back. Either
        # the type was wrong or the corpus is missing the document; both are
        # worth a human look, which is what the case-id list is for.
        metrics.false_filtering += 1
        metrics.false_filtering_case_ids.append(result.case.id)


# =============================================================================
# Guardrail
# =============================================================================
@dataclass(slots=True)
class GuardrailMetrics:
    """Confusion matrix over "should this question have been answered?"."""

    #: Answerable, and answered.
    true_accept: int = 0
    #: Unanswerable, and correctly declined.
    true_reject: int = 0
    #: Unanswerable, but answered anyway. The dangerous quadrant: the platform
    #: produced contract terms for a question the corpus cannot support.
    false_accept: int = 0
    #: Answerable, but declined. Costly but safe - the user gets nothing rather
    #: than something wrong.
    false_reject: int = 0

    #: Cases where an L1 document summary scored above the answer threshold while
    #: no clause or chunk did. Each one would have passed the guardrail before it
    #: was computed per level - the exact defect that fix addressed, now measured
    #: rather than assumed.
    document_summary_would_have_passed: int = 0
    #: Of those, how many were genuinely unanswerable - i.e. how many
    #: hallucinations the per-level guardrail actually prevented.
    hallucinations_prevented: int = 0

    false_accept_case_ids: list[str] = field(default_factory=list)
    false_reject_case_ids: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.true_accept + self.true_reject + self.false_accept + self.false_reject

    @property
    def accuracy(self) -> float:
        return ((self.true_accept + self.true_reject) / self.total) if self.total else 0.0

    @property
    def false_accept_rate(self) -> float:
        negatives = self.true_reject + self.false_accept
        return (self.false_accept / negatives) if negatives else 0.0

    @property
    def false_reject_rate(self) -> float:
        positives = self.true_accept + self.false_reject
        return (self.false_reject / positives) if positives else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "true_accept": self.true_accept,
            "true_reject": self.true_reject,
            "false_accept": self.false_accept,
            "false_reject": self.false_reject,
            "accuracy": round(self.accuracy, 4),
            "false_accept_rate": round(self.false_accept_rate, 4),
            "false_reject_rate": round(self.false_reject_rate, 4),
            "document_summary_would_have_passed": self.document_summary_would_have_passed,
            "hallucinations_prevented": self.hallucinations_prevented,
            "false_accept_cases": list(self.false_accept_case_ids[:20]),
            "false_reject_cases": list(self.false_reject_case_ids[:20]),
        }


def compute_guardrail_metrics(
    results: list[CaseResult], *, answer_threshold: float
) -> GuardrailMetrics:
    """Score the guardrail against ``shouldAnswer``.

    ``answer_threshold`` is passed rather than read from settings so a sweep can
    score the same recorded run against several thresholds without re-running it.
    """
    metrics = GuardrailMetrics()

    for result in results:
        if not result.ok:
            continue
        # A generation failure is an availability problem, not a guardrail
        # decision. Counting it as a rejection would blame the guardrail for the
        # provider being down.
        if result.generation_failed:
            continue

        should = result.case.expected.should_answer
        answered = result.answered

        if should and answered:
            metrics.true_accept += 1
        elif should and not answered:
            metrics.false_reject += 1
            metrics.false_reject_case_ids.append(result.case.id)
        elif not should and answered:
            metrics.false_accept += 1
            metrics.false_accept_case_ids.append(result.case.id)
        else:
            metrics.true_reject += 1

        _score_level_guardrail(metrics, result, answer_threshold)

    return metrics


def _score_level_guardrail(metrics: GuardrailMetrics, result: CaseResult, threshold: float) -> None:
    """Would a whole-result maximum have let this through?

    Counts the cases where a document summary cleared the bar and nothing that
    can actually answer did. Those are the questions the old guardrail passed to
    the model with weak clause evidence.
    """
    by_level = result.similarity_by_level
    if not by_level:
        return

    document = by_level.get("document_summary", 0.0)
    answerable = max(
        (score for level, score in by_level.items() if level in {"clause", "chunk"}),
        default=0.0,
    )

    if document >= threshold > answerable:
        metrics.document_summary_would_have_passed += 1
        if not result.case.expected.should_answer:
            metrics.hallucinations_prevented += 1


def _round(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


__all__ = [
    "GuardrailMetrics",
    "PlannerMetrics",
    "compute_guardrail_metrics",
    "compute_planner_metrics",
]

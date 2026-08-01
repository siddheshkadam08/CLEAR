"""Citation quality: are the answer's references real, and are they the right ones.

The platform already strips citations the model was never offered, so the naive
version of this metric would read 1.0 forever. It is therefore built to measure
four genuinely different failures:

* **Hallucinated** - a label the model invented. Stripped before display, so
  invisible in production and only countable here. Its rate is a model-quality
  signal, and a rise in it is the earliest warning that a model change has
  degraded grounding.
* **Broken** - a label that resolved but points at a passage retrieval did not
  return. Should be structurally impossible; a non-zero count means the
  validator's allow-list is wrong, which is a defect in *our* code.
* **Imprecise** - a real citation to a real passage that is not what the case
  expected. This is where citation precision actually lives.
* **Missed** - an expected clause that was retrieved, shown to the model, and
  then not cited. Recall over what the answer *should* have referenced, which is
  the one that catches an answer that quietly leans on a single source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.evaluation.metrics.relevance import Grade, RelevanceJudge, normalise
from app.evaluation.runner.result import CaseResult, CitationRecord, RetrievedItem


@dataclass(slots=True)
class CaseCitationOutcome:
    """Per-case citation verdict, before aggregation."""

    case_id: str
    total: int = 0
    correct: int = 0
    imprecise: int = 0
    broken: int = 0
    hallucinated: int = 0
    #: Expected units that were shown to the model and not cited.
    missed: int = 0
    expected_available: int = 0

    @property
    def precision(self) -> float | None:
        resolvable = self.total - self.hallucinated - self.broken
        if resolvable <= 0:
            return None
        return self.correct / resolvable

    @property
    def recall(self) -> float | None:
        if self.expected_available <= 0:
            return None
        return (self.expected_available - self.missed) / self.expected_available


@dataclass(slots=True)
class CitationMetrics:
    """Aggregate citation quality over a run."""

    scored_cases: int = 0
    total_citations: int = 0

    precision: float = 0.0
    recall: float = 0.0

    broken: int = 0
    hallucinated: int = 0
    imprecise: int = 0

    #: Answers of real length citing nothing at all. The single strongest
    #: hallucination proxy available without a human reader: an answer stating
    #: contract terms with no reference did not come from the evidence.
    uncited_answers: int = 0
    answers_scored: int = 0

    #: Share of answers where every citation resolved and matched expectation.
    fully_grounded_rate: float = 0.0

    worst_case_ids: list[str] = field(default_factory=list)

    @property
    def hallucination_rate(self) -> float:
        """Fabricated labels plus uncited factual answers, over answers scored."""
        if self.answers_scored <= 0:
            return 0.0
        return (self.hallucinated + self.uncited_answers) / self.answers_scored

    def as_dict(self) -> dict[str, Any]:
        return {
            "scored_cases": self.scored_cases,
            "total_citations": self.total_citations,
            "citation_precision": round(self.precision, 4),
            "citation_recall": round(self.recall, 4),
            "broken_citations": self.broken,
            "hallucinated_citations": self.hallucinated,
            "imprecise_citations": self.imprecise,
            "uncited_answers": self.uncited_answers,
            "answers_scored": self.answers_scored,
            "hallucination_rate": round(self.hallucination_rate, 4),
            "fully_grounded_rate": round(self.fully_grounded_rate, 4),
            "worst_cases": list(self.worst_case_ids[:20]),
        }


#: Below this many characters an answer is a refusal or a clarification, not a
#: factual claim, so the missing-citation rule does not apply. Mirrors the
#: threshold the RAG engine's own validator uses - two different numbers would
#: mean the benchmark and the platform disagreed about what an answer is.
_TRIVIAL_ANSWER_CHARS = 160


def evaluate_case_citations(result: CaseResult) -> CaseCitationOutcome:
    """Classify every citation one answer made."""
    outcome = CaseCitationOutcome(case_id=result.case.id)
    judge = RelevanceJudge(result.case.expected)
    by_ref = {item.ref_id: item for item in result.retrieved}

    for citation in result.citations:
        outcome.total += 1
        if not citation.resolved:
            outcome.hallucinated += 1
            continue
        if citation.ref_id is None or citation.ref_id not in by_ref:
            outcome.broken += 1
            continue
        if _matches_expectation(judge, by_ref[citation.ref_id], citation):
            outcome.correct += 1
        else:
            outcome.imprecise += 1

    # Recall denominator: expected units the model was actually *shown*. Counting
    # units retrieval never surfaced would make this a second, worse measurement
    # of recall rather than a measurement of citing behaviour.
    shown = [item for item in result.retrieved if item.in_context and judge.is_relevant(item)]
    outcome.expected_available = len(shown)
    outcome.missed = sum(1 for item in shown if not item.cited)
    return outcome


def _matches_expectation(
    judge: RelevanceJudge, item: RetrievedItem, citation: CitationRecord
) -> bool:
    """Is this citation pointing where the case said the answer lives?

    A case with no relevance expectation cannot judge precision, so every
    resolved citation counts as correct - the alternative would score a
    guardrail-focused negative case as citing badly when it cited nothing wrong.
    """
    if not judge.expectation.has_relevance_signal:
        return True

    grade = judge.grade(item)
    if grade >= Grade.PAGE:
        return True
    if grade == Grade.CONTRACT:
        # Right document. Only accept it when the case expressed nothing finer -
        # otherwise a citation to page 3 of the right contract would pass a case
        # that named clause 12.3 exactly.
        if not (judge.expectation.clauses or judge.expectation.pages):
            return True
        return _heading_agrees(judge, citation)
    return False


def _heading_agrees(judge: RelevanceJudge, citation: CitationRecord) -> bool:
    wanted = {normalise(heading) for heading in judge.expectation.headings}
    if not wanted:
        return False
    return normalise(citation.section_title) in wanted


def compute_citation_metrics(results: list[CaseResult]) -> CitationMetrics:
    metrics = CitationMetrics()
    precisions: list[float] = []
    recalls: list[float] = []
    fully_grounded = 0
    outcomes: list[CaseCitationOutcome] = []

    for result in results:
        if not result.ok:
            continue

        # Only answers can be scored for citations. A guardrail response cites
        # nothing *correctly*, and counting it as a citation failure would make
        # the metric reward answering when the platform should decline.
        if not result.answered:
            continue

        metrics.answers_scored += 1
        substantive = len(result.answer) > _TRIVIAL_ANSWER_CHARS
        if substantive and not result.citations:
            metrics.uncited_answers += 1

        outcome = evaluate_case_citations(result)
        outcomes.append(outcome)
        metrics.total_citations += outcome.total
        metrics.broken += outcome.broken
        metrics.hallucinated += outcome.hallucinated
        metrics.imprecise += outcome.imprecise

        precision = outcome.precision
        if precision is not None:
            precisions.append(precision)
        recall = outcome.recall
        if recall is not None:
            recalls.append(recall)

        if outcome.total and outcome.correct == outcome.total:
            fully_grounded += 1

    metrics.scored_cases = len(outcomes)
    metrics.precision = _mean(precisions)
    metrics.recall = _mean(recalls)
    metrics.fully_grounded_rate = (
        fully_grounded / metrics.scored_cases if metrics.scored_cases else 0.0
    )
    ranked = sorted(
        outcomes,
        key=lambda outcome: (outcome.hallucinated + outcome.broken, outcome.imprecise),
        reverse=True,
    )
    metrics.worst_case_ids = [
        outcome.case_id
        for outcome in ranked
        if outcome.hallucinated or outcome.broken or outcome.imprecise
    ]
    return metrics


def _mean(values: list[float]) -> float:
    return (sum(values) / len(values)) if values else 0.0


__all__ = [
    "CaseCitationOutcome",
    "CitationMetrics",
    "compute_citation_metrics",
    "evaluate_case_citations",
]

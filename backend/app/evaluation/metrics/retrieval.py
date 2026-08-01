"""Retrieval quality: recall, precision, MRR, nDCG, and the shape of the results.

Pure functions over recorded results - no I/O, no services, no settings. That is
what makes them testable against hand-computed examples, which matters because a
quality gate nobody has verified is a quality gate nobody should trust.

One convention throughout: a case with no relevance expectation is **excluded**
from these metrics rather than scored as zero. A dataset that is half negative
cases would otherwise report a recall of 0.5 no matter how well retrieval worked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.evaluation.metrics.relevance import RelevanceJudge, found_units
from app.evaluation.runner.result import CaseResult, RetrievedItem

#: Cut-offs reported for every rank metric.
DEFAULT_KS = (5, 10, 20)


def recall_at_k(judge: RelevanceJudge, items: list[RetrievedItem], k: int) -> float | None:
    """Fraction of the expected units that appear in the top ``k``.

    ``None`` - not zero - when the case expects nothing, so the caller can
    exclude it from the average rather than dragging the mean down with a case
    that was never scoreable.
    """
    total = judge.total_relevant
    if total <= 0:
        return None
    return min(found_units(judge, items[:k]) / total, 1.0)


def precision_at_k(judge: RelevanceJudge, items: list[RetrievedItem], k: int) -> float | None:
    """Fraction of the top ``k`` that is relevant.

    Denominator is ``min(k, len(items))``, not ``k``: a run that returned three
    passages, all correct, has perfect precision. Dividing by ``k`` would score
    it 0.3 and make precision a measure of how much was retrieved.
    """
    if not judge.expectation.has_relevance_signal:
        return None
    window = items[:k]
    if not window:
        return 0.0
    return sum(1 for item in window if judge.is_relevant(item)) / len(window)


def reciprocal_rank(judge: RelevanceJudge, items: list[RetrievedItem]) -> float | None:
    """1/rank of the first relevant passage; 0 when none is found.

    The metric that matters most for a Copilot, because the context budget only
    admits the first handful. A relevant passage at rank 40 is one the model
    never saw.
    """
    if not judge.expectation.has_relevance_signal:
        return None
    for index, item in enumerate(items, start=1):
        if judge.is_relevant(item):
            return 1.0 / index
    return 0.0


def ndcg_at_k(judge: RelevanceJudge, items: list[RetrievedItem], k: int) -> float | None:
    """Normalised discounted cumulative gain over graded relevance.

    Uses the graded scale rather than a boolean, which is the reason to compute
    nDCG at all: it distinguishes "the exact clause, first" from "the right
    contract, first", and those are different qualities of result that recall and
    MRR both score identically.

    The ideal ranking is constructed from the *expectation*, not from the
    retrieved set - an ideal derived from what was found could never exceed what
    was found, and nDCG would be pinned near 1.0.
    """
    if not judge.expectation.has_relevance_signal:
        return None

    grades = [int(grade) for grade in judge.grades(items[:k])]
    dcg = sum(
        (2**grade - 1) / math.log2(index + 1)
        for index, grade in enumerate(grades, start=1)
        if grade > 0
    )

    # Ideal: every expected unit retrieved at the best possible grade, in order.
    best_grade = 4 if judge.expectation.clauses else (3 if judge.expectation.headings else 1)
    ideal_count = min(judge.total_relevant, k)
    idcg = sum((2**best_grade - 1) / math.log2(index + 1) for index in range(1, ideal_count + 1))
    if idcg <= 0:
        return None
    return min(dcg / idcg, 1.0)


def duplicate_rate(items: list[RetrievedItem]) -> float:
    """Share of retrieved passages that repeat text already present.

    Near-duplicates are suppressed in the engine, so a rising figure here is the
    suppression failing rather than chunking changing - and every duplicate that
    survives occupies one of the eight context slots.
    """
    if len(items) < 2:
        return 0.0

    seen: list[frozenset[str]] = []
    duplicates = 0
    for item in items:
        shingles = _shingles(item.text)
        if not shingles:
            continue
        if any(len(shingles & other) / min(len(shingles), len(other)) >= 0.8 for other in seen):
            duplicates += 1
            continue
        seen.append(shingles)
    return duplicates / len(items)


def _shingles(text: str, *, size: int = 8) -> frozenset[str]:
    words = text.lower().split()
    if len(words) < size:
        return frozenset({" ".join(words)}) if words else frozenset()
    return frozenset(
        " ".join(words[index : index + size]) for index in range(len(words) - size + 1)
    )


@dataclass(slots=True)
class RetrievalMetrics:
    """Aggregate retrieval quality over a run."""

    #: How many cases actually contributed. Reported because every average below
    #: is meaningless without it - "recall 1.0" over two cases is not a result.
    scored_cases: int = 0
    total_cases: int = 0

    recall: dict[int, float] = field(default_factory=dict)
    precision: dict[int, float] = field(default_factory=dict)
    ndcg: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0

    mean_similarity: float = 0.0
    mean_rerank_score: float | None = None
    mean_retrieved: float = 0.0
    mean_in_context: float = 0.0
    mean_context_tokens: float = 0.0
    mean_context_dropped: float = 0.0
    mean_duplicate_rate: float = 0.0

    #: Cases where nothing relevant was retrieved at any rank. The list a triage
    #: session actually starts from.
    zero_recall_case_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scored_cases": self.scored_cases,
            "total_cases": self.total_cases,
            **{f"recall@{k}": round(v, 4) for k, v in sorted(self.recall.items())},
            **{f"precision@{k}": round(v, 4) for k, v in sorted(self.precision.items())},
            **{f"ndcg@{k}": round(v, 4) for k, v in sorted(self.ndcg.items())},
            "mrr": round(self.mrr, 4),
            "mean_similarity": round(self.mean_similarity, 4),
            "mean_rerank_score": (
                round(self.mean_rerank_score, 4) if self.mean_rerank_score is not None else None
            ),
            "mean_retrieved": round(self.mean_retrieved, 2),
            "mean_in_context": round(self.mean_in_context, 2),
            "mean_context_tokens": round(self.mean_context_tokens, 1),
            "mean_context_dropped": round(self.mean_context_dropped, 2),
            "mean_duplicate_rate": round(self.mean_duplicate_rate, 4),
            "zero_recall_cases": len(self.zero_recall_case_ids),
        }


def compute_retrieval_metrics(
    results: list[CaseResult], *, ks: tuple[int, ...] = DEFAULT_KS
) -> RetrievalMetrics:
    """Aggregate retrieval quality across a run."""
    metrics = RetrievalMetrics(total_cases=len(results))

    recalls: dict[int, list[float]] = {k: [] for k in ks}
    precisions: dict[int, list[float]] = {k: [] for k in ks}
    ndcgs: dict[int, list[float]] = {k: [] for k in ks}
    rrs: list[float] = []

    similarities: list[float] = []
    rerank_scores: list[float] = []
    retrieved_counts: list[int] = []
    in_context_counts: list[int] = []
    context_tokens: list[int] = []
    context_dropped: list[int] = []
    duplicate_rates: list[float] = []

    for result in results:
        if not result.ok:
            continue

        retrieved_counts.append(len(result.retrieved))
        in_context_counts.append(sum(1 for item in result.retrieved if item.in_context))
        context_tokens.append(result.context_tokens)
        context_dropped.append(result.context_dropped)
        duplicate_rates.append(duplicate_rate(result.retrieved))
        similarities.extend(
            item.similarity for item in result.retrieved if item.similarity is not None
        )
        rerank_scores.extend(
            item.rerank_score for item in result.retrieved if item.rerank_score is not None
        )

        if not result.case.expected.has_relevance_signal:
            continue

        judge = RelevanceJudge(result.case.expected)
        metrics.scored_cases += 1

        for k in ks:
            value = recall_at_k(judge, result.retrieved, k)
            if value is not None:
                recalls[k].append(value)
            value = precision_at_k(judge, result.retrieved, k)
            if value is not None:
                precisions[k].append(value)
            value = ndcg_at_k(judge, result.retrieved, k)
            if value is not None:
                ndcgs[k].append(value)

        rr = reciprocal_rank(judge, result.retrieved)
        if rr is not None:
            rrs.append(rr)
            if rr == 0.0:
                metrics.zero_recall_case_ids.append(result.case.id)

    metrics.recall = {k: _mean(values) for k, values in recalls.items() if values}
    metrics.precision = {k: _mean(values) for k, values in precisions.items() if values}
    metrics.ndcg = {k: _mean(values) for k, values in ndcgs.items() if values}
    metrics.mrr = _mean(rrs)
    metrics.mean_similarity = _mean(similarities)
    metrics.mean_rerank_score = _mean(rerank_scores) if rerank_scores else None
    metrics.mean_retrieved = _mean(retrieved_counts)
    metrics.mean_in_context = _mean(in_context_counts)
    metrics.mean_context_tokens = _mean(context_tokens)
    metrics.mean_context_dropped = _mean(context_dropped)
    metrics.mean_duplicate_rate = _mean(duplicate_rates)
    return metrics


def _mean(values: list[float] | list[int]) -> float:
    return (sum(values) / len(values)) if values else 0.0


__all__ = [
    "DEFAULT_KS",
    "RetrievalMetrics",
    "compute_retrieval_metrics",
    "duplicate_rate",
    "ndcg_at_k",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
]

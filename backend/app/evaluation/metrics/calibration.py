"""Confidence calibration - is 0.71 actually right 71% of the time?

The platform's confidence is a heuristic: a weighted blend of citation coverage
and evidence strength. It varies in the right direction, which is all it was ever
designed to do. It is displayed as a percentage, which invites a reader to
interpret it as a probability, and nothing has ever checked whether that
interpretation holds.

This module checks it. **It does not change runtime behaviour.** The fitted
calibrators are stored so the shift can be inspected and argued about before
anyone considers applying one - a confidence figure that silently changed
meaning between releases would be worse than one that was never calibrated.

The "correct" label is derived from what the case expected: an answered case is
correct when the answer cited the evidence the case named, and a declined case is
correct when the case was unanswerable. That is a proxy for human judgement, and
it is a defensible one, but it is a proxy - a calibration curve here says the
confidence tracks *retrieval correctness*, not *legal correctness*.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.evaluation.metrics.citation import evaluate_case_citations
from app.evaluation.runner.result import CaseResult

#: Bins for the reliability diagram. Ten is conventional and readable; more bins
#: on a few hundred cases produces buckets of two, where the observed frequency
#: is noise rather than signal.
DEFAULT_BINS = 10


@dataclass(slots=True)
class CalibrationBin:
    """One bucket of the reliability diagram."""

    lower: float
    upper: float
    count: int = 0
    mean_confidence: float = 0.0
    observed_accuracy: float = 0.0

    @property
    def gap(self) -> float:
        """Signed: positive means over-confident, which is the harmful direction."""
        return self.mean_confidence - self.observed_accuracy

    def as_dict(self) -> dict[str, Any]:
        return {
            "lower": round(self.lower, 3),
            "upper": round(self.upper, 3),
            "count": self.count,
            "mean_confidence": round(self.mean_confidence, 4),
            "observed_accuracy": round(self.observed_accuracy, 4),
            "gap": round(self.gap, 4),
        }


@dataclass(slots=True)
class CalibrationMetrics:
    """How well the confidence figure predicts correctness."""

    samples: int = 0
    #: Expected Calibration Error: mean |confidence - accuracy|, weighted by bin
    #: population. The headline number.
    ece: float = 0.0
    #: Maximum Calibration Error: the worst single bin. Matters more than ECE for
    #: a reviewer, who experiences one answer at a time rather than an average.
    mce: float = 0.0
    #: Brier score: mean squared error of the probability. Lower is better;
    #: 0.25 is what predicting 0.5 for everything achieves.
    brier: float = 0.0
    #: Base rate. A Brier score has to be read against it - on a set that is 90%
    #: correct, 0.09 is unimpressive.
    base_rate: float = 0.0
    #: Positive means systematically over-confident across the whole range.
    mean_bias: float = 0.0
    bins: list[CalibrationBin] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "ece": round(self.ece, 4),
            "mce": round(self.mce, 4),
            "brier": round(self.brier, 4),
            "base_rate": round(self.base_rate, 4),
            "mean_bias": round(self.mean_bias, 4),
            "bins": [bin_.as_dict() for bin_ in self.bins],
        }


def correctness_label(result: CaseResult) -> bool | None:
    """Was this case's outcome right?

    ``None`` when the case cannot be judged - a generation failure, or a case
    with no expectation to judge against. Excluded rather than guessed: a
    calibration curve built on invented labels is worse than no curve.
    """
    if not result.ok or result.generation_failed:
        return None

    expected = result.case.expected

    if not expected.should_answer:
        # For an unanswerable question the correct outcome is to decline, and
        # confidence is only meaningful when something was answered.
        return not result.answered if result.answered else None

    if not result.answered:
        # Declined an answerable question. Wrong, but there is no confidence
        # attached to a guardrail response to calibrate.
        return None

    if expected.answer_contains:
        lowered = result.answer.lower()
        return all(needle.lower() in lowered for needle in expected.answer_contains)

    if not expected.has_relevance_signal:
        return None

    outcome = evaluate_case_citations(result)
    if outcome.total == 0:
        # A substantive answer citing nothing is wrong by the platform's own
        # standard; a short one is a clarification and unjudgeable.
        return False if len(result.answer) > 160 else None
    precision = outcome.precision
    return None if precision is None else precision >= 0.5


def collect_samples(results: list[CaseResult]) -> list[tuple[float, bool]]:
    """``(confidence, correct)`` pairs, for everything judgeable."""
    samples: list[tuple[float, bool]] = []
    for result in results:
        label = correctness_label(result)
        if label is None:
            continue
        samples.append((max(0.0, min(result.confidence, 1.0)), label))
    return samples


def compute_calibration(
    samples: list[tuple[float, bool]], *, bins: int = DEFAULT_BINS
) -> CalibrationMetrics:
    """Reliability diagram, ECE, MCE and Brier over ``(confidence, correct)``."""
    metrics = CalibrationMetrics(samples=len(samples))
    if not samples:
        return metrics

    edges = [index / bins for index in range(bins + 1)]
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, correct in samples:
        # The top edge belongs to the last bin rather than opening a new one.
        index = min(int(confidence * bins), bins - 1)
        buckets[index].append((confidence, correct))

    total = len(samples)
    weighted_error = 0.0
    worst = 0.0
    bias = 0.0

    for index, bucket in enumerate(buckets):
        bin_ = CalibrationBin(lower=edges[index], upper=edges[index + 1], count=len(bucket))
        if bucket:
            bin_.mean_confidence = sum(value for value, _ in bucket) / len(bucket)
            bin_.observed_accuracy = sum(1 for _, correct in bucket if correct) / len(bucket)
            error = abs(bin_.gap)
            weighted_error += (len(bucket) / total) * error
            worst = max(worst, error)
            bias += (len(bucket) / total) * bin_.gap
        metrics.bins.append(bin_)

    metrics.ece = weighted_error
    metrics.mce = worst
    metrics.mean_bias = bias
    metrics.brier = (
        sum((confidence - (1.0 if correct else 0.0)) ** 2 for confidence, correct in samples)
        / total
    )
    metrics.base_rate = sum(1 for _, correct in samples if correct) / total
    return metrics


# =============================================================================
# Calibrators
# =============================================================================
@dataclass(slots=True)
class PlattCalibrator:
    """Logistic recalibration: ``sigmoid(a * x + b)``.

    Fitted by gradient descent rather than by pulling in scipy. The problem is
    two-parameter and convex, so a few hundred iterations converge comfortably,
    and not adding a numerical dependency to the backend image for one function
    is worth more than the last decimal place.

    Platt fits a *shape*: it can stretch and shift, but it cannot fix a
    confidence that is non-monotonic in correctness. When the reliability diagram
    is bumpy rather than merely offset, isotonic is the right choice.
    """

    a: float = 1.0
    b: float = 0.0

    def predict(self, confidence: float) -> float:
        return _sigmoid(self.a * confidence + self.b)

    @classmethod
    def fit(
        cls,
        samples: list[tuple[float, bool]],
        *,
        iterations: int = 800,
        learning_rate: float = 0.5,
    ) -> PlattCalibrator:
        if not samples:
            return cls()

        a, b = 1.0, 0.0
        n = len(samples)
        for _ in range(iterations):
            grad_a = 0.0
            grad_b = 0.0
            for confidence, correct in samples:
                predicted = _sigmoid(a * confidence + b)
                error = predicted - (1.0 if correct else 0.0)
                grad_a += error * confidence
                grad_b += error
            a -= learning_rate * grad_a / n
            b -= learning_rate * grad_b / n
        return cls(a=a, b=b)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": "platt", "a": round(self.a, 6), "b": round(self.b, 6)}


@dataclass(slots=True)
class IsotonicCalibrator:
    """Monotonic step-function recalibration, via pool-adjacent-violators.

    Strictly more flexible than Platt and correspondingly easier to overfit: with
    a few hundred samples it will happily memorise noise. The rule of thumb is
    that isotonic wants at least a thousand samples; below that, Platt's two
    parameters are the safer fit.

    Monotonicity is the constraint that makes the output still *mean* something:
    a higher raw confidence can never map to a lower calibrated one, so the
    ordering a reader relies on survives.
    """

    #: Ascending breakpoints, and the calibrated value from each one onwards.
    thresholds: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)

    def predict(self, confidence: float) -> float:
        if not self.thresholds:
            return confidence
        result = self.values[0]
        for threshold, value in zip(self.thresholds, self.values, strict=True):
            if confidence >= threshold:
                result = value
            else:
                break
        return result

    @classmethod
    def fit(cls, samples: list[tuple[float, bool]]) -> IsotonicCalibrator:
        if not samples:
            return cls()

        ordered = sorted(samples, key=lambda pair: pair[0])
        # Each block: (summed target, weight, representative x).
        blocks: list[list[float]] = [
            [1.0 if correct else 0.0, 1.0, confidence] for confidence, correct in ordered
        ]

        # Pool adjacent violators: merge any pair where the left mean exceeds the
        # right, repeatedly, until the sequence is non-decreasing.
        merged = True
        while merged:
            merged = False
            index = 0
            while index < len(blocks) - 1:
                left, right = blocks[index], blocks[index + 1]
                if left[0] / left[1] > right[0] / right[1]:
                    left[0] += right[0]
                    left[1] += right[1]
                    left[2] = min(left[2], right[2])
                    del blocks[index + 1]
                    merged = True
                    if index:
                        index -= 1
                else:
                    index += 1

        return cls(
            thresholds=[block[2] for block in blocks],
            values=[block[0] / block[1] for block in blocks],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "isotonic",
            "points": [
                {"threshold": round(threshold, 6), "value": round(value, 6)}
                for threshold, value in zip(self.thresholds, self.values, strict=True)
            ],
        }


@dataclass(slots=True)
class CalibrationReport:
    """Raw calibration, both fitted calibrators, and what they would achieve."""

    raw: CalibrationMetrics
    platt: PlattCalibrator
    platt_metrics: CalibrationMetrics
    isotonic: IsotonicCalibrator
    isotonic_metrics: CalibrationMetrics

    @property
    def recommendation(self) -> str:
        """Which calibrator to consider, stated with its reason.

        Deliberately conservative: a calibrator is only worth the complexity if
        it improves ECE materially, and isotonic is only worth its overfitting
        risk with enough samples to support it.
        """
        if self.raw.samples < 100:
            return (
                f"Too few judgeable cases ({self.raw.samples}) to calibrate. "
                "Grow the golden set before fitting anything."
            )

        candidates = [
            ("none", self.raw.ece),
            ("platt", self.platt_metrics.ece),
            ("isotonic", self.isotonic_metrics.ece if self.raw.samples >= 1000 else 1.0),
        ]
        best, best_ece = min(candidates, key=lambda pair: pair[1])

        if best == "none" or best_ece > self.raw.ece - 0.02:
            return (
                f"Keep the raw heuristic. ECE {self.raw.ece:.3f}; no calibrator "
                "improves it by more than 0.02, which does not justify the "
                "indirection."
            )
        return (
            f"Consider {best} scaling: ECE {self.raw.ece:.3f} -> {best_ece:.3f}. "
            "Apply it to the displayed figure only after a human has reviewed the "
            "reliability diagram."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw.as_dict(),
            "platt": {**self.platt.as_dict(), "metrics": self.platt_metrics.as_dict()},
            "isotonic": {
                **self.isotonic.as_dict(),
                "metrics": self.isotonic_metrics.as_dict(),
            },
            "recommendation": self.recommendation,
        }


def build_calibration_report(
    results: list[CaseResult], *, bins: int = DEFAULT_BINS
) -> CalibrationReport:
    """Fit and score both calibrators without changing anything at runtime."""
    samples = collect_samples(results)
    raw = compute_calibration(samples, bins=bins)

    platt = PlattCalibrator.fit(samples)
    platt_samples = [(platt.predict(confidence), correct) for confidence, correct in samples]

    isotonic = IsotonicCalibrator.fit(samples)
    isotonic_samples = [(isotonic.predict(confidence), correct) for confidence, correct in samples]

    return CalibrationReport(
        raw=raw,
        platt=platt,
        platt_metrics=compute_calibration(platt_samples, bins=bins),
        isotonic=isotonic,
        isotonic_metrics=compute_calibration(isotonic_samples, bins=bins),
    )


def _sigmoid(value: float) -> float:
    # Split by sign to keep exp() away from overflow at either extreme.
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


__all__ = [
    "DEFAULT_BINS",
    "CalibrationBin",
    "CalibrationMetrics",
    "CalibrationReport",
    "IsotonicCalibrator",
    "PlattCalibrator",
    "build_calibration_report",
    "collect_samples",
    "compute_calibration",
    "correctness_label",
]

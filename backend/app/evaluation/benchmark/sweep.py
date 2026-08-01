"""Threshold sweeps and ablations.

Both answer the same question - *does this setting earn its value?* - and both
work the same way: run the same dataset repeatedly, varying one knob, and rank
the outcomes.

Sweeps default to **retrieval-only** mode. A six-point sweep of a five-hundred
case dataset is three thousand pipeline executions; with generation that is a
serious bill and an hour of wall-clock, and generation contributes nothing to the
question a similarity threshold is being asked. Ablations that touch answer
quality - the re-ranker arm in particular - do run generation, because citation
quality is the thing being compared.

One property matters more than it looks: **the best configuration is chosen by
the composite score, and the composite's weights are visible in the scorecard
module.** A sweep that optimised a single metric would find the threshold that
maximises recall by retrieving everything, which is not an improvement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.evaluation.dataset.models import GoldenDataset
from app.evaluation.metrics.scorecard import Scorecard, build_scorecard
from app.evaluation.runner.runner import EvaluationRunner, RunnerOptions

logger = get_logger(__name__)

#: The sweep the review asked for, plus the knobs that interact with it. Each is
#: a settings attribute and the values to try.
DEFAULT_SWEEPS: dict[str, tuple[float, ...]] = {
    "min_similarity_clause": (0.30, 0.35, 0.40, 0.45, 0.50, 0.55),
    "min_similarity_document": (0.25, 0.30, 0.35, 0.40, 0.45),
    "min_similarity_chunk": (0.30, 0.35, 0.40, 0.45, 0.50),
    "answer_similarity_threshold": (0.35, 0.40, 0.45, 0.50, 0.55),
    "document_type_confidence_threshold": (0.60, 0.70, 0.75, 0.80, 0.90),
}


@dataclass(slots=True)
class SweepPoint:
    """One configuration and what it scored."""

    parameter: str
    value: Any
    scorecard: Scorecard

    def as_dict(self) -> dict[str, Any]:
        metrics = self.scorecard.metrics_for_comparison()
        return {
            "parameter": self.parameter,
            "value": self.value,
            "composite": round(self.scorecard.composite, 4),
            "recall@10": round(metrics.get("recall@10", 0.0), 4),
            "recall@5": round(metrics.get("recall@5", 0.0), 4),
            "mrr": round(metrics.get("mrr", 0.0), 4),
            "ndcg@10": round(metrics.get("ndcg@10", 0.0), 4),
            "precision@5": round(metrics.get("precision@5", 0.0), 4),
            "guardrail_accuracy": round(metrics.get("guardrail_accuracy", 0.0), 4),
            "false_accept_rate": round(metrics.get("false_accept_rate", 0.0), 4),
            "false_reject_rate": round(metrics.get("false_reject_rate", 0.0), 4),
            "latency_p95_ms": round(metrics.get("latency_p95_ms", 0.0), 1),
            "mean_cost_usd": round(metrics.get("mean_cost_usd", 0.0), 6),
        }


@dataclass(slots=True)
class SweepResult:
    """Every point of one parameter's sweep, and the winner."""

    parameter: str
    points: list[SweepPoint] = field(default_factory=list)
    current_value: Any = None

    @property
    def best(self) -> SweepPoint | None:
        return max(self.points, key=lambda p: p.scorecard.composite, default=None)

    @property
    def recommendation(self) -> str:
        best = self.best
        if best is None:
            return f"{self.parameter}: no points completed."

        current = next((point for point in self.points if point.value == self.current_value), None)
        if current is None:
            return (
                f"{self.parameter}: best at {best.value} "
                f"(composite {best.scorecard.composite:.3f}). The configured value "
                f"{self.current_value} was not swept."
            )

        gain = best.scorecard.composite - current.scorecard.composite
        if gain <= 0.005:
            return (
                f"{self.parameter}: keep {self.current_value}. The best point "
                f"({best.value}) gains only {gain:+.3f} composite, which is inside "
                "run-to-run noise."
            )
        return (
            f"{self.parameter}: consider {best.value} - composite "
            f"{current.scorecard.composite:.3f} -> {best.scorecard.composite:.3f} "
            f"({gain:+.3f})."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "parameter": self.parameter,
            "current_value": self.current_value,
            "best_value": self.best.value if self.best else None,
            "recommendation": self.recommendation,
            "points": [point.as_dict() for point in self.points],
        }


async def sweep_parameter(
    dataset: GoldenDataset,
    parameter: str,
    values: tuple[float, ...],
    *,
    retrieval_only: bool = True,
    concurrency: int = 4,
) -> SweepResult:
    """Run ``dataset`` once per value of ``parameter``."""
    current = getattr(get_settings().retrieval, parameter, None)
    result = SweepResult(parameter=parameter, current_value=current)

    for value in values:
        logger.info("sweep_point_started", parameter=parameter, value=value)
        runner = EvaluationRunner(
            RunnerOptions(
                concurrency=concurrency,
                retrieval_only=retrieval_only,
                label=f"{parameter}={value}",
                overrides={parameter: value},
            )
        )
        run = await runner.run(dataset)
        # The answer threshold is scored against the value in force for this
        # point, not the configured one - otherwise sweeping it would change
        # retrieval and leave the guardrail judged by the old bar.
        threshold = (
            float(value)
            if parameter == "answer_similarity_threshold"
            else get_settings().retrieval.answer_similarity_threshold
        )
        result.points.append(
            SweepPoint(
                parameter=parameter,
                value=value,
                scorecard=build_scorecard(run, answer_threshold=threshold),
            )
        )

    logger.info("sweep_complete", parameter=parameter, recommendation=result.recommendation)
    return result


async def sweep_all(
    dataset: GoldenDataset,
    *,
    parameters: dict[str, tuple[float, ...]] | None = None,
    retrieval_only: bool = True,
    concurrency: int = 4,
) -> list[SweepResult]:
    """Sweep several parameters, one at a time.

    One at a time, not a grid. A full grid over five parameters at five points
    each is 3,125 runs of the whole dataset - which nobody will wait for, and
    which would mostly measure interactions that do not exist. Coordinate descent
    over single sweeps finds the same practical answer for a thousandth of the
    cost; where two knobs genuinely interact, running the sweep twice shows it.
    """
    selected = parameters or DEFAULT_SWEEPS
    return [
        await sweep_parameter(
            dataset, parameter, values, retrieval_only=retrieval_only, concurrency=concurrency
        )
        for parameter, values in selected.items()
    ]


# =============================================================================
# Ablations
# =============================================================================
@dataclass(slots=True)
class AblationArm:
    """One configuration in an A/B comparison."""

    name: str
    scorecard: Scorecard


@dataclass(slots=True)
class AblationResult:
    """Two arms, and the difference between them."""

    name: str
    control: AblationArm
    treatment: AblationArm

    def gain(self, metric: str) -> float:
        before = self.control.scorecard.metrics_for_comparison().get(metric, 0.0)
        after = self.treatment.scorecard.metrics_for_comparison().get(metric, 0.0)
        return after - before

    @property
    def verdict(self) -> str:
        """Is the treatment worth what it costs?

        Judged on quality *and* on the price of getting it. A re-ranker that adds
        two points of recall for two extra seconds and a doubled bill is not
        obviously worth enabling, and a comparison that reported only the recall
        would say it was.
        """
        composite = self.gain("composite")
        latency = self.gain("latency_p95_ms")
        cost = self.gain("mean_cost_usd")

        if composite <= 0.005:
            return (
                f"No material gain ({composite:+.3f} composite) for "
                f"{latency:+.0f} ms p95 and {cost:+.6f} USD per query. Leave it off."
            )
        return (
            f"Gains {composite:+.3f} composite "
            f"(recall@10 {self.gain('recall@10'):+.3f}, MRR {self.gain('mrr'):+.3f}, "
            f"citation precision {self.gain('citation_precision'):+.3f}) for "
            f"{latency:+.0f} ms p95 and {cost:+.6f} USD per query."
        )

    def as_dict(self) -> dict[str, Any]:
        tracked = (
            "composite",
            "recall@5",
            "recall@10",
            "mrr",
            "ndcg@10",
            "precision@5",
            "citation_precision",
            "citation_recall",
            "hallucination_rate",
            "latency_p95_ms",
            "mean_cost_usd",
        )
        control = self.control.scorecard.metrics_for_comparison()
        treatment = self.treatment.scorecard.metrics_for_comparison()
        return {
            "name": self.name,
            "control": self.control.name,
            "treatment": self.treatment.name,
            "verdict": self.verdict,
            "metrics": [
                {
                    "metric": metric,
                    "control": round(control.get(metric, 0.0), 6),
                    "treatment": round(treatment.get(metric, 0.0), 6),
                    "gain": round(treatment.get(metric, 0.0) - control.get(metric, 0.0), 6),
                }
                for metric in tracked
            ],
        }


async def reranker_ablation(dataset: GoldenDataset, *, concurrency: int = 4) -> AblationResult:
    """Run the dataset with the re-ranker off, then on.

    Generation runs in both arms. The re-ranker changes which passages reach the
    prompt, so citation quality is exactly what it is being judged on - a
    retrieval-only comparison would miss the effect it exists to have.
    """
    arms: dict[str, Scorecard] = {}
    for name, enabled in (("reranker-off", False), ("reranker-on", True)):
        logger.info("ablation_arm_started", arm=name)
        runner = EvaluationRunner(
            RunnerOptions(
                concurrency=concurrency,
                label=name,
                overrides={"reranker_enabled": enabled},
            )
        )
        arms[name] = build_scorecard(await runner.run(dataset))

    return AblationResult(
        name="reranker",
        control=AblationArm("reranker-off", arms["reranker-off"]),
        treatment=AblationArm("reranker-on", arms["reranker-on"]),
    )


async def embedding_ablation(
    dataset: GoldenDataset,
    models: list[dict[str, Any]],
    *,
    concurrency: int = 4,
) -> list[AblationArm]:
    """Compare embedding models on the same dataset.

    Each entry is the settings needed to reach one model, e.g.::

        [{"name": "nemotron-2048",
          "embedding.provider": "nvidia",
          "embedding.model": "nvidia/nemotron-3-embed-1b",
          "embedding.dim": 2048}]

    **This does not re-embed the corpus.** Vectors from two models do not share a
    space, so a meaningful comparison needs the index rebuilt per model - which is
    an ingest run, not something a benchmark can do inside a sweep. Run
    ``cip reindex-embeddings`` between arms, or point each arm at a database
    already indexed with that model. Run it without doing so and every arm but
    the live one scores near zero, which is a real result about mismatched
    vectors and not a comparison of models.
    """
    arms: list[AblationArm] = []
    for entry in models:
        name = str(entry.get("name") or entry.get("embedding.model") or "unnamed")
        overrides = {key: value for key, value in entry.items() if key != "name"}
        logger.info("embedding_arm_started", arm=name, overrides=overrides)

        runner = EvaluationRunner(
            RunnerOptions(
                concurrency=concurrency,
                retrieval_only=True,
                label=name,
                overrides=overrides,
            )
        )
        arms.append(AblationArm(name, build_scorecard(await runner.run(dataset))))
    return arms


__all__ = [
    "DEFAULT_SWEEPS",
    "AblationArm",
    "AblationResult",
    "SweepPoint",
    "SweepResult",
    "embedding_ablation",
    "reranker_ablation",
    "sweep_all",
    "sweep_parameter",
]

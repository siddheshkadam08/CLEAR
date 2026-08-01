"""Run history - the trend line behind a single verdict.

A pass/fail against one baseline answers "did this change break something". It
does not answer "has quality been drifting for six weeks", and the second is the
question that matters for a system whose thresholds are tuned by hand.

History is assembled from the ``summary.json`` files previous runs already wrote,
so nothing extra is stored and a history exists retroactively for every run ever
made. Ordering is by recorded timestamp rather than by directory name, because
directory names are labels and labels are whatever the person running it typed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Metrics carried on the trend chart. Deliberately few - a trend with twenty
#: lines is a picture of nothing.
TRENDED = ("composite", "recall@10", "mrr", "citation_precision", "guardrail_accuracy")


@dataclass(slots=True)
class HistoryPoint:
    label: str
    recorded_at: str
    dataset: str
    passed: bool
    cases: int
    metrics: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "recorded_at": self.recorded_at,
            "dataset": self.dataset,
            "passed": self.passed,
            "cases": self.cases,
            "metrics": self.metrics,
        }


def load_history(
    root: str | Path, *, dataset: str | None = None, limit: int = 30
) -> list[HistoryPoint]:
    """Every previous run under ``root``, oldest first.

    A malformed or half-written summary is skipped with a log line rather than
    raising: a history view that refuses to render because one old run was
    interrupted is less useful than one that shows the rest.
    """
    directory = Path(root)
    if not directory.exists():
        return []

    points: list[HistoryPoint] = []
    for path in sorted(directory.glob("*/summary.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("history_entry_unreadable", path=str(path), error=str(exc))
            continue

        if dataset and payload.get("dataset") != dataset:
            continue

        points.append(
            HistoryPoint(
                label=str(payload.get("label") or path.parent.name),
                # Falls back to the directory's mtime: a summary written before
                # the timestamp field existed still has a position in the order.
                recorded_at=str(payload.get("recorded_at") or path.parent.name),
                dataset=str(payload.get("dataset") or ""),
                passed=bool(payload.get("passed", True)),
                cases=int(payload.get("cases") or 0),
                metrics={
                    str(k): float(v)
                    for k, v in (payload.get("metrics") or {}).items()
                    if isinstance(v, int | float)
                },
            )
        )

    points.sort(key=lambda point: point.recorded_at)
    return points[-limit:]


def trend_series(
    points: list[HistoryPoint], metrics: tuple[str, ...] = TRENDED
) -> dict[str, list[float]]:
    """``{metric: [value per run]}``, for charting.

    A run missing a metric contributes its previous value rather than a zero. A
    zero would draw a cliff in the trend line for a metric that was simply not
    computed - which is exactly the shape of the regression the chart exists to
    reveal, and therefore the worst possible artefact.
    """
    series: dict[str, list[float]] = {metric: [] for metric in metrics}
    for metric in metrics:
        previous = 0.0
        for point in points:
            value = point.metrics.get(metric)
            previous = value if value is not None else previous
            series[metric].append(previous)
    return series


__all__ = ["TRENDED", "HistoryPoint", "load_history", "trend_series"]

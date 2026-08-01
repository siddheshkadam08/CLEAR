"""Evaluation results, served to the admin dashboard.

Reads the artefacts a benchmark run already wrote rather than running one. A
benchmark takes minutes and costs money; an HTTP handler must do neither, and a
dashboard that could trigger one would be a way to spend the budget by refreshing
a page.

Administrator-only. Retrieval quality is an internal engineering signal, and the
failing-case lists name the questions people asked - which is chat history, and
belongs to its author rather than to whoever opens a dashboard.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.core.config import get_settings
from app.core.deps import SystemAdminDep
from app.core.errors import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/evaluation", tags=["Administration"])


def _results_root() -> Path:
    """Where benchmark artefacts live.

    Configurable so a deployment can mount a shared volume that the scheduled
    in-cluster run writes and the API reads - the two are different processes and
    frequently different pods.
    """
    return Path(get_settings().evaluation_results_dir)


@router.get("/runs", summary="Benchmark runs, newest first")
async def list_runs(
    _: SystemAdminDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
    dataset: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Every run's summary, for the trend chart and the regression history."""
    from app.evaluation.benchmark.history import load_history

    points = load_history(_results_root(), dataset=dataset, limit=limit)
    return {
        "results_dir": str(_results_root()),
        "runs": [point.as_dict() for point in reversed(points)],
    }


@router.get("/runs/latest", summary="The most recent run in full")
async def latest_run(
    _: SystemAdminDep,
    dataset: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """The newest run's summary, plus the failing-case lists worth triaging."""
    from app.evaluation.benchmark.history import load_history

    points = load_history(_results_root(), dataset=dataset, limit=1)
    if not points:
        raise NotFoundError("Evaluation run", "latest")

    summary = _read(_results_root() / points[-1].label / "summary.json")
    if summary is None:
        # The history entry came from a directory whose summary has since been
        # removed or truncated. Report the miss rather than a half-populated page.
        raise NotFoundError("Evaluation run", points[-1].label)

    detail = _read(_results_root() / points[-1].label / "evaluation.json") or {}
    scorecard = detail.get("scorecard") or {}

    return {
        "summary": summary,
        "regression": detail.get("regression"),
        "retrieval": scorecard.get("retrieval"),
        "citation": scorecard.get("citation"),
        "planner": scorecard.get("planner"),
        "guardrail": scorecard.get("guardrail"),
        "performance": scorecard.get("performance"),
        "calibration": scorecard.get("calibration"),
        "by_tag": scorecard.get("by_tag"),
        "failing": _failing(detail),
    }


@router.get("/datasets", summary="Golden datasets available to this build")
async def list_datasets(_: SystemAdminDep) -> dict[str, Any]:
    from app.evaluation.dataset import available_datasets, load_dataset

    datasets: list[dict[str, Any]] = []
    for name in available_datasets():
        try:
            datasets.append(load_dataset(name).statistics())
        except Exception as exc:  # noqa: BLE001 - one broken file must not hide the rest
            datasets.append({"name": name, "error": str(exc)})
    return {"datasets": datasets}


def _failing(detail: dict[str, Any]) -> dict[str, Any]:
    """The triage lists, with each case's question attached.

    A list of ids is not actionable. Joining the question back on is the
    difference between "eleven cases regressed" and "every NDA confidentiality
    question stopped working".
    """
    scorecard = detail.get("scorecard") or {}
    run = detail.get("run") or {}
    questions = {
        (result.get("case") or {}).get("id"): (result.get("case") or {}).get("question")
        for result in run.get("results") or []
    }

    def _expand(ids: list[str]) -> list[dict[str, Any]]:
        return [{"id": case_id, "question": questions.get(case_id, "")} for case_id in ids[:25]]

    return {
        "zero_recall": _expand((scorecard.get("retrieval") or {}).get("zero_recall_cases") or []),
        "false_accept": _expand((scorecard.get("guardrail") or {}).get("false_accept_cases") or []),
        "false_reject": _expand((scorecard.get("guardrail") or {}).get("false_reject_cases") or []),
        "worst_cited": _expand((scorecard.get("citation") or {}).get("worst_cases") or []),
        "false_filtering": _expand(
            (scorecard.get("planner") or {}).get("false_filtering_cases") or []
        ),
        "most_expensive": _expand(
            (scorecard.get("performance") or {}).get("most_expensive_cases") or []
        ),
        "slowest": _expand((scorecard.get("performance") or {}).get("slowest_cases") or []),
    }


def _read(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("evaluation_artifact_unreadable", path=str(path), error=str(exc))
        return None


__all__ = ["router"]

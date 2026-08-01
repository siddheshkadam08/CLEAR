"""Retrieval and answer evaluation (§ quality gate).

The Copilot has four tunable similarity thresholds, a confidence heuristic, a
document-type gate, a re-ranker and a context budget. Every one of them was set
by argument rather than by measurement, and until something measures them a
change to any of them is a guess with a plausible story attached.

This package is that measurement. Its organising rule: **nothing here mocks the
pipeline**. The runner drives the real ``CopilotService`` against a real
database, so what is scored is what a user would have got. A framework that
scored a stubbed retriever would report on itself.

Layout
------
``dataset``   loading, validating, versioning and tagging golden question sets
``runner``    executes the real pipeline per case and records what happened
``metrics``   pure functions over those records - retrieval, citation, planner,
              guardrail, calibration, performance
``benchmark`` orchestration: full runs, threshold sweeps, ablations, baselines
``reports``   JSON, CSV, Markdown and self-contained HTML
``golden``    the datasets themselves
``cli``       ``python -m app.evaluation.cli``

The metrics layer is deliberately pure: every function takes recorded results and
returns numbers, with no I/O and no service dependencies. That is what makes the
metrics themselves testable, which matters more than it sounds - a quality gate
nobody has verified is a quality gate nobody should trust.
"""

from app.evaluation.dataset import (
    GoldenCase,
    GoldenDataset,
    GoldenExpectation,
    load_dataset,
)

__all__ = [
    "GoldenCase",
    "GoldenDataset",
    "GoldenExpectation",
    "load_dataset",
]

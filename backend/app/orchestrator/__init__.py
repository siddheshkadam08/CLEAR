"""Processing orchestrator.

Three separated concerns (§10):

* :mod:`app.orchestrator.workflow` - the **decision plane**. What should run?
* :mod:`app.orchestrator.execution` - the **execution plane**. Dispatch, retries,
  worker allocation, DLQ.
* :mod:`app.orchestrator.runner` - runs one stage and checkpoints it.

The orchestrator coordinates; it never parses, chunks or embeds anything itself.
"""

from app.orchestrator.queue import StageMessage, get_queue_client
from app.orchestrator.workflow import ExecutionPlan, WorkflowEngine

__all__ = [
    "ExecutionPlan",
    "StageMessage",
    "WorkflowEngine",
    "get_queue_client",
]

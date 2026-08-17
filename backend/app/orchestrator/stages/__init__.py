"""Pipeline stage handlers.

One module per stage, each registering a :class:`~app.orchestrator.stages.base.StageHandler`
on import. :func:`app.orchestrator.stages.base.get_stage_handler` imports this
package lazily, so the API process never loads parser or embedding dependencies
just to serve a request.

Stage order (§10.1) - the six of ``STAGE_ORDER``::

    validation -> parser -> docpipeline -> extraction -> embedding -> indexing
"""

from app.orchestrator.stages.base import (
    StageArtifact,
    StageContext,
    StageHandler,
    StageResult,
    get_stage_handler,
    register_stage,
    registered_stages,
    stage_load_errors,
)

__all__ = [
    "StageArtifact",
    "StageContext",
    "StageHandler",
    "StageResult",
    "get_stage_handler",
    "register_stage",
    "registered_stages",
    "stage_load_errors",
]

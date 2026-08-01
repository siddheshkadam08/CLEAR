"""Stage contract: context, result and the handler registry.

Every pipeline stage implements :class:`StageHandler`. That uniformity is what
makes the frozen rules mechanical rather than per-stage discipline:

* **Stage isolation** - a handler receives a :class:`StageContext` and returns a
  :class:`StageResult`. It knows nothing about which stage ran before it or which
  runs next; the Workflow Engine owns sequencing.
* **Artifact-driven** - a handler's output is an artifact plus a summary. The
  runner persists both as a checkpoint, so every stage boundary is resumable.
* **Idempotency** - handlers declare their own replay behaviour by deleting their
  prior output before writing (``delete_for_contract``), so re-running a stage
  replaces rather than duplicates.
* **Version-gated reuse** - a handler declares the versions that affect its output
  via ``versions_for``; the runner compares them against the stored checkpoint and
  skips the stage when nothing relevant moved.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.enums import ArtifactKind, PipelineStage
from app.core.logging import get_logger
from app.core.versions import ComponentVersions, current_versions_for_stage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.contract import Contract
    from app.models.processing import ProcessingJob
    from app.models.profile import DocumentProfile
    from app.storage import IObjectStorage

logger = get_logger(__name__)


@dataclass(slots=True)
class StageContext:
    """Everything a stage handler is given.

    Deliberately concrete rather than a service locator: a handler's dependencies
    are visible in one place, which makes stages straightforward to test in
    isolation.
    """

    db: AsyncSession
    storage: IObjectStorage
    job: ProcessingJob
    contract: Contract
    stage: PipelineStage
    attempt: int = 1
    #: Resolved after classification; ``None`` for the stages that precede it.
    profile: DocumentProfile | None = None
    #: Per-run options from the queue message (e.g. re-extract one category only).
    options: dict[str, Any] = field(default_factory=dict)
    #: Set by the runner so a handler can report incremental progress.
    progress_callback: Any = None

    @property
    def project_id(self) -> uuid.UUID:
        return self.contract.project_id

    @property
    def contract_id(self) -> uuid.UUID:
        return self.contract.id

    @property
    def job_id(self) -> uuid.UUID:
        return self.job.id

    async def report_progress(self, percent: int, note: str | None = None) -> None:
        """Report progress for a long-running stage.

        Also refreshes the job heartbeat, which is what distinguishes a slow parse
        from a dead worker in the stalled-job sweep.
        """
        if self.progress_callback is not None:
            await self.progress_callback(percent, note)

    def profile_setting(self, path: str, default: Any = None) -> Any:
        """Read a dotted setting from the active profile.

        ``ctx.profile_setting("chunk_config.max_tokens", 900)``. Falls back to the
        default when no profile is attached, so a stage never has to branch on
        profile presence.
        """
        if self.profile is None:
            return default
        head, _, tail = path.partition(".")
        container = getattr(self.profile, head, None)
        if not tail:
            return container if container is not None else default
        if isinstance(container, dict):
            return container.get(tail, default)
        return default


@dataclass(slots=True)
class StageArtifact:
    """One artifact a stage produced."""

    kind: ArtifactKind
    payload: Any
    #: Small, queryable facts kept inline in the DB so listing artifacts does not
    #: require reading object storage.
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StageResult:
    """What a stage handler returns."""

    #: Artifacts to persist. The first is treated as the stage's primary output and
    #: becomes the checkpoint's ``artifact_ref``.
    artifacts: list[StageArtifact] = field(default_factory=list)
    #: Counters recorded on the stage run and aggregated onto the job.
    stats: dict[str, Any] = field(default_factory=dict)
    #: Values merged into the job's ``execution_plan`` for later stages to read
    #: (e.g. the profile the classifier selected).
    context_updates: dict[str, Any] = field(default_factory=dict)
    #: When set, the pipeline stops here successfully - used by validation to reject
    #: a document without marking the job failed.
    halt: bool = False
    halt_reason: str | None = None
    #: Warnings surfaced to the user without failing the stage.
    warnings: list[str] = field(default_factory=list)

    @property
    def primary(self) -> StageArtifact | None:
        return self.artifacts[0] if self.artifacts else None

    def artifact(self, kind: ArtifactKind) -> StageArtifact | None:
        return next((item for item in self.artifacts if item.kind is kind), None)


class StageHandler(ABC):
    """Base class for a pipeline stage."""

    #: Which stage this handler implements.
    stage: PipelineStage

    #: Stages whose output this stage reads. Validated by the Workflow Engine before
    #: dispatch, so a handler never runs without its inputs present.
    requires: tuple[ArtifactKind, ...] = ()

    #: True when the stage may reuse a version-compatible checkpoint instead of
    #: re-running. Validation sets this False: file checks are cheap and must not be
    #: skipped on a replacement upload.
    cacheable: bool = True

    #: Whether a failure here is worth retrying. Overridden per exception by the
    #: error's own ``retryable`` flag; this is the stage-level default.
    retryable: bool = True

    @abstractmethod
    async def run(self, ctx: StageContext) -> StageResult:
        """Execute the stage.

        Must be idempotent: called twice with the same inputs it produces the same
        result and no duplicate rows.
        """

    def versions_for(self, ctx: StageContext) -> ComponentVersions:
        """Versions that affect this stage's output.

        The runner compares this against the stored checkpoint; any difference
        forces a re-run. Overridden where a stage needs profile- or
        strategy-specific version keys.
        """
        return current_versions_for_stage(
            ctx.stage,
            profile_id=str(ctx.profile.id) if ctx.profile else None,
            profile_version=ctx.profile.version if ctx.profile else None,
        )

    async def cleanup(self, ctx: StageContext) -> None:
        """Remove this stage's previous output before re-running.

        The idempotency hook. Stages that write relational rows (extraction,
        chunking, embedding) delete theirs here so a retry replaces rather than
        accumulates. Stages that only write artifacts need no cleanup - superseding
        the artifact is enough.
        """
        return None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} stage={self.stage.value}>"


# =============================================================================
# Registry
# =============================================================================
_registry: dict[PipelineStage, StageHandler] = {}

#: Whether the discovery pass has run. A separate flag rather than testing
#: ``_registry`` for emptiness: importing any single stage module registers one
#: handler as a side effect, which would satisfy an emptiness check and leave the
#: other seven permanently unfindable.
_loaded = False

#: Import failures from the discovery pass, keyed by module name. Kept so the
#: health endpoint and the error raised on lookup can say *why* a stage is
#: unavailable rather than only that it is.
_load_errors: dict[str, str] = {}

#: One module per stage, in pipeline order.
_STAGE_MODULES: tuple[str, ...] = (
    "validation",
    "parser",
    "docpipeline",
    "extraction",
    "enrichment",
    "classification",
    "chunking",
    "ai_extraction",
    "embedding",
    "indexing",
)


def register_stage(handler: StageHandler) -> StageHandler:
    """Register a stage handler. One handler per stage."""
    if handler.stage in _registry:
        logger.warning("stage_handler_replaced", stage=handler.stage.value)
    _registry[handler.stage] = handler
    return handler


def get_stage_handler(stage: PipelineStage) -> StageHandler:
    """Look up a handler, importing the stage modules on first use."""
    _load_handlers()
    handler = _registry.get(stage)
    if handler is None:
        from app.core.errors import PipelineError

        reason = _load_errors.get(stage.value)
        raise PipelineError(
            f"No handler is registered for the '{stage.value}' stage."
            + (f" Its module failed to import: {reason}" if reason else ""),
            stage=stage.value,
            retryable=False,
            details={"import_error": reason} if reason else {},
        )
    return handler


def registered_stages() -> list[PipelineStage]:
    """Stages that have a usable handler, in pipeline order."""
    _load_handlers()
    return sorted(_registry, key=lambda s: list(PipelineStage).index(s))


def stage_load_errors() -> dict[str, str]:
    """Stage modules that failed to import, for the health endpoint."""
    _load_handlers()
    return dict(_load_errors)


def _load_handlers(*, force: bool = False) -> None:
    """Import each stage module so its registration side effect runs.

    Lazy rather than at package import time: the API process does not need the
    parser, embedding or OCR dependencies loaded to serve a request.

    **Imported one at a time, deliberately.** A single combined import statement
    would make every stage unavailable when any one module failed - so an absent
    optional extra (``sentence-transformers`` for the local embedding provider, say)
    would stop even the validation stage, which needs nothing. Each failure is
    recorded against its own stage and the rest keep working; the stage that is
    genuinely unavailable then fails loudly at lookup with the import error
    attached, which is where the failure belongs.

    ``force`` re-runs the import attempt. Its purpose is retrying the modules that
    *failed*: a failed import is not cached in ``sys.modules``, so those genuinely
    re-import - which is what makes a stage recoverable once a missing extra is
    installed, without restarting the process. Modules that already succeeded are
    cached and their registration side effect does not run again, which is harmless
    because their handler is already in the registry.
    """
    global _loaded
    if _loaded and not force:
        return

    import importlib

    _load_errors.clear()
    for name in _STAGE_MODULES:
        try:
            importlib.import_module(f"app.orchestrator.stages.{name}")
        except Exception as exc:
            _load_errors[name] = f"{type(exc).__name__}: {exc}"
            logger.error(
                "stage_module_import_failed",
                module=name,
                error=str(exc),
                exc_info=True,
            )

    _loaded = True
    missing = [name for name in _STAGE_MODULES if name in _load_errors]
    logger.info(
        "stage_handlers_loaded",
        registered=[stage.value for stage in _registry],
        unavailable=missing,
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

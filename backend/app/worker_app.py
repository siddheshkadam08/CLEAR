"""Worker application - the same code as the API, a much smaller surface.

``uvicorn app.worker_app:app`` in the ``worker-parser`` and ``worker-ai`` services.
It exposes **only** the internal stage endpoints plus liveness, and deliberately not
the public API:

* A worker pool is scaled on pipeline throughput, not on request traffic. Serving
  the public API from it would make those two concerns share one scaling decision.
* The public routes carry authentication, CORS and rate limiting that a
  cluster-internal service has no business exposing. The smaller the surface, the
  fewer ways in.

Same image, same code, same database - only the routes differ. That is what makes a
worker's behaviour identical to the API's when it runs a stage: there is no second
implementation to drift.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app import __version__
from app.api.internal import router as internal_router
from app.api.internal import stages_for_role
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware, ResponseHeadersMiddleware
from app.core.telemetry import instrument_app, setup_telemetry, shutdown_telemetry

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the stage handlers at startup, not on the first message.

    A worker that discovers a missing handler while holding a job has already taken
    work it cannot do. Importing at startup puts the failure in the logs and the
    readiness probe before anything is dispatched to it.
    """
    settings = get_settings()

    from app.orchestrator.stages.base import registered_stages, stage_load_errors

    available = set(registered_stages())
    errors = stage_load_errors()
    served = [stage for stage in stages_for_role(settings.worker_role) if stage in available]

    logger.info(
        "worker_started",
        worker_role=settings.worker_role,
        serves=[stage.value for stage in served],
        unavailable=sorted(errors),
        environment=settings.app_env,
    )
    if errors:
        # Loud, but not fatal: a worker serving five of eight stages is still useful,
        # and the dispatcher reads /internal/stages to route around the gap.
        logger.error("worker_stages_unavailable", errors=errors)
    if not served:
        logger.error(
            "worker_has_no_stages",
            worker_role=settings.worker_role,
            detail="This worker can run nothing. Check WORKER_ROLE and the import errors.",
        )

    yield

    from app.db.session import shutdown_engine

    await shutdown_engine()
    shutdown_telemetry()
    logger.info("worker_stopped", worker_role=settings.worker_role)


def create_worker_app() -> FastAPI:
    """Build the worker application."""
    settings = get_settings()

    configure_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        service_name=settings.observability.service_name,
    )
    setup_telemetry(settings.observability.service_name)

    app = FastAPI(
        title="CIP Worker",
        version=__version__,
        description=(
            "Internal stage execution service. Not a public API - every route "
            "requires the internal service token."
        ),
        lifespan=lifespan,
        # No public docs or schema: this service is not something anyone should be
        # exploring from a browser.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Only the two middlewares a worker actually needs: request context for
    # correlated logging, and response headers for the trace id. No CORS (no
    # browser), no rate limiting (one trusted caller), no gzip (small payloads).
    app.add_middleware(ResponseHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    app.include_router(internal_router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        """Liveness. Unauthenticated and dependency-free, for the container probe."""
        return {"status": "ok", "service": "worker", "role": settings.worker_role}

    instrument_app(app)
    logger.info("worker_app_configured", worker_role=settings.worker_role)
    return app


app = create_worker_app()

__all__ = ["app", "create_worker_app"]

"""FastAPI application factory.

Middleware order is deliberate and runs outermost-first:

1. ``RequestContextMiddleware`` - assigns the request id and binds log context, so
   everything downstream (including rejections) is correlated.
2. ``ResponseHeadersMiddleware`` - correlation + security headers on every response.
3. ``CORSMiddleware`` - must see the request before routing so preflights work.
4. ``BodySizeLimitMiddleware`` - rejects oversized uploads before the body is read.
5. ``RateLimitMiddleware`` - innermost, so a throttled request is still logged and
   measured by the layers above it.

Startup is fail-fast on configuration but tolerant of transient dependency
outages: a bad ``JWT_SECRET`` in production stops the process (see the production
guard in :class:`~app.core.config.Settings`), while an unreachable database leaves
the process alive and failing readiness so it can recover without a restart loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from starlette.middleware.gzip import GZipMiddleware

from app import __version__
from app.api import health, internal
from app.api.v1 import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    ResponseHeadersMiddleware,
)
from app.core.telemetry import instrument_app, setup_telemetry, shutdown_telemetry

logger = get_logger(__name__)


async def _validate_embedding_configuration(settings: Settings) -> None:
    """Run the embedding diagnostics and abort on anything fatal.

    The database check is best-effort: a compose stack where Postgres is still
    coming up would otherwise fail the API for a reason that resolves itself. The
    configuration and provider checks are not - those are wrong in a way that
    waiting does not fix.
    """
    from app.ai.embedding.diagnostics import assert_ready, diagnose, log_diagnostics

    session: Any = None
    try:
        from app.db.session import session_scope

        async with session_scope() as db:
            report = await diagnose(
                db,
                probe_provider=settings.embedding.verify_on_startup,
                settings=settings,
            )
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - DB not up yet; still check the rest
        logger.warning("embedding_db_check_skipped", error=str(exc)[:200])
        report = await diagnose(
            session,
            probe_provider=settings.embedding.verify_on_startup,
            settings=settings,
        )

    log_diagnostics(report)
    assert_ready(report)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and graceful shutdown."""
    settings = get_settings()

    logger.info(
        "application_starting",
        version=__version__,
        environment=settings.app_env,
        parser=settings.parser.active_parser,
        llm_provider=settings.llm.provider,
        embedding_provider=settings.embedding.provider,
        storage_provider=settings.storage.provider,
        queue_driver=settings.queue.driver,
    )

    # Warm the engine and storage handle so the first request does not pay for
    # connection setup, but do not make startup fail if a dependency is briefly
    # unavailable - readiness reports that instead.
    from app.db.session import database_healthy, get_engine

    get_engine()
    if not await database_healthy():
        logger.warning("database_unreachable_at_startup")

    try:
        from app.storage import get_storage

        get_storage()
    except Exception as exc:  # noqa: BLE001
        logger.error("storage_init_failed_at_startup", error=str(exc))

    if settings.llm.provider == "mock" or settings.embedding.provider == "mock":
        logger.warning(
            "mock_ai_providers_active",
            detail="Deterministic stubs in use; extraction quality is not representative.",
            llm=settings.llm.provider,
            embedding=settings.embedding.provider,
        )

    # Embedding diagnostics, and an abort if the configuration cannot work.
    #
    # This is the one dependency where "degrade and report via readiness" is the
    # wrong call. A database that is briefly down comes back; an embedding config
    # whose dimension disagrees with its column, or whose 2048-wide vectors cannot
    # carry an HNSW index, does not fail requests - it silently returns worse
    # answers for as long as it runs. Refusing to start is the only signal that
    # cannot be ignored.
    await _validate_embedding_configuration(settings)

    try:
        yield
    finally:
        logger.info("application_stopping")
        from app.alerting import close_alert_dispatcher
        from app.core.cache import close_redis
        from app.db.session import shutdown_engine
        from app.storage import close_storage

        # Before the rest: the webhook providers hold pooled HTTP clients, and a
        # shutdown that leaks them shows up as "Unclosed client session" noise that
        # buries whatever the real shutdown problem was.
        await close_alert_dispatcher()
        await close_redis()
        await close_storage()
        await shutdown_engine()
        shutdown_telemetry()
        logger.info("application_stopped")


def create_app() -> FastAPI:
    """Build the API application."""
    settings = get_settings()

    configure_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        service_name=settings.observability.service_name,
    )
    setup_telemetry(settings.observability.service_name)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Enterprise AI Contract Intelligence Platform.\n\n"
            "Ingests PDF/DOCX contracts, extracts structured legal knowledge, and "
            "serves explainable, evidence-grounded answers, search and analytics.\n\n"
            "**Every response that carries AI output also carries its evidence** - "
            "document, page, bounding box, confidence, and the exact "
            "parser/prompt/model versions used."
        ),
        docs_url=settings.docs_url,
        redoc_url=settings.redoc_url,
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )

    # --- middleware (registered inner-to-outer; Starlette reverses the order) ---
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,  # required for the HttpOnly refresh cookie
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Internal-Token"],
        expose_headers=["X-Request-ID", "X-Trace-ID", "Content-Disposition"],
        max_age=600,
    )
    # Compress JSON payloads; 1 KiB minimum avoids wasting CPU on tiny responses.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(ResponseHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    # --- routes ---
    app.include_router(health.router)
    # Stage endpoints are mounted on the API too, so a single-container
    # deployment works without a separate worker service. They stay guarded by
    # the internal token and hidden from the public schema.
    app.include_router(internal.router)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    _customise_openapi(app)
    instrument_app(app)

    # Said loudly and on every start. A deployment running on the mock providers
    # produces invented clause attributes and risk scores that look exactly like
    # real ones on screen, so the only place the difference is visible is here.
    if settings.llm.provider == "mock" or settings.embedding.provider == "mock":
        logger.warning(
            "mock_ai_providers_active",
            llm_provider=settings.llm.provider,
            embedding_provider=settings.embedding.provider,
            detail=(
                "Extractions are synthesised, not read from the contract, and "
                "embeddings are not semantically meaningful. Configure "
                "LLM_PROVIDER/EMBEDDING_PROVIDER for real analysis."
            ),
        )

    logger.info(
        "application_configured",
        routes=len(app.routes),
        cors_origins=settings.cors_origins,
    )
    return app


def _customise_openapi(app: FastAPI) -> None:
    """Add the bearer security scheme and document the error envelope once."""

    def openapi() -> dict:  # type: ignore[type-arg]
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Access token from POST /api/v1/auth/login",
        }
        # Applied globally; endpoints that genuinely allow anonymous access declare
        # `security: []` themselves.
        schema["security"] = [{"BearerAuth": []}]

        schema["tags"] = [
            {"name": "Authentication", "description": "Sign-in, tokens and sessions"},
            {"name": "Users", "description": "User, role and profile administration"},
            {"name": "Projects", "description": "Projects and membership (the security boundary)"},
            {"name": "Contracts", "description": "Upload, repository and document access"},
            {"name": "Processing", "description": "Pipeline jobs, stages and checkpoints"},
            {"name": "Knowledge", "description": "Extracted clauses, obligations, risks, evidence"},
            {"name": "Search", "description": "Keyword, semantic and hybrid search"},
            {"name": "Copilot", "description": "Grounded question answering"},
            {"name": "Dashboards", "description": "KPIs and analytics"},
            {"name": "Alerts", "description": "Expiry, risk and missing-clause alerts"},
            {"name": "Export", "description": "Excel/CSV/JSON/PDF export jobs"},
            {"name": "Administration", "description": "Clause Master, profiles, AI settings"},
        ]

        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


app = create_app()


__all__ = ["app", "create_app"]

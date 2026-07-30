"""Liveness, readiness and metrics endpoints.

The distinction matters to Kubernetes:

* ``/healthz`` (liveness) answers "is this process alive?" and must not touch a
  dependency. A database blip must not cause the orchestrator to kill and restart
  every pod, which would turn a brief outage into a thundering-herd restart storm.
* ``/readyz`` (readiness) answers "can this process serve traffic?" and does check
  dependencies, so a pod that cannot reach Postgres is removed from the load
  balancer while staying alive to recover.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Response, status

from app import __version__
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import metrics_enabled, render_metrics
from app.core.versions import platform_versions
from app.schemas.common import HealthStatus

logger = get_logger(__name__)
router = APIRouter(tags=["Health"], include_in_schema=False)


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, Any]:
    """Process-local liveness. Deliberately checks nothing external."""
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "environment": settings.app_env,
        "service": settings.observability.service_name,
    }


@router.get("/readyz", response_model=HealthStatus, summary="Readiness probe")
async def readyz(response: Response) -> HealthStatus:
    """Dependency readiness: database, required extensions, Redis, storage."""
    from app.core.cache import redis_healthy
    from app.db.session import check_extensions, database_healthy

    settings = get_settings()
    checks: dict[str, Any] = {}
    ready = True

    db_ok = await database_healthy()
    checks["database"] = {"status": "ok" if db_ok else "error"}
    ready &= db_ok

    if db_ok:
        extensions = await check_extensions()
        missing = [name for name, installed in extensions.items() if not installed]
        checks["extensions"] = {
            "status": "ok" if not missing else "error",
            "missing": missing,
        }
        # pgvector missing is the silent killer: inserts succeed and every vector
        # search quietly returns nothing useful. Fail readiness rather than serve
        # broken retrieval.
        ready &= not missing

    redis_ok = await redis_healthy()
    checks["redis"] = {"status": "ok" if redis_ok else "error"}
    ready &= redis_ok

    storage_ok = await _storage_healthy()
    checks["storage"] = {
        "status": "ok" if storage_ok else "error",
        "provider": settings.storage.provider,
    }
    ready &= storage_ok

    # Provider reachability is reported but not gating: the API must keep serving
    # search and reads when an LLM vendor is down. Ingestion degrades instead.
    checks["ai_providers"] = {
        "llm": settings.llm.provider,
        "embedding": settings.embedding.provider,
        "gating": False,
    }

    checks["embedding"] = await _embedding_health()

    # Which pipeline stages actually have a handler. A stage whose module failed to
    # import - a missing optional extra, usually - silently truncates every job at
    # that point, so it is surfaced here rather than discovered from stalled jobs.
    # Not gating: the API serves reads perfectly well with a degraded pipeline, and
    # a worker-only dependency must not take the API out of the load balancer.
    checks["pipeline_stages"] = _stage_availability()

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning("readiness_failed", checks=checks)

    return HealthStatus(
        status="ok" if ready else "error",
        version=__version__,
        environment=settings.app_env,
        checks=checks,
        timestamp=datetime.now(UTC),
    )


async def _embedding_health() -> dict[str, Any]:
    """Live embedding-provider status for the readiness payload.

    Reported, never gating. An embedding outage stops *ingestion*; it does not stop
    the API serving contracts, clauses and keyword search, and taking the pod out of
    the load balancer for it would turn a degraded feature into an outage.

    The dimension reported here is the one the provider actually returned on the
    last probe, not the configured value - the whole point is to catch the case
    where the two disagree.
    """
    from app.ai.embedding import get_embedding_provider

    settings = get_settings().embedding
    payload: dict[str, Any] = {
        "provider": settings.provider,
        "model": settings.model,
        "configured_dimension": settings.dim,
        "storage": settings.storage,
        "gating": False,
    }

    try:
        provider = get_embedding_provider()
    except Exception as exc:  # noqa: BLE001
        payload.update(status="error", detail=str(exc)[:200])
        return payload

    probe = await provider.probe()
    payload.update(
        status="healthy" if probe.ok else "unhealthy",
        available=probe.ok,
        dimension=probe.dim,
        latency_ms=probe.latency_ms,
    )
    if probe.error:
        payload["detail"] = probe.error
    if probe.ok and probe.dim is not None and probe.dim != settings.dim:
        # A live mismatch: the provider works, and every vector it produces will be
        # rejected at insert. Worth flagging even though readiness stays green.
        payload["status"] = "misconfigured"
        payload["detail"] = (
            f"The provider returns {probe.dim} dimensions but EMBEDDING_DIM is "
            f"{settings.dim}. Ingestion will fail on every batch."
        )

    last_success = getattr(provider, "last_success_at", None)
    if last_success:
        payload["last_success_at"] = datetime.fromtimestamp(last_success, UTC).isoformat()

    return payload


def _stage_availability() -> dict[str, Any]:
    """Registered vs unavailable pipeline stages.

    Imports the stage modules as a side effect, which is why it is only called from
    readiness and not from liveness - liveness must stay free of heavy imports.
    """
    from app.core.enums import STAGE_ORDER
    from app.orchestrator.stages.base import registered_stages, stage_load_errors

    try:
        available = {stage.value for stage in registered_stages()}
        errors = stage_load_errors()
    except Exception as exc:  # noqa: BLE001 - readiness must always answer
        logger.error("stage_availability_check_failed", error=str(exc))
        return {"status": "unknown", "error": str(exc)}

    missing = [stage.value for stage in STAGE_ORDER if stage.value not in available]
    return {
        "status": "ok" if not missing else "degraded",
        "registered": [stage.value for stage in STAGE_ORDER if stage.value in available],
        "unavailable": missing,
        "import_errors": errors,
        "gating": False,
    }


async def _storage_healthy() -> bool:
    try:
        from app.storage import get_storage

        return await get_storage().health()
    except Exception as exc:  # noqa: BLE001
        logger.warning("storage_health_check_failed", error=str(exc))
        return False


@router.get("/metrics", summary="Prometheus metrics")
async def prometheus_metrics() -> Response:
    if not metrics_enabled():
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


@router.get("/version", summary="Component versions")
async def versions() -> dict[str, Any]:
    """Every component version in effect.

    Operationally useful for reproducing an extraction: the versions here are the
    ones stamped onto artifacts produced right now (§25).
    """
    return {"application": __version__, "components": platform_versions()}


__all__ = ["router"]

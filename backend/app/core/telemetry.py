"""OpenTelemetry tracing.

Traces span the whole request path: API → orchestrator → queue → worker →
provider call. The queue shim propagates W3C ``traceparent`` in the job payload,
so a stage that runs minutes later on another host still joins the trace that
uploaded the document (see :func:`inject_context` / :func:`extract_context`).

Everything here degrades gracefully: if the collector is unreachable or the SDK
is missing, the application runs untraced rather than failing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from app.core.config import get_settings
from app.core.logging import get_logger

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = get_logger(__name__)

_initialised = False


def setup_telemetry(service_name: str | None = None) -> None:
    """Install the tracer provider and OTLP exporter. Idempotent."""
    global _initialised
    settings = get_settings()

    if _initialised or not settings.observability.otel_enabled:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        resource = Resource.create(
            {
                "service.name": service_name or settings.observability.service_name,
                "service.version": "1.0.0",
                "deployment.environment": settings.app_env,
            }
        )

        provider = TracerProvider(
            resource=resource,
            # Parent-based so a sampled upstream request keeps every child span,
            # which is what makes cross-service pipeline traces usable.
            sampler=ParentBased(root=TraceIdRatioBased(settings.observability.sampler_ratio)),
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=settings.observability.otlp_endpoint,
                    insecure=True,
                ),
                max_queue_size=2048,
                max_export_batch_size=512,
            )
        )
        trace.set_tracer_provider(provider)
        _initialised = True
        logger.info(
            "telemetry_initialised",
            service=service_name or settings.observability.service_name,
            endpoint=settings.observability.otlp_endpoint,
        )
    except Exception as exc:  # noqa: BLE001 - never fail startup over telemetry
        logger.warning("telemetry_init_failed", error=str(exc))


def instrument_app(app: FastAPI) -> None:
    """Auto-instrument FastAPI, httpx and redis."""
    settings = get_settings()
    if not settings.observability.otel_enabled:
        return

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls="healthz,readyz,metrics",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("fastapi_instrumentation_failed", error=str(exc))

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        logger.debug("httpx_instrumentation_skipped", error=str(exc))

    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        RedisInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        logger.debug("redis_instrumentation_skipped", error=str(exc))


def instrument_engine(engine: AsyncEngine) -> None:
    """Instrument SQLAlchemy. Statement text is not captured (may hold PII)."""
    settings = get_settings()
    if not settings.observability.otel_enabled:
        return
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sqlalchemy_instrumentation_skipped", error=str(exc))


def shutdown_telemetry() -> None:
    """Flush pending spans on graceful shutdown."""
    if not _initialised:
        return
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception as exc:  # noqa: BLE001
        logger.debug("telemetry_shutdown_failed", error=str(exc))


def get_tracer(name: str) -> Any:
    """Tracer handle. Returns a no-op tracer when tracing is disabled."""
    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except Exception:  # noqa: BLE001
        return _NoopTracer()


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Start a span with attributes; records exceptions and sets error status."""
    tracer = get_tracer("app")
    try:
        from opentelemetry.trace import Status, StatusCode

        with tracer.start_as_current_span(name) as current:
            for key, value in attributes.items():
                if value is not None:
                    current.set_attribute(key, _attr(value))
            try:
                yield current
            except Exception as exc:
                current.record_exception(exc)
                current.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
    except ImportError:
        yield _NoopSpan()


def _attr(value: Any) -> Any:
    """Coerce to a type the OTel attribute API accepts."""
    if isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, list | tuple):
        return [str(v) for v in value]
    return str(value)


def set_span_attributes(**attributes: Any) -> None:
    """Annotate the current span - useful for adding ids mid-handler."""
    try:
        from opentelemetry import trace

        current = trace.get_current_span()
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, _attr(value))
    # Suppressed deliberately: an unset span attribute is not worth failing work over.
    except Exception:  # noqa: BLE001
        logger.debug("span_attributes_unset", keys=sorted(attributes))


# =============================================================================
# Context propagation across the queue boundary
# =============================================================================
def inject_context(carrier: dict[str, str] | None = None) -> dict[str, str]:
    """Serialise the active trace context into a job payload."""
    carrier = carrier if carrier is not None else {}
    try:
        from opentelemetry.propagate import inject

        inject(carrier)
    # Suppressed deliberately: without propagation the worker starts a new trace
    # instead of continuing this one, which loses correlation but not the work.
    except Exception:  # noqa: BLE001
        logger.debug("trace_context_injection_failed")
    return carrier


def extract_context(carrier: Mapping[str, str] | None) -> Any:
    """Rebuild a trace context received from a job payload or HTTP headers."""
    if not carrier:
        return None
    try:
        from opentelemetry.propagate import extract

        return extract(dict(carrier))
    except Exception:  # noqa: BLE001
        return None


@contextmanager
def continued_span(
    name: str,
    carrier: Mapping[str, str] | None,
    **attributes: Any,
) -> Iterator[Any]:
    """Open a span that continues an upstream trace carried in ``carrier``.

    Used by every stage worker so the parse of page 90 links back to the upload.
    """
    parent = extract_context(carrier)
    tracer = get_tracer("app")
    try:
        from opentelemetry.trace import Status, StatusCode

        with tracer.start_as_current_span(name, context=parent) as current:
            for key, value in attributes.items():
                if value is not None:
                    current.set_attribute(key, _attr(value))
            try:
                yield current
            except Exception as exc:
                current.record_exception(exc)
                current.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
    except ImportError:
        yield _NoopSpan()


# =============================================================================
# No-op fallbacks
# =============================================================================
class _NoopSpan:
    def set_attribute(self, *_: Any, **__: Any) -> None: ...
    def record_exception(self, *_: Any, **__: Any) -> None: ...
    def set_status(self, *_: Any, **__: Any) -> None: ...
    def add_event(self, *_: Any, **__: Any) -> None: ...
    def end(self) -> None: ...
    def __enter__(self) -> _NoopSpan:
        return self

    def __exit__(self, *_: Any) -> None: ...


class _NoopTracer:
    @contextmanager
    def start_as_current_span(self, *_: Any, **__: Any) -> Iterator[_NoopSpan]:
        yield _NoopSpan()


__all__ = [
    "continued_span",
    "extract_context",
    "get_tracer",
    "inject_context",
    "instrument_app",
    "instrument_engine",
    "set_span_attributes",
    "setup_telemetry",
    "shutdown_telemetry",
    "span",
]

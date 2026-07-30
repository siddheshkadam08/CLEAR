"""Structured logging.

Every record is JSON in non-local environments and carries the request context
(request id, trace id, user, project, job, stage) without the call site having
to pass it. Context is stored in :mod:`contextvars`, so it survives ``await``
boundaries and stays isolated per request/task.

Correlation model:
  * ``request_id`` - generated per HTTP request, echoed in ``X-Request-ID``.
  * ``trace_id``   - the OpenTelemetry trace id when a span is active, else the
    request id. This is what the API returns in an error envelope, so an
    operator can jump straight from a user's screenshot to the trace.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

import structlog
from structlog.types import EventDict, Processor

# --- request-scoped context --------------------------------------------------
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_user_id: ContextVar[str | None] = ContextVar("user_id", default=None)
_project_id: ContextVar[str | None] = ContextVar("project_id", default=None)
_job_id: ContextVar[str | None] = ContextVar("job_id", default=None)
_stage: ContextVar[str | None] = ContextVar("stage", default=None)
_contract_id: ContextVar[str | None] = ContextVar("contract_id", default=None)

_CONTEXT_VARS: dict[str, ContextVar[str | None]] = {
    "request_id": _request_id,
    "user_id": _user_id,
    "project_id": _project_id,
    "job_id": _job_id,
    "stage": _stage,
    "contract_id": _contract_id,
}

_configured = False


# =============================================================================
# Context helpers
# =============================================================================
def new_request_id() -> str:
    return uuid.uuid4().hex


def set_request_id(value: str | None) -> Token[str | None]:
    return _request_id.set(value)


def get_request_id() -> str | None:
    return _request_id.get()


def bind_context(**values: Any) -> dict[str, Token[str | None]]:
    """Bind context values for the remainder of this task. Returns reset tokens."""
    tokens: dict[str, Token[str | None]] = {}
    for key, value in values.items():
        var = _CONTEXT_VARS.get(key)
        if var is not None:
            tokens[key] = var.set(None if value is None else str(value))
    return tokens


def reset_context(tokens: dict[str, Token[str | None]]) -> None:
    for key, token in tokens.items():
        var = _CONTEXT_VARS.get(key)
        if var is not None:
            var.reset(token)


@contextmanager
def log_context(**values: Any) -> Iterator[None]:
    """Scope extra log context to a block.

    >>> with log_context(job_id=job.id, stage="parser"):
    ...     logger.info("stage_started")
    """
    tokens = bind_context(**values)
    try:
        yield
    finally:
        reset_context(tokens)


def clear_context() -> None:
    for var in _CONTEXT_VARS.values():
        var.set(None)


def get_trace_id() -> str:
    """The active OpenTelemetry trace id, falling back to the request id.

    Never raises and never returns empty: an error envelope must always carry
    something an operator can search for.
    """
    # Suppressed deliberately: telemetry must never break error reporting. A missing
    # trace id degrades to the request id, which is always available.
    with contextlib.suppress(Exception):
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx is not None and ctx.trace_id:
            return format(ctx.trace_id, "032x")
    return _request_id.get() or "unavailable"


# =============================================================================
# Processors
# =============================================================================
def _inject_context(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """Attach request-scoped context to every record."""
    for key, var in _CONTEXT_VARS.items():
        value = var.get()
        if value is not None:
            event_dict.setdefault(key, value)
    return event_dict


def _inject_trace(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """Attach OTel trace/span ids so logs and traces join up."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx is not None and ctx.trace_id:
            event_dict.setdefault("trace_id", format(ctx.trace_id, "032x"))
            event_dict.setdefault("span_id", format(ctx.span_id, "016x"))
    except Exception:  # noqa: BLE001 - a log line without a trace id is still a log line
        logging.getLogger(__name__).debug("trace_context_unavailable", exc_info=False)
    return event_dict


def _rename_event(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """``event`` -> ``message`` so records match common log schemas."""
    if "event" in event_dict:
        event_dict["message"] = event_dict.pop("event")
    return event_dict


def _redact(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """Scrub anything that looks like a credential before it reaches a sink.

    Defence in depth: a stray ``logger.info("login", **payload)`` must not put a
    password or bearer token in the log pipeline.
    """
    sensitive = (
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "refresh_token",
        "client_secret",
        "password_hash",
        "jwt_secret",
        "connection_string",
    )
    for key in list(event_dict.keys()):
        lowered = key.lower()
        if any(marker in lowered for marker in sensitive):
            event_dict[key] = "***redacted***"
    return event_dict


# =============================================================================
# Configuration
# =============================================================================
def configure_logging(
    level: str = "INFO",
    fmt: str = "json",
    service_name: str = "cip-backend",
) -> None:
    """Configure structlog + stdlib logging. Idempotent."""
    global _configured
    if _configured:
        return

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _inject_context,
        _inject_trace,
        _redact,
    ]

    renderer: Processor
    if fmt == "json":
        # `event` -> `message` only for JSON: the console renderer needs `event`.
        shared_processors.extend([structlog.processors.format_exc_info, _rename_event])
        renderer = structlog.processors.JSONRenderer()
    else:
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Third-party loggers: keep them, but stop them shouting.
    for name, lvl in {
        "uvicorn": "INFO",
        "uvicorn.error": "INFO",
        "uvicorn.access": "WARNING",  # our middleware logs access records
        "gunicorn.error": "INFO",
        "sqlalchemy.engine": "WARNING",
        "sqlalchemy.pool": "WARNING",
        "alembic": "INFO",
        "httpx": "WARNING",
        "httpcore": "WARNING",
        "asyncio": "WARNING",
        "apscheduler": "WARNING",
        "opentelemetry": "WARNING",
        "urllib3": "WARNING",
    }.items():
        logging.getLogger(name).setLevel(lvl)
        logging.getLogger(name).propagate = True

    structlog.contextvars.bind_contextvars(service=service_name)
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a bound structlog logger. Safe to call at import time."""
    if not _configured:
        # Minimal bootstrap so imports before configure_logging() still log.
        configure_logging()
    return structlog.stdlib.get_logger(name or "app")


__all__ = [
    "bind_context",
    "clear_context",
    "configure_logging",
    "get_logger",
    "get_request_id",
    "get_trace_id",
    "log_context",
    "new_request_id",
    "reset_context",
    "set_request_id",
]

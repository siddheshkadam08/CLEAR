"""Alert payload, severity ladder and the provider interface.

The platform already persists :class:`~app.models.alert.Alert` rows for things a
*user* should act on - an expiring contract, a missing mandatory clause. This
package covers the other half: telling an *operator* that the machinery broke.

The two are deliberately separate types. A user-facing alert is scoped to a
project and rendered in the UI; an operational alert carries a stack trace, a
worker name and a host, and goes to whatever channel the on-call rota watches.
:class:`AlertEvent` is that operational payload, and :class:`IAlertProvider` is
the one seam a new channel has to implement.

Providers never raise. A channel being down is not permitted to change the
outcome of the pipeline that is already failing - see
:mod:`app.alerting.dispatcher` for where that guarantee is enforced.
"""

from __future__ import annotations

import socket
import traceback
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import AlertSeverity

#: Cached once. ``gethostname`` hits the resolver on some platforms, and an alert
#: raised on a failing worker is the worst moment to pay for that.
_HOSTNAME = socket.gethostname()


class AlertLevel(StrEnum):
    """Operational severity, ordered.

    Distinct from :class:`~app.core.enums.AlertSeverity`, which is the five-value
    ladder the *product* uses for contract alerts. Operators think in the four
    syslog-ish levels below, and ``ALERT_MIN_LEVEL`` filters on them.
    :meth:`to_alert_severity` maps one onto the other for persistence.
    """

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        """Position in the ladder, for ``>=`` comparisons against a minimum."""
        return _LEVEL_RANK[self]

    def at_least(self, minimum: AlertLevel) -> bool:
        return self.rank >= minimum.rank

    def to_alert_severity(self) -> AlertSeverity:
        """Map onto the persisted product ladder."""
        return _LEVEL_TO_SEVERITY[self]

    @classmethod
    def parse(cls, value: str | AlertLevel) -> AlertLevel:
        """Case-insensitive lookup with an actionable error."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().upper())
        except ValueError as exc:
            valid = ", ".join(level.value for level in cls)
            raise ValueError(f"Unknown alert level {value!r}; expected one of {valid}") from exc


_LEVEL_RANK: dict[AlertLevel, int] = {
    AlertLevel.INFO: 0,
    AlertLevel.WARNING: 1,
    AlertLevel.ERROR: 2,
    AlertLevel.CRITICAL: 3,
}

_LEVEL_TO_SEVERITY: dict[AlertLevel, AlertSeverity] = {
    AlertLevel.INFO: AlertSeverity.INFO,
    AlertLevel.WARNING: AlertSeverity.MEDIUM,
    AlertLevel.ERROR: AlertSeverity.HIGH,
    AlertLevel.CRITICAL: AlertSeverity.CRITICAL,
}


class AlertCategory(StrEnum):
    """What broke. Drives routing, dashboards and the suggested-resolution table.

    Kept coarse on purpose: a category earns its place only if an operator would
    do something different on seeing it.
    """

    DOCUMENT_PROCESSING = "document_processing"
    PARSER = "parser"
    OCR = "ocr"
    AI_EXTRACTION = "ai_extraction"
    EMBEDDING = "embedding"
    LLM = "llm"
    QUEUE = "queue"
    WORKER_CRASH = "worker_crash"
    SCHEDULER = "scheduler"
    DATABASE = "database"
    STORAGE = "storage"
    UNHANDLED_EXCEPTION = "unhandled_exception"
    CRITICAL_SYSTEM = "critical_system"


#: First-line guidance per category, included in the payload so the person woken
#: at 03:00 has somewhere to start. Deliberately about *this* system rather than
#: generic advice - "check IDOC_API_KEY" beats "investigate the parser".
_RESOLUTIONS: dict[AlertCategory, str] = {
    AlertCategory.DOCUMENT_PROCESSING: (
        "Inspect the job's stage runs (`/api/v1/jobs/{id}`). Retry with "
        "`make reprocess job=<id> stage=<stage>` once the cause is understood."
    ),
    AlertCategory.PARSER: (
        "Check the active parser's health (`ACTIVE_PARSER`). For `idoc`, confirm "
        "IDOC_ENDPOINT is reachable and IDOC_API_KEY is set; the registry falls back "
        "to `pymupdf` when it is not."
    ),
    AlertCategory.OCR: (
        "Confirm the OCR engine is installed in the image and OCR_ENABLED is set. "
        "Scanned pages below OCR_SCANNED_PAGE_CHAR_THRESHOLD route through OCR."
    ),
    AlertCategory.AI_EXTRACTION: (
        "Check the LLM provider's health and quota. With LLM_PROVIDER=mock the "
        "extraction is synthesised and must not be trusted."
    ),
    AlertCategory.EMBEDDING: (
        "Run `make embedding-check`. A dimension mismatch between EMBEDDING_DIM and "
        "the live schema needs `make verify-pgvector`."
    ),
    AlertCategory.LLM: (
        "Check vendor status and the API key. Reads keep serving while inference is "
        "down, so this is urgent rather than an outage."
    ),
    AlertCategory.QUEUE: (
        "Check Redis reachability and queue depth at `/queues` on the dispatcher. "
        "A stuck queue with idle workers usually means a QUEUE_PREFIX mismatch "
        "between producer and consumer."
    ),
    AlertCategory.WORKER_CRASH: (
        "Inspect container logs and memory limits. A worker killed repeatedly at the "
        "same stage points at a payload the stage cannot handle."
    ),
    AlertCategory.SCHEDULER: (
        "Check the scheduler container. Missed runs delay alert evaluation and DLQ "
        "sweeps but do not affect ingestion."
    ),
    AlertCategory.DATABASE: (
        "Check connectivity, pool saturation (DB_POOL_SIZE) and that every required "
        "extension is installed - `python -m app.cli smoke` reports all three."
    ),
    AlertCategory.STORAGE: (
        "Check the object store credentials and that the container exists. "
        "STORAGE_PROVIDER selects the adapter."
    ),
    AlertCategory.UNHANDLED_EXCEPTION: (
        "An unexpected code path. The stack trace is the primary evidence; capture it "
        "before retrying."
    ),
    AlertCategory.CRITICAL_SYSTEM: (
        "The platform cannot serve correctly. Escalate before retrying anything."
    ),
}


def suggested_resolution(category: AlertCategory) -> str:
    return _RESOLUTIONS[category]


class AlertEvent(BaseModel):
    """One operational alert, fully self-describing.

    Frozen because an event is a record of something that already happened; a
    provider that needed to mutate it would be rewriting history rather than
    formatting it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: AlertLevel
    category: AlertCategory
    title: str
    message: str

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    environment: str = "development"

    project_id: uuid.UUID | None = None
    #: The contract being processed. Named ``document_id`` because that is the word
    #: an operator uses, and the two are the same thing at this layer.
    document_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None

    #: Ties the alert to the request and to the trace in Jaeger.
    correlation_id: str | None = None
    trace_id: str | None = None

    stage: str | None = None
    exception_type: str | None = None
    stack_trace: str | None = None
    retry_count: int = 0

    worker_name: str | None = None
    host_name: str = _HOSTNAME
    suggested_resolution: str | None = None

    #: Anything category-specific. Kept free-form so a new alert source does not
    #: need a schema change to carry its own context.
    details: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        level: AlertLevel = AlertLevel.ERROR,
        category: AlertCategory = AlertCategory.UNHANDLED_EXCEPTION,
        title: str | None = None,
        max_stack_chars: int = 8000,
        **fields: Any,
    ) -> Self:
        """Build an event from a live exception, capturing its traceback.

        The trace is truncated: chat and webhook endpoints reject large bodies, and
        a 200-frame async traceback would push the useful top frames out of view.
        """
        stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if len(stack) > max_stack_chars:
            stack = stack[:max_stack_chars] + "\n... [truncated]"
        fields.setdefault("suggested_resolution", suggested_resolution(category))
        return cls(
            level=level,
            category=category,
            title=title or f"{type(exc).__name__} in {fields.get('stage') or category.value}",
            message=str(exc) or type(exc).__name__,
            exception_type=type(exc).__name__,
            stack_trace=stack,
            **fields,
        )

    def dedupe_key(self) -> str:
        """Stable identity of the underlying condition.

        Two failures of the same stage on the same document are one condition, not
        two alerts - the persisted row carries a unique index on this among open
        alerts, and channels use it to thread updates.
        """
        parts = [
            self.category.value,
            str(self.document_id or self.project_id or "global"),
            self.stage or "-",
        ]
        return ":".join(parts)

    def as_log_fields(self) -> dict[str, Any]:
        """Flat, structured-log-friendly view. Excludes the stack trace."""
        return {
            "alert_level": self.level.value,
            "alert_category": self.category.value,
            "alert_title": self.title,
            "environment": self.environment,
            "project_id": str(self.project_id) if self.project_id else None,
            "document_id": str(self.document_id) if self.document_id else None,
            "job_id": str(self.job_id) if self.job_id else None,
            "correlation_id": self.correlation_id,
            "trace_id": self.trace_id,
            "stage": self.stage,
            "exception_type": self.exception_type,
            "retry_count": self.retry_count,
            "worker_name": self.worker_name,
            "host_name": self.host_name,
        }


class IAlertProvider(ABC):
    """One notification channel.

    Implementations format an :class:`AlertEvent` and deliver it. They may raise;
    the dispatcher owns retry and suppression, so a provider stays a pure
    "format and send" and does not reimplement backoff five times.
    """

    #: Stable identifier, matching the value used in ``ALERT_PROVIDER``.
    name: str = "base"

    @abstractmethod
    async def send(self, event: AlertEvent) -> None:
        """Deliver one alert, or raise if delivery failed."""

    def is_configured(self) -> bool:
        """Whether this provider has what it needs to deliver.

        Checked at construction so a misconfigured channel is reported once at
        startup rather than once per alert.
        """
        return True

    async def aclose(self) -> None:
        """Release any held resources. Safe to call more than once."""
        return None


__all__ = [
    "AlertCategory",
    "AlertEvent",
    "AlertLevel",
    "IAlertProvider",
    "suggested_resolution",
]

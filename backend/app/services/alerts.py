"""Alert service - raise an operational alert and make sure it lands.

Two destinations, one call. Every alert is:

* **persisted** as an :class:`~app.models.alert.Alert` row, so it appears in the
  product's Alerts screen and survives a restart; and
* **dispatched** to the configured notification channels via
  :mod:`app.alerting`, so someone finds out without watching a screen.

Neither may fail the caller. This service is invoked from the orchestrator's
terminal-failure path, where an exception would replace an accurate "stage X
failed because Y" with a misleading alerting error - destroying the evidence the
operator needs. Every public method therefore returns rather than raises, and
reports what it managed to do.

De-duplication is structural, matching the existing evaluator: ``dedupe_key`` is
unique among *open* alerts, so a document that fails the same stage on three
retries produces one row that is updated, not three the operator must dismiss.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerting import AlertCategory, AlertEvent, AlertLevel, get_alert_dispatcher
from app.alerting.base import suggested_resolution
from app.core.config import get_settings
from app.core.enums import AlertStatus, AlertType, PipelineStage
from app.core.logging import get_logger, get_request_id, get_trace_id
from app.models.alert import Alert

logger = get_logger(__name__)

#: Which stage failure maps onto which operational category, so the alert says
#: "embedding" rather than a generic "processing failed" and the suggested
#: resolution is the useful one.
_STAGE_CATEGORY: dict[str, AlertCategory] = {
    PipelineStage.VALIDATION.value: AlertCategory.DOCUMENT_PROCESSING,
    PipelineStage.PARSER.value: AlertCategory.PARSER,
    PipelineStage.ENRICHMENT.value: AlertCategory.DOCUMENT_PROCESSING,
    PipelineStage.CLASSIFICATION.value: AlertCategory.DOCUMENT_PROCESSING,
    PipelineStage.CHUNKING.value: AlertCategory.DOCUMENT_PROCESSING,
    PipelineStage.AI_EXTRACTION.value: AlertCategory.AI_EXTRACTION,
    PipelineStage.EMBEDDING.value: AlertCategory.EMBEDDING,
    PipelineStage.INDEXING.value: AlertCategory.DOCUMENT_PROCESSING,
}

#: Error codes that mean the platform itself is unwell rather than one awkward
#: document. These escalate to CRITICAL: one bad PDF is routine, an unreachable
#: database is not.
_CRITICAL_ERROR_CODES = frozenset(
    {"database_error", "storage_error", "configuration_error", "queue_error"}
)

_CODE_CATEGORY: dict[str, AlertCategory] = {
    "database_error": AlertCategory.DATABASE,
    "storage_error": AlertCategory.STORAGE,
    "queue_error": AlertCategory.QUEUE,
    "parser_error": AlertCategory.PARSER,
    "ocr_error": AlertCategory.OCR,
    "llm_error": AlertCategory.LLM,
    "embedding_error": AlertCategory.EMBEDDING,
}


@dataclass(slots=True)
class AlertOutcome:
    """What actually happened, so a caller (and a test) can tell."""

    raised: bool = False
    persisted: bool = False
    deduplicated: bool = False
    alert_id: uuid.UUID | None = None
    delivered: dict[str, bool] = field(default_factory=dict)


class AlertService:
    """Raise operational alerts. Construct per unit of work, like the repositories."""

    def __init__(self, db: AsyncSession | None = None) -> None:
        #: Optional: an alert raised from a context with no session (a worker
        #: crash handler, a scheduler tick) still notifies, it just cannot persist.
        self.db = db

    # ------------------------------------------------------------ entry points
    async def raise_processing_failure(
        self,
        *,
        contract: Any,
        stage: PipelineStage | str,
        error: dict[str, Any],
        job_id: uuid.UUID | None = None,
    ) -> AlertOutcome:
        """A document failed a pipeline stage terminally.

        Called by the orchestrator once retries are exhausted. ``error`` is the
        structured payload already stored on the job row, so the alert and the job
        cannot disagree about what went wrong.
        """
        stage_value = stage.value if isinstance(stage, PipelineStage) else str(stage)
        code = str(error.get("code") or "pipeline_error")
        message = str(error.get("message") or "Processing failed.")
        attempt = int(error.get("attempt") or 0)

        category = _CODE_CATEGORY.get(code) or _STAGE_CATEGORY.get(
            stage_value, AlertCategory.DOCUMENT_PROCESSING
        )
        level = AlertLevel.CRITICAL if code in _CRITICAL_ERROR_CODES else AlertLevel.ERROR

        title = self._contract_title(contract)
        event = self._event(
            level=level,
            category=category,
            title=f"{stage_value} failed for {title}",
            message=message,
            stage=stage_value,
            project_id=getattr(contract, "project_id", None),
            document_id=getattr(contract, "id", None),
            job_id=job_id,
            exception_type=str(error.get("exception") or code),
            retry_count=attempt,
            details={
                "error_code": code,
                "retryable": bool(error.get("retryable", False)),
                **dict(error.get("details") or {}),
            },
        )
        return await self.raise_event(event, alert_type=AlertType.PROCESSING_FAILED, persist=True)

    async def raise_exception(
        self,
        exc: BaseException,
        *,
        category: AlertCategory = AlertCategory.UNHANDLED_EXCEPTION,
        level: AlertLevel = AlertLevel.ERROR,
        persist: bool = False,
        **fields: Any,
    ) -> AlertOutcome:
        """Alert on a live exception, capturing its traceback.

        ``persist`` defaults to False: an infrastructure failure is not scoped to a
        project, and the ``alerts`` table requires one. Pass a ``project_id`` and
        ``persist=True`` when the exception does belong to a project.
        """
        event = AlertEvent.from_exception(
            exc,
            level=level,
            category=category,
            environment=str(get_settings().app_env),
            correlation_id=get_request_id(),
            trace_id=get_trace_id(),
            worker_name=get_settings().worker_role,
            **fields,
        )
        return await self.raise_event(event, persist=persist)

    async def raise_event(
        self,
        event: AlertEvent,
        *,
        alert_type: AlertType = AlertType.PROCESSING_FAILED,
        persist: bool = True,
    ) -> AlertOutcome:
        """Persist (best effort) and dispatch one event. Never raises."""
        outcome = AlertOutcome(raised=True)

        if persist and self.db is not None and event.project_id is not None:
            try:
                outcome.alert_id, outcome.deduplicated = await self._persist(event, alert_type)
                outcome.persisted = True
            except Exception as exc:  # noqa: BLE001 - alerting must never propagate
                logger.warning(
                    "alert_not_persisted",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    **event.as_log_fields(),
                )

        try:
            outcome.delivered = await get_alert_dispatcher().dispatch(event)
        except Exception as exc:  # noqa: BLE001 - defence in depth; dispatch also catches
            logger.warning(
                "alert_not_dispatched",
                error=str(exc),
                error_type=type(exc).__name__,
                **event.as_log_fields(),
            )

        return outcome

    # ---------------------------------------------------------------- internals
    def _event(self, **fields: Any) -> AlertEvent:
        """Fill in the ambient context every alert should carry."""
        settings = get_settings()
        fields.setdefault("environment", str(settings.app_env))
        fields.setdefault("correlation_id", get_request_id())
        fields.setdefault("trace_id", get_trace_id())
        fields.setdefault("worker_name", settings.worker_role)
        category = fields.get("category")
        if isinstance(category, AlertCategory):
            fields.setdefault("suggested_resolution", suggested_resolution(category))
        return AlertEvent(**fields)

    async def _persist(
        self, event: AlertEvent, alert_type: AlertType
    ) -> tuple[uuid.UUID | None, bool]:
        """Insert the alert row, or refresh the open one for the same condition.

        Returns ``(alert_id, deduplicated)``. The caller's transaction owns the
        commit: this service is invoked inside the orchestrator's failure handling,
        and committing here would split that unit of work in two.
        """
        assert self.db is not None  # guarded by the caller
        dedupe_key = event.dedupe_key()

        existing = (
            await self.db.execute(
                select(Alert).where(
                    Alert.dedupe_key == dedupe_key,
                    Alert.status == AlertStatus.OPEN,
                )
            )
        ).scalar_one_or_none()

        details = {
            **event.details,
            "level": event.level.value,
            "category": event.category.value,
            "stage": event.stage,
            "exception_type": event.exception_type,
            "retry_count": event.retry_count,
            "correlation_id": event.correlation_id,
            "trace_id": event.trace_id,
            "worker_name": event.worker_name,
            "host_name": event.host_name,
            "suggested_resolution": event.suggested_resolution,
            "occurred_at": event.timestamp.isoformat(),
        }

        if existing is not None:
            # Same condition, still open: update in place. A repeat is new
            # information about *when* and *how often*, not a new alert.
            existing.message = event.message
            existing.severity = event.level.to_alert_severity()
            existing.details = {
                **details,
                "occurrences": int((existing.details or {}).get("occurrences", 1)) + 1,
                "first_seen_at": (existing.details or {}).get(
                    "first_seen_at", event.timestamp.isoformat()
                ),
            }
            await self.db.flush()
            logger.info("alert_deduplicated", alert_id=str(existing.id), **event.as_log_fields())
            return existing.id, True

        alert = Alert(
            project_id=event.project_id,
            contract_id=event.document_id,
            alert_type=alert_type,
            severity=event.level.to_alert_severity(),
            status=AlertStatus.OPEN,
            title=event.title[:255],
            message=event.message,
            details={**details, "occurrences": 1, "first_seen_at": event.timestamp.isoformat()},
            dedupe_key=dedupe_key[:255],
        )
        self.db.add(alert)
        await self.db.flush()
        logger.info("alert_persisted", alert_id=str(alert.id), **event.as_log_fields())
        return alert.id, False

    @staticmethod
    def _contract_title(contract: Any) -> str:
        """Best available human label for the document."""
        for attribute in ("display_title", "title", "original_file_name"):
            value = getattr(contract, attribute, None)
            if value:
                return str(value)
        identifier = getattr(contract, "id", None)
        return f"contract {identifier}" if identifier else "an unknown document"


__all__ = ["AlertOutcome", "AlertService"]

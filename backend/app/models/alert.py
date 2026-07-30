"""Alerts and alert rules (§19 alerts).

Alerts are *generated*, not stored ad hoc: a background evaluator runs after
processing completes and on a schedule, comparing ``contract_metadata`` against
configurable thresholds. Types: contract expiring, high risk, missing mandatory
clause, processing failed, auto-renewal notice, obligation due, review required.

De-duplication is structural. ``dedupe_key`` is unique among open alerts, so an
evaluator that runs hourly does not create 24 copies of "this contract expires in
30 days" per day - it finds the existing open alert and leaves it alone.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import AlertSeverity, AlertStatus, AlertType
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import pg_enum

if TYPE_CHECKING:
    from app.models.contract import Contract


class Alert(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A single actionable alert."""

    __tablename__ = "alerts"

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: NULL for project-level alerts that are not about one document.
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    alert_type: Mapped[AlertType] = mapped_column(
        pg_enum(AlertType, "alert_type"), nullable=False, index=True
    )
    severity: Mapped[AlertSeverity] = mapped_column(
        pg_enum(AlertSeverity, "alert_severity"), nullable=False, index=True
    )
    status: Mapped[AlertStatus] = mapped_column(
        pg_enum(AlertStatus, "alert_status"),
        nullable=False,
        default=AlertStatus.OPEN,
        server_default=AlertStatus.OPEN.value,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    #: Structured payload for the UI: days remaining, missing clause list, risk
    #: score, failing stage. Lets one alert row render a rich card.
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    #: The date this alert is *about* (expiry, notice deadline, due date) - what
    #: the Alerts screen sorts by, distinct from when the alert was generated.
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    #: Stable identity of the underlying condition, e.g.
    #: ``expiring:<contract_id>:2026-09-30``. Unique among open alerts.
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Which rule produced this, so retuning a threshold can retire its alerts.
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("alert_rules.id", ondelete="SET NULL"), nullable=True
    )

    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    contract: Mapped[Contract | None] = relationship("Contract", lazy="joined")

    __table_args__ = (
        # One open alert per underlying condition.
        Index(
            "uq_alerts_open_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        # Alerts screen: this project's open alerts, most urgent first.
        Index(
            "ix_alerts_project_open",
            "project_id",
            "severity",
            "due_date",
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_alerts_project_type_status", "project_id", "alert_type", "status"),
        # Notification bell count.
        Index(
            "ix_alerts_unacknowledged",
            "project_id",
            postgresql_where=text("acknowledged_at IS NULL AND status = 'open'"),
        ),
    )


class AlertRule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Configurable threshold for one alert type (``/api/v1/alerts/rules``).

    NULL ``project_id`` is the platform default; a project-scoped row overrides
    it, so one project can watch a 180-day expiry window while the rest use 90.
    """

    __tablename__ = "alert_rules"

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    #: Human-readable label for the administration screen. Nullable because a rule
    #: is identified by ``(project_id, alert_type)``, not by its name - but "90-day
    #: renewal warning" is far more use to an administrator than "contract_expiring",
    #: and two projects may configure the same type differently.
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    alert_type: Mapped[AlertType] = mapped_column(
        pg_enum(AlertType, "alert_type"), nullable=False, index=True
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    severity: Mapped[AlertSeverity] = mapped_column(
        pg_enum(AlertSeverity, "alert_severity"),
        nullable=False,
        default=AlertSeverity.MEDIUM,
        server_default=AlertSeverity.MEDIUM.value,
    )

    #: Type-specific thresholds:
    #: ``contract_expiring``  -> ``{"window_days": 90, "escalate_days": 30}``
    #: ``high_risk``          -> ``{"risk_score_cutoff": 67}``
    #: ``missing_mandatory_clause`` -> ``{"clause_types": [...]}``
    #: ``obligation_due``     -> ``{"window_days": 14}``
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Escalation window in days; alerts older than this are raised a severity.
    escalate_after_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Notification channels. In-app is always on; email/webhook are opt-in.
    notify_channels: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("project_id", "alert_type", name="uq_alert_rules_project_type"),
        # Platform default: exactly one per type with a NULL project.
        Index(
            "uq_alert_rules_global_type",
            "alert_type",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
        ),
    )


__all__ = ["Alert", "AlertRule"]

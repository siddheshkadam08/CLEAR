"""Clause Master, dashboard and alert schemas.

The Clause Master is the platform's configuration surface: adding a clause category
is a data change, not a deployment. These schemas are therefore the contract an
administrator edits against, and they carry the UI placement that decides whether a
clause gets its own tab.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import Field

from app.core.enums import AlertSeverity, AlertStatus, AlertType, RiskSeverity
from app.schemas.common import BaseSchema, ResponseSchema

# =============================================================================
# Clause Master
# =============================================================================


class ClauseRuleResponse(ResponseSchema):
    """One version of a clause category's extraction contract."""

    id: uuid.UUID
    version: int
    #: Heading patterns, keywords and exclusions applied *before* any model call.
    #: Most of the extraction cost saving on a long agreement comes from here.
    extraction_rule: dict[str, Any] = Field(default_factory=dict)
    #: The attribute contract. Enforced by the extraction validator, which is what
    #: makes ``clauses.attributes`` queryable structured data.
    output_schema: dict[str, Any] = Field(default_factory=dict)
    synonyms: list[str] = Field(default_factory=list)
    prompt_template: str | None = None
    standard_text: str | None = None
    validation_rules: dict[str, Any] = Field(default_factory=dict)
    examples: list[dict[str, Any]] = Field(default_factory=list)
    is_active: bool = True
    change_note: str | None = None
    created_at: datetime


class ClauseCategoryResponse(ResponseSchema):
    """A Clause Master category."""

    id: uuid.UUID
    key: str
    name: str
    description: str | None = None
    group_name: str | None = None
    #: 1 is highest. Drives extraction order - a job that fails part-way still
    #: produced the terms that matter most - and the default sort in every list.
    priority: int
    display_order: int = 0
    mandatory: bool = False
    confidence_threshold: float = 0.85
    default_risk_severity: RiskSeverity | None = None
    is_active: bool = True
    #: True for seeded categories. Editable, but deleting one would break the
    #: profiles that reference it.
    is_system: bool = False
    #: Decides how the frontend surfaces this clause: ``placement: dedicated_tab``
    #: gives it its own tab with the primary fields listed.
    ui_config: dict[str, Any] = Field(default_factory=dict)
    current_rule: ClauseRuleResponse | None = None
    created_at: datetime
    updated_at: datetime | None = None


class ClauseCategoryCreate(BaseSchema):
    key: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    group_name: str | None = Field(default=None, max_length=100)
    priority: int = Field(default=100, ge=1, le=999)
    mandatory: bool = False
    confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    default_risk_severity: RiskSeverity | None = None
    ui_config: dict[str, Any] = Field(default_factory=dict)
    extraction_rule: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    synonyms: list[str] = Field(default_factory=list)


class ClauseCategoryUpdate(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    group_name: str | None = None
    priority: int | None = Field(default=None, ge=1, le=999)
    display_order: int | None = None
    mandatory: bool | None = None
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    default_risk_severity: RiskSeverity | None = None
    is_active: bool | None = None
    ui_config: dict[str, Any] | None = None


class ClauseRuleCreate(BaseSchema):
    """A new rule version.

    Versions accumulate rather than mutate: a contract extracted under version 3
    must stay reproducible after an administrator edits the rule to version 4.
    """

    extraction_rule: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    synonyms: list[str] = Field(default_factory=list)
    prompt_template: str | None = None
    standard_text: str | None = None
    validation_rules: dict[str, Any] = Field(default_factory=dict)
    examples: list[dict[str, Any]] = Field(default_factory=list)
    change_note: str | None = Field(default=None, max_length=500)


# =============================================================================
# Dashboards
# =============================================================================
class KpiTile(ResponseSchema):
    """One headline number."""

    key: str
    label: str
    value: float
    #: Change against the comparison window, where one applies.
    delta: float | None = None
    unit: str | None = None
    #: A filter the tile links to, so clicking "12 expiring" lands on those twelve.
    drilldown: dict[str, Any] | None = None


class DistributionBucket(ResponseSchema):
    label: str
    value: int
    percentage: float = 0.0


class TimeSeriesPoint(ResponseSchema):
    period: date
    value: float


class DashboardResponse(ResponseSchema):
    """The overview screen.

    Scoped to the caller's accessible projects. "All projects" means exactly the
    projects this user belongs to, never the whole table (§1.1).
    """

    scope: str
    project_ids: list[uuid.UUID] = Field(default_factory=list)
    kpis: list[KpiTile] = Field(default_factory=list)
    risk_distribution: list[DistributionBucket] = Field(default_factory=list)
    agreement_type_distribution: list[DistributionBucket] = Field(default_factory=list)
    status_distribution: list[DistributionBucket] = Field(default_factory=list)
    clause_coverage: list[DistributionBucket] = Field(default_factory=list)
    expiring_soon: list[dict[str, Any]] = Field(default_factory=list)
    top_risks: list[dict[str, Any]] = Field(default_factory=list)
    uploads_over_time: list[TimeSeriesPoint] = Field(default_factory=list)
    generated_at: datetime


class ProcessingStatsResponse(ResponseSchema):
    """Pipeline throughput and health, for the admin view."""

    jobs_by_state: dict[str, int] = Field(default_factory=dict)
    stage_durations_ms: dict[str, float] = Field(default_factory=dict)
    success_rate: float = 0.0
    average_duration_ms: float = 0.0
    failed_last_24h: int = 0
    in_flight: int = 0
    #: Extraction and embedding spend, so cost is attributable rather than a
    #: surprise on the invoice (§17).
    total_cost_usd: float = 0.0
    total_tokens: int = 0


# =============================================================================
# Alerts
# =============================================================================
class AlertResponse(ResponseSchema):
    id: uuid.UUID
    project_id: uuid.UUID
    contract_id: uuid.UUID | None = None
    contract_title: str | None = None
    alert_type: AlertType
    severity: AlertSeverity
    status: AlertStatus
    title: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    due_date: date | None = None
    created_at: datetime
    acknowledged_at: datetime | None = None
    acknowledged_by: uuid.UUID | None = None
    resolved_at: datetime | None = None
    note: str | None = None


class AlertUpdateRequest(BaseSchema):
    status: AlertStatus
    note: str | None = Field(default=None, max_length=2000)


class AlertRuleResponse(ResponseSchema):
    id: uuid.UUID
    project_id: uuid.UUID | None = None
    name: str
    alert_type: AlertType
    is_enabled: bool = True
    severity: AlertSeverity
    #: Rule parameters, e.g. ``{"days_before": 90}`` for a renewal warning.
    config: dict[str, Any] = Field(default_factory=dict)
    escalate_after_days: int | None = None
    notify_channels: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime | None = None


class AlertRuleCreate(BaseSchema):
    name: str = Field(min_length=1, max_length=255)
    alert_type: AlertType
    severity: AlertSeverity = AlertSeverity.MEDIUM
    is_enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    escalate_after_days: int | None = Field(default=None, ge=1, le=365)
    notify_channels: list[str] = Field(default_factory=list)
    #: Omitted means the rule applies to every project the caller administers.
    project_id: uuid.UUID | None = None


class AlertRuleUpdate(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    severity: AlertSeverity | None = None
    is_enabled: bool | None = None
    config: dict[str, Any] | None = None
    escalate_after_days: int | None = Field(default=None, ge=1, le=365)
    notify_channels: list[str] | None = None


__all__ = [
    "AlertResponse",
    "AlertRuleCreate",
    "AlertRuleResponse",
    "AlertRuleUpdate",
    "AlertUpdateRequest",
    "ClauseCategoryCreate",
    "ClauseCategoryResponse",
    "ClauseCategoryUpdate",
    "ClauseRuleCreate",
    "ClauseRuleResponse",
    "DashboardResponse",
    "DistributionBucket",
    "KpiTile",
    "ProcessingStatsResponse",
    "TimeSeriesPoint",
]

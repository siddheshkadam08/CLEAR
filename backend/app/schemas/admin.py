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
# Clause Master, grouped by agreement type
# =============================================================================
class AgreementClause(ResponseSchema):
    """One clause as it applies to one agreement type.

    Flattens the category and the mapping row into the shape the screen renders:
    what the clause *is* comes from the Clause Master, whether it applies here
    comes from ``agreement_type_clauses``.
    """

    clause_key: str
    name: str
    description: str | None = None
    group_name: str | None = None
    synonyms: list[str] = Field(default_factory=list)

    #: Applied when a document of this type is processed. Switching it off affects
    #: **new uploads only** - already-extracted contracts keep their clauses.
    is_active: bool = True
    #: Its absence is a finding for this type.
    is_mandatory: bool = False
    display_order: int = 100

    #: False when the clause exists in the Clause Master but has no mapping row for
    #: this type. The screen offers those as "available to add" rather than hiding
    #: them, so growing a type's coverage does not require knowing what exists.
    is_mapped: bool = True


class AgreementTypeClauses(ResponseSchema):
    """Every clause configured for one agreement type."""

    agreement_type: str
    #: The profile's display name where one exists, else the humanised type.
    label: str
    clauses: list[AgreementClause] = Field(default_factory=list)

    @property
    def active_count(self) -> int:
        return sum(1 for clause in self.clauses if clause.is_active and clause.is_mapped)


class AgreementClauseUpsert(BaseSchema):
    """Attach a clause to an agreement type, or change how it applies there."""

    clause_key: str = Field(min_length=2, max_length=64)
    is_active: bool = True
    is_mandatory: bool = False
    display_order: int | None = Field(default=None, ge=0, le=9999)


class ClauseDefinitionUpsert(BaseSchema):
    """Create or edit a clause in the Clause Master itself.

    Deliberately small. The old screen also surfaced ``ui_config``,
    ``output_schema``, ``extraction_rule``, rule versions and a confidence slider -
    none of which a person maintaining a clause list can act on, and two of which
    were raw JSON in a read-only ``<pre>``. Those keep their existing endpoints;
    this is the everyday surface.
    """

    key: str | None = Field(
        default=None,
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Required on create, ignored on update - the key is the identity.",
    )
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    group_name: str | None = Field(default=None, max_length=100)
    #: Alternative headings. The strongest signal the detector has, which is why
    #: this is on the everyday form and `output_schema` is not.
    synonyms: list[str] = Field(default_factory=list)


class ClauseImportRow(BaseSchema):
    """One row of an uploaded sheet.

    Mirrors the export columns exactly, so a round trip is lossless and the file a
    user downloads is the file they can edit and send back.
    """

    agreement_type: str = Field(min_length=1, max_length=64)
    clause_key: str = Field(min_length=2, max_length=64)
    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    group_name: str | None = Field(default=None, max_length=100)
    synonyms: list[str] = Field(default_factory=list)
    is_active: bool = True
    is_mandatory: bool = False
    display_order: int | None = Field(default=None, ge=0, le=9999)


class ClauseImportRequest(BaseSchema):
    """A parsed sheet, plus how to treat rows it does not mention."""

    rows: list[ClauseImportRow] = Field(min_length=1, max_length=5000)
    #: When true, mappings absent from the file are deactivated rather than left
    #: alone. Off by default: a partial sheet is the common case, and silently
    #: switching off everything it omits is the kind of surprise that costs a
    #: reprocessing run.
    deactivate_missing: bool = False
    #: When true, a clause_key with no Clause Master entry is created from the
    #: row's name/description rather than rejected.
    create_missing_clauses: bool = True


class ClauseImportResult(ResponseSchema):
    """What the import actually did, per outcome."""

    clauses_created: int = 0
    clauses_updated: int = 0
    mappings_created: int = 0
    mappings_updated: int = 0
    mappings_deactivated: int = 0
    #: Rows that were not applied, each with the reason. Reported rather than
    #: raised: one bad row in a 400-row sheet should not discard the other 399.
    skipped: list[dict[str, str]] = Field(default_factory=list)


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


class AuditEntryResponse(ResponseSchema):
    """One audit record, as the activity viewer renders it.

    ``before``/``after`` are already sanitised on write - ``audit.sanitize``
    redacts password hashes, tokens and secrets before the row is stored - so what
    is served here is what was recorded. Nothing is redacted at read time, because
    a viewer that hides fields the row does contain would misrepresent the trail.
    """

    id: uuid.UUID
    created_at: datetime
    action: str
    entity_type: str
    entity_id: uuid.UUID | None = None
    entity_label: str | None = None
    project_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    #: Denormalised on write, so a deleted user's actions stay attributable.
    user_email: str | None = None
    succeeded: bool = True
    error_code: str | None = None
    ip: str | None = None
    route: str | None = None
    #: Correlates a row with the request that produced it, and with the logs.
    request_id: str | None = None
    trace_id: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


__all__ = [
    "AgreementClause",
    "AgreementClauseUpsert",
    "AgreementTypeClauses",
    "AlertResponse",
    "AlertRuleCreate",
    "AlertRuleResponse",
    "AlertRuleUpdate",
    "AlertUpdateRequest",
    "AuditEntryResponse",
    "ClauseCategoryCreate",
    "ClauseCategoryResponse",
    "ClauseCategoryUpdate",
    "ClauseDefinitionUpsert",
    "ClauseImportRequest",
    "ClauseImportResult",
    "ClauseImportRow",
    "ClauseRuleCreate",
    "ClauseRuleResponse",
    "DashboardResponse",
    "DistributionBucket",
    "KpiTile",
    "ProcessingStatsResponse",
    "TimeSeriesPoint",
]

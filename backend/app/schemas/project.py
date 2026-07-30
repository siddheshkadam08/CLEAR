"""Project and membership contracts.

The Project is the security boundary, so these schemas are also the surface where
access is granted and revoked. Membership changes are audited as permission
changes.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.core.enums import ProjectStatus, RoleName
from app.schemas.common import BaseSchema, ResponseSchema, UserRef

_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def slugify(value: str) -> str:
    """Derive a URL-safe slug from a project name."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:140] or "project"


# =============================================================================
# Settings
# =============================================================================
class ProjectSettings(BaseSchema):
    """Per-project overrides read by the pipeline and the alert evaluator.

    Every field is optional and falls back to deployment configuration, so a
    project only states what it wants to differ.
    """

    #: Force a specific Document Intelligence Profile instead of letting
    #: classification choose. Useful for a project of known-homogeneous documents.
    default_profile_key: str | None = None
    #: Raise the bar for auto-accepting an extraction in this project.
    review_confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Expiry alert window for this project, overriding the global default.
    alert_expiry_window_days: int | None = Field(default=None, ge=1, le=730)
    alert_risk_score_cutoff: int | None = Field(default=None, ge=0, le=100)
    #: Require human review of every contract regardless of confidence.
    require_human_review: bool | None = None
    default_currency: str | None = Field(default=None, max_length=8)
    default_governing_law: str | None = Field(default=None, max_length=150)
    #: Retention override; null defers to the profile's retention policy.
    retain_years: int | None = Field(default=None, ge=1, le=99)
    #: Processing priority for uploads into this project.
    processing_priority: str | None = Field(default=None, pattern="^(high|normal|low)$")


# =============================================================================
# Requests
# =============================================================================
class ProjectCreateRequest(BaseSchema):
    name: str = Field(min_length=1, max_length=255)
    #: Derived from the name when omitted.
    slug: str | None = Field(default=None, max_length=140)
    description: str | None = None
    client_name: str | None = Field(default=None, max_length=255)
    department: str | None = Field(default=None, max_length=150)
    business_unit: str | None = Field(default=None, max_length=150)
    default_language: str = "en"
    settings: ProjectSettings = Field(default_factory=ProjectSettings)
    #: Members to add on creation. The creator is always added as Project Manager,
    #: so a new project is never left with nobody able to administer it.
    members: list[ProjectMemberCreate] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("Project name must not be blank")
        return cleaned

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str | None) -> str | None:
        if value is None:
            return None
        slug = value.strip().lower()
        if not _SLUG_PATTERN.match(slug):
            raise ValueError("Slug must be lowercase alphanumeric words separated by hyphens")
        return slug


class ProjectUpdateRequest(BaseSchema):
    """Partial update. The slug is immutable once created - it appears in URLs."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    status: ProjectStatus | None = None
    client_name: str | None = Field(default=None, max_length=255)
    department: str | None = Field(default=None, max_length=150)
    business_unit: str | None = Field(default=None, max_length=150)
    default_language: str | None = None
    settings: ProjectSettings | None = None


class ProjectMemberCreate(BaseSchema):
    user_id: uuid.UUID
    role: RoleName
    #: Narrow the role for this member without inventing a new role.
    permission_overrides: list[str] = Field(default_factory=list)


class ProjectMemberUpdate(BaseSchema):
    role: RoleName | None = None
    permission_overrides: list[str] | None = None


class ProjectMemberBulkCreate(BaseSchema):
    """Invite several users at once with the same role."""

    user_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    role: RoleName


# =============================================================================
# Responses
# =============================================================================
class ProjectMemberResponse(ResponseSchema):
    id: uuid.UUID
    project_id: uuid.UUID
    user: UserRef
    role: str
    role_display_name: str
    permissions: list[str] = Field(default_factory=list)
    permission_overrides: list[str] = Field(default_factory=list)
    added_by: uuid.UUID | None = None
    created_at: datetime
    last_accessed_at: datetime | None = None


class ProjectStats(ResponseSchema):
    """Counters shown on a project card."""

    contract_count: int = 0
    ready_contract_count: int = 0
    processing_count: int = 0
    failed_count: int = 0
    needs_review_count: int = 0
    high_risk_count: int = 0
    expiring_count: int = 0
    member_count: int = 0
    open_alert_count: int = 0


class ProjectResponse(ResponseSchema):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None = None
    status: str
    client_name: str | None = None
    department: str | None = None
    business_unit: str | None = None
    default_language: str = "en"
    settings: dict[str, Any] = Field(default_factory=dict)
    created_by: uuid.UUID | None = None
    creator: UserRef | None = None
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime | None = None
    stats: ProjectStats = Field(default_factory=ProjectStats)
    #: The caller's own role and permissions on this project, so the SPA can
    #: render controls correctly without a second request.
    my_role: str | None = None
    my_permissions: list[str] = Field(default_factory=list)


class ProjectListItem(ResponseSchema):
    """Row shape for the project list and switcher."""

    id: uuid.UUID
    name: str
    slug: str
    description: str | None = None
    status: str
    client_name: str | None = None
    contract_count: int = 0
    ready_contract_count: int = 0
    member_count: int = 0
    last_activity_at: datetime | None = None
    created_at: datetime
    my_role: str | None = None
    is_favourite: bool = False


class ProjectFilterParams(BaseSchema):
    search: str | None = Field(default=None, max_length=200)
    status: ProjectStatus | None = None
    department: str | None = None
    client_name: str | None = None
    favourites_only: bool = False


class ProjectActivityResponse(ResponseSchema):
    """Entry in the "Recent Activities" feed."""

    id: uuid.UUID
    project_id: uuid.UUID
    activity_type: str
    summary: str
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    user: UserRef | None = None
    created_at: datetime


# Resolve the forward reference used in ProjectCreateRequest.members.
ProjectCreateRequest.model_rebuild()


__all__ = [
    "ProjectActivityResponse",
    "ProjectCreateRequest",
    "ProjectFilterParams",
    "ProjectListItem",
    "ProjectMemberBulkCreate",
    "ProjectMemberCreate",
    "ProjectMemberResponse",
    "ProjectMemberUpdate",
    "ProjectResponse",
    "ProjectSettings",
    "ProjectStats",
    "ProjectUpdateRequest",
    "slugify",
]

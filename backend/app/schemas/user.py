"""User and role contracts (admin User Management screen)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.core.enums import RoleName
from app.schemas.common import BaseSchema, ProjectRef, ResponseSchema


# =============================================================================
# Roles
# =============================================================================
class RoleResponse(ResponseSchema):
    id: uuid.UUID
    name: str
    display_name: str
    description: str | None = None
    permissions: list[str] = Field(default_factory=list)
    is_system: bool = True
    rank: int = 0


class RoleUpdateRequest(BaseSchema):
    """Adjust a role's permission set. Seeded roles cannot be renamed or deleted."""

    description: str | None = Field(default=None, max_length=500)
    permissions: list[str] | None = None


# =============================================================================
# Users
# =============================================================================
class UserProjectAssignment(BaseSchema):
    """Assign a user to a project with a role, at creation time."""

    project_id: uuid.UUID
    role: RoleName


class UserCreateRequest(BaseSchema):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    #: Omit for an SSO-only account: the user authenticates against Microsoft and
    #: has no local credential to compromise.
    password: str | None = Field(default=None, min_length=8, max_length=256)
    #: Issue the deployment's configured starting password instead of choosing one,
    #: so an administrator provisioning a team does not invent and then have to
    #: communicate a different credential per person. Ignored when ``password`` is
    #: given. The account is always forced to change it at first sign-in.
    use_default_password: bool = False
    is_active: bool = True
    is_system_admin: bool = False
    must_change_password: bool = True
    job_title: str | None = Field(default=None, max_length=150)
    department: str | None = Field(default=None, max_length=150)
    locale: str = "en"
    timezone: str = "UTC"
    #: Project memberships granted immediately, so an invited user is not created
    #: with access to nothing.
    project_assignments: list[UserProjectAssignment] = Field(default_factory=list)

    @field_validator("full_name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("Full name must not be blank")
        return cleaned


class UserUpdateRequest(BaseSchema):
    """Partial update. Email is immutable - it is the SSO join key."""

    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    is_active: bool | None = None
    is_system_admin: bool | None = None
    must_change_password: bool | None = None
    job_title: str | None = Field(default=None, max_length=150)
    department: str | None = Field(default=None, max_length=150)
    avatar_url: str | None = Field(default=None, max_length=1024)
    locale: str | None = None
    timezone: str | None = None


class ProfileUpdateRequest(BaseSchema):
    """What a user may change about themselves (Profile screen).

    Deliberately excludes ``is_active`` and ``is_system_admin``: privilege changes
    go through the admin endpoint and are audited as such.
    """

    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    job_title: str | None = Field(default=None, max_length=150)
    department: str | None = Field(default=None, max_length=150)
    avatar_url: str | None = Field(default=None, max_length=1024)
    locale: str | None = None
    timezone: str | None = None
    preferences: dict[str, object] | None = None


class AdminSetPasswordRequest(BaseSchema):
    """Administrative password reset."""

    new_password: str = Field(min_length=8, max_length=256)
    must_change_password: bool = True


class UserMembershipSummary(ResponseSchema):
    project: ProjectRef
    role: str
    role_display_name: str
    added_at: datetime | None = None


class UserResponse(ResponseSchema):
    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool
    is_system_admin: bool
    must_change_password: bool = False
    auth_provider: str = "local"
    job_title: str | None = None
    department: str | None = None
    avatar_url: str | None = None
    locale: str = "en"
    timezone: str = "UTC"
    last_login_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    #: True when the account is temporarily locked by failed-login throttling.
    is_locked: bool = False
    project_count: int = 0
    memberships: list[UserMembershipSummary] = Field(default_factory=list)


class UserListItem(ResponseSchema):
    """Row shape for the User Management table."""

    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool
    is_system_admin: bool
    auth_provider: str = "local"
    job_title: str | None = None
    department: str | None = None
    #: Highest-ranked role the user holds anywhere, shown as their effective role.
    primary_role: str | None = None
    project_count: int = 0
    last_login_at: datetime | None = None
    created_at: datetime


class UserFilterParams(BaseSchema):
    """Query filters for the user list."""

    search: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None
    is_system_admin: bool | None = None
    role: RoleName | None = None
    project_id: uuid.UUID | None = None
    department: str | None = None
    auth_provider: str | None = None


__all__ = [
    "AdminSetPasswordRequest",
    "ProfileUpdateRequest",
    "RoleResponse",
    "RoleUpdateRequest",
    "UserCreateRequest",
    "UserFilterParams",
    "UserListItem",
    "UserMembershipSummary",
    "UserProjectAssignment",
    "UserResponse",
    "UserUpdateRequest",
]

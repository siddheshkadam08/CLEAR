"""Authentication contracts.

Two login paths, per the Login screen: "Sign in with Microsoft" (Azure AD OIDC,
primary) and email + password for local accounts. There is no self-service
signup - accounts are created by an administrator, which is why the UI says
"Contact Admin".
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Self

from pydantic import EmailStr, Field, model_validator

from app.schemas.common import BaseSchema, ResponseSchema


# =============================================================================
# Requests
# =============================================================================
class LoginRequest(BaseSchema):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    #: Extends the refresh token's life for a trusted device.
    remember_me: bool = False


class RefreshRequest(BaseSchema):
    """Refresh token payload.

    Optional because the token is normally read from an HttpOnly cookie; the body
    form exists for non-browser clients.
    """

    refresh_token: str | None = None


class LogoutRequest(BaseSchema):
    refresh_token: str | None = None
    #: Revoke every session for this user, not just the current one.
    all_sessions: bool = False


class ChangePasswordRequest(BaseSchema):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)
    confirm_password: str = Field(min_length=8, max_length=256)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.new_password != self.confirm_password:
            raise ValueError("New password and confirmation do not match")
        if self.new_password == self.current_password:
            raise ValueError("New password must differ from the current password")
        return self


class OIDCCallbackRequest(BaseSchema):
    """Authorization-code callback from the identity provider."""

    code: str
    #: Signed CSRF state issued when the flow started; verified before exchange.
    state: str


# =============================================================================
# Responses
# =============================================================================
class ProjectMembershipInfo(ResponseSchema):
    """One of the caller's project memberships, with resolved permissions.

    Returned with the session rather than encoded in the JWT: memberships are
    resolved per request so revoking access takes effect immediately instead of
    at token expiry.
    """

    project_id: uuid.UUID
    project_name: str
    project_slug: str
    role: str
    role_display_name: str
    permissions: list[str]
    is_favourite: bool = False


class CurrentUser(ResponseSchema):
    """The authenticated user, as the SPA needs it."""

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
    preferences: dict[str, object] = Field(default_factory=dict)
    last_login_at: datetime | None = None
    #: Every project the caller can see, with the permissions they hold there.
    #: Drives route guards and the project switcher without extra requests.
    memberships: list[ProjectMembershipInfo] = Field(default_factory=list)

    @property
    def accessible_project_ids(self) -> list[uuid.UUID]:
        return [m.project_id for m in self.memberships]


class TokenResponse(ResponseSchema):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - the OAuth token type, not a token
    expires_in: int = Field(description="Access token lifetime in seconds")
    expires_at: datetime
    #: Omitted when the refresh token is delivered as an HttpOnly cookie.
    refresh_token: str | None = None
    user: CurrentUser


class OIDCAuthorizeResponse(ResponseSchema):
    """Where to send the browser to begin the Microsoft sign-in flow."""

    authorization_url: str
    state: str
    #: The PKCE verifier for this attempt. The caller stores it in an HttpOnly
    #: cookie and presents it at the code exchange; it must never reach the page.
    code_verifier: str = Field(default="", exclude=True)
    #: ``none`` on the silent attempt, ``select_account`` on the retry, or
    #: ``None`` when the deployment has silent-first turned off.
    prompt: str | None = None


class SessionInfo(ResponseSchema):
    """An active refresh-token session, for the "signed-in devices" view."""

    id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    user_agent: str | None = None
    ip_address: str | None = None
    is_current: bool = False


class AuthMethodsResponse(ResponseSchema):
    """Which login methods this deployment offers.

    The Login screen queries this rather than hardcoding the Microsoft button, so
    turning SSO on is purely a configuration change.
    """

    password_enabled: bool = True
    microsoft_sso_enabled: bool = False
    microsoft_button_label: str = "Sign in with Microsoft"
    self_signup_enabled: bool = False
    contact_admin_message: str = "Don't have an account? Contact Admin"


__all__ = [
    "AuthMethodsResponse",
    "ChangePasswordRequest",
    "CurrentUser",
    "LoginRequest",
    "LogoutRequest",
    "OIDCAuthorizeResponse",
    "OIDCCallbackRequest",
    "ProjectMembershipInfo",
    "RefreshRequest",
    "SessionInfo",
    "TokenResponse",
]

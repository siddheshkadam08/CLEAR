"""Authentication endpoints.

The refresh token is delivered as an **HttpOnly, SameSite=Lax cookie** rather than
in the response body wherever the caller is a browser. That keeps it out of reach
of JavaScript, so an XSS bug cannot exfiltrate a long-lived credential. Non-browser
clients can opt into the body form with ``?token_in_body=true``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Cookie, Depends, Query, Response, status
from fastapi.responses import RedirectResponse

from app.core.config import get_settings
from app.core.deps import (
    CurrentUserDep,
    DbSession,
    RequestInfoDep,
    get_current_user,
)
from app.core.errors import UnauthenticatedError
from app.core.logging import get_logger
from app.schemas.auth import (
    AuthMethodsResponse,
    ChangePasswordRequest,
    CurrentUser,
    LoginRequest,
    LogoutRequest,
    OIDCAuthorizeResponse,
    RefreshRequest,
    SessionInfo,
    TokenResponse,
)
from app.schemas.common import MessageResponse
from app.services.auth import AuthService

logger = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"])

#: Cookie name for the rotating refresh token.
REFRESH_COOKIE = "cip_refresh"


def _set_refresh_cookie(response: Response, token: str, max_age_days: int) -> None:
    settings = get_settings()
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        max_age=max_age_days * 24 * 3600,
        httponly=True,
        # Lax rather than Strict: the OIDC redirect returns cross-site, and Strict
        # would drop the cookie on that navigation.
        samesite="lax",
        secure=settings.is_production,
        path="/api/v1/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")


@router.get(
    "/methods",
    response_model=AuthMethodsResponse,
    summary="Which sign-in methods this deployment offers",
)
async def auth_methods(db: DbSession) -> AuthMethodsResponse:
    """Read by the Login screen so enabling SSO is purely configuration."""
    return AuthService(db).auth_methods()


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Sign in with email and password",
    responses={
        401: {"description": "Incorrect email or password"},
        403: {"description": "Account deactivated or temporarily locked"},
        429: {"description": "Too many sign-in attempts"},
    },
)
async def login(
    payload: LoginRequest,
    response: Response,
    db: DbSession,
    info: RequestInfoDep,
    token_in_body: Annotated[
        bool, Query(description="Return the refresh token in the body")
    ] = False,
) -> TokenResponse:
    settings = get_settings()
    result = await AuthService(db).login(
        email=payload.email,
        password=payload.password,
        remember_me=payload.remember_me,
        ip=info.ip,
        user_agent=info.user_agent,
    )

    if result.refresh_token:
        _set_refresh_cookie(
            response, result.refresh_token, settings.security.refresh_token_expire_days
        )
        if not token_in_body:
            result.refresh_token = None
    return result


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate the refresh token and issue a new access token",
    responses={401: {"description": "Session expired or token reused"}},
)
async def refresh(
    response: Response,
    db: DbSession,
    info: RequestInfoDep,
    payload: Annotated[RefreshRequest | None, Body()] = None,
    cookie_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
    token_in_body: Annotated[bool, Query()] = False,
) -> TokenResponse:
    token = (payload.refresh_token if payload else None) or cookie_token
    if not token:
        raise UnauthenticatedError("No refresh token supplied.")

    settings = get_settings()
    result = await AuthService(db).refresh(
        refresh_token=token, ip=info.ip, user_agent=info.user_agent
    )

    if result.refresh_token:
        _set_refresh_cookie(
            response, result.refresh_token, settings.security.refresh_token_expire_days
        )
        if not token_in_body:
            result.refresh_token = None
    return result


@router.post("/logout", response_model=MessageResponse, summary="Sign out")
async def logout(
    response: Response,
    db: DbSession,
    user: CurrentUserDep,
    payload: Annotated[LogoutRequest | None, Body()] = None,
    cookie_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> MessageResponse:
    token = (payload.refresh_token if payload else None) or cookie_token
    all_sessions = bool(payload and payload.all_sessions)

    revoked = await AuthService(db).logout(
        user_id=user.id, refresh_token=token, all_sessions=all_sessions
    )
    _clear_refresh_cookie(response)

    return MessageResponse(
        message="Signed out.",
        detail=f"{revoked} session(s) revoked." if revoked else None,
    )


@router.get(
    "/me",
    response_model=CurrentUser,
    summary="The signed-in user, with resolved project permissions",
)
async def me(user: CurrentUserDep, db: DbSession) -> CurrentUser:
    """Memberships are resolved live, so revoked access disappears immediately."""
    return await AuthService(db).build_current_user(user)


@router.get("/sessions", response_model=list[SessionInfo], summary="Active sessions")
async def sessions(
    user: CurrentUserDep,
    db: DbSession,
    cookie_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> list[SessionInfo]:
    return await AuthService(db).list_sessions(user.id, current_token=cookie_token)


@router.post(
    "/change-password",
    response_model=MessageResponse,
    summary="Change your password",
    responses={401: {"description": "Current password is incorrect"}},
)
async def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    db: DbSession,
    info: RequestInfoDep,
    # Deliberately depends on get_current_user rather than require_password_current:
    # a user forced to change their password must be able to reach this endpoint.
    user: Annotated[object, Depends(get_current_user)] = None,
) -> MessageResponse:
    from app.models.identity import User

    assert isinstance(user, User)
    await AuthService(db).change_password(
        user=user,
        current_password=payload.current_password,
        new_password=payload.new_password,
        ip=info.ip,
    )
    # Every other session was revoked; drop this browser's cookie too so the client
    # re-authenticates cleanly rather than failing its next refresh.
    _clear_refresh_cookie(response)
    return MessageResponse(
        message="Password changed.",
        detail="All other sessions have been signed out. Please sign in again.",
    )


# =============================================================================
# Microsoft / Azure AD SSO
# =============================================================================
@router.get(
    "/oidc/authorize",
    response_model=OIDCAuthorizeResponse,
    summary="Begin the Microsoft sign-in flow",
    responses={501: {"description": "Microsoft sign-in is not configured"}},
)
async def oidc_authorize(
    db: DbSession,
    redirect_after: Annotated[str | None, Query(description="Path to return to")] = None,
) -> OIDCAuthorizeResponse:
    return AuthService(db).authorize_url(redirect_after=redirect_after)


@router.get(
    "/oidc/callback",
    summary="Microsoft sign-in callback",
    status_code=status.HTTP_302_FOUND,
    responses={302: {"description": "Redirects to the SPA with a one-time code"}},
)
async def oidc_callback(
    code: Annotated[str, Query()],
    state: Annotated[str, Query()],
    db: DbSession,
    info: RequestInfoDep,
) -> RedirectResponse:
    """Complete the code exchange and hand the session to the SPA.

    The access token travels in the redirect fragment, which browsers do not send
    to servers or write to Referer headers; the refresh token stays in an HttpOnly
    cookie and never reaches JavaScript.
    """
    settings = get_settings()
    result = await AuthService(db).complete_oidc_login(
        code=code, state=state, ip=info.ip, user_agent=info.user_agent
    )

    target = (
        f"{settings.oidc.post_login_redirect}"
        f"#access_token={result.access_token}&expires_in={result.expires_in}"
    )
    response = RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
    if result.refresh_token:
        _set_refresh_cookie(
            response, result.refresh_token, settings.security.refresh_token_expire_days
        )
    return response


__all__ = ["router"]

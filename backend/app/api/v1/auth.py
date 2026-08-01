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
from app.services.auth import INTERACTION_REQUIRED_ERRORS, AuthService

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
#: Carries the PKCE verifier from the authorize redirect to the callback.
#:
#: A cookie rather than the ``state`` blob: state is signed, not encrypted, so a
#: verifier inside it would be readable by anyone holding the URL - and a
#: readable verifier protects nothing. HttpOnly keeps it away from page script
#: too, which matters because the whole point of PKCE here is that the browser
#: holds no secret it can leak.
PKCE_COOKIE = "cip_pkce"

#: Short. The window between the redirect out and the callback back is seconds;
#: anything longer is an abandoned attempt whose verifier should not still work.
_PKCE_COOKIE_MAX_AGE = 600


def _set_pkce_cookie(response: Response, verifier: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=PKCE_COOKIE,
        value=verifier,
        max_age=_PKCE_COOKIE_MAX_AGE,
        httponly=True,
        # Lax, not Strict: the return from Microsoft is a cross-site top-level
        # navigation, and Strict would drop the cookie exactly when it is needed.
        samesite="lax",
        secure=settings.is_production,
        path="/api/v1/auth",
    )


@router.get(
    "/oidc/login",
    summary="Sign in with Microsoft",
    status_code=status.HTTP_302_FOUND,
    responses={
        302: {"description": "Redirects to Microsoft"},
        501: {"description": "Microsoft sign-in is not configured"},
    },
)
async def oidc_login(
    db: DbSession,
    redirect_after: Annotated[str | None, Query(description="Path to return to")] = None,
    prompt: Annotated[
        str | None, Query(description="Force 'select_account' to show the picker")
    ] = None,
) -> RedirectResponse:
    """Begin the flow as a plain redirect.

    A redirect rather than JSON because this is what a link can point at. The
    sign-in button is an ``<a href>``, which means the navigation is a real
    top-level one - and a top-level navigation is what carries cookies back from
    Microsoft and what lets the browser's password manager and Conditional Access
    prompts behave normally. Driving it from ``fetch`` would break all three.
    """
    result = AuthService(db).authorize_url(redirect_after=redirect_after, prompt=prompt)
    response = RedirectResponse(
        url=result.authorization_url, status_code=status.HTTP_302_FOUND
    )
    _set_pkce_cookie(response, result.code_verifier)
    return response


@router.get(
    "/oidc/authorize",
    response_model=OIDCAuthorizeResponse,
    summary="The Microsoft authorization URL, as JSON",
    responses={501: {"description": "Microsoft sign-in is not configured"}},
)
async def oidc_authorize(
    response: Response,
    db: DbSession,
    redirect_after: Annotated[str | None, Query(description="Path to return to")] = None,
    prompt: Annotated[str | None, Query()] = None,
) -> OIDCAuthorizeResponse:
    """For a caller that needs the URL rather than a redirect - a native shell, a
    Teams tab, or a test. The PKCE cookie is set here too, so whichever way the
    browser reaches Microsoft the callback can complete the exchange."""
    result = AuthService(db).authorize_url(redirect_after=redirect_after, prompt=prompt)
    _set_pkce_cookie(response, result.code_verifier)
    return result


@router.get(
    "/oidc/callback",
    summary="Microsoft sign-in callback",
    status_code=status.HTTP_302_FOUND,
    responses={302: {"description": "Redirects to the SPA, or retries interactively"}},
)
async def oidc_callback(
    db: DbSession,
    info: RequestInfoDep,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
    pkce_verifier: Annotated[str | None, Cookie(alias=PKCE_COOKIE)] = None,
) -> RedirectResponse:
    """Complete the code exchange and hand the session to the SPA.

    Also the place the silent-first fallback lands. ``prompt=none`` does not
    render a page when it cannot complete - it redirects straight back here with
    ``error=login_required`` or similar - so this handler is what turns "could not
    do it silently" into the account picker. That retry is deliberately bounded to
    one attempt: the state records which prompt produced it, so a picker that
    itself fails cannot loop the user between Microsoft and here.

    The access token travels in the redirect fragment, which browsers do not send
    to servers or write to Referer headers; the refresh token stays in an HttpOnly
    cookie and never reaches JavaScript.
    """
    settings = get_settings()
    service = AuthService(db)

    if error:
        return _handle_authorize_error(
            service, error=error, description=error_description, state=state
        )

    if not code or not state:
        logger.warning("oidc_callback_missing_parameters", has_code=bool(code))
        return _fail_to_login("Microsoft did not return a sign-in result. Please try again.")

    result = await service.complete_oidc_login(
        code=code,
        state=state,
        code_verifier=pkce_verifier,
        ip=info.ip,
        user_agent=info.user_agent,
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
    # The verifier is single-use. Leaving it set would let a replayed code be
    # exchanged a second time from the same browser.
    response.delete_cookie(PKCE_COOKIE, path="/api/v1/auth")
    return response


def _handle_authorize_error(
    service: AuthService, *, error: str, description: str | None, state: str | None
) -> RedirectResponse:
    """Turn an authorize-endpoint error into a retry or an honest message."""
    from app.core.security import verify_signed_state

    attempted_prompt = ""
    redirect_after = ""
    if state:
        try:
            claims = verify_signed_state(state)
            attempted_prompt = str(claims.get("prompt") or "")
            redirect_after = str(claims.get("redirect_after") or "")
        except Exception:  # noqa: BLE001 - a bad state just means no retry context
            logger.warning("oidc_error_state_unreadable", error=error)

    retryable = error in INTERACTION_REQUIRED_ERRORS
    # Only the silent attempt is retried. A picker that came back with the same
    # class of error means the user cannot complete it, and sending them round
    # again would be a loop rather than a fallback.
    if retryable and attempted_prompt == "none":
        logger.info("oidc_silent_failed_retrying_interactively", error=error)
        retry = service.authorize_url(
            redirect_after=redirect_after or None, prompt="select_account"
        )
        response = RedirectResponse(
            url=retry.authorization_url, status_code=status.HTTP_302_FOUND
        )
        _set_pkce_cookie(response, retry.code_verifier)
        return response

    logger.warning("oidc_authorize_error", error=error, description=(description or "")[:200])
    if error == "access_denied":
        return _fail_to_login("Sign-in was cancelled.")
    return _fail_to_login(
        "Microsoft could not sign you in. Contact your administrator if this continues."
    )


def _fail_to_login(message: str) -> RedirectResponse:
    """Send the browser back to the login screen with something readable.

    The message goes in the fragment rather than the query string: a query string
    is written to server logs and Referer headers all the way down, and a failed
    sign-in should not leave a trail describing itself in every proxy on the path.
    """
    from urllib.parse import quote

    settings = get_settings()
    target = f"{settings.oidc.post_login_redirect}#error={quote(message)}"
    return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)


__all__ = ["router"]

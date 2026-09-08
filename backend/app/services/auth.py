"""Authentication service: password login, Microsoft SSO, refresh rotation.

Security decisions worth stating explicitly, because they are the kind that look
like details and are not:

* **No enumeration.** Unknown email and wrong password produce the same error and
  comparable timing (see :func:`~app.core.security.verify_password`).
* **Permissions are not in the token.** The JWT carries identity only; project
  memberships are resolved per request, so revoking a membership takes effect on
  the next call rather than at token expiry.
* **Refresh tokens rotate.** Each use revokes the presented token and issues a
  successor. Presenting an already-revoked token means it was captured, so every
  session for that user is revoked and the event is logged.
* **Password change ends other sessions.** An access token dies in minutes, but a
  live refresh token would otherwise keep an attacker signed in for days.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from functools import lru_cache
from html import escape
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.cache import cache_get, cache_set, make_key
from app.core.config import get_settings
from app.core.enums import AuditAction, AuthProvider, RoleName
from app.core.errors import (
    ForbiddenError,
    InvalidCredentialsError,
    NotImplementedFeatureError,
    TokenInvalidError,
    UnauthenticatedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    create_nonce,
    create_password_reset_token,
    create_pkce_verifier,
    create_refresh_token,
    create_signed_state,
    decode_token,
    equalise_password_timing,
    hash_password,
    hash_token,
    needs_rehash,
    pkce_challenge,
    validate_password_strength,
    verify_password,
    verify_password_reset_token,
    verify_signed_state,
)
from app.models.identity import User
from app.repositories.identity import RefreshTokenRepository, RoleRepository, UserRepository
from app.repositories.project import ProjectMemberRepository
from app.schemas.auth import (
    AuthMethodsResponse,
    CurrentUser,
    OIDCAuthorizeResponse,
    ProjectMembershipInfo,
    SessionInfo,
    TokenResponse,
)
from app.services.audit import AuditService
from app.services.mailer import send_mail

logger = get_logger(__name__)

#: How long one email address is left alone after a reset request. Long enough
#: that a mailbox cannot be flooded, short enough that a user who deleted the
#: first mail by accident is not locked out of trying again.
_RESET_COOLDOWN_SECONDS = 120

#: Entra error codes that mean "the silent attempt could not complete, ask the
#: user". Anything else is a real failure and must not be retried - retrying a
#: consent or configuration error just shows the user the same wall twice.
INTERACTION_REQUIRED_ERRORS = frozenset(
    {
        "login_required",
        "interaction_required",
        "consent_required",
        "account_selection_required",
    }
)


@lru_cache(maxsize=4)
def _jwks_client(jwks_url: str) -> Any:
    """One cached JWKS client per authority.

    ``PyJWKClient`` caches signing keys internally, but only for its own
    lifetime - constructing a new one per sign-in would fetch the key set on
    every login and make Microsoft's JWKS endpoint a dependency of each one.
    """
    import jwt

    return jwt.PyJWKClient(jwks_url, cache_keys=True)


class AuthService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.settings = get_settings()
        self.users = UserRepository(db)
        self.roles = RoleRepository(db)
        self.tokens = RefreshTokenRepository(db)
        self.members = ProjectMemberRepository(db)
        self.audit = AuditService(db)

    # =========================================================================
    # Password login
    # =========================================================================
    async def login(
        self,
        *,
        email: str,
        password: str,
        remember_me: bool = False,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> TokenResponse:
        """Authenticate with email + password."""
        user = await self.users.get_by_email(email)

        # Verify even when the user is absent, so timing does not reveal existence.
        password_ok = verify_password(password, user.password_hash if user else None)

        if user is None or not password_ok:
            if user is not None:
                locked = await self.users.record_failed_login(user)
                if locked:
                    logger.warning("account_locked", user_id=str(user.id))
            metrics.auth_attempts_total.labels(method="password", outcome="failure").inc()
            await self.audit.record_login(
                user_id=user.id if user else None,
                email=email,
                succeeded=False,
                ip=ip,
                user_agent=user_agent,
                reason="invalid_credentials",
            )
            raise InvalidCredentialsError()

        if user.is_locked:
            metrics.auth_attempts_total.labels(method="password", outcome="locked").inc()
            await self.audit.record_login(
                user_id=user.id,
                email=email,
                succeeded=False,
                ip=ip,
                user_agent=user_agent,
                reason="account_locked",
            )
            raise ForbiddenError(
                "This account is temporarily locked after repeated failed sign-in "
                "attempts. Please try again shortly."
            )

        if not user.is_active:
            metrics.auth_attempts_total.labels(method="password", outcome="inactive").inc()
            await self.audit.record_login(
                user_id=user.id,
                email=email,
                succeeded=False,
                ip=ip,
                user_agent=user_agent,
                reason="inactive_account",
            )
            raise ForbiddenError("This account has been deactivated. Contact your administrator.")

        # Opportunistic upgrade: re-hash with current parameters while we have the
        # plaintext, so old hashes migrate without asking users to reset.
        if user.password_hash and needs_rehash(user.password_hash):
            await self.users.set_password(
                user, hash_password(password), must_change=user.must_change_password
            )
            logger.info("password_hash_upgraded", user_id=str(user.id))

        await self.users.record_successful_login(user)
        metrics.auth_attempts_total.labels(method="password", outcome="success").inc()
        await self.audit.record_login(
            user_id=user.id, email=email, succeeded=True, ip=ip, user_agent=user_agent
        )

        return await self._issue_session(
            user, remember_me=remember_me, ip=ip, user_agent=user_agent
        )

    # =========================================================================
    # Microsoft / Azure AD SSO
    # =========================================================================
    def authorize_url(
        self,
        *,
        redirect_after: str | None = None,
        prompt: str | None = None,
    ) -> OIDCAuthorizeResponse:
        """Build the Microsoft authorization URL.

        ``prompt`` drives the silent-first behaviour:

        * ``None`` and ``silent_first`` on → ``prompt=none``. Entra completes
          invisibly when the browser already holds exactly one usable session,
          and otherwise returns an error the callback turns into a retry.
        * ``select_account`` → the account picker, which is where that retry
          lands and where a user switching identity starts.

        The PKCE verifier is returned rather than embedded in the state. The
        state is a *signed* blob, not an encrypted one, so anything inside it is
        readable by whoever holds it - and a verifier the client can read is a
        verifier that has stopped protecting the exchange. The caller puts it in
        an HttpOnly cookie.
        """
        if not self.settings.oidc.is_configured:
            raise NotImplementedFeatureError("Microsoft sign-in is not configured.")

        from urllib.parse import urlencode

        resolved_prompt = prompt
        if resolved_prompt is None and self.settings.oidc.silent_first:
            resolved_prompt = "none"

        verifier = create_pkce_verifier()
        nonce = create_nonce()
        # The nonce goes in the state so the callback can compare it against the
        # id token's claim. It needs integrity, not secrecy - knowing it lets an
        # attacker do nothing, whereas *changing* it is what the signature stops.
        state = create_signed_state(
            {
                "redirect_after": redirect_after or "",
                "nonce": nonce,
                "prompt": resolved_prompt or "",
            }
        )

        params = {
            "client_id": self.settings.oidc.client_id,
            "response_type": "code",
            "redirect_uri": self.settings.oidc.redirect_uri,
            "response_mode": "query",
            "scope": self.settings.oidc.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        if resolved_prompt:
            params["prompt"] = resolved_prompt

        url = f"{self.settings.oidc.authority}/oauth2/v2.0/authorize?{urlencode(params)}"
        logger.info("oidc_authorize_url_built", prompt=resolved_prompt or "default")
        return OIDCAuthorizeResponse(
            authorization_url=url, state=state, code_verifier=verifier, prompt=resolved_prompt
        )

    async def complete_oidc_login(
        self,
        *,
        code: str,
        state: str,
        code_verifier: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> TokenResponse:
        """Exchange an authorization code and sign the user in."""
        if not self.settings.oidc.is_configured:
            raise NotImplementedFeatureError("Microsoft sign-in is not configured.")

        # Verify state before spending a network call on the code exchange.
        try:
            state_claims = verify_signed_state(state)
        except Exception as exc:
            logger.warning("oidc_state_invalid")
            raise TokenInvalidError("Sign-in request expired or was tampered with.") from exc

        claims = await self._exchange_code(code, code_verifier=code_verifier)

        # The nonce binds this token to *this* sign-in attempt. Without the check
        # a token captured from one flow could be replayed into another.
        expected_nonce = str(state_claims.get("nonce") or "")
        if expected_nonce and str(claims.get("nonce") or "") != expected_nonce:
            logger.warning("oidc_nonce_mismatch")
            raise TokenInvalidError("Sign-in response did not match the request.")

        subject = str(claims.get("oid") or claims.get("sub") or "")
        email = str(claims.get("email") or claims.get("preferred_username") or "").lower()
        full_name = str(claims.get("name") or email.split("@")[0] or "Unknown")

        if not subject or not email:
            raise TokenInvalidError("The identity provider did not return an email address.")

        self._require_allowed_domain(email)

        user = await self._resolve_sso_user(subject=subject, email=email, full_name=full_name)

        if not user.is_active:
            metrics.auth_attempts_total.labels(method="microsoft", outcome="inactive").inc()
            await self.audit.record_login(
                user_id=user.id,
                email=email,
                succeeded=False,
                method="microsoft",
                ip=ip,
                reason="inactive_account",
            )
            raise ForbiddenError("This account has been deactivated. Contact your administrator.")

        await self.users.record_successful_login(user)
        metrics.auth_attempts_total.labels(method="microsoft", outcome="success").inc()
        await self.audit.record_login(
            user_id=user.id,
            email=email,
            succeeded=True,
            method="microsoft",
            ip=ip,
            user_agent=user_agent,
        )
        return await self._issue_session(user, ip=ip, user_agent=user_agent)

    def _require_allowed_domain(self, email: str) -> None:
        """Refuse an address outside the configured domains.

        Only bites on a multi-tenant authority, where Entra itself will happily
        authenticate any Microsoft account. With a pinned tenant the list is
        normally empty and this does nothing, which is correct - the tenant is
        already the boundary.
        """
        allowed = self.settings.oidc.allowed_email_domains
        if not allowed:
            return
        domain = email.rsplit("@", 1)[-1].lower()
        if domain in allowed:
            return

        logger.warning("oidc_domain_rejected", domain=domain)
        metrics.auth_attempts_total.labels(method="microsoft", outcome="domain_rejected").inc()
        raise ForbiddenError(
            "That Microsoft account is not permitted to sign in to this workspace."
        )

    async def _exchange_code(
        self, code: str, *, code_verifier: str | None = None
    ) -> dict[str, Any]:
        """Swap the authorization code for tokens and return the id-token claims."""
        token_url = f"{self.settings.oidc.authority}/oauth2/v2.0/token"
        data = {
            "client_id": self.settings.oidc.client_id,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.settings.oidc.redirect_uri,
            "scope": self.settings.oidc.scopes,
        }
        # A public client sends only the PKCE verifier; a confidential one sends
        # the secret as well. Sending an empty secret is not the same as omitting
        # it - Entra rejects the request outright - which is why this is a
        # conditional rather than a default.
        if self.settings.oidc.is_confidential_client:
            data["client_secret"] = self.settings.oidc.client_secret
        if code_verifier:
            data["code_verifier"] = code_verifier
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(token_url, data=data)
                if response.status_code != 200:
                    logger.warning(
                        "oidc_token_exchange_failed",
                        status_code=response.status_code,
                        body=response.text[:400],
                    )
                    raise TokenInvalidError("Microsoft sign-in could not be completed.")
                payload = response.json()
        except httpx.HTTPError as exc:
            logger.error("oidc_token_exchange_error", error=str(exc))
            raise TokenInvalidError("Could not reach the identity provider.") from exc

        id_token = payload.get("id_token")
        if not id_token:
            raise TokenInvalidError("The identity provider did not return an id token.")
        return self._decode_id_token(id_token)

    def _decode_id_token(self, id_token: str) -> dict[str, Any]:
        """Verify the id token against Microsoft's JWKS and return its claims.

        **The signature is always checked.** This used to fall through to
        ``verify_signature: False`` when no tenant was pinned, on the reasoning
        that TLS plus the client secret already established authenticity. That
        reasoning does not survive a public client - there is no secret - and it
        never justified the fallback anyway: an unverified token is a set of
        attacker-controlled claims, and the identity built from them is whoever
        the attacker named.

        Four things are verified, and each rejects a different attack:

        * **signature** against the tenant's published keys - the token was minted
          by Entra and not by whoever sent it;
        * **audience** equals our client id - a token issued for a different
          application cannot be replayed into this one;
        * **issuer** is our tenant - a token from another tenant is not our user;
        * **expiry**, which ``PyJWT`` enforces by default.
        """
        import jwt

        jwks_url = f"{self.settings.oidc.authority}/discovery/v2.0/keys"
        try:
            # PyJWKClient caches the key set in-process, so this is one network
            # call on the first sign-in after a restart rather than one per login.
            jwks_client = _jwks_client(jwks_url)
            signing_key = jwks_client.get_signing_key_from_jwt(id_token)
            return dict(
                jwt.decode(
                    id_token,
                    signing_key.key,
                    algorithms=["RS256"],
                    audience=self.settings.oidc.client_id,
                    issuer=f"{self.settings.oidc.authority}/v2.0",
                )
            )
        except Exception as exc:
            logger.warning("oidc_id_token_verification_failed", error=str(exc))
            raise TokenInvalidError("Could not verify the Microsoft sign-in response.") from exc

    async def _resolve_sso_user(self, *, subject: str, email: str, full_name: str) -> User:
        """Find or provision the local account behind an SSO identity."""
        user = await self.users.get_by_external_subject(AuthProvider.MICROSOFT, subject)
        if user is not None:
            # Keep the display name and email in step with the directory.
            if user.full_name != full_name or user.email.lower() != email:
                user.full_name = full_name
                user.email = email
                await self.db.flush()
            return user

        # An account may already exist locally with this email - link rather than
        # duplicate, so an administrator can pre-create users before first login.
        existing = await self.users.get_by_email(email)
        if existing is not None:
            existing.auth_provider = AuthProvider.MICROSOFT
            existing.external_subject = subject
            existing.full_name = existing.full_name or full_name
            await self.db.flush()
            logger.info("sso_linked_existing_user", user_id=str(existing.id))
            return existing

        if not self.settings.oidc.auto_provision_users:
            raise ForbiddenError("No account exists for this address. Contact your administrator.")

        # Auto-provisioned users start with no project memberships: authenticating
        # proves identity, not authorisation. An administrator grants project
        # access, which is what the "assigned projects" model requires (§18).
        user = await self.users.create(
            email=email,
            full_name=full_name,
            password_hash=None,
            is_active=True,
            is_system_admin=False,
            auth_provider=AuthProvider.MICROSOFT,
            external_subject=subject,
        )
        logger.info("sso_user_provisioned", user_id=str(user.id), email=email)
        return user

    # =========================================================================
    # Refresh & logout
    # =========================================================================
    async def refresh(
        self,
        *,
        refresh_token: str,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> TokenResponse:
        """Rotate a refresh token and issue a new access token."""
        if not refresh_token:
            raise UnauthenticatedError("No refresh token supplied.")

        digest = hash_token(refresh_token)
        stored = await self.tokens.get_active_by_hash(digest)

        if stored is None:
            # Distinguish "never existed" from "already used". Reuse of a revoked
            # token means it leaked, so every session for that user is cut.
            replayed = await self.tokens.get_any_by_hash(digest)
            if replayed is not None:
                revoked = await self.tokens.revoke_all_for_user(replayed.user_id)
                logger.warning(
                    "refresh_token_reuse_detected",
                    user_id=str(replayed.user_id),
                    sessions_revoked=revoked,
                )
                await self.audit.record_denial(
                    user_id=replayed.user_id,
                    user_email=None,
                    entity_type="refresh_token",
                    entity_id=replayed.id,
                    reason="refresh_token_reuse",
                    ip=ip,
                )
            metrics.auth_attempts_total.labels(method="refresh", outcome="failure").inc()
            raise TokenInvalidError("Your session is no longer valid. Please sign in again.")

        user = await self.users.get(stored.user_id)
        if user is None or not user.is_active:
            await self.tokens.revoke(stored)
            metrics.auth_attempts_total.labels(method="refresh", outcome="inactive").inc()
            raise ForbiddenError("This account is no longer active.")

        new_token, new_hash, expires_at = create_refresh_token(user.id)
        await self.tokens.rotate(
            stored,
            token_hash=new_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip,
        )
        metrics.auth_attempts_total.labels(method="refresh", outcome="success").inc()

        access_token, access_expires = create_access_token(
            user.id,
            is_system_admin=user.is_system_admin,
            email=user.email,
            auth_provider=str(user.auth_provider),
        )
        return TokenResponse(
            access_token=access_token,
            expires_in=self.settings.security.access_token_expire_minutes * 60,
            expires_at=access_expires,
            refresh_token=new_token,
            user=await self.build_current_user(user),
        )

    async def logout(
        self,
        *,
        user_id: uuid.UUID,
        refresh_token: str | None = None,
        all_sessions: bool = False,
    ) -> int:
        """Revoke the current session, or every session for the user."""
        if all_sessions:
            count = await self.tokens.revoke_all_for_user(user_id)
            logger.info("logout_all_sessions", user_id=str(user_id), sessions=count)
            return count

        if refresh_token:
            stored = await self.tokens.get_active_by_hash(hash_token(refresh_token))
            if stored is not None and stored.user_id == user_id:
                await self.tokens.revoke(stored)
                return 1
        return 0

    async def list_sessions(
        self, user_id: uuid.UUID, *, current_token: str | None = None
    ) -> list[SessionInfo]:
        current_hash = hash_token(current_token) if current_token else None
        return [
            SessionInfo(
                id=token.id,
                created_at=token.created_at,
                expires_at=token.expires_at,
                user_agent=token.user_agent,
                ip_address=token.ip_address,
                is_current=token.token_hash == current_hash,
            )
            for token in await self.tokens.active_sessions(user_id)
        ]

    # =========================================================================
    # Password management
    # =========================================================================
    async def change_password(
        self,
        *,
        user: User,
        current_password: str,
        new_password: str,
        ip: str | None = None,
    ) -> None:
        """Change a password and end every other session."""
        if user.password_hash is None:
            raise ValidationError(
                "This account signs in with Microsoft and has no password to change."
            )
        if not verify_password(current_password, user.password_hash):
            await self.audit.record_denial(
                user_id=user.id,
                user_email=user.email,
                entity_type="user",
                entity_id=user.id,
                reason="wrong_current_password",
                ip=ip,
            )
            raise InvalidCredentialsError("Your current password is incorrect.")

        validate_password_strength(new_password)
        await self.users.set_password(user, hash_password(new_password), must_change=False)

        # A password change should invalidate anything an attacker already holds.
        revoked = await self.tokens.revoke_all_for_user(user.id)
        logger.info("password_changed", user_id=str(user.id), sessions_revoked=revoked)

        from app.core.enums import AuditAction

        await self.audit.record(
            action=AuditAction.UPDATE,
            entity_type="user",
            entity_id=user.id,
            entity_label=user.email,
            user_id=user.id,
            user_email=user.email,
            after={"password_changed": True, "sessions_revoked": revoked},
            ip=ip,
        )

    async def request_password_reset(self, *, email: str, ip: str | None = None) -> None:
        """Email a reset link, if that address belongs to a resettable account.

        Returns nothing in every case, and the endpoint says the same thing in
        every case. Whether an address is registered is exactly the fact an
        attacker wants from this endpoint, so the three outcomes - unknown
        address, SSO-only account, link sent - are indistinguishable from
        outside. `equalise_password_timing` burns the same CPU a real mint costs so the
        answer is not readable from response timing either, which is the same
        trick the login path uses.
        """
        # A second limit, keyed on the *address* rather than the caller's IP. The
        # gateway bucket cannot tell that one attacker rotating IPs is mailing the
        # same person over and over, and the mailbox is what suffers. Set for
        # unknown addresses too, so the throttle itself reveals nothing.
        cooldown_key = make_key("pwdreset", email.strip().lower())
        if await cache_get(cooldown_key, cache_name="auth") is not None:
            equalise_password_timing(email)
            logger.info("password_reset_throttled_for_address", ip=ip)
            return
        await cache_set(cooldown_key, True, ttl=_RESET_COOLDOWN_SECONDS, cache_name="auth")

        user = await self.users.get_by_email(email)

        if user is None or not user.is_active or user.password_hash is None:
            equalise_password_timing(email)
            logger.info(
                "password_reset_requested_no_action",
                # Deliberately not the address: this log line would otherwise be
                # the enumeration oracle the endpoint is careful not to be.
                reason=(
                    "unknown"
                    if user is None
                    else ("inactive" if not user.is_active else "sso_only")
                ),
                ip=ip,
            )
            return

        token = create_password_reset_token(user.id, password_hash=user.password_hash)
        settings = get_settings()
        link = f"{settings.frontend_base_url.rstrip('/')}/reset-password?token={token}"
        minutes = settings.security.password_reset_token_ttl_minutes

        await send_mail(
            to=user.email,
            subject="Reset your iRIS CLEAR password",
            text=(
                f"Hello {user.full_name or ''},\n\n"
                "We received a request to reset your iRIS CLEAR password.\n\n"
                f"Open this link to choose a new one:\n{link}\n\n"
                f"The link expires in {minutes} minutes and can be used once.\n\n"
                "If you did not ask for this, you can ignore this message - your "
                "password has not changed.\n"
            ),
            html=(
                f"<p>Hello {escape(user.full_name or '')},</p>"
                "<p>We received a request to reset your iRIS CLEAR password.</p>"
                f'<p><a href="{escape(link)}">Choose a new password</a></p>'
                f"<p>The link expires in {minutes} minutes and can be used once.</p>"
                "<p>If you did not ask for this, you can ignore this message — "
                "your password has not changed.</p>"
            ),
            fallback_log_body=link,
        )

        await self.audit.record(
            action=AuditAction.UPDATE,
            entity_type="user",
            entity_id=user.id,
            entity_label=user.email,
            user_id=user.id,
            user_email=user.email,
            after={"password_reset_requested": True},
            ip=ip,
        )

    async def reset_password(
        self,
        *,
        token: str,
        new_password: str,
        ip: str | None = None,
    ) -> None:
        """Redeem a reset link and set a new password."""
        payload = decode_token(token, expected_type="password_reset")
        try:
            user_id = uuid.UUID(str(payload.get("sub")))
        except (TypeError, ValueError):
            raise TokenInvalidError("This password reset link is not valid.") from None

        user = await self.users.get(user_id)
        # One message for every way this can fail. A distinct "no such account"
        # would hand back the account existence the request step refused to give.
        if user is None or not user.is_active or user.password_hash is None:
            raise TokenInvalidError("This password reset link is not valid.")

        # Re-checks the token against the *current* password, so redeeming it
        # here invalidates it - as does any other change to the password.
        verify_password_reset_token(token, password_hash=user.password_hash)

        validate_password_strength(new_password)
        await self.users.set_password(user, hash_password(new_password), must_change=False)

        # Whoever prompted the reset may already hold a session.
        revoked = await self.tokens.revoke_all_for_user(user.id)
        logger.info("password_reset_completed", user_id=str(user.id), sessions_revoked=revoked)

        await self.audit.record(
            action=AuditAction.UPDATE,
            entity_type="user",
            entity_id=user.id,
            entity_label=user.email,
            user_id=user.id,
            user_email=user.email,
            after={"password_reset": True, "sessions_revoked": revoked},
            ip=ip,
        )

    # =========================================================================
    # Session assembly
    # =========================================================================
    async def _issue_session(
        self,
        user: User,
        *,
        remember_me: bool = False,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> TokenResponse:
        access_token, access_expires = create_access_token(
            user.id,
            is_system_admin=user.is_system_admin,
            email=user.email,
            auth_provider=str(user.auth_provider),
        )
        refresh_value, refresh_hash, refresh_expires = create_refresh_token(user.id)

        if remember_me:
            # Trusted device: double the refresh window, capped by policy.
            from datetime import timedelta

            refresh_expires = min(
                refresh_expires + timedelta(days=self.settings.security.refresh_token_expire_days),
                datetime.now(UTC) + timedelta(days=90),
            )

        await self.tokens.issue(
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=refresh_expires,
            user_agent=user_agent,
            ip_address=ip,
        )

        return TokenResponse(
            access_token=access_token,
            expires_in=self.settings.security.access_token_expire_minutes * 60,
            expires_at=access_expires,
            refresh_token=refresh_value,
            user=await self.build_current_user(user),
        )

    async def build_current_user(self, user: User) -> CurrentUser:
        """Assemble the session payload, including resolved project permissions.

        Memberships are read from the database on every call rather than cached in
        the token: that is what makes a revoked membership take effect immediately.
        """
        memberships: list[ProjectMembershipInfo] = []

        for member in await self.members.list_for_user(user.id):
            project = member.project
            if project is None or project.deleted_at is not None:
                continue
            role = member.role
            permissions = list(role.permissions or [])
            if member.permission_overrides:
                # Overrides narrow a role for one member; they never widen it.
                permissions = [p for p in permissions if p in set(member.permission_overrides)]
            memberships.append(
                ProjectMembershipInfo(
                    project_id=project.id,
                    project_name=project.name,
                    project_slug=project.slug,
                    role=str(role.name),
                    role_display_name=role.display_name,
                    permissions=permissions,
                    is_favourite=member.is_favourite,
                )
            )

        return CurrentUser(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            is_active=user.is_active,
            is_system_admin=user.is_system_admin,
            must_change_password=user.must_change_password,
            auth_provider=str(user.auth_provider),
            job_title=user.job_title,
            department=user.department,
            avatar_url=user.avatar_url,
            locale=user.locale,
            timezone=user.timezone,
            preferences=user.preferences or {},
            last_login_at=user.last_login_at,
            memberships=memberships,
        )

    def auth_methods(self) -> AuthMethodsResponse:
        """Which sign-in methods the Login screen should offer."""
        return AuthMethodsResponse(
            password_enabled=True,
            microsoft_sso_enabled=self.settings.oidc.is_configured,
            self_signup_enabled=False,
        )

    async def default_role_id(self, role: RoleName) -> uuid.UUID:
        record = await self.roles.get_by_name(role)
        if record is None:
            raise ValidationError(f"Role '{role}' is not configured. Run the seed step.")
        return record.id


__all__ = ["AuthService"]

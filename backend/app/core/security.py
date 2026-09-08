"""Password hashing, JWT issuance/verification and token utilities.

Design notes
------------
* **Hashing** uses argon2id by default (bcrypt is supported for migration).
  ``verify_password`` reports when a hash needs upgrading so the login path can
  transparently re-hash with current parameters.
* **Access tokens** are short-lived JWTs carrying only identity plus the
  ``is_system_admin`` flag. Project membership and permissions are *never* baked
  into the token - they are resolved per request against the database, so a
  revoked membership takes effect immediately rather than at token expiry.
* **Refresh tokens** are opaque random strings. Only a SHA-256 digest is stored,
  and rotation revokes the previous digest, so a stolen refresh token is usable
  at most once and detectably.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

from app.core.config import get_settings
from app.core.errors import TokenExpiredError, TokenInvalidError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

TokenType = Literal["access", "refresh", "oidc_state", "download"]

_ARGON2_PREFIX = "$argon2"
_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")

# argon2 parameters: OWASP-recommended baseline, ~50ms on a modern core.
_ARGON2_TIME_COST = 3
_ARGON2_MEMORY_COST = 65536  # 64 MiB
_ARGON2_PARALLELISM = 4


# =============================================================================
# Password hashing
# =============================================================================
def _argon2_hasher() -> Any:
    from argon2 import PasswordHasher

    return PasswordHasher(
        time_cost=_ARGON2_TIME_COST,
        memory_cost=_ARGON2_MEMORY_COST,
        parallelism=_ARGON2_PARALLELISM,
    )


def hash_password(password: str) -> str:
    """Hash a plaintext password using the configured scheme."""
    settings = get_settings()
    if not password:
        raise ValidationError("Password must not be empty.")

    if settings.security.password_hash_scheme == "bcrypt":  # noqa: S105 - scheme name
        import bcrypt

        # bcrypt silently truncates at 72 bytes; pre-hash so long passwords keep
        # their full entropy.
        material = hashlib.sha256(password.encode()).digest()
        return bcrypt.hashpw(material, bcrypt.gensalt(rounds=12)).decode()

    return str(_argon2_hasher().hash(password))


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify a password against a stored hash.

    Always performs work even when the hash is missing, so a request for an
    unknown email takes the same time as one for a known email.
    """
    if not password_hash:
        # Dummy verify to equalise timing against the enumeration oracle.
        _dummy_verify(password)
        return False

    try:
        if password_hash.startswith(_ARGON2_PREFIX):
            from argon2.exceptions import VerificationError, VerifyMismatchError

            try:
                return bool(_argon2_hasher().verify(password_hash, password))
            except (VerifyMismatchError, VerificationError):
                return False

        if password_hash.startswith(_BCRYPT_PREFIXES):
            import bcrypt

            material = hashlib.sha256(password.encode()).digest()
            return bcrypt.checkpw(material, password_hash.encode())
    except Exception:  # noqa: BLE001 - a malformed hash must not 500 the login
        logger.warning("password_hash_verification_error", scheme=password_hash[:7])
        return False

    logger.warning("unknown_password_hash_scheme", prefix=password_hash[:7])
    return False


def _dummy_verify(password: str) -> None:
    """Burn comparable CPU so timing does not reveal account existence."""
    # The verify is *expected* to raise - the point is to spend the same CPU as a
    # real verification so a missing user and a wrong password take equal time.
    with contextlib.suppress(Exception):
        hasher = _argon2_hasher()
        hasher.verify(
            "$argon2id$v=19$m=65536,t=3,p=4$"
            "c2FsdHNhbHRzYWx0c2FsdA$B2s7v+3W0i0V0P1yqSVvXqjBpjXhCT8Rf5xJp1nZ9tE",
            password,
        )


def needs_rehash(password_hash: str) -> bool:
    """True when a stored hash uses outdated parameters or a legacy scheme."""
    settings = get_settings()
    if settings.security.password_hash_scheme == "argon2":  # noqa: S105 - scheme name
        if not password_hash.startswith(_ARGON2_PREFIX):
            return True
        try:
            return bool(_argon2_hasher().check_needs_rehash(password_hash))
        except Exception:  # noqa: BLE001
            return True
    return not password_hash.startswith(_BCRYPT_PREFIXES)


def validate_password_strength(password: str) -> None:
    """Enforce the password policy. Raises :class:`ValidationError`."""
    settings = get_settings()
    minimum = settings.security.password_min_length
    problems: list[str] = []

    if len(password) < minimum:
        problems.append(f"at least {minimum} characters")
    if not any(c.islower() for c in password):
        problems.append("a lowercase letter")
    if not any(c.isupper() for c in password):
        problems.append("an uppercase letter")
    if not any(c.isdigit() for c in password):
        problems.append("a digit")
    if password.isalnum():
        problems.append("a special character")

    if problems:
        raise ValidationError(
            "Password must contain " + ", ".join(problems) + ".",
            details={"requirements": problems},
        )


# =============================================================================
# JWT
# =============================================================================
def _now() -> datetime:
    return datetime.now(UTC)


def _encode(payload: dict[str, Any]) -> str:
    settings = get_settings()
    return jwt.encode(
        payload,
        settings.security.jwt_secret,
        algorithm=settings.security.jwt_algorithm,
    )


def create_access_token(
    subject: uuid.UUID | str,
    *,
    is_system_admin: bool = False,
    email: str | None = None,
    auth_provider: str = "local",
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, datetime]:
    """Issue an access token. Returns ``(token, expires_at)``.

    Deliberately excludes project memberships and permissions - see the module
    docstring.
    """
    settings = get_settings()
    issued_at = _now()
    expires_at = issued_at + (
        expires_delta or timedelta(minutes=settings.security.access_token_expire_minutes)
    )

    payload: dict[str, Any] = {
        "sub": str(subject),
        "type": "access",
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_urlsafe(16),
        "iss": settings.app_name,
        "adm": is_system_admin,
        "ap": auth_provider,
    }
    if email:
        payload["email"] = email
    if extra_claims:
        payload.update(extra_claims)

    return _encode(payload), expires_at


def create_refresh_token(subject: uuid.UUID | str) -> tuple[str, str, datetime]:
    """Mint an opaque refresh token.

    Returns ``(token, token_hash, expires_at)``. Persist only ``token_hash``.
    """
    settings = get_settings()
    token = secrets.token_urlsafe(48)
    expires_at = _now() + timedelta(days=settings.security.refresh_token_expire_days)
    return token, hash_token(token), expires_at


def hash_token(token: str) -> str:
    """SHA-256 digest used to store refresh tokens at rest."""
    return hashlib.sha256(token.encode()).hexdigest()


def decode_token(token: str, *, expected_type: TokenType = "access") -> dict[str, Any]:
    """Decode and validate a JWT.

    Raises :class:`TokenExpiredError` or :class:`TokenInvalidError`; callers can
    map those straight onto 401 responses.
    """
    settings = get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.security.jwt_secret,
            algorithms=[settings.security.jwt_algorithm],
            options={"require": ["exp", "iat", "sub"]},
        )
    except ExpiredSignatureError as exc:
        raise TokenExpiredError() from exc
    except InvalidTokenError as exc:
        logger.info("jwt_decode_failed", reason=type(exc).__name__)
        raise TokenInvalidError() from exc

    token_type = payload.get("type")
    if token_type != expected_type:
        logger.info("jwt_wrong_type", expected=expected_type, actual=token_type)
        raise TokenInvalidError(f"Expected a {expected_type} token.")

    return payload


def create_signed_state(data: dict[str, Any], ttl_seconds: int = 600) -> str:
    """Short-lived signed blob - used for the OIDC ``state`` parameter (CSRF).

    Two details here were wrong and are worth naming, because both failed
    silently in a way that made Microsoft sign-in impossible to complete:

    * **``sub`` is required.** :func:`decode_token` demands ``exp``, ``iat`` and
      ``sub`` on every token it validates, and this payload had no ``sub`` - so
      ``verify_signed_state`` raised on *every* state it had itself produced, and
      the callback always reported "expired or tampered with". A random state id
      satisfies it and is the honest subject: the thing this token identifies is
      one sign-in attempt.
    * **Caller data is spread last.** It used to be spread first and then have a
      freshly generated ``nonce`` written over the top, which discarded any nonce
      the caller passed in. The replay check compares the id token's ``nonce``
      claim against the one in the state; against a value that was never sent,
      that check can only ever fail.
    """
    issued_at = _now()
    payload = {
        "type": "oidc_state",
        "sub": secrets.token_urlsafe(12),
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + timedelta(seconds=ttl_seconds)).timestamp()),
        # Last, so a caller-supplied nonce survives rather than being overwritten.
        **data,
    }
    return _encode(payload)


def verify_signed_state(token: str) -> dict[str, Any]:
    return decode_token(token, expected_type="oidc_state")


def create_download_token(
    subject: uuid.UUID | str,
    *,
    resource: str,
    resource_id: uuid.UUID | str,
    ttl_seconds: int = 300,
) -> str:
    """Mint a short-lived credential that travels *in a URL*.

    This exists because a download is a browser navigation, not an XHR: the tab is
    sent to the URL and carries no ``Authorization`` header, so a bearer token
    cannot authenticate it. Object storage solves this with a presigned URL; on the
    local filesystem adapter there is nothing to presign, and the API has to issue
    the equivalent itself.

    Bound to three things, so a leaked URL is worth as little as possible: the user
    it was minted for, the exact object it addresses, and five minutes. The route
    checks all three - a token for one export cannot fetch another, and one user's
    token cannot be replayed by someone else.

    ``type`` is ``download`` rather than reusing ``oidc_state``: distinct types are
    what stop a token minted for one purpose being accepted for the other, and
    :func:`decode_token` enforces it.
    """
    issued_at = _now()
    payload = {
        "sub": str(subject),
        "type": "download",
        "res": resource,
        "rid": str(resource_id),
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + timedelta(seconds=ttl_seconds)).timestamp()),
        "jti": secrets.token_urlsafe(12),
    }
    return _encode(payload)


def verify_download_token(
    token: str,
    *,
    resource: str,
    resource_id: uuid.UUID | str,
) -> dict[str, Any]:
    """Validate a download token and confirm it addresses this exact object.

    Raises :class:`TokenInvalidError` on a mismatch, which is deliberately the same
    error a forged token produces: the caller maps both onto the same 404, so a
    token for someone else's export cannot be used to prove that export exists.
    """
    payload = decode_token(token, expected_type="download")
    if payload.get("res") != resource or payload.get("rid") != str(resource_id):
        logger.info(
            "download_token_resource_mismatch",
            expected=f"{resource}:{resource_id}",
            actual=f"{payload.get('res')}:{payload.get('rid')}",
        )
        raise TokenInvalidError("This download link is not valid for this item.")
    return payload


# =============================================================================
# PKCE (RFC 7636)
# =============================================================================
#: Length of the PKCE verifier in random bytes. RFC 7636 requires the encoded
#: form to be 43-128 characters; 32 bytes of entropy encodes to 43.
_PKCE_VERIFIER_BYTES = 32


def create_pkce_verifier() -> str:
    """A high-entropy secret that proves the code exchange came from us.

    PKCE is what lets a **public** client - a browser app with no secret it can
    keep - exchange an authorization code safely. The verifier is generated
    before the redirect, only its SHA-256 hash travels to the identity provider,
    and the original is presented at the exchange. Anyone who intercepts the code
    cannot use it without the verifier they never saw.

    It is required here even though the redirect lands on our own backend, where
    code interception is already unlikely: it is one line, Microsoft recommends
    it for every flow, and it removes the client secret from the list of things
    this deployment has to hold.
    """
    return secrets.token_urlsafe(_PKCE_VERIFIER_BYTES)


def pkce_challenge(verifier: str) -> str:
    """The S256 challenge for a verifier.

    S256 rather than ``plain``: with ``plain`` the challenge *is* the verifier,
    so anyone who sees the authorization request can complete the exchange, and
    PKCE stops protecting anything.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def create_nonce() -> str:
    """Replay guard bound into the id token.

    The value is sent on the authorization request and Entra echoes it into the
    ``nonce`` claim of the token it issues. Comparing the two is what stops a
    token minted for one sign-in attempt being replayed into another.
    """
    return secrets.token_urlsafe(16)


# =============================================================================
# Misc
# =============================================================================
def verify_internal_token(presented: str | None) -> bool:
    """Constant-time check of the queue shim's shared secret."""
    if not presented:
        return False
    expected = get_settings().security.internal_api_token
    return hmac.compare_digest(presented.encode(), expected.encode())


def sha256_bytes(data: bytes) -> str:
    """Content hash used for duplicate detection and artifact checksums."""
    return hashlib.sha256(data).hexdigest()


def generate_api_key() -> str:
    return f"cip_{secrets.token_urlsafe(32)}"


__all__ = [
    "create_access_token",
    "create_nonce",
    "create_pkce_verifier",
    "create_refresh_token",
    "create_signed_state",
    "decode_token",
    "generate_api_key",
    "hash_password",
    "hash_token",
    "needs_rehash",
    "pkce_challenge",
    "sha256_bytes",
    "validate_password_strength",
    "verify_internal_token",
    "verify_password",
    "verify_signed_state",
]

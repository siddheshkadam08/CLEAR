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

TokenType = Literal["access", "refresh", "oidc_state", "password_reset"]

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
    """Short-lived signed blob - used for the OIDC ``state`` parameter (CSRF)."""
    issued_at = _now()
    payload = {
        **data,
        "type": "oidc_state",
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + timedelta(seconds=ttl_seconds)).timestamp()),
        "nonce": secrets.token_urlsafe(12),
    }
    return _encode(payload)


def verify_signed_state(token: str) -> dict[str, Any]:
    return decode_token(token, expected_type="oidc_state")


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
    "create_refresh_token",
    "create_signed_state",
    "decode_token",
    "generate_api_key",
    "hash_password",
    "hash_token",
    "needs_rehash",
    "sha256_bytes",
    "validate_password_strength",
    "verify_internal_token",
    "verify_password",
    "verify_signed_state",
]

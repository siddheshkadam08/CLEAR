"""Password-reset tokens.

The reset link is a bearer credential that travels through email and sits in a
mailbox afterwards, so the properties worth pinning are the ones that limit what
a copy of it is worth: it expires, it is bound to one account, it cannot be
presented as any other kind of token, and it stops working the moment the
password it was minted against changes.

That last one is what makes the link single-use without a revocation table, and
it is the only one of the four that is not obvious from reading the mint
function - hence the emphasis here.

Pure unit tests: the suite has no HTTP client or database fixture, and none of
this needs one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import TokenExpiredError, TokenInvalidError
from app.core.security import (
    create_access_token,
    create_password_reset_token,
    decode_token,
    hash_password,
    password_reset_fingerprint,
    verify_password_reset_token,
)

HASH_A = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0c2FsdA$aaaaaaaaaaaaaaaaaaaaaaaaaaa"
HASH_B = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0c2FsdA$bbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _env(settings_env: None) -> None:
    """Every test needs the JWT secret the shared fixture installs."""


class TestRoundTrip:
    def test_a_fresh_token_verifies(self) -> None:
        user_id = uuid.uuid4()
        token = create_password_reset_token(user_id, password_hash=HASH_A)

        payload = verify_password_reset_token(token, password_hash=HASH_A)

        assert payload["sub"] == str(user_id)
        assert payload["type"] == "password_reset"

    def test_the_subject_claim_is_present(self) -> None:
        """`decode_token` rejects a token with no `sub`, which once broke SSO."""
        token = create_password_reset_token(uuid.uuid4(), password_hash=HASH_A)
        assert decode_token(token, expected_type="password_reset")["sub"]

    def test_each_token_is_unique(self) -> None:
        """A replayed jti would let two links share a fate."""
        first = create_password_reset_token(uuid.uuid4(), password_hash=HASH_A)
        second = create_password_reset_token(uuid.uuid4(), password_hash=HASH_A)
        assert first != second


class TestSingleUse:
    """Redemption invalidates the link, because the password it names has changed."""

    def test_a_token_stops_working_once_the_password_changes(self) -> None:
        token = create_password_reset_token(uuid.uuid4(), password_hash=HASH_A)

        with pytest.raises(TokenInvalidError):
            verify_password_reset_token(token, password_hash=HASH_B)

    def test_a_real_password_change_invalidates_it(self) -> None:
        """Same property, against hashes the application would actually store."""
        before = hash_password("Str0ng!Passw0rd")
        after = hash_password("An0ther!Passw0rd")
        token = create_password_reset_token(uuid.uuid4(), password_hash=before)

        assert verify_password_reset_token(token, password_hash=before)
        with pytest.raises(TokenInvalidError):
            verify_password_reset_token(token, password_hash=after)

    def test_two_outstanding_links_are_both_killed_by_one_redemption(self) -> None:
        """Requesting twice must not leave the older link live afterwards."""
        user_id = uuid.uuid4()
        older = create_password_reset_token(user_id, password_hash=HASH_A)
        newer = create_password_reset_token(user_id, password_hash=HASH_A)

        for token in (older, newer):
            with pytest.raises(TokenInvalidError):
                verify_password_reset_token(token, password_hash=HASH_B)

    def test_an_account_with_no_password_does_not_match_one_that_has_one(self) -> None:
        token = create_password_reset_token(uuid.uuid4(), password_hash=HASH_A)
        with pytest.raises(TokenInvalidError):
            verify_password_reset_token(token, password_hash=None)


class TestRejection:
    def test_an_expired_token_is_refused(self) -> None:
        token = create_password_reset_token(uuid.uuid4(), password_hash=HASH_A, ttl_seconds=-1)
        with pytest.raises(TokenExpiredError):
            verify_password_reset_token(token, password_hash=HASH_A)

    def test_a_tampered_token_is_refused(self) -> None:
        token = create_password_reset_token(uuid.uuid4(), password_hash=HASH_A)
        # Flip a character in the signature.
        head, _, signature = token.rpartition(".")
        forged = f"{head}.{'x' if signature[0] != 'x' else 'y'}{signature[1:]}"

        with pytest.raises(TokenInvalidError):
            verify_password_reset_token(forged, password_hash=HASH_A)

    def test_an_access_token_cannot_be_redeemed_as_a_reset(self) -> None:
        """The `type` claim is what keeps the two credentials apart."""
        access, _ = create_access_token(uuid.uuid4())
        with pytest.raises(TokenInvalidError):
            verify_password_reset_token(access, password_hash=HASH_A)

    def test_a_reset_token_is_not_an_access_token(self) -> None:
        token = create_password_reset_token(uuid.uuid4(), password_hash=HASH_A)
        with pytest.raises(TokenInvalidError):
            decode_token(token, expected_type="access")


class TestFingerprint:
    def test_it_does_not_leak_the_hash(self) -> None:
        """The token rides in an email; the stored hash must not ride with it."""
        fingerprint = password_reset_fingerprint(HASH_A)
        assert fingerprint not in HASH_A
        assert HASH_A not in fingerprint
        assert len(fingerprint) == 16

    def test_it_is_stable_and_distinct(self) -> None:
        assert password_reset_fingerprint(HASH_A) == password_reset_fingerprint(HASH_A)
        assert password_reset_fingerprint(HASH_A) != password_reset_fingerprint(HASH_B)

    def test_none_is_handled(self) -> None:
        """SSO-only accounts have no hash; this must not raise on the way past."""
        assert password_reset_fingerprint(None) == password_reset_fingerprint("")


class TestExpiry:
    def test_the_configured_ttl_is_applied(self) -> None:
        from app.core.config import get_settings

        minutes = get_settings().security.password_reset_token_ttl_minutes
        token = create_password_reset_token(uuid.uuid4(), password_hash=HASH_A)
        payload = decode_token(token, expected_type="password_reset")

        expected = datetime.now(UTC) + timedelta(minutes=minutes)
        actual = datetime.fromtimestamp(payload["exp"], tz=UTC)
        assert abs((actual - expected).total_seconds()) < 30

"""Microsoft SSO: the authorization request, and what must never be trusted.

The tests that matter most here are the negative ones. A sign-in flow that works
is easy to confirm by using it; a sign-in flow that *cannot be tricked* is not,
and every check below corresponds to a specific way an attacker gets in.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from app.core.security import (
    create_nonce,
    create_pkce_verifier,
    pkce_challenge,
    verify_signed_state,
)
from app.services.auth import INTERACTION_REQUIRED_ERRORS, AuthService

#: The real values, so a test failure reads against what is actually deployed.
#: Tenant is the *directory* id and becomes the authority path; client is the
#: *application* id and becomes the `client_id` parameter.
TENANT = "06e84b96-907a-4418-ae29-211bfd190e84"
CLIENT = "f72edf57-01e0-4138-aca3-de022cfc0ca2"


@pytest.fixture
def sso(settings_env):
    """A configured public-client registration - no secret, PKCE only."""
    return settings_env(
        OIDC_ENABLED="true",
        AZURE_AD_TENANT_ID=TENANT,
        AZURE_AD_CLIENT_ID=CLIENT,
        AZURE_AD_CLIENT_SECRET="",
        OIDC_REDIRECT_URI="http://localhost:8000/api/v1/auth/oidc/callback",
    )


def _service() -> AuthService:
    # `authorize_url` touches no database; the session is never used.
    return AuthService(db=None)  # type: ignore[arg-type]


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


# =============================================================================
# Configuration
# =============================================================================
class TestConfiguration:
    def test_a_secret_is_not_required(self, sso) -> None:
        """A SPA registration has no secret it can keep. Demanding one would make
        the normal browser-app setup look unconfigured."""
        assert sso.oidc.is_configured is True
        assert sso.oidc.is_confidential_client is False

    def test_a_secret_is_used_when_present(self, settings_env) -> None:
        configured = settings_env(
            OIDC_ENABLED="true",
            AZURE_AD_TENANT_ID=TENANT,
            AZURE_AD_CLIENT_ID=CLIENT,
            AZURE_AD_CLIENT_SECRET="s3cret",
        )
        assert configured.oidc.is_confidential_client is True

    def test_a_missing_tenant_is_not_configured(self, settings_env) -> None:
        """Without a tenant the authority is `common`, which accepts any Microsoft
        account on earth. That must fail at configuration, not at the first
        outsider's login."""
        loose = settings_env(
            OIDC_ENABLED="true", AZURE_AD_TENANT_ID="", AZURE_AD_CLIENT_ID=CLIENT
        )
        assert loose.oidc.is_configured is False

    def test_disabled_is_not_configured(self, settings_env) -> None:
        off = settings_env(
            OIDC_ENABLED="false", AZURE_AD_TENANT_ID=TENANT, AZURE_AD_CLIENT_ID=CLIENT
        )
        assert off.oidc.is_configured is False

    def test_the_authority_pins_the_tenant(self, sso) -> None:
        assert sso.oidc.authority == f"https://login.microsoftonline.com/{TENANT}"

    def test_allowed_domains_are_normalised(self, settings_env) -> None:
        scoped = settings_env(
            OIDC_ENABLED="true",
            AZURE_AD_TENANT_ID=TENANT,
            AZURE_AD_CLIENT_ID=CLIENT,
            OIDC_ALLOWED_EMAIL_DOMAINS=" @Example.com , irisregtech.com ",
        )
        assert scoped.oidc.allowed_email_domains == ["example.com", "irisregtech.com"]


# =============================================================================
# The authorization request
# =============================================================================
class TestAuthorizeUrl:
    def test_it_targets_the_tenant_authorize_endpoint(self, sso) -> None:
        url = _service().authorize_url().authorization_url

        assert url.startswith(
            f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?"
        )

    def test_it_requests_a_code_not_an_id_token(self, sso) -> None:
        """Implicit flow puts a token in the address bar, where the page can read
        it and history keeps it. The code flow puts nothing usable in the browser."""
        params = _query(_service().authorize_url().authorization_url)

        assert params["response_type"] == "code"
        assert params["response_mode"] == "query"

    def test_it_carries_a_redirect_uri(self, sso) -> None:
        """Entra rejects the request outright without one - AADSTS900971."""
        params = _query(_service().authorize_url().authorization_url)

        assert params["redirect_uri"] == "http://localhost:8000/api/v1/auth/oidc/callback"

    def test_it_uses_pkce_with_s256(self, sso) -> None:
        result = _service().authorize_url()
        params = _query(result.authorization_url)

        assert params["code_challenge_method"] == "S256"
        # The challenge, not the verifier, is what travels.
        assert params["code_challenge"] == pkce_challenge(result.code_verifier)
        assert params["code_challenge"] != result.code_verifier

    def test_the_verifier_is_never_serialised_to_the_client(self, sso) -> None:
        """It goes in an HttpOnly cookie. A verifier the page can read protects
        nothing."""
        result = _service().authorize_url()

        assert result.code_verifier
        assert "code_verifier" not in result.model_dump()

    def test_the_state_is_signed_and_carries_the_nonce(self, sso) -> None:
        result = _service().authorize_url(redirect_after="/contracts")
        params = _query(result.authorization_url)

        claims = verify_signed_state(result.state)
        assert claims["redirect_after"] == "/contracts"
        # The nonce in the state must match the one sent, or the callback's replay
        # check compares against the wrong value and always fails.
        assert claims["nonce"] == params["nonce"]

    def test_a_tampered_state_is_rejected(self, sso) -> None:
        """The state is what stops a forged callback: an attacker who can choose
        `redirect_after` can send a freshly authenticated user anywhere."""
        result = _service().authorize_url()
        forged = result.state[:-4] + "AAAA"

        with pytest.raises(Exception):  # noqa: B017 - any rejection is correct
            verify_signed_state(forged)

    def test_each_request_is_unique(self, sso) -> None:
        """Reused nonces and verifiers make replay possible across attempts."""
        first, second = _service().authorize_url(), _service().authorize_url()

        assert first.code_verifier != second.code_verifier
        assert _query(first.authorization_url)["nonce"] != _query(second.authorization_url)["nonce"]

    def test_it_is_silent_by_default(self, sso) -> None:
        """`prompt=none` is what makes the fallback meaningful. Omitting prompt
        would still render a picker whenever several sessions exist, so the retry
        would never fire and the user would see a page flash."""
        result = _service().authorize_url()

        assert result.prompt == "none"
        assert _query(result.authorization_url)["prompt"] == "none"

    def test_the_fallback_asks_for_the_account_picker(self, sso) -> None:
        result = _service().authorize_url(prompt="select_account")

        assert _query(result.authorization_url)["prompt"] == "select_account"

    def test_silent_first_can_be_turned_off(self, settings_env) -> None:
        settings_env(
            OIDC_ENABLED="true",
            AZURE_AD_TENANT_ID=TENANT,
            AZURE_AD_CLIENT_ID=CLIENT,
            OIDC_SILENT_FIRST="false",
        )
        result = _service().authorize_url()

        assert result.prompt is None
        assert "prompt" not in _query(result.authorization_url)

    def test_the_prompt_is_recorded_in_the_state(self, sso) -> None:
        """The callback reads it to decide whether a failure is retryable. Without
        it a picker that fails would bounce the user round the loop again."""
        assert verify_signed_state(_service().authorize_url().state)["prompt"] == "none"

    def test_an_unconfigured_deployment_refuses(self, settings_env) -> None:
        from app.core.errors import NotImplementedFeatureError

        settings_env(OIDC_ENABLED="false")
        with pytest.raises(NotImplementedFeatureError):
            _service().authorize_url()


# =============================================================================
# Domain restriction
# =============================================================================
class TestDomainRestriction:
    def test_an_allowed_domain_passes(self, settings_env) -> None:
        settings_env(
            OIDC_ENABLED="true",
            AZURE_AD_TENANT_ID=TENANT,
            AZURE_AD_CLIENT_ID=CLIENT,
            OIDC_ALLOWED_EMAIL_DOMAINS="irisregtech.com",
        )
        _service()._require_allowed_domain("priya@irisregtech.com")

    def test_an_outside_domain_is_refused(self, settings_env) -> None:
        from app.core.errors import ForbiddenError

        settings_env(
            OIDC_ENABLED="true",
            AZURE_AD_TENANT_ID=TENANT,
            AZURE_AD_CLIENT_ID=CLIENT,
            OIDC_ALLOWED_EMAIL_DOMAINS="irisregtech.com",
        )
        with pytest.raises(ForbiddenError):
            _service()._require_allowed_domain("attacker@gmail.com")

    def test_an_empty_list_permits_everything(self, sso) -> None:
        """Correct with a pinned tenant: Entra already refused everyone else."""
        _service()._require_allowed_domain("anyone@anywhere.com")


# =============================================================================
# Token verification
# =============================================================================
class TestIdTokenVerification:
    def test_an_unsigned_token_is_rejected(self, sso) -> None:
        """The bug this replaces: claims were read with `verify_signature: False`
        when no tenant was pinned. An unverified token is a set of
        attacker-controlled claims, and the identity built from them is whoever
        the attacker named."""
        import jwt

        from app.core.errors import TokenInvalidError

        forged = jwt.encode(
            {"oid": "attacker", "email": "ceo@irisregtech.com", "aud": CLIENT},
            key="",
            algorithm="none",
        )

        with pytest.raises(TokenInvalidError):
            _service()._decode_id_token(forged)

    def test_a_token_signed_with_the_wrong_key_is_rejected(self, sso) -> None:
        import jwt

        from app.core.errors import TokenInvalidError

        forged = jwt.encode({"oid": "x", "email": "x@y.com"}, key="not-microsoft", algorithm="HS256")

        with pytest.raises(TokenInvalidError):
            _service()._decode_id_token(forged)

    def test_garbage_is_rejected_rather_than_crashing(self, sso) -> None:
        from app.core.errors import TokenInvalidError

        with pytest.raises(TokenInvalidError):
            _service()._decode_id_token("not-a-jwt")


# =============================================================================
# PKCE primitives
# =============================================================================
class TestPkce:
    def test_the_challenge_is_a_hash_not_the_verifier(self) -> None:
        verifier = create_pkce_verifier()

        assert pkce_challenge(verifier) != verifier

    def test_the_challenge_is_deterministic(self) -> None:
        verifier = create_pkce_verifier()

        assert pkce_challenge(verifier) == pkce_challenge(verifier)

    def test_the_challenge_is_unpadded_base64url(self) -> None:
        """RFC 7636 requires base64url with the padding stripped; Entra rejects
        a padded challenge."""
        challenge = pkce_challenge(create_pkce_verifier())

        assert "=" not in challenge
        assert "+" not in challenge and "/" not in challenge

    def test_the_verifier_meets_the_length_requirement(self) -> None:
        verifier = create_pkce_verifier()

        assert 43 <= len(verifier) <= 128

    def test_verifiers_and_nonces_are_unpredictable(self) -> None:
        assert len({create_pkce_verifier() for _ in range(50)}) == 50
        assert len({create_nonce() for _ in range(50)}) == 50


# =============================================================================
# Fallback classification
# =============================================================================
class TestInteractionRequired:
    @pytest.mark.parametrize(
        "error",
        ["login_required", "interaction_required", "consent_required", "account_selection_required"],
    )
    def test_these_trigger_the_interactive_retry(self, error: str) -> None:
        assert error in INTERACTION_REQUIRED_ERRORS

    @pytest.mark.parametrize("error", ["access_denied", "invalid_client", "server_error"])
    def test_these_do_not(self, error: str) -> None:
        """Retrying a cancellation or a misconfiguration shows the user the same
        wall twice."""
        assert error not in INTERACTION_REQUIRED_ERRORS

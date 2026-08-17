"""A mock provider must never be something a deployment arrives at by accident.

Two halves of the same incident:

* the compose file re-declared ``EMBEDDING_PROVIDER`` on the backend service with
  a ``mock`` default, and a service-level value overrides the shared anchor - so
  the API ran on mock embeddings while the configuration plainly said Azure;
* nothing detected it. Mock vectors are produced successfully, the pipeline
  completes, and search simply returns nothing useful.

The first is guarded by reading the compose file; the second by
``Settings.accidental_mock_providers``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import Settings

COMPOSE = Path(__file__).resolve().parents[3] / "docker-compose.yml"


def _compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


# =============================================================================
# The compose file
# =============================================================================
@pytest.mark.skipif(not COMPOSE.is_file(), reason="compose file not in this checkout")
@pytest.mark.parametrize("variable", ["EMBEDDING_PROVIDER", "LLM_PROVIDER"])
def test_no_service_overrides_a_provider_with_a_mock_default(variable: str) -> None:
    """A service-level default beats the anchor, silently.

    This is the regression: ``EMBEDDING_PROVIDER: ${EMBEDDING_PROVIDER:-mock}``
    on the backend service while ``x-backend-env`` set the real provider.
    """
    compose = _compose()
    offenders = []

    for name, service in (compose.get("services") or {}).items():
        environment = service.get("environment")
        if not isinstance(environment, dict):
            continue
        value = str(environment.get(variable, ""))
        if "mock" in value:
            offenders.append(f"{name}: {variable}={value}")

    assert not offenders, (
        "a service-level environment value overrides the x-backend-env anchor, so "
        "these would silently run on mock: " + "; ".join(offenders)
    )


@pytest.mark.skipif(not COMPOSE.is_file(), reason="compose file not in this checkout")
def test_the_anchor_supplies_both_providers() -> None:
    """The shared anchor is where a provider is chosen, for every service."""
    compose = _compose()
    anchor = compose["services"]["backend"]["environment"]

    assert "mock" not in str(anchor.get("LLM_PROVIDER", ""))
    assert "mock" not in str(anchor.get("EMBEDDING_PROVIDER", ""))
    # Every worker resolves the same anchor, so a pool cannot diverge from the API.
    for name in ("worker-parser", "worker-ai"):
        service = compose["services"][name]["environment"]
        assert service.get("EMBEDDING_PROVIDER") == anchor.get("EMBEDDING_PROVIDER"), (
            f"{name} does not share the backend's embedding provider"
        )


# =============================================================================
# The runtime guard
# =============================================================================
def _settings(**overrides: Any) -> Settings:
    """Settings built from explicit values, ignoring the developer's .env."""
    settings = Settings()
    for group, values in overrides.items():
        target = getattr(settings, group)
        for key, value in values.items():
            object.__setattr__(target, key, value)
    return settings


def test_mock_embeddings_with_azure_credentials_present_is_reported() -> None:
    settings = _settings(
        embedding={"provider": "mock", "azure_api_key": "a-key"},
        llm={"provider": "azure_openai"},
    )

    conflicts = settings.accidental_mock_providers()

    assert len(conflicts) == 1
    assert "EMBEDDING_PROVIDER=mock" in conflicts[0]


def test_mock_inference_with_credentials_present_is_reported() -> None:
    settings = _settings(
        llm={"provider": "mock", "azure_openai_api_key": "a-key"},
        embedding={"provider": "azure_openai"},
    )

    conflicts = settings.accidental_mock_providers()

    assert len(conflicts) == 1
    assert "LLM_PROVIDER=mock" in conflicts[0]


def test_mock_with_no_credentials_at_all_is_left_alone() -> None:
    """Running without a vendor is a legitimate choice, not a misconfiguration."""
    settings = _settings(
        llm={"provider": "mock", "azure_openai_api_key": "", "anthropic_api_key": "",
             "openai_api_key": ""},
        embedding={"provider": "mock", "azure_api_key": "", "azure_endpoint": ""},
    )

    assert settings.accidental_mock_providers() == []


def test_a_correctly_configured_deployment_reports_nothing() -> None:
    settings = _settings(
        llm={"provider": "azure_openai", "azure_openai_api_key": "a-key"},
        embedding={"provider": "azure_openai", "azure_api_key": "a-key"},
    )

    assert settings.accidental_mock_providers() == []


def test_production_refuses_to_boot_on_an_accidental_mock() -> None:
    """ALLOW_MOCK_AI does not excuse it: the flag says "no vendor", the key says otherwise."""
    settings = _settings(
        llm={"provider": "azure_openai", "azure_openai_api_key": "a-key"},
        embedding={"provider": "mock", "azure_api_key": "a-key"},
    )
    object.__setattr__(settings, "app_env", "production")
    object.__setattr__(settings, "allow_mock_ai", True)
    object.__setattr__(settings, "debug", False)

    with pytest.raises(ValueError, match="EMBEDDING_PROVIDER=mock"):
        settings._guard_production()

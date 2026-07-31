"""Shared test fixtures.

Environment is set before any application module is imported: settings are cached
per process, so a module that imports ``get_settings`` at import time would
otherwise capture whatever the developer's shell happened to hold.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://cip:cip@localhost:5432/cip_test")
os.environ.setdefault("JWT_SECRET", "t" * 48)
os.environ.setdefault("INTERNAL_API_TOKEN", "t" * 48)
os.environ.setdefault("OTEL_ENABLED", "false")
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("EMBEDDING_PROVIDER", "mock")
os.environ.setdefault("EMBEDDING_VERIFY_ON_STARTUP", "false")


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Set embedding environment variables and clear the settings cache.

    Both directions matter: the cache is cleared before the test so the overrides
    take effect, and after it so a later test does not inherit them.
    """
    from app.core.config import get_settings

    def apply(**values: str) -> Any:
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        return get_settings()

    get_settings.cache_clear()
    yield apply
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def reset_embedding_provider() -> Iterator[None]:
    """Drop the cached provider singleton around every test."""
    from app.ai.embedding.providers import set_embedding_provider

    set_embedding_provider(None)
    yield
    set_embedding_provider(None)


@pytest.fixture(autouse=True)
def reset_alert_dispatcher_cache() -> Iterator[None]:
    """Drop the cached alert dispatcher around every test.

    The dispatcher is built once per process from settings, so without this a test
    that overrides ``ALERT_*`` would either inherit an earlier test's providers or
    leak its own into the next one.
    """
    from app.alerting import reset_alert_dispatcher

    reset_alert_dispatcher()
    yield
    reset_alert_dispatcher()

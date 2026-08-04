"""Shared test fixtures.

Environment is set before any application module is imported: settings are cached
per process, so a module that imports ``get_settings`` at import time would
otherwise capture whatever the developer's shell happened to hold.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# The settings groups read `.env` when it exists (see `config._group_config`).
# For the suite that would mean asserting against whatever the developer last
# configured locally - the embedding tests pin the shape the migrations target,
# and a local file running a different provider fails them for reasons unrelated
# to the change under test.
os.environ.setdefault("CIP_DISABLE_DOTENV", "1")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://cip:cip@localhost:5432/cip_test")
os.environ.setdefault("JWT_SECRET", "t" * 48)
os.environ.setdefault("INTERNAL_API_TOKEN", "t" * 48)
os.environ.setdefault("OTEL_ENABLED", "false")
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("EMBEDDING_PROVIDER", "mock")
os.environ.setdefault("EMBEDDING_VERIFY_ON_STARTUP", "false")


#: Where per-test temporary directories are rooted, when nothing overrides it.
#:
#: Short on purpose. pytest's default is
#: ``<tempdir>/pytest-of-<username>/pytest-<n>/<test-name><n>``, which on this
#: platform spends ~63 characters before a test has written anything - and a
#: storage key is another ~130. Windows' 260-character ceiling is then reached
#: partway through a suite that passes everywhere else.
_TMP_ROOT_NAME = "ct"


def pytest_configure(config: pytest.Config) -> None:
    """Root temporary directories somewhere short enough for Windows.

    Windows caps paths at 260 characters unless long-path support is enabled
    system-wide, which is a registry change and an administrator prompt - not
    something running the tests should require. The storage adapter now asks for
    the extended-length API so it is no longer the binding constraint (see
    ``app.storage.local._long_path_safe``), but any test that builds a path
    through some other library is still exposed, so the base is kept short too.
    Two independent defences, because this failure is expensive to diagnose: it
    surfaces as ``No such file or directory`` naming a directory that plainly
    exists.

    Precedence is deliberate. An explicit ``--basetemp`` always wins, because
    someone who passed it is debugging and wants their directory used. Otherwise
    ``TEST_TMP_DIR`` - the documented escape hatch for a machine where even this
    is too deep, or where the temp volume is unsuitable. Otherwise a short
    directory inside the OS temp folder.

    Not applied on POSIX, where the limit is per-component and ~4096 overall:
    pytest's numbered directories are useful for debugging a failure days later,
    and there is no reason to give them up.
    """
    if os.name != "nt" or config.option.basetemp:
        return

    override = os.environ.get("TEST_TMP_DIR", "").strip()
    base = Path(override) if override else Path(tempfile.gettempdir()) / _TMP_ROOT_NAME
    base.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(base)


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


@pytest.fixture
def cache_breaker_reset() -> Iterator[None]:
    """Clear the cache breaker for a test that exercises the cache path.

    Deliberately **not** autouse. There is no Redis in the unit suite, so the first
    cache call trips the breaker and every later one is skipped - which is both the
    correct production behaviour and what keeps the suite from paying a connect
    timeout per test. A test that wants the cache attempted asks for this fixture.
    """
    from app.core.cache import reset_cache_breaker

    reset_cache_breaker()
    yield
    reset_cache_breaker()


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

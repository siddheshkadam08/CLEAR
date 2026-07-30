"""Schema, health and startup-validation integration.

The DDL tests compile against the PostgreSQL dialect rather than executing, so they
run without a live database and still catch the failures that matter here: a column
type that does not match the configuration, and an HNSW index whose operator class
disagrees with its column. Tests needing a real server are marked ``integration``
and skipped unless one is configured.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy.schema import CreateIndex, CreateTable

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="Set TEST_DATABASE_URL to run tests against a live Postgres.",
)


def _embeddings_ddl() -> str:
    from sqlalchemy.dialects import postgresql

    from app.models.embedding import Embedding

    return str(CreateTable(Embedding.__table__).compile(dialect=postgresql.dialect()))


def _index_ddl() -> list[str]:
    from sqlalchemy.dialects import postgresql

    from app.models.embedding import Embedding

    return [
        str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        for index in Embedding.__table__.indexes
    ]


# =============================================================================
# Column
# =============================================================================
def test_embedding_column_is_halfvec_2048() -> None:
    """The shipped default has to match the model's real output width."""
    ddl = _embeddings_ddl().lower()
    assert "halfvec(2048)" in ddl


def test_embedding_column_is_not_a_plain_vector_at_2048() -> None:
    """`vector(2048)` stores fine and then cannot carry an HNSW index at all.

    Catching it here rather than in production is the whole point of the check: the
    symptom is a slow search, not an error.
    """
    ddl = _embeddings_ddl().lower()
    assert "vector(2048)" not in ddl.replace("halfvec(2048)", "")


# =============================================================================
# Indexes
# =============================================================================
def test_hnsw_indexes_exist_for_every_level() -> None:
    ddl = " ".join(_index_ddl()).lower()
    for level in ("document_summary", "clause", "chunk"):
        assert f"level = '{level}'" in ddl


def test_hnsw_operator_class_matches_the_column_type() -> None:
    """`vector_cosine_ops` on a halfvec column fails index creation outright."""
    ddl = " ".join(_index_ddl()).lower()
    assert "halfvec_cosine_ops" in ddl
    assert "vector_cosine_ops" not in ddl


def test_indexes_are_partial_per_level() -> None:
    """One graph per level: an L1 candidate search must not traverse L3 chunks."""
    hnsw = [ddl for ddl in _index_ddl() if "hnsw" in ddl.lower()]
    assert len(hnsw) == 3
    assert all("WHERE" in ddl for ddl in hnsw)


# =============================================================================
# Configuration coherence
# =============================================================================
def test_column_width_tracks_the_configured_dimension(settings_env: Any) -> None:
    """The column is generated from settings, so the two can never drift."""
    from app.db.types import vector_column

    settings_env(EMBEDDING_DIM="1024", EMBEDDING_STORAGE="halfvec")
    column = vector_column()
    assert getattr(column, "dim", None) == 1024


def test_storage_setting_selects_the_operator_class(settings_env: Any) -> None:
    from app.db.types import vector_ops

    settings_env(EMBEDDING_STORAGE="halfvec")
    assert vector_ops() == "halfvec_cosine_ops"
    settings_env(EMBEDDING_STORAGE="vector")
    assert vector_ops() == "vector_cosine_ops"


# =============================================================================
# Migration
# =============================================================================
def test_migration_targets_the_configured_shape() -> None:
    """The migration hardcodes its target on purpose; it must match the defaults.

    A migration that read live settings would produce a different schema per
    environment while reporting the same revision.
    """
    import importlib.util
    from pathlib import Path

    from app.core.config import get_settings

    path = next(Path(__file__).resolve().parents[2].glob("migrations/versions/*0002*nvidia*.py"))
    spec = importlib.util.spec_from_file_location("migration_0002", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    settings = get_settings().embedding
    assert settings.dim == module.NEW_DIM
    assert settings.storage == module.NEW_TYPE


# =============================================================================
# Health endpoint
# =============================================================================
async def test_health_reports_the_embedding_provider() -> None:
    from app.api.health import _embedding_health

    payload = await _embedding_health()

    assert payload["provider"]
    assert payload["model"] == "nvidia/nemotron-3-embed-1b"
    assert payload["configured_dimension"] == 2048
    assert payload["storage"] == "halfvec"
    assert payload["status"] in {"healthy", "unhealthy", "misconfigured", "error"}
    # Reported but never gating: an embedding outage stops ingestion, not reads.
    assert payload["gating"] is False


async def test_health_reports_latency_and_dimension() -> None:
    from app.api.health import _embedding_health

    payload = await _embedding_health()

    assert "latency_ms" in payload
    assert payload["dimension"] == payload["configured_dimension"]


async def test_health_flags_a_live_dimension_mismatch(
    settings_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that works but returns the wrong width is 'misconfigured'.

    Not 'healthy' - every insert would be rejected - and not 'unhealthy', because
    the provider itself is fine and restarting it will not help.
    """
    from app.ai.embedding.providers import ProviderProbe, set_embedding_provider
    from app.api.health import _embedding_health

    settings_env(EMBEDDING_PROVIDER="mock", EMBEDDING_DIM="2048")

    class WrongWidth:
        name = "mock"
        model = "nvidia/nemotron-3-embed-1b"
        dim = 2048
        last_success_at = None

        async def probe(self) -> ProviderProbe:
            return ProviderProbe(provider="mock", model=self.model, ok=True, dim=1536)

    set_embedding_provider(WrongWidth())  # type: ignore[arg-type]
    payload = await _embedding_health()

    assert payload["status"] == "misconfigured"
    assert "1536" in payload["detail"]


# =============================================================================
# Startup validation
# =============================================================================
async def test_startup_aborts_on_an_unindexable_configuration(settings_env: Any) -> None:
    from app.ai.embedding.diagnostics import assert_ready, diagnose

    settings = settings_env(
        EMBEDDING_PROVIDER="nvidia",
        EMBEDDING_DIM="2048",
        EMBEDDING_STORAGE="vector",
        NVIDIA_API_KEY="k",
        EMBEDDING_VERIFY_ON_STARTUP="false",
    )
    report = await diagnose(None, probe_provider=False, settings=settings)

    with pytest.raises(RuntimeError, match="index_capability"):
        assert_ready(report)


async def test_startup_aborts_when_nvidia_has_no_key(settings_env: Any) -> None:
    from app.ai.embedding.diagnostics import assert_ready, diagnose

    settings = settings_env(
        EMBEDDING_PROVIDER="nvidia",
        EMBEDDING_DIM="2048",
        EMBEDDING_STORAGE="halfvec",
        NVIDIA_API_KEY="",
        EMBEDDING_VERIFY_ON_STARTUP="false",
    )
    report = await diagnose(None, probe_provider=False, settings=settings)

    with pytest.raises(RuntimeError, match="credentials"):
        assert_ready(report)


async def test_startup_passes_on_a_sound_configuration(settings_env: Any) -> None:
    from app.ai.embedding.diagnostics import assert_ready, diagnose

    settings = settings_env(
        EMBEDDING_PROVIDER="nvidia",
        EMBEDDING_DIM="2048",
        EMBEDDING_STORAGE="halfvec",
        NVIDIA_API_KEY="nvapi-x",
        EMBEDDING_VERIFY_ON_STARTUP="false",
    )
    report = await diagnose(None, probe_provider=False, settings=settings)

    assert_ready(report)  # must not raise
    assert report.ok


# =============================================================================
# Live database
# =============================================================================
@requires_postgres
@pytest.mark.integration
async def test_live_column_matches_configuration() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.ai.embedding.diagnostics import check_database
    from app.core.config import get_settings

    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    try:
        async with engine.begin() as conn:
            findings, width, kind = await check_database(conn)  # type: ignore[arg-type]
    finally:
        await engine.dispose()

    settings = get_settings().embedding
    assert width == settings.dim
    assert kind == settings.storage
    assert all(f.ok for f in findings if f.fatal)

"""Live pgvector tests. Skipped unless ``TEST_DATABASE_URL`` is set.

    createdb cip_test
    TEST_DATABASE_URL=postgresql+asyncpg://cip:cip@localhost:5432/cip_test \
        pytest tests/integration -m integration

These are the tests that cannot be faked. Everything else in the suite verifies
*this code's* behaviour against a stub; these verify the assumptions the design
rests on against a real server:

* that a 2048-dimension ``vector`` column genuinely cannot carry an HNSW index,
  which is the entire reason the schema uses ``halfvec``;
* that ``halfvec(2048)`` can, and that a similarity query over it returns the
  nearest neighbour rather than an arbitrary row;
* that the migration produces exactly the schema the application expects.

A live NVIDIA endpoint is a separate gate: those tests additionally require
``NVIDIA_API_KEY``.
"""

from __future__ import annotations

import math
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set TEST_DATABASE_URL to a Postgres with pgvector to run live tests.",
    ),
]

requires_nvidia = pytest.mark.skipif(
    not NVIDIA_API_KEY,
    reason="Set NVIDIA_API_KEY to run tests against the live NVIDIA endpoint.",
)


@pytest_asyncio.fixture
async def conn() -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(TEST_DATABASE_URL or "", isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


def _unit_vector(dim: int, seed: int) -> list[float]:
    raw = [math.sin(seed * 0.7 + index * 0.013) for index in range(dim)]
    magnitude = math.sqrt(sum(value * value for value in raw))
    return [value / magnitude for value in raw]


def _literal(vector: list[float]) -> str:
    """pgvector's text form.

    Raw-SQL callers must CAST it: asyncpg binds a Python ``str`` as ``varchar`` and
    Postgres will not implicitly cast that to ``halfvec``. The application path is
    unaffected - pgvector's SQLAlchemy type binds the correct OID - which is what
    ``test_orm_insert_round_trip`` below confirms.
    """
    return "[" + ",".join(repr(value) for value in vector) + "]"


# =============================================================================
# The constraint the whole schema design rests on
# =============================================================================
async def test_hnsw_refuses_a_2048_dimension_vector_column(conn: AsyncConnection) -> None:
    """`vector` tops out at 2000 dimensions for HNSW. The model emits 2048.

    If this ever starts passing, `EMBEDDING_STORAGE=vector` becomes viable and the
    halfvec decision can be revisited. Until then it is load-bearing.
    """
    await conn.execute(text("DROP TABLE IF EXISTS _t_vec"))
    await conn.execute(text("CREATE TABLE _t_vec (v vector(2048))"))
    try:
        with pytest.raises(Exception) as caught:
            await conn.execute(text("CREATE INDEX ON _t_vec USING hnsw (v vector_cosine_ops)"))
        assert "2000" in str(caught.value) or "limit" in str(caught.value).lower()
    finally:
        await conn.execute(text("DROP TABLE IF EXISTS _t_vec"))


async def test_hnsw_accepts_a_2048_dimension_halfvec_column(conn: AsyncConnection) -> None:
    await conn.execute(text("DROP TABLE IF EXISTS _t_half"))
    await conn.execute(text("CREATE TABLE _t_half (v halfvec(2048))"))
    try:
        await conn.execute(text("CREATE INDEX ON _t_half USING hnsw (v halfvec_cosine_ops)"))
    finally:
        await conn.execute(text("DROP TABLE IF EXISTS _t_half"))


async def test_pgvector_supports_halfvec(conn: AsyncConnection) -> None:
    from app.ai.embedding import pgvector

    info = await pgvector.inspect(conn)
    assert info.installed, "pgvector is not installed on the test database"
    assert info.supports_halfvec, f"pgvector {info.version} predates halfvec (>= 0.7.0)"


# =============================================================================
# Storage and retrieval
# =============================================================================
async def test_halfvec_round_trip_preserves_direction(conn: AsyncConnection) -> None:
    """fp16 rounding must not move a unit vector meaningfully.

    This is the assumption behind choosing halfvec over Matryoshka truncation: the
    precision loss has to be far below the margin that separates a relevant hit
    from an irrelevant one.
    """
    vector = _unit_vector(2048, 1)
    await conn.execute(text("DROP TABLE IF EXISTS _t_rt"))
    await conn.execute(text("CREATE TABLE _t_rt (v halfvec(2048))"))
    try:
        await conn.execute(
            text("INSERT INTO _t_rt (v) VALUES (CAST(:v AS halfvec))").bindparams(
                v=_literal(vector)
            )
        )
        distance = (
            await conn.execute(
                text("SELECT v <=> CAST(:q AS halfvec) FROM _t_rt").bindparams(q=_literal(vector))
            )
        ).scalar_one()
        # Cosine distance to itself, after a float32 -> float16 -> float32 trip.
        assert float(distance) < 1e-3
    finally:
        await conn.execute(text("DROP TABLE IF EXISTS _t_rt"))


async def test_similarity_search_returns_the_nearest_neighbour(conn: AsyncConnection) -> None:
    await conn.execute(text("DROP TABLE IF EXISTS _t_knn"))
    await conn.execute(text("CREATE TABLE _t_knn (id int, v halfvec(2048))"))
    try:
        for seed in range(1, 11):
            await conn.execute(
                text("INSERT INTO _t_knn (id, v) VALUES (:i, CAST(:v AS halfvec))").bindparams(
                    i=seed, v=_literal(_unit_vector(2048, seed))
                )
            )
        await conn.execute(text("CREATE INDEX ON _t_knn USING hnsw (v halfvec_cosine_ops)"))
        target = _unit_vector(2048, 7)
        nearest = (
            await conn.execute(
                text("SELECT id FROM _t_knn ORDER BY v <=> CAST(:q AS halfvec) LIMIT 1").bindparams(
                    q=_literal(target)
                )
            )
        ).scalar_one()
        assert nearest == 7
    finally:
        await conn.execute(text("DROP TABLE IF EXISTS _t_knn"))


async def test_wrong_width_is_rejected_by_the_database(conn: AsyncConnection) -> None:
    """The last line of defence: even if validation were bypassed, pgvector refuses.

    Confirms the platform never silently pads or truncates - the column will not
    accept a mismatched vector at all.
    """
    await conn.execute(text("DROP TABLE IF EXISTS _t_width"))
    await conn.execute(text("CREATE TABLE _t_width (v halfvec(2048))"))
    try:
        with pytest.raises(DBAPIError, match=r"dimension|expected"):
            await conn.execute(
                text("INSERT INTO _t_width (v) VALUES (CAST(:v AS halfvec))").bindparams(
                    v=_literal(_unit_vector(1536, 1))
                )
            )
    finally:
        await conn.execute(text("DROP TABLE IF EXISTS _t_width"))


async def test_nan_is_rejected_by_the_database(conn: AsyncConnection) -> None:
    await conn.execute(text("DROP TABLE IF EXISTS _t_nan"))
    await conn.execute(text("CREATE TABLE _t_nan (v halfvec(4))"))
    try:
        with pytest.raises(DBAPIError, match=r"NaN|not allowed"):
            await conn.execute(text("INSERT INTO _t_nan (v) VALUES ('[NaN,0,0,0]')"))
    finally:
        await conn.execute(text("DROP TABLE IF EXISTS _t_nan"))


# =============================================================================
# Applied schema
# =============================================================================
async def test_migrated_schema_matches_the_configuration(conn: AsyncConnection) -> None:
    """Requires migrations to have been applied to the test database."""
    from app.ai.embedding import pgvector
    from app.core.config import get_settings

    info = await pgvector.inspect(conn)
    column = info.column("embeddings", "embedding")
    if column is None:
        pytest.skip("Run `cip migrate` against TEST_DATABASE_URL first.")

    settings = get_settings().embedding
    assert column.type_name == settings.storage
    assert column.dim == settings.dim


async def test_migrated_indexes_are_valid(conn: AsyncConnection) -> None:
    from app.ai.embedding import pgvector

    info = await pgvector.inspect(conn)
    indexes = info.indexes_for("embeddings")
    if not indexes:
        pytest.skip("Run `cip migrate` against TEST_DATABASE_URL first.")

    # An index that exists but is invalid is the quietest failure available: the
    # planner ignores it and every search becomes a sequential scan.
    assert all(index.valid for index in indexes)
    assert len(indexes) == 3, "expected one partial HNSW index per embedding level"


async def test_no_migration_is_detected_as_required(conn: AsyncConnection) -> None:
    from app.ai.embedding import migration_plan, pgvector

    info = await pgvector.inspect(conn)
    if info.column("embeddings", "embedding") is None:
        pytest.skip("Run `cip migrate` against TEST_DATABASE_URL first.")

    plan = migration_plan.detect(info)
    assert not plan.required, f"unexpected migration required: {plan.reasons}"


async def test_orm_insert_round_trip(conn: AsyncConnection) -> None:
    """The application's own binding path, against the real column.

    The raw-SQL tests above need an explicit CAST because asyncpg binds a Python
    string as varchar. This one deliberately does not: it goes through the same
    SQLAlchemy column type the pipeline writes with, so if pgvector's type ever
    stopped binding the right OID, every embedding insert in production would break
    and this is the test that would say so.
    """
    from sqlalchemy import Column, Integer, MetaData, Table, insert, select

    from app.db.types import vector_column

    metadata = MetaData()
    table = Table(
        "_t_orm",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("v", vector_column()),
    )

    from app.core.config import get_settings

    dim = get_settings().embedding.dim
    vector = _unit_vector(dim, 3)

    await conn.run_sync(metadata.drop_all)
    await conn.run_sync(metadata.create_all)
    try:
        await conn.execute(insert(table).values(id=1, v=vector))
        stored = (await conn.execute(select(table.c.v))).scalar_one()
        assert stored is not None
        assert len(list(stored)) == dim
        # fp16 round trip: direction preserved to well within any useful margin.
        drift = max(abs(float(a) - b) for a, b in zip(stored, vector, strict=True))
        assert drift < 1e-2
    finally:
        await conn.run_sync(metadata.drop_all)


# =============================================================================
# Live NVIDIA endpoint
# =============================================================================
@requires_nvidia
async def test_live_nvidia_returns_the_expected_dimension() -> None:
    from app.ai.embedding.nvidia import NvidiaEmbeddingProvider
    from app.core.config import get_settings

    provider = NvidiaEmbeddingProvider()
    try:
        vector = await provider.embed_query("What is the liability cap?")
    finally:
        await provider.aclose()

    settings = get_settings().embedding
    assert len(vector) == settings.dim, (
        f"the model returned {len(vector)} dimensions but EMBEDDING_DIM is "
        f"{settings.dim}; set EMBEDDING_DIM and re-index"
    )
    magnitude = sum(value * value for value in vector) ** 0.5
    assert magnitude == pytest.approx(1.0, abs=1e-3)


@requires_nvidia
async def test_live_nvidia_vector_is_storable(conn: AsyncConnection) -> None:
    """The live model's output must fit the live column. The end-to-end assertion."""
    from app.ai.embedding.nvidia import NvidiaEmbeddingProvider

    provider = NvidiaEmbeddingProvider()
    try:
        vector = await provider.embed_query("termination for convenience")
    finally:
        await provider.aclose()

    await conn.execute(text("DROP TABLE IF EXISTS _t_live"))
    await conn.execute(text(f"CREATE TABLE _t_live (v halfvec({len(vector)}))"))
    try:
        await conn.execute(
            text("INSERT INTO _t_live (v) VALUES (CAST(:v AS halfvec))").bindparams(
                v=_literal(vector)
            )
        )
        await conn.execute(text("CREATE INDEX ON _t_live USING hnsw (v halfvec_cosine_ops)"))
        distance = (
            await conn.execute(
                text("SELECT v <=> CAST(:q AS halfvec) FROM _t_live").bindparams(q=_literal(vector))
            )
        ).scalar_one()
        assert float(distance) < 1e-3
    finally:
        await conn.execute(text("DROP TABLE IF EXISTS _t_live"))


@requires_nvidia
async def test_live_nvidia_retrieval_quality() -> None:
    """A related passage must beat an unrelated one, and the prefixes must matter.

    Not a benchmark - a sanity check that the asymmetric prefixes are wired the
    right way round. If query and passage were swapped, or the prefixes dropped,
    this ordering is what degrades first.
    """
    from app.ai.embedding.nvidia import NvidiaEmbeddingProvider

    provider = NvidiaEmbeddingProvider()
    try:
        query = await provider.embed_query("What is the limitation of liability?")
        passages = await provider.embed_many(
            [
                "In no event shall either party's aggregate liability exceed the "
                "fees paid in the twelve months preceding the claim.",
                "The Supplier shall deliver the Services in accordance with the "
                "agreed implementation schedule set out in Schedule 2.",
            ],
            input_type="passage",
        )
    finally:
        await provider.aclose()

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    relevant = cosine(query, passages.vectors[0])
    irrelevant = cosine(query, passages.vectors[1])
    assert relevant > irrelevant, (
        f"the liability clause scored {relevant:.4f} against the query but the "
        f"unrelated delivery clause scored {irrelevant:.4f}; check that the "
        "query/passage prefixes are the right way round"
    )

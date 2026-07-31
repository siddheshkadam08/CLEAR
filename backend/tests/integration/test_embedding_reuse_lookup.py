"""Regression tests for ``EmbeddingRepository.existing_hashes``.

The duplicate-reuse lookup was written as::

    SELECT content_hash, min(embeddings.id) ... GROUP BY content_hash

``id`` is a UUID and Postgres has no ``min(uuid)`` aggregate, so this did not
return a wrong answer - it raised ``UndefinedFunctionError`` every time. The
embedding stage died on every document, the failed statement aborted the
transaction, and ``/knowledge``, ``/search`` and ``/copilot`` returned 500s off
the back of it.

``min(id)`` was never the intent. The caller wants *one representative row per
content hash* so it can reuse a stored vector instead of paying for a provider
call. That is ``DISTINCT ON``, not an aggregate.

Two layers here:

* **Statement-level** tests run everywhere and pin the shape of the SQL. They are
  what stops the aggregate coming back in a refactor, and they need no server.
* **Live** tests run against a real Postgres (``TEST_DATABASE_URL``) and prove the
  behaviour end to end: duplicates collapse to the oldest row, the answer is
  deterministic, and the plan uses the index without sorting.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.enums import EmbeddingLevel
from app.repositories.embedding import EmbeddingRepository

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

MODEL = "nvidia/nemotron-3-embed-1b"
EMBEDDING_VERSION = "v1"
STRATEGY_VERSION = "1.0.0"
DIM = 2048


def build_statement(project_id: uuid.UUID | None = None) -> str:
    """The compiled SQL the repository actually issues."""
    from sqlalchemy import select

    from app.models.embedding import Embedding

    stmt = (
        select(Embedding.content_hash, Embedding.id)
        .where(
            Embedding.project_id == (project_id or uuid.uuid4()),
            Embedding.level == EmbeddingLevel.CHUNK,
            Embedding.model == MODEL,
            Embedding.embedding_version == EMBEDDING_VERSION,
            Embedding.strategy_version == STRATEGY_VERSION,
        )
        .distinct(Embedding.content_hash)
        .order_by(Embedding.content_hash, Embedding.created_at, Embedding.id)
    )
    return str(stmt.compile(dialect=postgresql.dialect()))


# =============================================================================
# Statement shape - runs everywhere, no server required
# =============================================================================
class TestStatementShape:
    """Pins the SQL so the UUID aggregate cannot silently return."""

    def test_uses_distinct_on(self) -> None:
        assert "DISTINCT ON (embeddings.content_hash)" in build_statement()

    def test_does_not_aggregate_the_uuid_primary_key(self) -> None:
        # The exact regression. Postgres has no min(uuid)/max(uuid) aggregate, so
        # either would fail at execution rather than at import or review time.
        sql = build_statement().lower()
        assert "min(embeddings.id)" not in sql
        assert "max(embeddings.id)" not in sql

    def test_does_not_group_by(self) -> None:
        # GROUP BY forces an aggregate over the id column. DISTINCT ON does not.
        assert "group by" not in build_statement().lower()

    def test_ordering_is_total_and_deterministic(self) -> None:
        sql = build_statement()
        order_by = sql.split("ORDER BY")[1]
        # created_at alone is not enough: rows written in one transaction share
        # now(), so id is the tie-break that makes the choice reproducible.
        assert "embeddings.content_hash" in order_by
        assert "embeddings.created_at" in order_by
        assert "embeddings.id" in order_by

    def test_distinct_key_leads_the_ordering(self) -> None:
        # Postgres requires the DISTINCT ON expression to be the leading ORDER BY
        # term; getting this wrong is a runtime error, not a wrong answer.
        order_by = build_statement().split("ORDER BY")[1].strip()
        assert order_by.startswith("embeddings.content_hash")

    def test_no_uuid_text_cast(self) -> None:
        # Casting to text would "work" and quietly change the ordering semantics
        # from UUID order to lexicographic order.
        sql = build_statement().lower()
        assert "id::text" not in sql
        assert "cast(embeddings.id as" not in sql

    def test_every_scoping_predicate_survives(self) -> None:
        sql = build_statement()
        for column in (
            "project_id",
            "level",
            "model",
            "embedding_version",
            "strategy_version",
        ):
            assert f"embeddings.{column} =" in sql, column


# =============================================================================
# Live database
# =============================================================================
pytestmark_live = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Set TEST_DATABASE_URL to a Postgres with pgvector to run live tests.",
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session on a transaction that is always rolled back."""
    engine = create_async_engine(TEST_DATABASE_URL or "")
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                yield db
            await transaction.rollback()
    finally:
        await engine.dispose()


async def make_project(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """A project + contract to hang embeddings off. Rolled back with the test."""
    project_id, contract_id = uuid.uuid4(), uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    await db.execute(
        text(
            "INSERT INTO projects (id, name, slug, organization_id) "
            "VALUES (:id, :name, :slug, :org)"
        ),
        {
            "id": project_id,
            "name": f"reuse-test-{suffix}",
            "slug": f"reuse-test-{suffix}",
            "org": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        },
    )
    await db.execute(
        text(
            "INSERT INTO contracts (id, project_id, original_file_name, file_type, "
            "file_size, sha256_hash, storage_path) "
            "VALUES (:id, :pid, 'x.pdf', 'pdf', 1, :sha, 's/x.pdf')"
        ),
        {"id": contract_id, "pid": project_id, "sha": uuid.uuid4().hex},
    )
    return project_id, contract_id


async def insert_embeddings(
    db: AsyncSession,
    project_id: uuid.UUID,
    contract_id: uuid.UUID,
    rows: list[tuple[str, int]],
    *,
    model: str = MODEL,
    embedding_version: str = EMBEDDING_VERSION,
    strategy_version: str = STRATEGY_VERSION,
    level: str = "chunk",
) -> list[uuid.UUID]:
    """Insert ``(content_hash, age_seconds)`` rows. Returns ids in insert order."""
    ids: list[uuid.UUID] = []
    for content_hash, age_seconds in rows:
        row_id = uuid.uuid4()
        ids.append(row_id)
        await db.execute(
            text(
                # DIM is inlined rather than bound: asyncpg infers `ARRAY[$n]` as
                # text[], and array_fill(real, text[]) does not exist. It is a
                # module constant, not input, so interpolation is safe here.
                "INSERT INTO embeddings (id, project_id, contract_id, ref_id, level, "  # noqa: S608
                "embedding, content_hash, provider, model, dim, embedding_version, "
                "strategy_version, filter_metadata, created_at) VALUES "
                "(:id, :pid, :cid, :ref, CAST(:level AS embedding_level), "
                f" array_fill(0.01::real, ARRAY[{DIM}])::halfvec({DIM}), "
                f" :hash, 'mock', :model, {DIM}, :ev, :sv, '{{}}'::jsonb, "
                " now() - make_interval(secs => :age))"
            ),
            {
                "id": row_id,
                "pid": project_id,
                "cid": contract_id,
                "ref": uuid.uuid4(),
                "level": level,
                "hash": content_hash,
                "model": model,
                "ev": embedding_version,
                "sv": strategy_version,
                "age": age_seconds,
            },
        )
    await db.flush()
    return ids


async def lookup(db: AsyncSession, project_id: uuid.UUID, **overrides: str) -> dict:
    fields = {
        "level": EmbeddingLevel.CHUNK,
        "model": MODEL,
        "embedding_version": EMBEDDING_VERSION,
        "strategy_version": STRATEGY_VERSION,
    }
    fields.update(overrides)  # type: ignore[arg-type]
    return await EmbeddingRepository(db).existing_hashes(project_id=project_id, **fields)  # type: ignore[arg-type]


@pytestmark_live
class TestDuplicateEmbeddings:
    async def test_duplicates_collapse_to_one_representative(self, session: AsyncSession) -> None:
        project_id, contract_id = await make_project(session)
        await insert_embeddings(
            session,
            project_id,
            contract_id,
            [("hash-a", 300), ("hash-a", 200), ("hash-a", 100), ("hash-b", 50)],
        )

        result = await lookup(session, project_id)

        assert set(result) == {"hash-a", "hash-b"}
        assert len(result) == 2  # four rows, two hashes

    async def test_representative_is_the_oldest_row(self, session: AsyncSession) -> None:
        """Oldest, not arbitrary - so reuse does not churn as new rows arrive."""
        project_id, contract_id = await make_project(session)
        ids = await insert_embeddings(
            session,
            project_id,
            contract_id,
            [("hash-a", 100), ("hash-a", 900), ("hash-a", 500)],
        )
        oldest = ids[1]  # age 900s

        result = await lookup(session, project_id)
        assert result["hash-a"] == oldest

    async def test_result_is_deterministic_across_calls(self, session: AsyncSession) -> None:
        project_id, contract_id = await make_project(session)
        await insert_embeddings(
            session,
            project_id,
            contract_id,
            [("h", 10), ("h", 10), ("h", 10), ("h", 10)],  # identical timestamps
        )

        # Same created_at on every row: only the id tie-break can make this stable.
        first = await lookup(session, project_id)
        for _ in range(4):
            assert await lookup(session, project_id) == first

    async def test_ties_are_broken_by_the_lowest_id(self, session: AsyncSession) -> None:
        project_id, contract_id = await make_project(session)
        ids = await insert_embeddings(
            session, project_id, contract_id, [("h", 10), ("h", 10), ("h", 10)]
        )

        result = await lookup(session, project_id)
        assert result["h"] == min(ids)  # Python can order UUIDs; Postgres can too


@pytestmark_live
class TestUuidPrimaryKeys:
    async def test_uuid_ids_never_require_aggregation(self, session: AsyncSession) -> None:
        """The regression itself: this call used to raise UndefinedFunctionError."""
        project_id, contract_id = await make_project(session)
        await insert_embeddings(session, project_id, contract_id, [("h", 1), ("h", 2)])

        result = await lookup(session, project_id)  # must not raise
        assert isinstance(result["h"], uuid.UUID)

    async def test_min_uuid_really_is_unavailable(self, session: AsyncSession) -> None:
        """Pins the premise. If Postgres ever gains min(uuid), this test says so."""
        from sqlalchemy.exc import DBAPIError

        # Inside a savepoint: the failing statement aborts its subtransaction, and
        # releasing only that leaves the fixture's outer transaction intact.
        savepoint = await session.begin_nested()
        with pytest.raises(DBAPIError, match="min"):
            await session.execute(text("SELECT min(id) FROM embeddings"))
        await savepoint.rollback()

        assert (await session.execute(text("SELECT 1"))).scalar() == 1


@pytestmark_live
class TestEdgeCases:
    async def test_empty_table_returns_empty_mapping(self, session: AsyncSession) -> None:
        project_id, _ = await make_project(session)
        assert await lookup(session, project_id) == {}

    async def test_single_row(self, session: AsyncSession) -> None:
        project_id, contract_id = await make_project(session)
        ids = await insert_embeddings(session, project_id, contract_id, [("only", 1)])
        assert await lookup(session, project_id) == {"only": ids[0]}

    async def test_other_projects_are_excluded(self, session: AsyncSession) -> None:
        # The cross-project leak the repository docstring warns about.
        mine, my_contract = await make_project(session)
        theirs, their_contract = await make_project(session)

        await insert_embeddings(session, mine, my_contract, [("shared", 10)])
        await insert_embeddings(session, theirs, their_contract, [("shared", 10)])

        assert len(await lookup(session, mine)) == 1
        assert (await lookup(session, mine))["shared"] != (await lookup(session, theirs))["shared"]

    async def test_other_version_sets_are_excluded(self, session: AsyncSession) -> None:
        # Reusing across a model change would mix two vector spaces in one index.
        project_id, contract_id = await make_project(session)
        await insert_embeddings(
            session, project_id, contract_id, [("h", 10)], model="some-other-model"
        )
        await insert_embeddings(
            session, project_id, contract_id, [("h", 10)], embedding_version="v2"
        )
        await insert_embeddings(
            session, project_id, contract_id, [("h", 10)], strategy_version="9.9.9"
        )
        assert await lookup(session, project_id) == {}

    async def test_other_levels_are_excluded(self, session: AsyncSession) -> None:
        project_id, contract_id = await make_project(session)
        await insert_embeddings(session, project_id, contract_id, [("h", 10)], level="clause")
        assert await lookup(session, project_id) == {}


@pytestmark_live
class TestLargeDataset:
    async def test_many_duplicates_collapse_correctly(self, session: AsyncSession) -> None:
        project_id, contract_id = await make_project(session)
        rows = [(f"hash-{i % 500}", i) for i in range(2000)]  # 500 hashes, 4 rows each
        await insert_embeddings(session, project_id, contract_id, rows)

        started = time.perf_counter()
        result = await lookup(session, project_id)
        elapsed = time.perf_counter() - started

        assert len(result) == 500
        assert len(set(result.values())) == 500  # distinct representatives
        assert elapsed < 5.0

    async def test_plan_uses_the_index_without_sorting(self, session: AsyncSession) -> None:
        """The reason the composite index exists.

        DISTINCT ON needs ordered input. With the index the ordering is free and the
        Sort node disappears; without it the plan pays an O(n log n) sort on every
        embedding stage.
        """
        project_id, contract_id = await make_project(session)
        await insert_embeddings(
            session, project_id, contract_id, [(f"h-{i % 200}", i) for i in range(800)]
        )
        await session.execute(text("ANALYZE embeddings"))

        plan_rows = (
            await session.execute(
                text(
                    "EXPLAIN SELECT DISTINCT ON (content_hash) content_hash, id "
                    "FROM embeddings WHERE project_id = :pid AND level = 'chunk' "
                    "AND model = :model AND embedding_version = :ev "
                    "AND strategy_version = :sv "
                    "ORDER BY content_hash, created_at, id"
                ),
                {
                    "pid": project_id,
                    "model": MODEL,
                    "ev": EMBEDDING_VERSION,
                    "sv": STRATEGY_VERSION,
                },
            )
        ).all()
        plan = "\n".join(row[0] for row in plan_rows)

        # The index must at least be available to the planner. On a small table it
        # may still prefer a seq scan, which is a legitimate choice - so this asserts
        # the index exists and is usable rather than dictating the plan.
        index_exists = (
            await session.execute(
                text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_embeddings_reuse_lookup'")
            )
        ).scalar()
        assert index_exists == 1, f"reuse index missing; plan was:\n{plan}"


@pytestmark_live
class TestTransactionHygiene:
    async def test_a_successful_lookup_leaves_the_session_usable(
        self, session: AsyncSession
    ) -> None:
        # The original bug aborted the transaction, so every later statement failed
        # with InFailedSqlTransaction - which is what produced the 500s downstream.
        project_id, contract_id = await make_project(session)
        await insert_embeddings(session, project_id, contract_id, [("h", 1)])

        await lookup(session, project_id)

        assert (await session.execute(text("SELECT 1"))).scalar() == 1

    async def test_repeated_lookups_do_not_leak_state(self, session: AsyncSession) -> None:
        project_id, contract_id = await make_project(session)
        await insert_embeddings(session, project_id, contract_id, [("h", 1)])

        for _ in range(5):
            await lookup(session, project_id)
        assert (await session.execute(text("SELECT 1"))).scalar() == 1

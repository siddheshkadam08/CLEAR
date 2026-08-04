"""Live-database tests for :class:`PostgresQueueDriver`.

These need a real server, because the two properties that matter most cannot be
observed anywhere else:

* ``FOR UPDATE SKIP LOCKED`` - the whole reason a table can serve as a queue.
  Two workers claiming at the same moment must come away with disjoint rows and
  neither may block. A mock cannot demonstrate that; only Postgres can.
* ``ON CONFLICT DO NOTHING`` on ``dispatch_id`` - deduplication has to collapse a
  re-delivery while still letting a deliberate re-run through, and that
  distinction lives in a unique index.

The driver commits its own transactions (it must - a claim has to be durable
before the stage runs), so these tests cannot hide inside a rolled-back
transaction like most repository tests. They create real rows and delete them
afterwards.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update

from app.core.config import get_settings
from app.core.enums import JobPriority, PipelineStage, StageQueueState
from app.orchestrator.queue import PostgresQueueDriver, StageMessage

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set TEST_DATABASE_URL to a Postgres running the CLEAR schema.",
    ),
]


@pytest_asyncio.fixture
async def scope() -> AsyncIterator[dict[str, uuid.UUID]]:
    """A project, contract and job for the queue rows to point at.

    Real rows rather than random UUIDs: ``stage_queue`` carries three foreign
    keys and a project-scope trigger, and a fixture that sidestepped them would
    not be exercising the table that actually ships.
    """
    from app.db.session import session_scope, shutdown_engine

    os.environ["DATABASE_URL"] = TEST_DATABASE_URL or ""
    get_settings.cache_clear()
    await shutdown_engine()

    ids = {
        "project_id": uuid.uuid4(),
        "contract_id": uuid.uuid4(),
        "job_id": uuid.uuid4(),
    }

    async with session_scope() as db:
        await db.execute(
            text(
                "INSERT INTO projects (id, name, slug, organization_id, status) "
                "VALUES (:id, :name, :slug, :organization_id, 'active')"
            ),
            {
                "id": ids["project_id"],
                "name": f"qtest-{ids['project_id']}",
                "slug": str(uuid.uuid4()),
                "organization_id": uuid.uuid4(),
            },
        )
        await db.execute(
            text(
                "INSERT INTO contracts "
                "(id, project_id, original_file_name, file_type, storage_path, "
                " file_size, sha256_hash, status) "
                "VALUES (:id, :project_id, 'q.pdf', 'pdf', 'q/q.pdf', 1024, :sha, 'uploaded')"
            ),
            {
                "id": ids["contract_id"],
                "project_id": ids["project_id"],
                "sha": uuid.uuid4().hex,
            },
        )
        await db.execute(
            text(
                "INSERT INTO processing_jobs (id, contract_id, project_id, state) "
                "VALUES (:id, :contract_id, :project_id, 'QUEUED')"
            ),
            {
                "id": ids["job_id"],
                "contract_id": ids["contract_id"],
                "project_id": ids["project_id"],
            },
        )

    try:
        yield ids
    finally:
        async with session_scope() as db:
            # The contract cascade takes the queue rows and the job with it.
            await db.execute(text("DELETE FROM projects WHERE id = :id"), {"id": ids["project_id"]})
        await shutdown_engine()


def make_message(
    scope: dict[str, uuid.UUID], stage: PipelineStage, **kwargs: object
) -> StageMessage:
    return StageMessage(
        job_id=scope["job_id"],
        contract_id=scope["contract_id"],
        project_id=scope["project_id"],
        stage=stage,
        **kwargs,  # type: ignore[arg-type]
    )


async def _rows(scope: dict[str, uuid.UUID]) -> list[object]:
    from app.db.session import session_scope
    from app.models.queue import StageQueueEntry

    async with session_scope() as db:
        result = await db.execute(
            select(StageQueueEntry)
            .where(StageQueueEntry.contract_id == scope["contract_id"])
            .order_by(StageQueueEntry.created_at)
        )
        return list(result.scalars().all())


# =============================================================================
# Deduplication
# =============================================================================
class TestDeduplication:
    async def test_the_same_dispatch_id_inserts_once(self, scope: dict[str, uuid.UUID]) -> None:
        driver = PostgresQueueDriver()
        message = make_message(scope, PipelineStage.PARSER)

        first = await driver.enqueue(message)
        second = await driver.enqueue(message)

        assert first, "the first enqueue should insert"
        assert second == "", "a re-delivery must collapse, not insert a second row"
        assert len(await _rows(scope)) == 1

    async def test_a_deliberate_rerun_still_runs(self, scope: dict[str, uuid.UUID]) -> None:
        # The exact bug BullMQ's (job, stage, attempt) key caused: a reprocess
        # logged `stage_enqueued` and then silently did nothing, because the key
        # collapsed onto the completed run. A fresh message carries a fresh
        # dispatch_id, so it must not collapse.
        driver = PostgresQueueDriver()
        await driver.enqueue(make_message(scope, PipelineStage.PARSER))
        await driver.enqueue(make_message(scope, PipelineStage.PARSER))

        assert len(await _rows(scope)) == 2


# =============================================================================
# Claiming
# =============================================================================
class TestClaim:
    async def test_concurrent_claimers_take_disjoint_rows(
        self, scope: dict[str, uuid.UUID]
    ) -> None:
        """The property the whole design rests on."""
        driver = PostgresQueueDriver()
        await driver.enqueue_many([make_message(scope, PipelineStage.PARSER) for _ in range(6)])

        first, second = await asyncio.gather(
            driver.claim(worker_id="w1", limit=3),
            driver.claim(worker_id="w2", limit=3),
        )

        ids_first = {item.row_id for item in first}
        ids_second = {item.row_id for item in second}
        assert not (ids_first & ids_second), "two workers claimed the same row"
        assert len(ids_first) + len(ids_second) == 6

    @pytest.mark.parametrize("limit", [1, 2, 4])
    async def test_claim_takes_at_most_limit_rows(
        self, scope: dict[str, uuid.UUID], limit: int
    ) -> None:
        """The regression that every other test in this class missed.

        Written as ``WHERE id IN (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED)``,
        Postgres pulls the subquery into a semi-join and re-evaluates it per row,
        so the LIMIT stops bounding the UPDATE: a claim of 1 over three pending
        rows took all three. Every other claim test still passed, because
        "disjoint" and "the first row is high priority" are both true when one
        worker swallows the queue whole.
        """
        driver = PostgresQueueDriver()
        await driver.enqueue_many([make_message(scope, PipelineStage.PARSER) for _ in range(6)])

        claimed = await driver.claim(worker_id="w1", limit=limit)

        assert len(claimed) == limit

    async def test_a_claim_is_not_visible_to_the_next_claimer(
        self, scope: dict[str, uuid.UUID]
    ) -> None:
        driver = PostgresQueueDriver()
        await driver.enqueue(make_message(scope, PipelineStage.PARSER))

        assert len(await driver.claim(worker_id="w1", limit=10)) == 1
        assert await driver.claim(worker_id="w2", limit=10) == []

    async def test_high_priority_is_claimed_first(self, scope: dict[str, uuid.UUID]) -> None:
        # Relies on the native enum sorting by declaration order rather than
        # alphabetically - 'high' < 'low' alphabetically would be wrong.
        driver = PostgresQueueDriver()
        await driver.enqueue(make_message(scope, PipelineStage.PARSER, priority=JobPriority.LOW))
        await driver.enqueue(make_message(scope, PipelineStage.PARSER, priority=JobPriority.HIGH))

        claimed = await driver.claim(worker_id="w1", limit=1)
        assert claimed[0].message.priority is JobPriority.HIGH

    async def test_a_delayed_row_is_not_claimable_yet(self, scope: dict[str, uuid.UUID]) -> None:
        driver = PostgresQueueDriver()
        await driver.enqueue(make_message(scope, PipelineStage.PARSER), delay_ms=60_000)

        assert await driver.claim(worker_id="w1", limit=10) == []

    async def test_stage_filter_limits_what_is_claimed(self, scope: dict[str, uuid.UUID]) -> None:
        # What lets a worker saturated on docpipeline still pick up parser work.
        driver = PostgresQueueDriver()
        await driver.enqueue(make_message(scope, PipelineStage.PARSER))
        await driver.enqueue(make_message(scope, PipelineStage.DOCPIPELINE))

        claimed = await driver.claim(worker_id="w1", limit=10, stages=[PipelineStage.DOCPIPELINE])
        assert [item.message.stage for item in claimed] == [PipelineStage.DOCPIPELINE]


# =============================================================================
# Completion, retry and death
# =============================================================================
class TestOutcomes:
    async def test_complete_marks_the_row_done(self, scope: dict[str, uuid.UUID]) -> None:
        driver = PostgresQueueDriver()
        await driver.enqueue(make_message(scope, PipelineStage.PARSER))
        claimed = await driver.claim(worker_id="w1", limit=1)

        await driver.complete(claimed[0].row_id)

        rows = await _rows(scope)
        assert rows[0].state is StageQueueState.DONE  # type: ignore[attr-defined]

    async def test_failure_reschedules_with_backoff(self, scope: dict[str, uuid.UUID]) -> None:
        driver = PostgresQueueDriver()
        await driver.enqueue(make_message(scope, PipelineStage.PARSER))
        claimed = await driver.claim(worker_id="w1", limit=1)

        retrying = await driver.fail(claimed[0].row_id, error={"message": "boom"})

        assert retrying is True
        row = (await _rows(scope))[0]
        assert row.state is StageQueueState.PENDING  # type: ignore[attr-defined]
        assert row.attempt == 2  # type: ignore[attr-defined]
        # Backed off, so it is not immediately claimable again - otherwise a
        # failing stage would spin as fast as the worker can poll.
        assert row.available_at > datetime.now(UTC)  # type: ignore[attr-defined]
        assert await driver.claim(worker_id="w1", limit=10) == []

    async def test_exhausted_attempts_land_in_dead(self, scope: dict[str, uuid.UUID]) -> None:
        from app.db.session import session_scope
        from app.models.queue import StageQueueEntry

        driver = PostgresQueueDriver()
        await driver.enqueue(make_message(scope, PipelineStage.PARSER))
        row_id = (await driver.claim(worker_id="w1", limit=1))[0].row_id

        # Jump to the last attempt rather than looping through the backoff.
        async with session_scope() as db:
            await db.execute(
                update(StageQueueEntry)
                .where(StageQueueEntry.id == row_id)
                .values(attempt=3, max_attempts=3)
            )

        retrying = await driver.fail(row_id, error={"message": "boom"})

        assert retrying is False
        assert (await _rows(scope))[0].state is StageQueueState.DEAD  # type: ignore[attr-defined]
        assert await driver.dlq_size() >= 1


# =============================================================================
# Lease recovery
# =============================================================================
class TestReclaim:
    async def test_a_stale_lease_returns_to_pending(self, scope: dict[str, uuid.UUID]) -> None:
        """A worker that dies mid-stage must not strand its row forever."""
        from app.db.session import session_scope
        from app.models.queue import StageQueueEntry

        driver = PostgresQueueDriver()
        await driver.enqueue(make_message(scope, PipelineStage.PARSER))
        row_id = (await driver.claim(worker_id="doomed", limit=1))[0].row_id

        # Age the lease rather than sleeping through it.
        async with session_scope() as db:
            await db.execute(
                update(StageQueueEntry)
                .where(StageQueueEntry.id == row_id)
                .values(claimed_at=datetime.now(UTC) - timedelta(hours=2))
            )

        assert await driver.reclaim_stale(lease_seconds=1800) == 1

        row = (await _rows(scope))[0]
        assert row.state is StageQueueState.PENDING  # type: ignore[attr-defined]
        assert row.claimed_by is None  # type: ignore[attr-defined]
        assert len(await driver.claim(worker_id="w2", limit=1)) == 1

    async def test_a_fresh_lease_is_left_alone(self, scope: dict[str, uuid.UUID]) -> None:
        driver = PostgresQueueDriver()
        await driver.enqueue(make_message(scope, PipelineStage.PARSER))
        await driver.claim(worker_id="busy", limit=1)

        assert await driver.reclaim_stale(lease_seconds=1800) == 0


# =============================================================================
# Stats
# =============================================================================
class TestStats:
    async def test_stats_report_waiting_and_active(self, scope: dict[str, uuid.UUID]) -> None:
        driver = PostgresQueueDriver()
        await driver.enqueue_many([make_message(scope, PipelineStage.PARSER) for _ in range(3)])
        await driver.claim(worker_id="w1", limit=1)

        by_queue = {item.queue: item for item in await driver.stats()}
        parser = by_queue[driver.queue_name(PipelineStage.PARSER)]

        assert parser.waiting >= 2
        assert parser.active >= 1

    async def test_a_backed_off_row_counts_as_delayed_not_waiting(
        self, scope: dict[str, uuid.UUID]
    ) -> None:
        """The Processing screen reads these two columns differently.

        A row held back by ``available_at`` - a retry backoff or an enqueue delay -
        is not claimable, so counting it as "waiting" shows a queue that looks
        stuck when it is deliberately paused. That is the reading an operator acts
        on, so the split is worth pinning.
        """
        driver = PostgresQueueDriver()
        await driver.enqueue(make_message(scope, PipelineStage.PARSER))
        await driver.enqueue(make_message(scope, PipelineStage.PARSER), delay_ms=60_000)

        parser = {item.queue: item for item in await driver.stats()}[
            driver.queue_name(PipelineStage.PARSER)
        ]

        assert parser.waiting == 1
        assert parser.delayed == 1

    async def test_health_is_true_against_a_live_database(self) -> None:
        assert await PostgresQueueDriver().health() is True

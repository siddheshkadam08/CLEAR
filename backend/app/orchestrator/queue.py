"""Queue client - the boundary between the Python orchestrator and the dispatcher.

Per the reconciliation in §1.2, BullMQ is a **logic-free dispatch layer**. Rather
than reimplement BullMQ's Lua-driven key structures in Python (fragile, and it
would duplicate queue semantics across two languages), Python enqueues by calling
the Node dispatcher's internal ``/enqueue`` endpoint. The Node worker then calls
back into ``POST /internal/stages/{stage}/run``. Both hops are trivial, so the
queue stays swappable.

Three drivers, all satisfying :class:`IQueueClient`:

* :class:`BullMQHttpDriver` - default. Delegates to the Node dispatcher.
* :class:`RedisListDriver` - pure-Python path over the same Redis, for a
  Node-free deployment (``QUEUE_DRIVER=arq``) and for local development.
* :class:`InlineDriver` - runs the stage immediately in-process. Tests only; it
  makes the pipeline synchronous and deterministic.

Enqueue failures are surfaced, never swallowed: a job that was never queued must
leave the contract visibly FAILED rather than sitting in QUEUED forever.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC
from typing import Any

import httpx
from sqlalchemy import func

from app.core import metrics
from app.core.config import get_settings
from app.core.enums import JobPriority, PipelineStage, StageQueueState
from app.core.errors import QueueError
from app.core.logging import get_logger
from app.core.telemetry import inject_context

logger = get_logger(__name__)


@dataclass(slots=True)
class StageMessage:
    """The queue payload. Deliberately minimal.

    Only identifiers travel through the queue - never document content or
    artifacts. The worker loads what it needs from the database and object
    storage, which keeps messages small, keeps them safe to log, and means a
    re-delivered message always reads current state rather than a stale snapshot.
    """

    job_id: uuid.UUID
    contract_id: uuid.UUID
    project_id: uuid.UUID
    stage: PipelineStage
    attempt: int = 1
    priority: JobPriority = JobPriority.NORMAL
    #: W3C traceparent, so a stage span minutes later joins the upload's trace.
    trace: dict[str, str] = field(default_factory=dict)
    #: Set when a reprocess should continue through subsequent stages.
    continue_pipeline: bool = True
    #: Free-form stage options (e.g. force re-extraction of one category).
    options: dict[str, Any] = field(default_factory=dict)
    #: Identifies one *dispatch decision*, and is what makes the queue's
    #: deduplication mean the right thing.
    #:
    #: BullMQ keys a job on `(job, stage, attempt)` so that a duplicate HTTP
    #: delivery of the same enqueue collapses onto one queued job instead of
    #: running the stage twice - which is correct. But completed jobs are
    #: retained, so that key also collapsed a *deliberate* re-run onto the run
    #: that already finished: a reprocess, or the Retry button, logged
    #: `stage_enqueued` and then silently did nothing.
    #:
    #: Generated once per message. A driver retrying the same payload sends the
    #: same id and still collapses; asking for the stage again builds a new
    #: message, so it runs.
    dispatch_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["job_id"] = str(self.job_id)
        data["contract_id"] = str(self.contract_id)
        data["project_id"] = str(self.project_id)
        data["stage"] = self.stage.value
        data["priority"] = self.priority.value
        return data

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> StageMessage:
        return cls(
            job_id=uuid.UUID(str(payload["job_id"])),
            contract_id=uuid.UUID(str(payload["contract_id"])),
            project_id=uuid.UUID(str(payload["project_id"])),
            stage=PipelineStage(payload["stage"]),
            attempt=int(payload.get("attempt", 1)),
            priority=JobPriority(payload.get("priority", JobPriority.NORMAL.value)),
            trace=dict(payload.get("trace") or {}),
            continue_pipeline=bool(payload.get("continue_pipeline", True)),
            options=dict(payload.get("options") or {}),
            # Absent on a message enqueued before this field existed; a fresh id
            # is the safe reading, since that message is being handled once.
            dispatch_id=str(payload.get("dispatch_id") or uuid.uuid4().hex[:12]),
        )


@dataclass(slots=True)
class QueueStats:
    queue: str
    waiting: int = 0
    active: int = 0
    completed: int = 0
    failed: int = 0
    delayed: int = 0


@dataclass(slots=True)
class ClaimedStage:
    """One leased row, as the worker loop sees it.

    ``row_id`` is the queue row, not the job: completing or failing addresses the
    lease, while everything inside ``message`` addresses the pipeline.
    """

    row_id: uuid.UUID
    message: StageMessage
    attempt: int
    max_attempts: int


class IQueueClient(ABC):
    """Dispatch contract. Implementations must be safe to call concurrently."""

    driver: str = "abstract"

    @abstractmethod
    async def enqueue(self, message: StageMessage, *, delay_ms: int = 0, db: Any = None) -> str:
        """Queue one stage execution. Returns the queue's job id.

        ``db`` is an open session to enqueue *within*, for callers that create the
        job and queue it in one transaction. Only a database-backed driver can
        honour it; the broker drivers ignore it, because a broker cannot take part
        in a Postgres transaction whatever it is passed.

        Passing it where one is open is not optional for those callers. The queue
        row carries a foreign key to ``processing_jobs``, and a driver opening its
        own session cannot see a job the caller has only flushed - the insert fails
        on the foreign key and the upload is rejected having already stored the file.
        """

    @abstractmethod
    async def stats(self) -> list[QueueStats]:
        """Per-queue depths, for monitoring and the admin screen."""

    @abstractmethod
    async def dlq_size(self) -> int:
        """Jobs that exhausted their retries."""

    async def enqueue_many(self, messages: list[StageMessage], *, db: Any = None) -> list[str]:
        """Queue several stages. Overridden where the driver supports batching."""
        return [await self.enqueue(message, db=db) for message in messages]

    async def health(self) -> bool:
        try:
            await self.stats()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        return None

    @staticmethod
    def queue_name(stage: PipelineStage) -> str:
        settings = get_settings()
        return f"{settings.queue.prefix}:{stage.value}"


# =============================================================================
# BullMQ via the Node dispatcher
# =============================================================================
class BullMQHttpDriver(IQueueClient):
    """Enqueue through the Node dispatcher's internal HTTP endpoint."""

    driver = "bullmq"

    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.queue.dispatcher_url.rstrip("/")
        self.token = settings.security.internal_api_token
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(10.0, connect=3.0),
                headers={"X-Internal-Token": self.token},
                # The dispatcher is a single in-cluster service; a small pool is
                # plenty and keeps connection churn down under burst uploads.
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            )
        return self._client

    async def enqueue(self, message: StageMessage, *, delay_ms: int = 0, db: Any = None) -> str:
        # `db` ignored: a broker cannot join a Postgres transaction.
        payload = message.to_payload()
        payload["trace"] = inject_context(dict(message.trace))
        payload["delay_ms"] = delay_ms

        queue = self.queue_name(message.stage)
        try:
            client = await self._http()
            response = await client.post("/enqueue", json=payload)
            if response.status_code >= 400:
                raise QueueError(
                    "The dispatcher rejected the job.",
                    details={
                        "status": response.status_code,
                        "body": response.text[:400],
                        "queue": queue,
                    },
                )
            queue_job_id = str(response.json().get("job_id") or "")
        except httpx.HTTPError as exc:
            metrics.queue_enqueue_failures_total.labels(queue=queue).inc()
            logger.error(
                "enqueue_failed",
                queue=queue,
                job_id=str(message.job_id),
                stage=message.stage.value,
                error=str(exc),
            )
            raise QueueError(f"Could not reach the job dispatcher: {exc}") from exc

        metrics.queue_enqueued_total.labels(queue=queue, priority=message.priority.value).inc()
        logger.info(
            "stage_enqueued",
            queue=queue,
            job_id=str(message.job_id),
            stage=message.stage.value,
            attempt=message.attempt,
            queue_job_id=queue_job_id,
        )
        return queue_job_id

    async def enqueue_many(self, messages: list[StageMessage], *, db: Any = None) -> list[str]:
        """Batch enqueue - one HTTP round trip for a whole upload."""
        if not messages:
            return []
        payloads = []
        for message in messages:
            payload = message.to_payload()
            payload["trace"] = inject_context(dict(message.trace))
            payloads.append(payload)

        try:
            client = await self._http()
            response = await client.post("/enqueue/bulk", json={"jobs": payloads})
            if response.status_code >= 400:
                raise QueueError(
                    "The dispatcher rejected the batch.",
                    details={"status": response.status_code, "body": response.text[:400]},
                )
            ids = [str(item) for item in response.json().get("job_ids", [])]
        except httpx.HTTPError as exc:
            for message in messages:
                metrics.queue_enqueue_failures_total.labels(
                    queue=self.queue_name(message.stage)
                ).inc()
            raise QueueError(f"Could not reach the job dispatcher: {exc}") from exc

        for message in messages:
            metrics.queue_enqueued_total.labels(
                queue=self.queue_name(message.stage), priority=message.priority.value
            ).inc()
        logger.info("stage_batch_enqueued", count=len(messages))
        return ids

    async def stats(self) -> list[QueueStats]:
        try:
            client = await self._http()
            response = await client.get("/queues")
            response.raise_for_status()
            return [
                QueueStats(
                    # The dispatcher's field is `queue`; see queue/README.md for the
                    # response shape. `name` is accepted as a fallback so an older
                    # dispatcher image does not blank out the admin screen.
                    queue=str(item.get("queue") or item.get("name") or ""),
                    waiting=int(item.get("waiting", 0)),
                    active=int(item.get("active", 0)),
                    completed=int(item.get("completed", 0)),
                    failed=int(item.get("failed", 0)),
                    delayed=int(item.get("delayed", 0)),
                )
                for item in response.json().get("queues", [])
            ]
        except httpx.HTTPError as exc:
            logger.warning("queue_stats_unavailable", error=str(exc))
            return []

    async def dlq_size(self) -> int:
        try:
            client = await self._http()
            response = await client.get("/dlq/size")
            response.raise_for_status()
            return int(response.json().get("size", 0))
        except httpx.HTTPError:
            # A dead-letter count is informational; failing the health endpoint over
            # it would hide the parts that did answer.
            return 0

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# =============================================================================
# Pure-Python driver
# =============================================================================
class RedisListDriver(IQueueClient):
    """Redis list queue with a sorted-set delay tier.

    The Node-free path. Priority is honoured by pushing high-priority work to the
    head of the list, which is a coarser guarantee than BullMQ's ordering but is
    sufficient because stages are independent and idempotent.
    """

    driver = "redis_list"

    def __init__(self) -> None:
        settings = get_settings()
        self.prefix = settings.queue.prefix
        self.dlq = f"{self.prefix}:{settings.queue.dlq_name}"

    def _list_key(self, stage: PipelineStage) -> str:
        return f"{self.prefix}:queue:{stage.value}"

    def _delayed_key(self) -> str:
        return f"{self.prefix}:delayed"

    async def enqueue(self, message: StageMessage, *, delay_ms: int = 0, db: Any = None) -> str:
        # `db` ignored: Redis cannot join a Postgres transaction.
        import orjson

        from app.core.cache import get_queue_redis

        payload = message.to_payload()
        payload["trace"] = inject_context(dict(message.trace))
        queue_job_id = uuid.uuid4().hex
        payload["queue_job_id"] = queue_job_id
        encoded = orjson.dumps(payload)
        queue = self.queue_name(message.stage)

        try:
            redis = await get_queue_redis()
            if delay_ms > 0:
                import time

                ready_at = time.time() + (delay_ms / 1000.0)
                await redis.zadd(
                    self._delayed_key(),
                    {
                        orjson.dumps(
                            {"queue": self._list_key(message.stage), "payload": payload}
                        ).decode(): ready_at
                    },
                )
            elif message.priority is JobPriority.HIGH:
                # High priority jumps the queue: workers pop from the tail.
                await redis.rpush(self._list_key(message.stage), encoded.decode())
            else:
                await redis.lpush(self._list_key(message.stage), encoded.decode())
        except Exception as exc:
            metrics.queue_enqueue_failures_total.labels(queue=queue).inc()
            raise QueueError(f"Could not enqueue to Redis: {exc}") from exc

        metrics.queue_enqueued_total.labels(queue=queue, priority=message.priority.value).inc()
        logger.info(
            "stage_enqueued",
            queue=queue,
            job_id=str(message.job_id),
            stage=message.stage.value,
            driver=self.driver,
        )
        return queue_job_id

    async def stats(self) -> list[QueueStats]:
        from app.core.cache import get_queue_redis

        redis = await get_queue_redis()
        results: list[QueueStats] = []
        for stage in PipelineStage:
            depth = await redis.llen(self._list_key(stage))
            results.append(QueueStats(queue=self.queue_name(stage), waiting=int(depth)))
        return results

    async def dlq_size(self) -> int:
        from app.core.cache import get_queue_redis

        redis = await get_queue_redis()
        return int(await redis.llen(self.dlq))

    async def send_to_dlq(self, message: StageMessage, error: dict[str, Any]) -> None:
        """Park an exhausted job for operator attention."""
        import orjson

        from app.core.cache import get_queue_redis

        redis = await get_queue_redis()
        await redis.lpush(
            self.dlq,
            orjson.dumps({"payload": message.to_payload(), "error": error}).decode(),
        )
        logger.error(
            "job_sent_to_dlq",
            job_id=str(message.job_id),
            stage=message.stage.value,
            attempt=message.attempt,
        )

    async def pop(
        self,
        stage: PipelineStage,
        *,
        timeout: int = 5,  # noqa: ASYNC109 - Redis BRPOP timeout, see below
    ) -> StageMessage | None:
        """Blocking pop - used by the pure-Python worker runner.

        The timeout is a parameter rather than an ``asyncio.timeout`` at the call
        site because it is Redis's own ``BRPOP`` timeout: the server returns nil when
        it expires, leaving the connection healthy. Cancelling the task instead would
        abandon a connection mid-command and force a reconnect on every idle poll.
        """
        import orjson

        from app.core.cache import get_queue_redis

        redis = await get_queue_redis()
        result = await redis.brpop([self._list_key(stage)], timeout=timeout)
        if result is None:
            return None
        _, raw = result
        return StageMessage.from_payload(orjson.loads(raw))

    async def promote_delayed(self) -> int:
        """Move due delayed jobs onto their queues. Called by the scheduler."""
        import time

        import orjson

        from app.core.cache import get_queue_redis

        redis = await get_queue_redis()
        now = time.time()
        due = await redis.zrangebyscore(self._delayed_key(), 0, now, start=0, num=200)
        promoted = 0
        for entry in due:
            record = orjson.loads(entry)
            await redis.lpush(record["queue"], orjson.dumps(record["payload"]).decode())
            await redis.zrem(self._delayed_key(), entry)
            promoted += 1
        return promoted


# =============================================================================
# Postgres driver
# =============================================================================
class PostgresQueueDriver(IQueueClient):
    """Queue in the same database the pipeline already writes to.

    The broker-free path. ``SELECT ... FOR UPDATE SKIP LOCKED`` is what makes a
    table viable here: several workers select the same rows and Postgres hands
    each of them a disjoint set rather than serialising them behind one another.

    Enqueue runs in its own short session. That is not incidental - the runner
    calls this *after* the stage transaction has committed, precisely so a worker
    cannot start the next stage before the previous one's checkpoint is durable.
    Joining the caller's transaction would undo that guarantee.

    ``delay_ms`` and retry backoff are the same column, ``available_at``. A
    delayed job and a backed-off job are indistinguishable to the claim query, so
    unlike the Redis driver there is no separate delay tier for the scheduler to
    promote.
    """

    driver = "postgres"

    def __init__(self) -> None:
        settings = get_settings()
        self.max_attempts = settings.queue.max_attempts

    async def enqueue(self, message: StageMessage, *, delay_ms: int = 0, db: Any = None) -> str:
        ids = await self._insert([(message, delay_ms)], db=db)
        return ids[0] if ids else ""

    async def enqueue_many(self, messages: list[StageMessage], *, db: Any = None) -> list[str]:
        if not messages:
            return []
        return await self._insert([(message, 0) for message in messages], db=db)

    async def _insert(self, items: list[tuple[StageMessage, int]], *, db: Any = None) -> list[str]:
        from datetime import datetime, timedelta

        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from app.db.session import session_scope
        from app.models.queue import StageQueueEntry

        now = datetime.now(UTC)
        rows = []
        for message, delay_ms in items:
            payload = message.to_payload()
            payload["trace"] = inject_context(dict(message.trace))
            rows.append(
                {
                    "dispatch_id": message.dispatch_id,
                    "job_id": message.job_id,
                    "contract_id": message.contract_id,
                    "project_id": message.project_id,
                    "stage": message.stage,
                    "priority": message.priority,
                    "payload": payload,
                    "attempt": message.attempt,
                    "max_attempts": self.max_attempts,
                    "state": StageQueueState.PENDING,
                    "available_at": now + timedelta(milliseconds=delay_ms),
                }
            )

        # ON CONFLICT DO NOTHING is the deduplication. A driver retrying the same
        # payload sends the same dispatch_id and collapses onto the existing row;
        # asking for the stage again builds a new message with a new id, so a
        # deliberate re-run still runs.
        statement = (
            pg_insert(StageQueueEntry)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["dispatch_id"])
            .returning(StageQueueEntry.id)
        )

        try:
            if db is not None:
                # The caller's transaction. Deliberately not committed here - the
                # caller owns it, and that is the point: the queue row and the job
                # row it references commit together or not at all. A broker cannot
                # offer that, so a rolled-back upload leaves BullMQ holding a job
                # for a contract that never existed.
                result = await db.execute(statement)
                inserted = [str(row[0]) for row in result.all()]
            else:
                async with session_scope() as own:
                    result = await own.execute(statement)
                    inserted = [str(row[0]) for row in result.all()]
        except Exception as exc:
            for message, _ in items:
                metrics.queue_enqueue_failures_total.labels(
                    queue=self.queue_name(message.stage)
                ).inc()
            raise QueueError(f"Could not enqueue to Postgres: {exc}") from exc

        for message, _ in items:
            metrics.queue_enqueued_total.labels(
                queue=self.queue_name(message.stage), priority=message.priority.value
            ).inc()
            logger.info(
                "stage_enqueued",
                queue=self.queue_name(message.stage),
                job_id=str(message.job_id),
                stage=message.stage.value,
                attempt=message.attempt,
                driver=self.driver,
            )

        if len(inserted) < len(items):
            # Not an error: this is deduplication doing its job. Logged because a
            # burst of it means something is re-delivering, which is worth seeing.
            logger.info(
                "stage_enqueue_deduplicated",
                requested=len(items),
                inserted=len(inserted),
            )
        return inserted

    async def stats(self) -> list[QueueStats]:
        from sqlalchemy import case, func, select

        from app.db.session import session_scope
        from app.models.queue import StageQueueEntry

        # `pending` splits into two things an operator reads differently: rows that
        # are claimable now, and rows held back by `available_at` - a retry backoff
        # or an enqueue delay. Reporting both as "waiting" would show a queue that
        # looks stuck while it is in fact deliberately paused, so the distinction
        # is made here rather than left for someone to query by hand.
        delayed = case((StageQueueEntry.available_at > func.now(), 1), else_=0)

        async with session_scope() as db:
            result = await db.execute(
                select(
                    StageQueueEntry.stage,
                    StageQueueEntry.state,
                    func.count().label("count"),
                    func.coalesce(func.sum(delayed), 0).label("delayed"),
                ).group_by(StageQueueEntry.stage, StageQueueEntry.state)
            )
            counts: dict[PipelineStage, dict[StageQueueState, tuple[int, int]]] = {}
            for stage, state, count, held in result.all():
                counts.setdefault(stage, {})[state] = (int(count), int(held))

        stats: list[QueueStats] = []
        for stage, by_state in sorted(counts.items(), key=lambda item: item[0].value):
            pending, held = by_state.get(StageQueueState.PENDING, (0, 0))
            stats.append(
                QueueStats(
                    queue=self.queue_name(stage),
                    waiting=pending - held,
                    delayed=held,
                    active=by_state.get(StageQueueState.CLAIMED, (0, 0))[0],
                    completed=by_state.get(StageQueueState.DONE, (0, 0))[0],
                    failed=by_state.get(StageQueueState.DEAD, (0, 0))[0],
                )
            )
        return stats

    async def dlq_size(self) -> int:
        from sqlalchemy import func, select

        from app.db.session import session_scope
        from app.models.queue import StageQueueEntry

        async with session_scope() as db:
            result = await db.execute(
                select(func.count())
                .select_from(StageQueueEntry)
                .where(StageQueueEntry.state == StageQueueState.DEAD)
            )
            return int(result.scalar_one())

    async def health(self) -> bool:
        from sqlalchemy import text as sa_text

        from app.db.session import session_scope

        try:
            async with session_scope() as db:
                await db.execute(sa_text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- worker-facing ----------------------------------------------------
    # Beyond IQueueClient, like RedisListDriver's pop/promote_delayed. Only the
    # worker loop and the scheduler sweep call these.

    async def claim(
        self,
        *,
        worker_id: str,
        limit: int,
        stages: Sequence[PipelineStage] | None = None,
    ) -> list[ClaimedStage]:
        """Take up to ``limit`` rows, marking them claimed.

        ``FOR UPDATE SKIP LOCKED`` is the whole trick: two workers running this
        at the same moment lock disjoint rows and neither waits for the other.

        **This commits before the caller runs anything.** A stage can take
        minutes, and holding the claim transaction open across it would pin a
        connection for the duration and serialise every other worker behind the
        row lock - which is exactly what SKIP LOCKED exists to avoid. The claim
        is therefore a lease, recovered by :meth:`reclaim_stale` if the worker
        dies holding it.

        ``stages`` lets a caller claim only what it has spare capacity for, so a
        worker saturated on ``docpipeline`` can still pick up parser work.
        """
        from sqlalchemy import select, update

        from app.db.session import session_scope
        from app.models.queue import StageQueueEntry

        if limit <= 0:
            return []

        candidates = (
            select(StageQueueEntry.id)
            .where(
                StageQueueEntry.state == StageQueueState.PENDING,
                StageQueueEntry.available_at <= func.now(),
            )
            .order_by(StageQueueEntry.priority, StageQueueEntry.available_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        if stages:
            candidates = candidates.where(StageQueueEntry.stage.in_(list(stages)))

        # A CTE, not `WHERE id IN (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED)`.
        #
        # The obvious IN form is wrong, and wrong quietly: Postgres pulls the
        # subquery up into a semi-join and re-evaluates it per candidate row, so
        # the LIMIT stops bounding the UPDATE. Measured against this schema, a
        # claim of `limit=1` over three pending rows updated all three - one
        # worker taking the entire queue every poll.
        #
        # `FOR UPDATE` forces the CTE to materialise, which restores the fence:
        # the inner SELECT runs exactly once, locks at most `limit` rows, and the
        # UPDATE joins against that fixed set.
        claim_cte = candidates.cte("claimable")

        async with session_scope() as db:
            result = await db.execute(
                update(StageQueueEntry)
                .where(StageQueueEntry.id == claim_cte.c.id)
                .values(
                    state=StageQueueState.CLAIMED,
                    claimed_at=func.now(),
                    claimed_by=worker_id[:128],
                )
                .returning(
                    StageQueueEntry.id,
                    StageQueueEntry.payload,
                    StageQueueEntry.attempt,
                    StageQueueEntry.max_attempts,
                ),
                # Plain SQL, not an ORM-synchronised UPDATE. Two reasons, both
                # load-bearing: the ORM path tries to build identity keys from
                # RETURNING and raises `unhashable type: 'dict'` on the JSONB
                # payload, and its 'fetch' strategy re-evaluates the WHERE
                # clause - which re-runs the LIMIT subquery and claims more rows
                # than were asked for.
                execution_options={"synchronize_session": False},
            )
            rows = result.all()

        claimed = []
        for row_id, payload, attempt, max_attempts in rows:
            message = StageMessage.from_payload(payload)
            message.attempt = attempt
            claimed.append(
                ClaimedStage(
                    row_id=row_id,
                    message=message,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
            )
        return claimed

    async def complete(self, row_id: uuid.UUID) -> None:
        """Mark a claimed row done."""
        from sqlalchemy import update

        from app.db.session import session_scope
        from app.models.queue import StageQueueEntry

        async with session_scope() as db:
            await db.execute(
                update(StageQueueEntry)
                .where(StageQueueEntry.id == row_id)
                .values(state=StageQueueState.DONE, claimed_at=None, claimed_by=None)
            )

    async def fail(self, row_id: uuid.UUID, *, error: dict[str, Any]) -> bool:
        """Reschedule a failed row, or retire it once attempts are spent.

        Returns ``True`` when it will be retried. Backoff is exponential from
        ``QUEUE_BACKOFF_MS`` and lands in ``available_at``, so a backed-off row is
        indistinguishable from a delayed one to the claim query.
        """
        from datetime import datetime, timedelta

        from sqlalchemy import select, update

        from app.db.session import session_scope
        from app.models.queue import StageQueueEntry

        async with session_scope() as db:
            current = (
                await db.execute(
                    select(StageQueueEntry.attempt, StageQueueEntry.max_attempts).where(
                        StageQueueEntry.id == row_id
                    )
                )
            ).first()
            if current is None:
                return False
            attempt, max_attempts = current
            retrying = attempt < max_attempts

            if retrying:
                backoff_ms = get_settings().queue.backoff_ms * (2 ** (attempt - 1))
                values: dict[str, Any] = {
                    "state": StageQueueState.PENDING,
                    "attempt": attempt + 1,
                    "available_at": datetime.now(UTC) + timedelta(milliseconds=backoff_ms),
                }
            else:
                values = {"state": StageQueueState.DEAD}

            values.update(claimed_at=None, claimed_by=None, last_error=error)
            await db.execute(
                update(StageQueueEntry).where(StageQueueEntry.id == row_id).values(**values)
            )

        if not retrying:
            logger.error("stage_queue_row_dead", row_id=str(row_id), attempts=attempt)
        return retrying

    async def reclaim_stale(self, *, lease_seconds: int) -> int:
        """Return leases whose worker stopped reporting.

        A worker that dies mid-stage leaves its row ``claimed`` forever; nothing
        else would ever pick it up. Rows with attempts left go back to
        ``pending``, the rest to ``dead`` - visible, rather than silently retried
        into the same crash.
        """
        from sqlalchemy import case, cast, update

        from app.db.session import session_scope
        from app.models.queue import StageQueueEntry

        cutoff = func.now() - func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds)

        async with session_scope() as db:
            result = await db.execute(
                update(StageQueueEntry)
                .where(
                    StageQueueEntry.state == StageQueueState.CLAIMED,
                    StageQueueEntry.claimed_at < cutoff,
                )
                .values(
                    # Cast explicitly: a CASE over string literals is typed
                    # VARCHAR, and Postgres will not coerce that into a native
                    # enum column on its own.
                    state=cast(
                        case(
                            (
                                StageQueueEntry.attempt < StageQueueEntry.max_attempts,
                                StageQueueState.PENDING.value,
                            ),
                            else_=StageQueueState.DEAD.value,
                        ),
                        StageQueueEntry.state.type,
                    ),
                    attempt=StageQueueEntry.attempt + 1,
                    available_at=func.now(),
                    claimed_at=None,
                    claimed_by=None,
                )
                .returning(StageQueueEntry.id)
            )
            reclaimed = len(result.all())

        if reclaimed:
            logger.warning("stage_queue_leases_reclaimed", count=reclaimed)
        return reclaimed


# =============================================================================
# Inline driver (tests)
# =============================================================================
class InlineDriver(IQueueClient):
    """Execute the stage immediately, in-process.

    Makes the pipeline synchronous so an integration test can upload a document and
    assert on the finished result without a queue or worker. Never for production -
    a slow parse would block the HTTP request that triggered it.
    """

    driver = "inline"

    def __init__(self) -> None:
        self.executed: list[StageMessage] = []

    async def enqueue(self, message: StageMessage, *, delay_ms: int = 0, db: Any = None) -> str:
        # `db` ignored: the stage runs in-process and opens its own session.
        self.executed.append(message)
        queue_job_id = uuid.uuid4().hex

        from app.orchestrator.runner import run_stage

        logger.info(
            "stage_running_inline",
            job_id=str(message.job_id),
            stage=message.stage.value,
        )
        # The runner dispatches the next stage through this same driver, so the
        # pipeline walks itself recursively - bounded by the eight stages plus the
        # retry limit, so it always terminates.
        await run_stage(message)
        return queue_job_id

    async def stats(self) -> list[QueueStats]:
        return []

    async def dlq_size(self) -> int:
        return 0


# =============================================================================
# Factory
# =============================================================================
_client: IQueueClient | None = None


def get_queue_client() -> IQueueClient:
    """The configured dispatch client (one per process)."""
    global _client
    if _client is not None:
        return _client

    settings = get_settings()
    driver = settings.queue.driver

    if settings.is_testing:
        _client = InlineDriver()
    elif driver == "bullmq":
        _client = BullMQHttpDriver()
    elif driver == "postgres":
        _client = PostgresQueueDriver()
    else:
        _client = RedisListDriver()

    logger.info("queue_client_initialised", driver=_client.driver)
    return _client


def set_queue_client(client: IQueueClient | None) -> None:
    """Override the client. Used by tests to inject a recording driver."""
    global _client
    _client = client


async def close_queue_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
    _client = None


__all__ = [
    "BullMQHttpDriver",
    "ClaimedStage",
    "IQueueClient",
    "InlineDriver",
    "PostgresQueueDriver",
    "QueueStats",
    "RedisListDriver",
    "StageMessage",
    "close_queue_client",
    "get_queue_client",
    "set_queue_client",
]

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
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from app.core import metrics
from app.core.config import get_settings
from app.core.enums import JobPriority, PipelineStage
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


class IQueueClient(ABC):
    """Dispatch contract. Implementations must be safe to call concurrently."""

    driver: str = "abstract"

    @abstractmethod
    async def enqueue(self, message: StageMessage, *, delay_ms: int = 0) -> str:
        """Queue one stage execution. Returns the queue's job id."""

    @abstractmethod
    async def stats(self) -> list[QueueStats]:
        """Per-queue depths, for monitoring and the admin screen."""

    @abstractmethod
    async def dlq_size(self) -> int:
        """Jobs that exhausted their retries."""

    async def enqueue_many(self, messages: list[StageMessage]) -> list[str]:
        """Queue several stages. Overridden where the driver supports batching."""
        return [await self.enqueue(message) for message in messages]

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

    async def enqueue(self, message: StageMessage, *, delay_ms: int = 0) -> str:
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

    async def enqueue_many(self, messages: list[StageMessage]) -> list[str]:
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

    async def enqueue(self, message: StageMessage, *, delay_ms: int = 0) -> str:
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

    async def enqueue(self, message: StageMessage, *, delay_ms: int = 0) -> str:
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
    "IQueueClient",
    "InlineDriver",
    "QueueStats",
    "RedisListDriver",
    "StageMessage",
    "close_queue_client",
    "get_queue_client",
    "set_queue_client",
]

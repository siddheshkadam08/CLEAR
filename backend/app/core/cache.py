"""Redis client and cache helpers.

Two logical databases on one server: DB 0 is the BullMQ queue backend, DB 1 is
the application cache. Separating them means a ``FLUSHDB`` on the cache cannot
destroy queued jobs.

Cache policy
------------
Every key is namespaced and, where it holds contract-derived data, includes the
``project_id`` - so a cached value can never be served across the project
boundary. Invalidation is by prefix scan on write.

All operations fail open: a Redis outage degrades the platform to
"uncached but correct", never to "down".
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

import orjson

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import cache_operations_total

logger = get_logger(__name__)

T = TypeVar("T")

_redis: Any | None = None
_queue_redis: Any | None = None


# =============================================================================
# Clients
# =============================================================================
async def get_redis() -> Any:
    """Shared async Redis client for the cache/session/rate-limit database."""
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis

        settings = get_settings()
        _redis = aioredis.from_url(
            settings.redis.cache_url,
            encoding="utf-8",
            decode_responses=False,  # values are orjson bytes
            max_connections=settings.redis.max_connections,
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=True,
            health_check_interval=30,
        )
    return _redis


async def get_queue_redis() -> Any:
    """Client for the queue database - used to read BullMQ queue depths."""
    global _queue_redis
    if _queue_redis is None:
        import redis.asyncio as aioredis

        settings = get_settings()
        _queue_redis = aioredis.from_url(
            str(settings.redis.url),
            encoding="utf-8",
            decode_responses=True,
            max_connections=16,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
    return _queue_redis


async def close_redis() -> None:
    """Release both pools on shutdown."""
    global _redis, _queue_redis
    for client in (_redis, _queue_redis):
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("redis_close_failed", error=str(exc))
    _redis = None
    _queue_redis = None


async def redis_healthy() -> bool:
    try:
        redis = await get_redis()
        return bool(await redis.ping())
    except Exception:  # noqa: BLE001
        return False


# =============================================================================
# Key building
# =============================================================================
class CacheNamespace:
    """Key prefixes. Grouped so invalidation can target a whole family."""

    DASHBOARD = "dashboard"
    SEARCH = "search"
    RETRIEVAL = "retrieval"
    RAG = "rag"
    CONTRACT = "contract"
    PROJECT = "project"
    PROFILE = "profile"
    CLAUSE_MASTER = "clause_master"
    PERMISSIONS = "permissions"
    QUEUE_STATS = "queue_stats"
    ALERTS = "alerts"
    EMBEDDING = "embedding"


def make_key(namespace: str, *parts: Any, project_id: Any = None) -> str:
    """Build a namespaced cache key.

    ``project_id`` is placed immediately after the namespace so a prefix scan can
    evict exactly one project's cached data, and so no key is ambiguous across
    projects.
    """
    segments = [namespace]
    if project_id is not None:
        segments.append(f"p:{project_id}")
    segments.extend(str(part) for part in parts if part is not None)
    key = ":".join(segments)
    # Long keys (a search query plus filters) are digested to stay bounded.
    if len(key) > 200:
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        key = f"{namespace}:{'p:' + str(project_id) + ':' if project_id else ''}h:{digest}"
    return key


def hash_payload(payload: Any) -> str:
    """Stable digest of an arbitrary payload - used for query cache keys."""
    encoded = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(encoded).hexdigest()[:32]


# =============================================================================
# Availability
# =============================================================================
#: Consecutive failures before the cache is treated as down.
#:
#: One. A cache is an optimisation, so the cost of being wrong in each direction is
#: wildly asymmetric: backing off unnecessarily costs a few uncached reads, while
#: confirming the outage costs another full connect timeout on every request that
#: arrives in the meantime. There is nothing to gain from a second opinion.
_BREAKER_THRESHOLD = 1
#: How long to skip Redis entirely once it is. Short enough that recovery is
#: picked up promptly, long enough that the retry is not on every request.
_BREAKER_COOLDOWN_SECONDS = 30.0

_failures = 0
_open_until = 0.0


def _breaker_open() -> bool:
    """True while Redis is presumed down and calls should be skipped.

    Failing open is correct, but failing open *slowly* is not: the client has a
    three-second connect timeout, so an unreachable Redis was adding three seconds
    to every cache read and another three to every write. On the Copilot path -
    which now reads the cache for the query classification and again for the query
    embedding - that is twelve seconds of pure latency added to a question that
    would otherwise have been answered normally, for a component whose entire
    purpose is to make things faster.
    """
    return _open_until > time.monotonic()


def _record_failure(cache_name: str) -> None:
    global _failures, _open_until
    _failures += 1
    cache_operations_total.labels(cache=cache_name, outcome="error").inc()
    if _failures >= _BREAKER_THRESHOLD and not _breaker_open():
        _open_until = time.monotonic() + _BREAKER_COOLDOWN_SECONDS
        logger.warning(
            "cache_unavailable",
            failures=_failures,
            cooldown_seconds=_BREAKER_COOLDOWN_SECONDS,
            detail="skipping the cache; the platform stays correct but uncached",
        )


def _record_success() -> None:
    global _failures
    if _failures:
        _failures = 0


def reset_cache_breaker() -> None:
    """Clear the breaker. For tests, and for a manual recovery poke."""
    global _failures, _open_until
    _failures = 0
    _open_until = 0.0


# =============================================================================
# Operations
# =============================================================================
async def cache_get(key: str, *, cache_name: str = "default") -> Any | None:
    if _breaker_open():
        cache_operations_total.labels(cache=cache_name, outcome="skipped").inc()
        return None
    try:
        redis = await get_redis()
        raw = await redis.get(key)
        _record_success()
        if raw is None:
            cache_operations_total.labels(cache=cache_name, outcome="miss").inc()
            return None
        cache_operations_total.labels(cache=cache_name, outcome="hit").inc()
        return orjson.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.debug("cache_get_failed", key=key, error=str(exc))
        _record_failure(cache_name)
        return None


async def cache_set(
    key: str,
    value: Any,
    *,
    ttl: int | None = None,
    cache_name: str = "default",
) -> None:
    if _breaker_open():
        cache_operations_total.labels(cache=cache_name, outcome="skipped").inc()
        return
    try:
        redis = await get_redis()
        ttl = ttl if ttl is not None else get_settings().redis.cache_ttl_seconds
        payload = orjson.dumps(value, default=_json_default)
        if ttl > 0:
            await redis.setex(key, ttl, payload)
        else:
            await redis.set(key, payload)
        _record_success()
    except Exception as exc:  # noqa: BLE001
        logger.debug("cache_set_failed", key=key, error=str(exc))
        _record_failure(cache_name)


async def cache_delete(*keys: str) -> None:
    if not keys:
        return
    try:
        redis = await get_redis()
        await redis.delete(*keys)
    except Exception as exc:  # noqa: BLE001
        logger.debug("cache_delete_failed", error=str(exc))


async def cache_invalidate_prefix(prefix: str) -> int:
    """Delete every key under ``prefix``.

    Uses ``SCAN`` in batches rather than ``KEYS`` so a large keyspace does not
    block the Redis event loop.
    """
    deleted = 0
    try:
        redis = await get_redis()
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor=cursor, match=f"{prefix}*", count=500)
            if keys:
                deleted += await redis.delete(*keys)
            if cursor == 0:
                break
    except Exception as exc:  # noqa: BLE001
        logger.debug("cache_invalidate_failed", prefix=prefix, error=str(exc))
    return deleted


async def invalidate_project_cache(project_id: Any) -> None:
    """Evict every cached derivation of a project's data.

    Called after ingestion completes, after a human review decision, and after a
    contract is deleted - anything that changes what a dashboard or search would
    return.
    """
    for namespace in (
        CacheNamespace.DASHBOARD,
        CacheNamespace.SEARCH,
        CacheNamespace.RETRIEVAL,
        CacheNamespace.RAG,
        CacheNamespace.CONTRACT,
        CacheNamespace.ALERTS,
    ):
        await cache_invalidate_prefix(f"{namespace}:p:{project_id}")
    # Application-wide aggregates include this project's numbers.
    await cache_invalidate_prefix(f"{CacheNamespace.DASHBOARD}:all")


async def cached(
    key: str,
    factory: Callable[[], Awaitable[T]],
    *,
    ttl: int | None = None,
    cache_name: str = "default",
) -> T:
    """Read-through cache.

    >>> await cached(make_key("dashboard", "overall", project_id=pid), build_kpis)
    """
    hit = await cache_get(key, cache_name=cache_name)
    if hit is not None:
        return cast(T, hit)
    value = await factory()
    await cache_set(key, value, ttl=ttl, cache_name=cache_name)
    return value


def _json_default(value: Any) -> Any:
    """orjson fallback for UUID/datetime/Decimal/Pydantic/enum values."""
    import datetime
    import decimal
    import enum
    import uuid

    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, set | frozenset):
        return sorted(str(v) for v in value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"Cannot serialise {type(value).__name__} for cache")


# =============================================================================
# Distributed lock
# =============================================================================
class RedisLock:
    """Best-effort distributed lock (``SET NX PX``).

    Used to make non-idempotent maintenance idempotent across replicas - the
    alert evaluator and index maintenance run on a schedule in every scheduler
    replica but must execute once. Stage idempotency does **not** rely on this:
    stages are idempotent by construction (delete-then-insert keyed by
    contract + version), because a lock cannot survive a crashed worker.
    """

    def __init__(self, name: str, *, ttl_seconds: int = 300) -> None:
        self.key = f"lock:{name}"
        self.ttl_ms = ttl_seconds * 1000
        self._token: str | None = None

    async def acquire(self) -> bool:
        import secrets

        self._token = secrets.token_hex(16)
        try:
            redis = await get_redis()
            return bool(await redis.set(self.key, self._token, nx=True, px=self.ttl_ms))
        except Exception as exc:  # noqa: BLE001
            logger.debug("lock_acquire_failed", key=self.key, error=str(exc))
            return False

    async def release(self) -> None:
        if self._token is None:
            return
        try:
            redis = await get_redis()
            # Only release our own hold, or a slow task could free someone else's.
            script = (
                "if redis.call('get', KEYS[1]) == ARGV[1] "
                "then return redis.call('del', KEYS[1]) else return 0 end"
            )
            await redis.eval(script, 1, self.key, self._token)
        except Exception as exc:  # noqa: BLE001
            logger.debug("lock_release_failed", key=self.key, error=str(exc))
        finally:
            self._token = None

    async def __aenter__(self) -> bool:
        return await self.acquire()

    async def __aexit__(self, *_: Any) -> None:
        await self.release()


__all__ = [
    "CacheNamespace",
    "RedisLock",
    "cache_delete",
    "cache_get",
    "cache_invalidate_prefix",
    "cache_set",
    "cached",
    "close_redis",
    "get_queue_redis",
    "get_redis",
    "hash_payload",
    "invalidate_project_cache",
    "make_key",
    "redis_healthy",
    "reset_cache_breaker",
]

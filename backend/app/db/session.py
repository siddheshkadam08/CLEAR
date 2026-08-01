"""Async engine, session factory and the request-scoped session dependency.

Transaction model
-----------------
:func:`get_db` yields a session and **commits on success, rolls back on any
exception**. Services therefore never call ``commit()`` themselves - one HTTP
request is one transaction, so a handler that fails halfway cannot leave a
half-written contract behind. Background work uses :func:`session_scope`, which
behaves identically outside a request.

Pooling is sized for many short transactions per request rather than long-held
connections; ``pool_pre_ping`` handles connections dropped by a managed
Postgres failover.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Create (once) and return the process-wide async engine."""
    global _engine
    if _engine is not None:
        return _engine

    settings = get_settings()

    connect_args: dict[str, Any] = {
        "server_settings": {
            "application_name": settings.observability.service_name,
            # Stops a runaway query from pinning a connection forever.
            "statement_timeout": str(settings.db.statement_timeout_ms),
            # Was hardcoded to 2 minutes, which silently capped how long a stage
            # could take. A stage runs inside one transaction and `ai_extraction`
            # spends minutes in provider calls, so the connection is idle *in
            # transaction* for the whole run - and Postgres terminated it partway
            # through every non-trivial contract. The stage then failed with a
            # message about missing clauses, because by the time anything noticed,
            # the connection that would have explained it was gone.
            #
            # The right fix is for provider work not to hold a transaction at all;
            # until then this has to accommodate the slowest stage, not the
            # fastest query.
            "idle_in_transaction_session_timeout": str(settings.db.idle_in_transaction_timeout_ms),
            "jit": "off",  # JIT hurts the many short OLTP queries we issue
            # Set per connection rather than on the role: the role may be shared
            # with another application in the same database, and changing its
            # default search_path would silently re-point *their* unqualified
            # table names at our schema. See DatabaseSettings.schema_name.
            "search_path": settings.db.search_path,
        },
        # asyncpg caches prepared statements per connection; pgbouncer in
        # transaction mode cannot support that, so it is disabled.
        "statement_cache_size": 0,
    }

    engine_kwargs: dict[str, Any] = {
        "echo": settings.db.echo,
        "future": True,
        "pool_pre_ping": True,
        "connect_args": connect_args,
        # asyncpg speaks native types; keep the driver's own JSON codec out of the
        # way so JSONB round-trips as dict/list without double encoding.
        "json_serializer": _json_serializer,
        "json_deserializer": _json_deserializer,
    }

    if settings.is_testing:
        # Tests share one event loop per test; pooling across them causes
        # "attached to a different loop" errors.
        engine_kwargs["poolclass"] = NullPool
    else:
        engine_kwargs.update(
            pool_size=settings.db.pool_size,
            max_overflow=settings.db.max_overflow,
            pool_timeout=settings.db.pool_timeout,
            pool_recycle=settings.db.pool_recycle,
        )

    _engine = create_async_engine(settings.db.async_url, **engine_kwargs)
    _register_listeners(_engine)

    from app.core.telemetry import instrument_engine

    instrument_engine(_engine)

    logger.info(
        "database_engine_created",
        pool_size=settings.db.pool_size if not settings.is_testing else 0,
        echo=settings.db.echo,
    )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the engine."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,  # response serialisation happens post-commit
            autoflush=False,  # explicit flushes keep ordering predictable
            autocommit=False,
        )
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one transaction per request.

    ``async def handler(db: AsyncSession = Depends(get_db))``
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        # A handler that raised never reaches here, so a partial write cannot be
        # committed by accident.
        if session.in_transaction():
            await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


class session_scope:  # noqa: N801 - used as a context manager, reads as one
    """Transactional scope for background work (workers, scheduler, CLI).

    >>> async with session_scope() as db:
    ...     db.add(row)
    """

    def __init__(self) -> None:
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> AsyncSession:
        self._session = get_session_factory()()
        return self._session

    async def __aexit__(self, exc_type: type[BaseException] | None, *_: Any) -> None:
        session = self._session
        if session is None:
            return
        try:
            if exc_type is None:
                if session.in_transaction():
                    await session.commit()
            else:
                await session.rollback()
        finally:
            await session.close()
            self._session = None


async def shutdown_engine() -> None:
    """Dispose the pool on graceful shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("database_engine_disposed")
    _engine = None
    _session_factory = None


async def database_healthy() -> bool:
    """Readiness probe: can we round-trip a query?"""
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("database_health_check_failed", error=str(exc))
        return False


async def check_extensions() -> dict[str, bool]:
    """Report which required Postgres extensions are installed.

    Surfaced by ``/readyz``: pgvector missing is a silent killer - inserts work
    and every vector search returns nothing useful.
    """
    required = ("vector", "pg_trgm", "uuid-ossp", "citext")
    result: dict[str, bool] = dict.fromkeys(required, False)
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT extname FROM pg_extension WHERE extname = ANY(:names)"),
                {"names": list(required)},
            )
            installed = {row[0] for row in rows}
        for name in required:
            result[name] = name in installed
    except Exception as exc:  # noqa: BLE001
        logger.warning("extension_check_failed", error=str(exc))
    return result


# =============================================================================
# Internals
# =============================================================================
def _json_serializer(value: Any) -> str:
    import orjson

    from app.core.cache import _json_default

    return orjson.dumps(value, default=_json_default).decode()


def _json_deserializer(value: str | bytes) -> Any:
    import orjson

    return orjson.loads(value)


def _register_listeners(engine: AsyncEngine) -> None:
    """Per-connection setup: register the pgvector codec and HNSW search depth."""
    settings = get_settings()

    # NOTE: pgvector's asyncpg codec is deliberately NOT registered here.
    #
    # It used to be, on the reasoning that it would "bind vector columns as lists
    # everywhere". It does the opposite. The vector columns are declared with
    # `pgvector.sqlalchemy.HALFVEC`, whose bind processor already renders a list
    # into pgvector's wire form - the string `'[0.1,0.2,...]'`. Registering the
    # asyncpg *binary* codec on the same connection puts a second encoder behind
    # that one, and it rejects what the first produced:
    #
    #     asyncpg.exceptions.DataError: invalid input for query argument $5:
    #     '[-0.038002303708988494,0.0052160...' (expected list or ndarray)
    #
    # so every embedding INSERT failed. The two integrations are alternatives, not
    # layers: use pgvector's SQLAlchemy types (which own both bind and result
    # processing, as here), or use the raw asyncpg codec - never both. This was
    # invisible until the duplicate-reuse lookup was fixed, because the stage died
    # before it ever reached an INSERT.

    @event.listens_for(engine.sync_engine, "connect")
    def _set_hnsw_search_params(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover
        """Set the HNSW search parameters once per connection.

        ``ef_search`` trades latency for recall. Setting it at connect time rather
        than per query keeps it out of the hot path.

        ``iterative_scan`` is the one that matters as the table grows, and it is
        worth stating why. Every similarity query this platform issues carries
        filters the index cannot use - ``project_id``, ``contract_id``, the model,
        and the metadata containment - so pgvector's default single-pass scan
        returns ``ef_search`` globally-nearest rows and *then* Postgres discards
        the ones outside the caller's scope. On a small single-tenant table that is
        invisible. At a million rows across hundreds of projects, the 80 nearest
        chunks overall are frequently none of the caller's, and a question with a
        perfect answer in the corpus returns nothing at all - silently, because an
        empty result is indistinguishable from a genuine miss.

        ``relaxed_order`` lets the scan keep going until it has enough rows that
        survive the filter, bounded by ``max_scan_tuples`` so a query that can
        never be satisfied gives up rather than walking the graph. Requires
        pgvector 0.8+; on an older server the SET fails and is logged, and
        behaviour is what it was before.
        """
        raw = getattr(dbapi_connection, "_connection", None)
        if raw is None:
            return

        embedding = settings.embedding
        statements = [
            f"SET hnsw.ef_search = {embedding.hnsw_ef_search}",
            f"SET hnsw.iterative_scan = {embedding.hnsw_iterative_scan}",
            f"SET hnsw.max_scan_tuples = {embedding.hnsw_max_scan_tuples}",
        ]
        for statement in statements:
            try:
                dbapi_connection.await_(raw.execute(statement))
            except Exception as exc:  # noqa: BLE001 - an older pgvector lacks these GUCs
                logger.debug("hnsw_parameter_not_set", statement=statement, error=str(exc))


__all__ = [
    "check_extensions",
    "database_healthy",
    "get_db",
    "get_engine",
    "get_session_factory",
    "session_scope",
    "shutdown_engine",
]

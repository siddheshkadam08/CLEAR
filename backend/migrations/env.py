"""Alembic environment.

The DSN comes from application settings, never from ``alembic.ini`` - one source
of truth for the connection string. Migrations run on the **sync** driver
(psycopg) even though the application is async, because Alembic's DDL is
synchronous and mixing an event loop into ``alembic upgrade`` buys nothing.
"""

from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Importing the models package registers every mapper on Base.metadata, which is
# what autogenerate compares the live database against.
import app.models  # noqa: F401
from app.core.config import get_settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

logger = logging.getLogger("alembic.env")

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.db.sync_url)

target_metadata = Base.metadata


def include_object(
    obj: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object,
) -> bool:
    """Filter objects out of autogenerate.

    Postgres-managed artefacts (extension-owned tables, the ``pg_stat_statements``
    view) would otherwise show up as spurious drops on every autogenerate run.
    """
    if type_ == "table" and name in {"pg_stat_statements", "pg_stat_statements_info"}:
        return False
    # Indexes created by raw SQL in a migration (expression indexes on
    # to_tsvector) are not always reproducible by autogenerate; keep them.
    if type_ == "index" and name and name.endswith("_fts"):
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``alembic upgrade --sql``)."""
    context.configure(
        url=settings.db.sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
        include_schemas=False,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = settings.db.sync_url

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
            include_schemas=False,
            # Deterministic constraint names come from Base's naming convention;
            # rendering them keeps generated migrations reversible.
            render_as_batch=False,
            transaction_per_migration=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

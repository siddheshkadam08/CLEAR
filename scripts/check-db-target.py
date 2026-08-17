#!/usr/bin/env python3
"""Assert this environment's database is usable before anything tries to use it.

What it checks is what the application cannot start without:

1. the DSN connects;
2. the ``vector`` extension is installed, or no embedding column can exist;
3. the schema named by ``DB_SCHEMA`` exists;
4. that schema holds the application's tables;
5. it is at a migration revision this checkout knows about, and ideally at head.

This used to pin one specific cluster by ``system_identifier``, require the
database to be named ``team-1``, and demand ``cip_*`` tables in a hardcoded
``clear`` schema. All three are retired: the clause taxonomy is read from the
Clause Master tables now rather than another team's ``cip_*`` tables (see
``app/ai/docpipeline/mapping.py``), and pinning a decommissioned host meant this
failed against every valid database. Checking *capability* rather than *identity*
is what makes it useful on a laptop, on the dev server and in CI alike.

    python scripts/check-db-target.py            # this machine's .env
    python scripts/check-db-target.py --url URL  # an explicit DSN
    make db-target                               # the same, via make

Exit codes, so CI and a pre-deploy step can gate on it:

    0  usable
    1  reachable, but the schema is not usable (missing extension/schema/tables)
    2  could not connect at all
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

#: Tables the application cannot function without. Not the full set - enough to
#: tell "migrated" from "an empty database that happens to accept connections".
REQUIRED_TABLES: tuple[str, ...] = (
    "contracts",
    "processing_jobs",
    "clause_master_categories",
    "document_profiles",
    "alembic_version",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_value(name: str) -> str | None:
    """A setting from the real environment, falling back to the repo's ``.env``.

    Read directly rather than through ``app.core.config`` so this stays runnable
    with no dependencies installed, and cannot be fooled by the settings layer's
    own fallback to a bundled Postgres.
    """
    value = os.environ.get(name)
    if value is not None:
        return value

    env_file = _REPO_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{name}="):
                return stripped.split("=", 1)[1].strip()
    return None


def dsn_from_env() -> str:
    url = _env_value("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set, and no .env holds one.")
    return url


def schema_from_env() -> str:
    """The schema the app will qualify its queries with. Empty means ``public``."""
    return (_env_value("DB_SCHEMA") or "").strip() or "public"


def redact(url: str) -> str:
    return re.sub(r"(://[^:/]+:)[^@]*(@)", r"\1***\2", url)


# =============================================================================
# What this checkout expects
# =============================================================================
# Both spellings appear in this tree - `revision = "0004"` and the annotated
# `revision: str = "0001"` - so the annotation is optional. Matching only one of
# them silently yields no head, and the check then reports every database as
# being at an unknown revision.
_REVISION = re.compile(r"^revision(?:\s*:[^=]+)?\s*=\s*[\"']([^\"']+)[\"']", re.M)
_DOWN_REVISION = re.compile(r"^down_revision(?:\s*:[^=]+)?\s*=\s*[\"']([^\"']+)[\"']", re.M)


def _versions_dir() -> Path | None:
    """Where the migrations live, in a checkout or in the image."""
    for candidate in (
        _REPO_ROOT / "backend" / "migrations" / "versions",
        _REPO_ROOT / "migrations" / "versions",
        Path("/app/migrations/versions"),
    ):
        if candidate.is_dir():
            return candidate
    return None


def known_revisions() -> tuple[set[str], str | None]:
    """Every revision this checkout defines, and its head.

    The head is the revision no other migration names as its ``down_revision``.
    Derived rather than hardcoded, so adding a migration does not also mean
    remembering to update this script.
    """
    versions = _versions_dir()
    if versions is None:
        return set(), None

    revisions: set[str] = set()
    downs: set[str] = set()
    for path in versions.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        match = _REVISION.search(text)
        if match:
            revisions.add(match.group(1))
        down = _DOWN_REVISION.search(text)
        if down:
            downs.add(down.group(1))

    heads = revisions - downs
    return revisions, heads.pop() if len(heads) == 1 else None


# =============================================================================
# Evaluate
# =============================================================================
def evaluate(
    *,
    schema: str,
    has_vector: bool,
    schema_exists: bool,
    tables: set[str],
    revision: str | None,
) -> tuple[list[str], list[str]]:
    """Turn the observed facts into problems and warnings.

    Pure, so the interesting logic is testable without a database.
    """
    problems: list[str] = []
    warnings: list[str] = []

    if not has_vector:
        problems.append(
            "The `vector` extension is not installed, so no embedding column can "
            "exist. Run: CREATE EXTENSION vector;"
        )

    if not schema_exists:
        problems.append(
            f"Schema {schema!r} does not exist. DB_SCHEMA is not a hint - it is put "
            f"on the SQLAlchemy metadata, so every query is qualified with it and "
            f"every one of them fails. Run `alembic upgrade head`, which creates it, "
            f"or correct DB_SCHEMA."
        )
        return problems, warnings

    missing = [name for name in REQUIRED_TABLES if name not in tables]
    if missing:
        problems.append(
            f"Schema {schema!r} is missing {', '.join(missing)}. Run: alembic upgrade head"
        )
        return problems, warnings

    revisions, head = known_revisions()
    if revision is None:
        problems.append(
            f"Schema {schema!r} has an alembic_version table with no row, so the "
            f"migration state is unknown. Run: alembic upgrade head"
        )
    elif revisions and revision not in revisions:
        problems.append(
            f"The database is at revision {revision!r}, which this checkout does not "
            f"define. It has been migrated by a newer build - deploying this one "
            f"risks running code against a schema it does not expect."
        )
    elif head and revision != head:
        warnings.append(
            f"The database is at revision {revision!r}; this checkout's head is "
            f"{head!r}. Run `alembic upgrade head` before relying on anything new."
        )

    return problems, warnings


# =============================================================================
# Check
# =============================================================================
async def gather(conn: Any, schema: str) -> dict[str, Any]:
    """Read the facts. Every schema value is a bound parameter, never SQL text.

    Postgres cannot parameterise an *identifier*, which is the usual excuse for
    interpolating one into the statement. ``set_config`` takes the search_path as
    an ordinary value, so the query stays a constant and a hostile DB_SCHEMA -
    ``public; DROP TABLE users; --`` - is carried as data. It is not a trust
    boundary worth discovering the hard way.
    """
    facts: dict[str, Any] = {
        "database": await conn.fetchval("select current_database()"),
        "server": await conn.fetchval("select inet_server_addr()"),
        "has_vector": bool(
            await conn.fetchval("select 1 from pg_extension where extname = 'vector'")
        ),
        "schema_exists": bool(
            await conn.fetchval(
                "select 1 from information_schema.schemata where schema_name = $1", schema
            )
        ),
    }
    facts["tables"] = {
        row["table_name"]
        for row in await conn.fetch(
            "select table_name from information_schema.tables where table_schema = $1", schema
        )
    }

    facts["revision"] = None
    if "alembic_version" in facts["tables"]:
        await conn.execute("select set_config('search_path', $1, false)", schema)
        facts["revision"] = await conn.fetchval("select version_num from alembic_version")
    return facts


async def check(url: str, *, connect: Any = None) -> int:
    if connect is None:
        try:
            import asyncpg
        except ImportError:
            sys.exit("asyncpg is not installed. Run this inside the backend venv or a container.")
        connect = asyncpg.connect

    # asyncpg speaks the wire protocol, not SQLAlchemy's dialect suffix.
    dsn = url.replace("+asyncpg", "").replace("+psycopg", "")
    schema = schema_from_env()

    print(f"configured : {redact(dsn)}")
    print(f"schema     : {schema}")
    try:
        conn = await connect(dsn, timeout=20)
    except Exception as exc:  # noqa: BLE001 - the reason is for a human
        print(f"FAIL       : cannot connect - {type(exc).__name__}: {exc}")
        return 2

    try:
        facts = await gather(conn, schema)
    finally:
        await conn.close()

    _, head = known_revisions()
    print(f"reached    : {facts['server']}")
    print(f"database   : {facts['database']}")
    print(f"pgvector   : {'yes' if facts['has_vector'] else 'NO'}")
    print(f"tables     : {len(facts['tables'])} in {schema}")
    print(f"revision   : {facts['revision'] or '-'} (checkout head: {head or 'unknown'})")

    problems, warnings = evaluate(
        schema=schema,
        has_vector=facts["has_vector"],
        schema_exists=facts["schema_exists"],
        tables=facts["tables"],
        revision=facts["revision"],
    )

    print()
    for warning in warnings:
        print(f"WARN       : {warning}")
    if problems:
        for problem in problems:
            print(f"FAIL       : {problem}")
        return 1

    print(f"OK         : {facts['database']}, schema {schema} at {facts['revision']}, pgvector present.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--url", help="DSN to check instead of this environment's.")
    args = parser.parse_args()
    return asyncio.run(check(args.url or dsn_from_env()))


if __name__ == "__main__":
    raise SystemExit(main())

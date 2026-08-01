#!/usr/bin/env python3
"""Assert this environment is pointed at Hackathon-DB-SRV, and nothing else.

Every environment - a laptop, the dev server, CI - reads and writes one
database. The `cip_*` tables that drive clause detection are maintained by
another system and exist only there, so a stack pointed elsewhere does not fail
loudly: it classifies a document, finds no clause list for the type, and reports
"No clauses are defined for document type" as though the taxonomy were at fault.

Identity is checked on `system_identifier`, not on the host in the URL. The
server answers to a public and a private address depending on where you are
connecting from, so comparing hostnames says nothing useful - two environments
can hold the same string and reach different servers, or hold different strings
and reach the same one. `system_identifier` is generated once when a cluster is
initialised and is the only value that settles it.

    python scripts/check-db-target.py            # this machine's .env
    python scripts/check-db-target.py --url URL  # an explicit DSN
    make db-target                               # the same, via make

Exits non-zero when the target is wrong, so CI and a pre-deploy step can gate
on it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

#: Hackathon-DB-SRV. Generated at initdb, so it survives a restart, a change of
#: address and a password rotation - and changes if the cluster is ever rebuilt,
#: which is precisely when this check should start failing.
EXPECTED_SYSTEM_IDENTIFIER = "7667820444287067012"
EXPECTED_DATABASE = "team-1"

#: Both are the same host. Which one works depends on where you are.
PUBLIC_ADDRESS = "35.154.17.203:5432"
PRIVATE_ADDRESS = "172.15.151.102:5432"


def dsn_from_env() -> str:
    """DATABASE_URL from the environment, falling back to the repo's .env.

    Read directly rather than through `app.core.config`, so this stays runnable
    with no dependencies installed and cannot be fooled by the settings layer's
    own fallback to the bundled Postgres.
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        return url

    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            if line.strip().startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()

    sys.exit("DATABASE_URL is not set, and no .env holds one.")


def redact(url: str) -> str:
    return re.sub(r"(://[^:/]+:)[^@]*(@)", r"\1***\2", url)


async def check(url: str) -> int:
    try:
        import asyncpg
    except ImportError:
        sys.exit("asyncpg is not installed. Run this inside the backend venv or a container.")

    # asyncpg speaks the wire protocol, not SQLAlchemy's dialect suffix.
    dsn = url.replace("+asyncpg", "").replace("+psycopg", "")

    print(f"configured : {redact(dsn)}")
    try:
        conn = await asyncpg.connect(dsn, timeout=20)
    except Exception as exc:  # noqa: BLE001 - the reason is for a human
        print(f"FAIL       : cannot connect - {type(exc).__name__}: {exc}")
        return 2

    try:
        identifier = str(await conn.fetchval("select system_identifier from pg_control_system()"))
        database = await conn.fetchval("select current_database()")
        server = await conn.fetchval("select inet_server_addr()")
        contracts = await conn.fetchval(
            "select count(*) from information_schema.tables "
            "where table_schema = 'clear' and table_name like 'cip_%'"
        )
    finally:
        await conn.close()

    print(f"reached    : {server}")
    print(f"database   : {database}")
    print(f"identifier : {identifier}")
    print(f"cip_* tables: {contracts}")

    problems = []
    if identifier != EXPECTED_SYSTEM_IDENTIFIER:
        problems.append(
            f"This is not Hackathon-DB-SRV. Expected system_identifier "
            f"{EXPECTED_SYSTEM_IDENTIFIER}, found {identifier}. Point "
            f"DATABASE_URL at {PUBLIC_ADDRESS} (from a laptop) or "
            f"{PRIVATE_ADDRESS} (from the dev server)."
        )
    if database != EXPECTED_DATABASE:
        problems.append(f"Connected to database {database!r}, expected {EXPECTED_DATABASE!r}.")
    if not contracts:
        problems.append(
            "The clause taxonomy is missing: no cip_* tables in schema `clear`. "
            "Clause detection will find nothing for every document type."
        )

    if problems:
        print()
        for problem in problems:
            print(f"FAIL       : {problem}")
        return 1

    print()
    print("OK         : Hackathon-DB-SRV, database team-1, taxonomy present.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--url", help="DSN to check instead of this environment's.")
    args = parser.parse_args()
    return asyncio.run(check(args.url or dsn_from_env()))


if __name__ == "__main__":
    raise SystemExit(main())

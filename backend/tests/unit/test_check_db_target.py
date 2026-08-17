"""The database preflight: what it accepts, what it rejects, and how it asks.

Two concerns, and the second is a security one.

**What it checks.** Capability, not identity. The previous version pinned a
retired cluster by ``system_identifier``, required the database to be called
``team-1`` and demanded ``cip_*`` tables nothing reads any more - so it failed
against every valid database and told the operator to go to a decommissioned
host.

**How it asks.** Postgres cannot parameterise an identifier, which is the usual
excuse for building the statement with an f-string. ``DB_SCHEMA`` comes from a
file, and a file is not a trust boundary; the tests below prove the schema
reaches the server as a bound *value* and never as SQL text.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-db-target.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("check_db_target", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pytestmark = pytest.mark.skipif(not _SCRIPT.is_file(), reason="script not in this checkout")
check_db_target = _load()


#: The payload from the brief. If any of it ever reaches the server as SQL, the
#: `--` comments out the rest of the statement and the DROP executes.
MALICIOUS_SCHEMA = "public; DROP TABLE users; --"


class _RecordingConnection:
    """An asyncpg-shaped double that records every statement and its arguments."""

    def __init__(self, **facts: Any) -> None:
        self.facts = facts
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.calls.append((query, args))
        if "current_database" in query:
            return self.facts.get("database", "cip")
        if "inet_server_addr" in query:
            return self.facts.get("server", "10.0.0.1")
        if "pg_extension" in query:
            return 1 if self.facts.get("has_vector", True) else None
        if "information_schema.schemata" in query:
            return 1 if self.facts.get("schema_exists", True) else None
        if "version_num" in query:
            return self.facts.get("revision", "0012")
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((query, args))
        return [{"table_name": name} for name in self.facts.get("tables", ())]

    async def execute(self, query: str, *args: Any) -> None:
        self.calls.append((query, args))

    async def close(self) -> None:
        self.closed = True


# =============================================================================
# SQL injection
# =============================================================================
def test_a_hostile_schema_never_appears_in_any_statement() -> None:
    """The payload must be an argument, everywhere it is used."""
    conn = _RecordingConnection(tables=check_db_target.REQUIRED_TABLES)

    asyncio.run(check_db_target.gather(conn, MALICIOUS_SCHEMA))

    assert conn.calls, "the check made no queries at all"
    for query, _args in conn.calls:
        assert MALICIOUS_SCHEMA not in query, f"schema interpolated into SQL: {query!r}"
        assert "DROP TABLE" not in query.upper(), f"payload reached the statement: {query!r}"

    # ...and it *is* passed, as a value, or the check would be testing nothing.
    passed = [args for _, args in conn.calls if MALICIOUS_SCHEMA in args]
    assert passed, "the schema was never sent as a bound parameter"


def test_the_search_path_is_set_through_a_bound_parameter() -> None:
    """`set_config` takes the path as an ordinary value; `SET search_path` cannot."""
    conn = _RecordingConnection(tables=check_db_target.REQUIRED_TABLES)

    asyncio.run(check_db_target.gather(conn, MALICIOUS_SCHEMA))

    set_calls = [(q, a) for q, a in conn.calls if "set_config" in q]
    assert set_calls, "the search_path was never set"
    for query, args in set_calls:
        assert "$1" in query
        assert MALICIOUS_SCHEMA in args


def test_the_script_builds_no_sql_from_the_schema() -> None:
    """A static guard: no f-string may carry a value into a statement.

    Scoped to f-strings that actually look like SQL, so the human-readable
    ``print(f"schema : {schema}")`` lines are not mistaken for query building.
    """
    source = _SCRIPT.read_text(encoding="utf-8")
    sql_fstrings = re.findall(r"""f["'][^"']*\b(?:select|from|where|set_config)\b""", source, re.I)

    assert sql_fstrings == [], f"SQL appears to be built by interpolation: {sql_fstrings}"


# =============================================================================
# What it accepts and rejects
# =============================================================================
def _evaluate(**overrides: Any) -> tuple[list[str], list[str]]:
    facts: dict[str, Any] = {
        "schema": "cip",
        "has_vector": True,
        "schema_exists": True,
        "tables": set(check_db_target.REQUIRED_TABLES),
        "revision": check_db_target.known_revisions()[1],
    }
    facts.update(overrides)
    return check_db_target.evaluate(**facts)


def test_a_healthy_database_passes() -> None:
    problems, warnings = _evaluate()

    assert problems == []
    assert warnings == []


def test_a_missing_pgvector_extension_is_fatal() -> None:
    problems, _ = _evaluate(has_vector=False)

    assert any("vector" in problem for problem in problems)


def test_a_missing_schema_is_fatal_and_explains_why_it_matters() -> None:
    problems, _ = _evaluate(schema_exists=False)

    assert len(problems) == 1
    assert "does not exist" in problems[0]
    assert "DB_SCHEMA" in problems[0]


def test_missing_application_tables_are_fatal() -> None:
    problems, _ = _evaluate(tables={"contracts"})

    assert any("alembic upgrade head" in problem for problem in problems)


def test_an_empty_alembic_version_is_fatal() -> None:
    problems, _ = _evaluate(revision=None)

    assert any("migration state is unknown" in problem for problem in problems)


def test_a_revision_this_checkout_does_not_know_is_fatal() -> None:
    """Newer database than code: running against it risks a schema mismatch."""
    problems, _ = _evaluate(revision="9999")

    assert any("does not define" in problem for problem in problems)


def test_an_older_but_known_revision_is_only_a_warning() -> None:
    revisions, head = check_db_target.known_revisions()
    older = sorted(revisions - {head})[0]

    problems, warnings = _evaluate(revision=older)

    assert problems == []
    assert any("upgrade head" in warning for warning in warnings)


# =============================================================================
# Nothing left of the retired assumptions
# =============================================================================
def test_no_retired_infrastructure_is_referenced() -> None:
    """Behavioural, not textual: the docstring may explain what was removed.

    What matters is that nothing *acts* on the retired assumptions - no pinned
    cluster identity, no required database name, no `cip_*` table requirement.
    """
    for gone in ("EXPECTED_SYSTEM_IDENTIFIER", "EXPECTED_DATABASE", "PUBLIC_ADDRESS"):
        assert not hasattr(check_db_target, gone), f"{gone} still drives the check"

    assert not any(name.startswith("cip_") for name in check_db_target.REQUIRED_TABLES)

    # A database named anything at all passes, provided it is usable.
    problems, _ = _evaluate()
    assert problems == []


def test_the_head_revision_is_derived_from_the_migrations_not_hardcoded() -> None:
    revisions, head = check_db_target.known_revisions()

    assert head is not None, "exactly one head must be derivable"
    assert head in revisions
    assert len(revisions) > 1


# =============================================================================
# Exit codes
# =============================================================================
def test_a_connection_failure_exits_two() -> None:
    async def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("connection refused")

    code = asyncio.run(check_db_target.check("postgresql://x/y", connect=_refuse))

    assert code == 2


def test_an_unusable_schema_exits_one() -> None:
    async def _connect(*_args: Any, **_kwargs: Any) -> Any:
        return _RecordingConnection(tables=(), schema_exists=False)

    code = asyncio.run(check_db_target.check("postgresql://x/y", connect=_connect))

    assert code == 1


def test_a_usable_database_exits_zero() -> None:
    head = check_db_target.known_revisions()[1]

    async def _connect(*_args: Any, **_kwargs: Any) -> Any:
        return _RecordingConnection(tables=check_db_target.REQUIRED_TABLES, revision=head)

    code = asyncio.run(check_db_target.check("postgresql://x/y", connect=_connect))

    assert code == 0

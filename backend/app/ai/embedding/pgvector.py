"""pgvector introspection - what the database actually has, not what we assume.

Every fact here is read from the live catalogue rather than inferred from the
models or the migration history. That distinction is the point of the module: a
schema one migration behind, an extension too old for ``halfvec``, or an HNSW index
that failed to create and was never noticed all present as "search returns nothing
useful" rather than as an error, and none of them are visible from application code.

Used by the startup validator, the health endpoint and the ``verify_pgvector`` tool,
so all three report the same facts from the same query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.logging import get_logger

logger = get_logger(__name__)

#: ``halfvec`` and its operator classes arrived here.
MIN_HALFVEC_VERSION = (0, 7)

#: pgvector's HNSW ceiling per column type. Exceeding it does not fail an insert -
#: it fails index *creation*, leaving every similarity query on a sequential scan.
HNSW_MAX_DIMS: dict[str, int] = {"vector": 2000, "halfvec": 4000}

Executor = AsyncSession | AsyncConnection


@dataclass(slots=True)
class VectorColumn:
    """One vector-typed column found in the database."""

    table: str
    column: str
    type_name: str
    dim: int | None

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "column": self.column,
            "type": self.type_name,
            "dimension": self.dim,
        }


@dataclass(slots=True)
class VectorIndex:
    """An index over a vector column, and whether it is usable."""

    name: str
    table: str
    method: str
    definition: str
    valid: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "table": self.table,
            "method": self.method,
            "valid": self.valid,
        }


@dataclass(slots=True)
class PgVectorInfo:
    """Everything the tools need to know about the vector store's real state."""

    reachable: bool = False
    installed: bool = False
    version: str | None = None
    columns: list[VectorColumn] = field(default_factory=list)
    indexes: list[VectorIndex] = field(default_factory=list)
    error: str | None = None

    @property
    def version_tuple(self) -> tuple[int, ...]:
        if not self.version:
            return ()
        parts: list[int] = []
        for part in str(self.version).split("."):
            digits = "".join(ch for ch in part if ch.isdigit())
            if not digits:
                break
            parts.append(int(digits))
        return tuple(parts)

    @property
    def supports_halfvec(self) -> bool:
        return self.version_tuple[:2] >= MIN_HALFVEC_VERSION if self.version_tuple else False

    def column(self, table: str, column: str) -> VectorColumn | None:
        for candidate in self.columns:
            if candidate.table == table and candidate.column == column:
                return candidate
        return None

    def indexes_for(self, table: str) -> list[VectorIndex]:
        return [index for index in self.indexes if index.table == table]

    def as_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "installed": self.installed,
            "version": self.version,
            "supports_halfvec": self.supports_halfvec,
            "columns": [column.as_dict() for column in self.columns],
            "indexes": [index.as_dict() for index in self.indexes],
            "error": self.error,
        }


_COLUMN_QUERY = text(
    """
    SELECT c.relname  AS table_name,
           a.attname  AS column_name,
           format_type(a.atttypid, a.atttypmod) AS col_type
    FROM pg_attribute a
    JOIN pg_class c     ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_type t      ON t.oid = a.atttypid
    WHERE c.relkind = 'r'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND a.attnum > 0
      AND NOT a.attisdropped
      AND t.typname IN ('vector', 'halfvec', 'sparsevec', 'bit')
      AND format_type(a.atttypid, a.atttypmod) ~ '^(vector|halfvec|sparsevec)'
    ORDER BY c.relname, a.attnum
    """
)

#: `indisvalid` is the part that matters. A CREATE INDEX CONCURRENTLY that failed
#: leaves an invalid index behind: it exists, `\d` shows it, and the planner will
#: not use it.
_INDEX_QUERY = text(
    """
    SELECT i.relname       AS index_name,
           c.relname       AS table_name,
           am.amname       AS method,
           pg_get_indexdef(i.oid) AS definition,
           x.indisvalid    AS is_valid
    FROM pg_index x
    JOIN pg_class i     ON i.oid = x.indexrelid
    JOIN pg_class c     ON c.oid = x.indrelid
    JOIN pg_am am       ON am.oid = i.relam
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND am.amname IN ('hnsw', 'ivfflat')
    ORDER BY c.relname, i.relname
    """
)


def parse_vector_type(col_type: str) -> tuple[str, int | None]:
    """``halfvec(2048)`` -> ``("halfvec", 2048)``. Width is optional in the catalogue."""
    name = col_type.split("(", 1)[0].strip()
    if "(" not in col_type:
        return name, None
    inner = col_type.split("(", 1)[1].rstrip(")").strip()
    try:
        return name, int(inner)
    except ValueError:
        return name, None


async def inspect(db: Executor) -> PgVectorInfo:
    """Read the extension, every vector column and every vector index.

    Never raises: an unreachable database is a *finding* for the caller to report,
    not a crash inside a diagnostic tool whose whole job is to say what is wrong.
    """
    info = PgVectorInfo()

    try:
        info.version = (
            await db.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
        ).scalar_one_or_none()
        info.reachable = True
        info.installed = info.version is not None
    except Exception as exc:  # noqa: BLE001 - see docstring
        info.error = str(exc)[:300]
        logger.warning("pgvector_inspect_failed", error=info.error)
        return info

    if not info.installed:
        return info

    try:
        for row in (await db.execute(_COLUMN_QUERY)).all():
            type_name, dim = parse_vector_type(str(row.col_type))
            info.columns.append(
                VectorColumn(
                    table=str(row.table_name),
                    column=str(row.column_name),
                    type_name=type_name,
                    dim=dim,
                )
            )
        for row in (await db.execute(_INDEX_QUERY)).all():
            info.indexes.append(
                VectorIndex(
                    name=str(row.index_name),
                    table=str(row.table_name),
                    method=str(row.method),
                    definition=str(row.definition),
                    valid=bool(row.is_valid),
                )
            )
    except Exception as exc:  # noqa: BLE001
        info.error = str(exc)[:300]
        logger.warning("pgvector_catalogue_read_failed", error=info.error)

    return info


__all__ = [
    "HNSW_MAX_DIMS",
    "MIN_HALFVEC_VERSION",
    "PgVectorInfo",
    "VectorColumn",
    "VectorIndex",
    "inspect",
    "parse_vector_type",
]

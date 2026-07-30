"""Custom column types and type helpers.

Enum policy
-----------
Two kinds of enumeration live in this schema and they are stored differently on
purpose:

* **Closed sets** (job state, severity, embedding level) become native Postgres
  enums via :func:`pg_enum`. The database rejects a bad value, and the type name
  is stable across environments.
* **Admin-extensible sets** (clause type, agreement type) are ``VARCHAR`` via
  :func:`extensible_enum`. The Clause Master lets an administrator add a clause
  category at runtime (§11 - "new document types = new profile, zero code
  changes"); a native enum would make that a migration.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import String, types
from sqlalchemy.dialects import postgresql

from app.core.config import get_settings


def pg_enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """Native Postgres enum backed by a Python :class:`StrEnum`.

    ``values_callable`` stores the enum *values* (``"high"``) rather than the
    member names (``"HIGH"``), so the database contents match the API contract
    and are readable in ad-hoc SQL.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        create_constraint=False,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


def extensible_enum(length: int = 64) -> String:
    """``VARCHAR`` for a taxonomy an administrator can extend at runtime."""
    return String(length)


class Vector(types.UserDefinedType[Any]):
    """``pgvector`` column.

    A thin wrapper over the ``pgvector`` package's type so the dimension can come
    from configuration (``EMBEDDING_DIM``) and every embedding table declares its
    dimension in one place.
    """

    cache_ok = True

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim if dim is not None else get_settings().embedding.dim

    def get_col_spec(self, **_: Any) -> str:
        return f"vector({self.dim})"

    def bind_processor(self, dialect: Any) -> Any:
        def process(value: Any) -> Any:
            if value is None:
                return None
            if isinstance(value, str):
                return value
            # pgvector accepts the literal text form '[1,2,3]'.
            return "[" + ",".join(repr(float(v)) for v in value) + "]"

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        def process(value: Any) -> Any:
            if value is None:
                return None
            if isinstance(value, list):
                return value
            return [float(part) for part in str(value).strip("[]").split(",") if part]

        return process


class HalfVector(types.UserDefinedType[Any]):
    """``pgvector`` ``halfvec`` column - half-precision, HNSW-indexable to 4000 dims."""

    cache_ok = True

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim if dim is not None else get_settings().embedding.dim

    def get_col_spec(self, **_: Any) -> str:
        return f"halfvec({self.dim})"

    def bind_processor(self, dialect: Any) -> Any:
        def process(value: Any) -> Any:
            if value is None:
                return None
            if isinstance(value, str):
                return value
            return "[" + ",".join(repr(float(v)) for v in value) + "]"

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        def process(value: Any) -> Any:
            if value is None:
                return None
            if isinstance(value, list):
                return value
            return [float(part) for part in str(value).strip("[]").split(",") if part]

        return process


def vector_column(dim: int | None = None, storage: str | None = None) -> Any:
    """The embedding column type, honouring ``EMBEDDING_STORAGE``.

    ``halfvec`` is the default for a reason that is easy to miss: pgvector's HNSW
    index tops out at **2000 dimensions** for the ``vector`` type, and the default
    model emits **2048**. A ``vector(2048)`` column is accepted, inserts fine, and
    then silently cannot carry an HNSW index - every similarity search falls back to
    a sequential scan over every vector in the table. ``halfvec`` indexes to 4000
    and halves storage; the fp16 rounding is immaterial for L2-normalised
    embeddings compared against each other.

    Prefers the ``pgvector`` package's types so pgvector's operator classes resolve;
    the local fallbacks keep the models importable where only the server extension
    is present.
    """
    settings = get_settings().embedding
    resolved = dim if dim is not None else settings.dim
    kind = storage if storage is not None else settings.storage

    if kind == "halfvec":
        try:
            from pgvector.sqlalchemy import HALFVEC

            return HALFVEC(resolved)
        except (ImportError, AttributeError):  # pragma: no cover - older pgvector
            return HalfVector(resolved)

    try:
        from pgvector.sqlalchemy import Vector as PGVector

        return PGVector(resolved)
    except ImportError:  # pragma: no cover
        return Vector(resolved)


def vector_ops(storage: str | None = None) -> str:
    """The HNSW operator class matching the column type.

    ``vector_cosine_ops`` on a ``halfvec`` column is not a silent no-op - index
    creation fails outright - so this has to track :func:`vector_column`.
    """
    kind = storage if storage is not None else get_settings().embedding.storage
    return "halfvec_cosine_ops" if kind == "halfvec" else "vector_cosine_ops"


class TSVector(types.UserDefinedType[Any]):
    """``tsvector`` column for keyword (BM25-style) search over chunk text."""

    cache_ok = True

    def get_col_spec(self, **_: Any) -> str:
        return "tsvector"


#: Postgres array of text - used for alias lists and synonym sets that need
#: containment queries rather than JSONB path lookups.
TextArray = postgresql.ARRAY(String)


__all__ = [
    "HalfVector",
    "TSVector",
    "TextArray",
    "Vector",
    "extensible_enum",
    "pg_enum",
    "vector_column",
    "vector_ops",
]

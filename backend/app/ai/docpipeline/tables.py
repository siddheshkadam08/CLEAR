"""Core table definitions for the externally-owned ``cip_*`` tables.

These three tables are created and owned outside this repository. They are
declared here as plain :class:`sqlalchemy.Table` objects on a **private
MetaData** rather than as ORM models on ``Base.metadata``, and that choice is
load-bearing: anything on ``Base.metadata`` is enrolled in
``alembic revision --autogenerate`` and in ``create_all``. Autogenerate would
then try to reconcile our declaration against a schema it does not control -
and, because the reverse is also true, ``migrations/env.py`` excludes these
names so an autogenerate run never proposes dropping them.

The column names are reproduced exactly as they exist in the database,
including the mixed case (``docType``, ``pageNumber``, ``jsonfilePath``).
SQLAlchemy quotes mixed-case identifiers automatically, so these resolve
correctly without manual quoting at every call site.
"""

from __future__ import annotations

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Column,
    Float,
    MetaData,
    Table,
    Text,
)

from app.core.config import get_settings
from app.db.types import vector_column


def _schema() -> str | None:
    """The schema the cip_* tables live in - ``clear`` under the current config."""
    name = get_settings().db.schema_name.strip()
    return name or None


#: Deliberately *not* ``Base.metadata``. See the module docstring.
cip_metadata = MetaData(schema=_schema())


#: The clause taxonomy: which clauses matter for which document type, and what
#: each one means. Read-only from this pipeline's point of view - 107 rows
#: across six document types.
cip_doc_mapping = Table(
    "cip_docMapping",
    cip_metadata,
    Column("id", BigInteger, primary_key=True),
    Column("docType", Text),
    Column("clause", Text),
    Column("description", Text),
)


#: One row per processed document.
cip_doc_master = Table(
    "cip_DocMaster",
    cip_metadata,
    Column("id", BigInteger, primary_key=True),
    Column("docid", BigInteger),
    Column("doc_path", Text),
    Column("doc_type", Text),
    Column("jsonPath", Text),
)


#: One row per clause detected in a document.
#:
#: ``docid`` is ``text`` here while ``cip_DocMaster.docid`` is ``bigint``; the
#: cast is done explicitly at the call site rather than papered over, so the
#: mismatch stays visible to whoever reads the join next.
cip_doc_content_master = Table(
    "cip_DocContentMaster",
    cip_metadata,
    Column("id", BigInteger, primary_key=True),
    Column("docid", Text),
    Column("clause", Text),
    # One bounding box per page, eight floats each, inches, as Azure emits them
    # - so `len(polygon) == 8 * len(pageNumber)` and the two arrays are read
    # together:
    #
    #     for page, start in zip(row["pageNumber"], range(0, len(row["polygon"]), 8)):
    #         box = row["polygon"][start : start + 8]
    #
    # A single box spanning a two-page clause describes a rectangle present on
    # neither page. The column was bigint[] until the schema fix, which would
    # have truncated 1.7371 to 1 - see sql/cip_schema_fixes.sql.
    Column("polygon", ARRAY(Float)),
    Column("pageNumber", ARRAY(BigInteger)),
    Column("jsonfilePath", Text),
    Column("textcontent", Text),
    # halfvec(2048), for the same reason the main embeddings table uses it:
    # pgvector's HNSW refuses `vector` above 2000 dimensions.
    Column("embeddings", vector_column()),
)

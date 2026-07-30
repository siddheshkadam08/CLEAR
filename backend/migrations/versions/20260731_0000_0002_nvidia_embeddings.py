"""Move the vector store to nvidia/nemotron-3-embed-1b (2048-d halfvec).

Revision ID: 0002
Revises: 0001

Two changes, both forced by the model:

**Width.** Nemotron 3 Embed 1B returns 2048 dimensions; the column was 1536
(``text-embedding-3-small``). A pgvector column's width is part of its type, so
this is an ``ALTER TYPE``, not a data migration.

**Storage type.** ``vector`` -> ``halfvec``. This is not an optimisation. pgvector's
HNSW index supports at most **2000 dimensions** for ``vector``, so a
``vector(2048)`` column is created happily and then cannot carry an index at all -
every similarity search silently degrades to a sequential scan over the whole
table. ``halfvec`` indexes to 4000 dimensions and halves storage; for L2-normalised
embeddings compared against each other, fp16 rounding is far below the margin that
separates a relevant hit from an irrelevant one.

Existing vectors are **deleted**, not converted
-----------------------------------------------
1536-d vectors from a different model cannot be widened to 2048. There is no
correct conversion: padding invents dimensions the model never produced, and
truncating the *other* direction is not applicable. Keeping them is worse than
deleting them - vectors from two models occupy unrelated spaces, so a mixed index
returns confident nonsense rather than failing.

The rows are fully derived data. Every one is regenerable from the contract text
that is still in object storage and the chunks/clauses still in Postgres. After
this migration runs::

    cip reindex-embeddings --all

Until that finishes, semantic search returns nothing and keyword search is
unaffected. Chunk, clause and contract rows are untouched.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

#: Must match `app.core.config.EmbeddingSettings` defaults. Hardcoded here on
#: purpose: a migration that reads live configuration produces a different schema
#: depending on the environment it is run in, which is how two deployments end up
#: with the same revision applied and different columns.
NEW_DIM = 2048
NEW_TYPE = "halfvec"
OLD_DIM = 1536
OLD_TYPE = "vector"

#: Kept in step with `app.models.embedding`; the partial predicates are what keep
#: each level's graph small enough to search quickly.
_LEVELS = (
    ("ix_embeddings_hnsw_document_summary", "document_summary"),
    ("ix_embeddings_hnsw_clause", "clause"),
    ("ix_embeddings_hnsw_chunk", "chunk"),
)

HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64


def _drop_indexes() -> None:
    for name, _ in _LEVELS:
        op.execute(f"DROP INDEX IF EXISTS {name}")


def _create_indexes(column_type: str) -> None:
    ops = "halfvec_cosine_ops" if column_type == "halfvec" else "vector_cosine_ops"
    for name, level in _LEVELS:
        op.execute(
            f"""
            CREATE INDEX {name}
            ON embeddings USING hnsw (embedding {ops})
            WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION})
            WHERE level = '{level}'
            """
        )


def upgrade() -> None:
    bind = op.get_bind()

    # halfvec arrived in pgvector 0.7.0. Checked explicitly because the failure
    # otherwise is a syntax error on the ALTER, which reads like a typo rather than
    # an out-of-date extension.
    version = bind.execute(
        sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    ).scalar_one_or_none()
    if version is None:
        raise RuntimeError("The pgvector extension is not installed.")
    parts = [int(part) for part in str(version).split(".")[:2] if part.isdigit()]
    if parts < [0, 7]:
        raise RuntimeError(
            f"pgvector {version} does not support halfvec (needs >= 0.7.0). "
            "Upgrade the extension, or set EMBEDDING_STORAGE=vector with "
            "EMBEDDING_DIM<=2000 and regenerate this migration."
        )

    # Report what is being discarded. A migration that deletes rows silently is a
    # migration nobody can audit afterwards.
    existing = bind.execute(sa.text("SELECT count(*) FROM embeddings")).scalar_one()
    if existing:
        print(  # noqa: T201 - alembic's own channel to the operator
            f"  -> discarding {existing} embedding(s) at {OLD_TYPE}({OLD_DIM}); "
            "they are from a different model and cannot be converted. "
            "Run `cip reindex-embeddings --all` after this migration."
        )

    _drop_indexes()

    # DELETE rather than TRUNCATE: TRUNCATE takes an ACCESS EXCLUSIVE lock and
    # cannot be rolled back cleanly alongside the DDL below in every Postgres
    # configuration. The table is regenerable either way.
    op.execute("DELETE FROM embeddings")

    # No USING clause: the cast is only valid because the table is now empty. With
    # rows present Postgres would (correctly) refuse to change the dimension.
    op.execute(f"ALTER TABLE embeddings ALTER COLUMN embedding TYPE {NEW_TYPE}({NEW_DIM})")

    # `dim` is provenance, and stale values would make the re-index utility think
    # rows were already current.
    op.execute(f"ALTER TABLE embeddings ALTER COLUMN dim SET DEFAULT {NEW_DIM}")

    _create_indexes(NEW_TYPE)


def downgrade() -> None:
    """Return to ``vector(1536)``.

    Also destructive, and for the same reason: 2048-d vectors do not fit a 1536-d
    column, and slicing them here would silently produce a differently-scaled space
    (Matryoshka truncation requires re-normalisation, which SQL will not do). Re-run
    the re-index utility against the older model afterwards.
    """
    _drop_indexes()
    op.execute("DELETE FROM embeddings")
    op.execute(f"ALTER TABLE embeddings ALTER COLUMN embedding TYPE {OLD_TYPE}({OLD_DIM})")
    op.execute(f"ALTER TABLE embeddings ALTER COLUMN dim SET DEFAULT {OLD_DIM}")
    _create_indexes(OLD_TYPE)

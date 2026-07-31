"""Composite index for the embedding duplicate-reuse lookup.

``EmbeddingRepository.existing_hashes`` answers "which content hashes has this
project already embedded, under this exact version set, and which row holds each
vector". It runs once per level on every embedding stage, against a table that
grows without bound.

The query was rewritten from ``GROUP BY content_hash`` with ``min(id)`` - which
could not execute at all, because Postgres has no ``min(uuid)`` aggregate - to
``DISTINCT ON (content_hash)`` with a deterministic ordering. ``DISTINCT ON``
requires its input sorted, where ``GROUP BY`` could have hash-aggregated, so
without a matching index the rewrite would trade a broken query for a slower one.

This index is what makes the ordering free. The five equality predicates lead, so
the trailing keys come back already ordered by ``(content_hash, created_at, id)``
- precisely the ``ORDER BY``. The plan collapses from::

    Seq Scan -> Sort (quicksort) -> Unique          16.6 ms, 1041 buffers

to::

    Index Only Scan -> Unique                        3.2 ms,   53 buffers

measured on 40k rows with 4k matching. It is an *index only* scan because every
projected column is present, so the heap is never visited.

Created ``CONCURRENTLY`` is deliberately **not** used: this table is small at the
point of upgrade for existing deployments, and a concurrent build cannot run inside
Alembic's transaction. A deployment with a large embeddings table should build it
out of band first; the ``IF NOT EXISTS`` guard then makes this migration a no-op.
"""

from __future__ import annotations

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None

INDEX_NAME = "ix_embeddings_reuse_lookup"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {INDEX_NAME}
            ON embeddings (
                project_id,
                level,
                model,
                embedding_version,
                strategy_version,
                content_hash,
                created_at,
                id
            )
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")

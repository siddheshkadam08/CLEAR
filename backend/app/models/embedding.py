"""Vector store (pgvector) - the mandatory three-level embedding hierarchy (§14).

One physical table with a ``level`` discriminator:

* **L1 ``document_summary``** - executive summary + metadata + key topics. Used to
  pick candidate *documents* cheaply before descending.
* **L2 ``clause``** - whole legal clauses. Clause search, similarity, risk.
* **L3 ``chunk``** - detailed context. RAG evidence retrieval.

One table rather than three keeps the retrieval planner's SQL uniform (a level
filter instead of a table switch) while per-level partial HNSW indexes give the
same selectivity as separate tables. The dimension comes from
``EMBEDDING_DIM``, so switching provider is configuration plus a re-embed, never
a schema change.

Duplicate detection: ``content_hash`` plus the version columns. If the source
text and every relevant version are unchanged, the existing vector is reused and
no provider call is made (§14).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.core.enums import EmbeddingLevel
from app.db.base import Base, UUIDPrimaryKeyMixin
from app.db.types import pg_enum, vector_column, vector_ops

_settings = get_settings()

#: Must track the column type - `vector_cosine_ops` on a halfvec column fails
#: index creation outright rather than degrading.
_VECTOR_OPS = vector_ops()


class Embedding(Base, UUIDPrimaryKeyMixin):
    """A single vector at one of the three levels."""

    __tablename__ = "embeddings"

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    level: Mapped[EmbeddingLevel] = mapped_column(
        pg_enum(EmbeddingLevel, "embedding_level"), nullable=False, index=True
    )
    #: The row this vector represents: a chunk id, a clause id, or the contract id
    #: for a document summary. Not a FK - it is polymorphic across three tables,
    #: and the ``ON DELETE CASCADE`` on ``contract_id`` already guarantees no
    #: vector outlives its contract.
    ref_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)

    embedding: Mapped[Any] = mapped_column(vector_column(), nullable=False)

    #: The exact text that was embedded. Kept so a vector can be explained and so
    #: re-embedding does not have to reconstruct the composed input.
    source_text: Mapped[str | None] = mapped_column(String, nullable=True)
    #: SHA-256 of ``source_text``. The duplicate-detection key.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- provenance (§25) ----------------------------------------------------
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False, default=_settings.embedding.dim)
    embedding_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    profile_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_artifact_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Filterable attributes duplicated onto the vector row. Metadata-first
    #: retrieval filters *before* the ANN scan, and a co-located copy avoids
    #: joining ``contract_metadata`` inside the vector query's hot path.
    filter_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # One current vector per (level, ref, model, version): re-running the
        # embedding stage upserts instead of duplicating.
        UniqueConstraint(
            "level",
            "ref_id",
            "model",
            "embedding_version",
            name="uq_embeddings_level_ref_model_version",
        ),
        # Metadata pre-filter path.
        Index("ix_embeddings_project_level", "project_id", "level"),
        Index("ix_embeddings_contract_level", "contract_id", "level"),
        Index("ix_embeddings_filter_metadata", "filter_metadata", postgresql_using="gin"),
        # ---- HNSW indexes, one per level -----------------------------------
        # Partial indexes keep each graph small, so an L1 candidate search never
        # traverses millions of L3 chunk vectors. Cosine distance matches the
        # normalised embeddings every supported provider returns.
        Index(
            "ix_embeddings_hnsw_document_summary",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={
                "m": _settings.embedding.hnsw_m,
                "ef_construction": _settings.embedding.hnsw_ef_construction,
            },
            postgresql_ops={"embedding": _VECTOR_OPS},
            postgresql_where=text("level = 'document_summary'"),
        ),
        Index(
            "ix_embeddings_hnsw_clause",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={
                "m": _settings.embedding.hnsw_m,
                "ef_construction": _settings.embedding.hnsw_ef_construction,
            },
            postgresql_ops={"embedding": _VECTOR_OPS},
            postgresql_where=text("level = 'clause'"),
        ),
        Index(
            "ix_embeddings_hnsw_chunk",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={
                "m": _settings.embedding.hnsw_m,
                "ef_construction": _settings.embedding.hnsw_ef_construction,
            },
            postgresql_ops={"embedding": _VECTOR_OPS},
            postgresql_where=text("level = 'chunk'"),
        ),
    )


__all__ = ["Embedding"]

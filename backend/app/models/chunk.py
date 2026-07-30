"""Semantic chunks - the output of the chunking engine (§12).

A chunk is a *meaning-preserving* unit, never a fixed-size window. Three
properties make the rest of the platform work:

* **Hierarchy** - ``parent_chunk_id`` gives document → section → clause →
  paragraph, which is what hierarchical retrieval walks.
* **Coordinates** - ``bounding_boxes`` on every chunk, so any retrieved evidence
  can be highlighted on the source page.
* **Reading order** - ``reading_order`` is the global block index from the CDM, so
  chunking is deterministic and re-chunking the same document produces the same
  ordering.

A clause split across a page break is stored as **one** chunk with a page range,
not two fragments.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ChunkType
from app.db.base import Base, EvidenceMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import TSVector, pg_enum

if TYPE_CHECKING:
    pass


class Chunk(Base, UUIDPrimaryKeyMixin, TimestampMixin, EvidenceMixin):
    """A semantic chunk of a contract."""

    __tablename__ = "chunks"

    contract_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Parent in the chunk hierarchy. Self-referential FK with ``SET NULL`` so a
    #: partial rebuild can never orphan a subtree into a dangling reference.
    parent_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True
    )

    section_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    section_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: Clause number exactly as printed in the contract ("11.2"). Stored as text, not
    #: parsed into components: a citation has to reproduce what the document says,
    #: and contracts number themselves in ways no scheme survives ("4.2(b)(iii)").
    clause_number: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: Depth in the section hierarchy; 0 is a document-level chunk.
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    chunk_type: Mapped[ChunkType] = mapped_column(
        pg_enum(ChunkType, "chunk_type"), nullable=False, index=True
    )
    text_content: Mapped[str] = mapped_column("text", Text, nullable=False)

    #: Global block index from the CDM - the deterministic ordering key.
    reading_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)

    #: True when the chunk spans a page break - i.e. one logical clause was
    #: reassembled from continuation text.
    is_cross_page: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Table chunks keep their structure so rows are never split mid-record.
    table_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    #: Versions that produced this chunk set. Chunking is re-run only when one of
    #: these moves (§25 selective regeneration).
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    strategy: Mapped[str] = mapped_column(String(32), nullable=False, default="hybrid")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    #: Precomputed lexical vector for the keyword leg of hybrid search. Maintained
    #: by a database trigger, so it cannot drift from ``text``.
    search_vector: Mapped[Any | None] = mapped_column(TSVector, nullable=True)

    #: Denormalised filter columns copied from the contract so the keyword and
    #: vector legs of a search can filter without joining ``contracts``.
    agreement_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    parent: Mapped[Chunk | None] = relationship(
        "Chunk", remote_side="Chunk.id", back_populates="children", lazy="noload"
    )
    children: Mapped[list[Chunk]] = relationship(
        "Chunk", back_populates="parent", lazy="noload", viewonly=True
    )

    __table_args__ = (
        # Deterministic identity of a chunk within a generation, so a re-run
        # upserts rather than duplicating.
        UniqueConstraint(
            "contract_id", "version", "reading_order", name="uq_chunks_contract_version_order"
        ),
        Index("ix_chunks_contract_order", "contract_id", "reading_order"),
        Index("ix_chunks_project_type", "project_id", "chunk_type"),
        Index("ix_chunks_parent", "parent_chunk_id"),
        # Keyword search leg of hybrid retrieval.
        Index("ix_chunks_search_vector", "search_vector", postgresql_using="gin"),
        # Trigram index supports "find this phrase" without full FTS parsing.
        Index(
            "ix_chunks_text_trgm",
            "text",
            postgresql_using="gin",
            postgresql_ops={"text": "gin_trgm_ops"},
        ),
        CheckConstraint("token_count >= 0", name="token_count_non_negative"),
        CheckConstraint("char_count >= 0", name="char_count_non_negative"),
    )

    @property
    def page_range(self) -> str:
        if self.page_start is None:
            return ""
        if self.page_end is None or self.page_end == self.page_start:
            return str(self.page_start)
        return f"{self.page_start}-{self.page_end}"


__all__ = ["Chunk"]

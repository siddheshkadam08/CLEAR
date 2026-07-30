"""Chunk persistence (§12).

Two things here are load-bearing rather than incidental:

* **Insertion order.** ``chunks.parent_chunk_id`` is a self-referential foreign key,
  so a child inserted before its parent would violate it. Rows are therefore
  inserted in ascending ``level`` order - a child's level is always its parent's
  level plus one, so that ordering guarantees parents land first, across batch
  boundaries as well as within them.
* **Project scope on every read.** Chunks are the retrieval substrate; a chunk query
  that forgets ``project_id`` is a cross-project evidence leak (§1.1), so scope is a
  required argument on every method rather than an optional filter.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select

from app.ai.chunking.models import SemanticChunk
from app.core.enums import ChunkType
from app.core.logging import get_logger
from app.models.chunk import Chunk
from app.repositories.base import ProjectScopedRepository, table_of

logger = get_logger(__name__)

#: Rows per INSERT. PostgreSQL evaluates foreign keys at end-of-statement, so a
#: parent and child in the same batch are fine; batching only bounds memory and
#: statement size.
_INSERT_BATCH = 500


class ChunkRepository(ProjectScopedRepository[Chunk]):
    model = Chunk

    async def persist(
        self,
        *,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        chunks: Sequence[SemanticChunk],
        strategy: str,
        engine_version: str,
        strategy_version: str,
        version: int = 1,
        language: str | None = None,
        agreement_type: str | None = None,
    ) -> dict[str, uuid.UUID]:
        """Insert a chunk set, returning engine chunk id → database id.

        The mapping is the return value because the embedding and extraction stages
        reference chunks by database id while the artifact - the reproducible record
        of what chunking produced - references them by engine id.
        """
        if not chunks:
            return {}

        # Engine id → database id, allocated up front so parent links can be
        # resolved without a second pass.
        id_map: dict[str, uuid.UUID] = {chunk.chunk_id: uuid.uuid4() for chunk in chunks}

        rows: list[dict[str, Any]] = []
        for chunk in chunks:
            parent_db_id = id_map.get(chunk.parent_id) if chunk.parent_id else None
            if chunk.parent_id and parent_db_id is None:
                # The engine's validator rejects dangling parents, so this means a
                # parent was dropped between chunking and persistence. Detach rather
                # than fail: an orphaned chunk is still retrievable evidence, a
                # failed insert loses the whole document.
                logger.warning(
                    "chunk_parent_missing",
                    contract_id=str(contract_id),
                    chunk_id=chunk.chunk_id,
                    parent_id=chunk.parent_id,
                )
            rows.append(
                {
                    "id": id_map[chunk.chunk_id],
                    "contract_id": contract_id,
                    "project_id": project_id,
                    "parent_chunk_id": parent_db_id,
                    "section_id": chunk.section_id,
                    "section_title": chunk.section_title,
                    "clause_number": chunk.clause_number,
                    "level": chunk.level,
                    "chunk_type": chunk.chunk_type,
                    "text": chunk.text,
                    "reading_order": chunk.reading_order,
                    "token_count": chunk.token_count,
                    "char_count": chunk.char_count,
                    "language": chunk.language or language,
                    "is_cross_page": chunk.is_cross_page,
                    "table_data": chunk.table_data,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "bounding_boxes": [box.to_dict() for box in chunk.bounding_boxes],
                    # The audit link back to the canonical document: which CDM
                    # elements this chunk was assembled from.
                    "evidence": {
                        "source_element_ids": chunk.source_element_ids,
                        "page_range": chunk.page_range,
                    },
                    "strategy": strategy,
                    "engine_version": engine_version,
                    "strategy_version": strategy_version,
                    "version": version,
                    "agreement_type": agreement_type,
                }
            )

        # Parents before children - see the module docstring.
        rows.sort(key=lambda row: (row["level"], row["reading_order"]))

        for start in range(0, len(rows), _INSERT_BATCH):
            batch = rows[start : start + _INSERT_BATCH]
            await self.db.execute(table_of(Chunk).insert(), batch)
        await self.db.flush()

        return id_map

    # =========================================================================
    # Reads - every one scoped by project
    # =========================================================================
    def _ordered(self, contract_id: uuid.UUID, project_id: uuid.UUID) -> Select[tuple[Chunk]]:
        return (
            self.scoped(project_id)
            .where(Chunk.contract_id == contract_id)
            .order_by(Chunk.reading_order)
        )

    async def list_for_contract(
        self,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        chunk_types: Sequence[ChunkType] | None = None,
        min_tokens: int | None = None,
    ) -> Sequence[Chunk]:
        stmt = self._ordered(contract_id, project_id)
        if chunk_types:
            stmt = stmt.where(Chunk.chunk_type.in_(list(chunk_types)))
        if min_tokens is not None:
            stmt = stmt.where(Chunk.token_count >= min_tokens)
        return (await self.db.execute(stmt)).scalars().all()

    async def list_by_ids(
        self, chunk_ids: Sequence[uuid.UUID], project_id: uuid.UUID
    ) -> Sequence[Chunk]:
        """Fetch specific chunks - the hydration step after a vector search.

        Scoped by project even though the ids came from an index query: the index is
        a cache, and the isolation boundary is enforced at the row, not upstream.
        """
        if not chunk_ids:
            return []
        stmt = self.scoped(project_id).where(Chunk.id.in_(list(chunk_ids)))
        return (await self.db.execute(stmt)).scalars().all()

    async def neighbours(
        self,
        chunk: Chunk,
        project_id: uuid.UUID,
        *,
        window: int = 1,
    ) -> Sequence[Chunk]:
        """Chunks immediately around one chunk, in reading order.

        Context assembly uses this to widen a retrieved clause into the passage it
        sits in - a cap that reads "as set out in clause 9" is unusable without its
        neighbour.
        """
        if window <= 0:
            return []
        stmt = (
            self.scoped(project_id)
            .where(
                Chunk.contract_id == chunk.contract_id,
                Chunk.version == chunk.version,
                Chunk.reading_order.between(
                    chunk.reading_order - window, chunk.reading_order + window
                ),
                Chunk.id != chunk.id,
            )
            .order_by(Chunk.reading_order)
        )
        return (await self.db.execute(stmt)).scalars().all()

    async def ancestors(self, chunk: Chunk, project_id: uuid.UUID) -> list[Chunk]:
        """Walk parent links to the root, nearest ancestor first.

        Hierarchical retrieval (§14) answers a clause-level hit with its section-level
        context; this is that walk. Bounded by a depth cap so a cycle introduced by a
        bad partial rebuild cannot spin.
        """
        chain: list[Chunk] = []
        current = chunk
        seen = {chunk.id}
        for _ in range(12):
            if current.parent_chunk_id is None:
                break
            parent = await self.get_scoped(current.parent_chunk_id, project_id)
            if parent is None or parent.id in seen:
                break
            chain.append(parent)
            seen.add(parent.id)
            current = parent
        return chain

    async def children_of(self, chunk_id: uuid.UUID, project_id: uuid.UUID) -> Sequence[Chunk]:
        stmt = (
            self.scoped(project_id)
            .where(Chunk.parent_chunk_id == chunk_id)
            .order_by(Chunk.reading_order)
        )
        return (await self.db.execute(stmt)).scalars().all()

    async def find_by_clause_number(
        self, contract_id: uuid.UUID, project_id: uuid.UUID, clause_number: str
    ) -> Sequence[Chunk]:
        """Resolve a citation like "9.2" back to its chunk(s)."""
        stmt = self._ordered(contract_id, project_id).where(Chunk.clause_number == clause_number)
        return (await self.db.execute(stmt)).scalars().all()

    async def latest_version(self, contract_id: uuid.UUID, project_id: uuid.UUID) -> int:
        stmt = select(func.max(Chunk.version)).where(
            Chunk.contract_id == contract_id, Chunk.project_id == project_id
        )
        return int((await self.db.execute(stmt)).scalar() or 0)

    async def statistics(self, contract_id: uuid.UUID, project_id: uuid.UUID) -> dict[str, Any]:
        """Counts by type plus token totals, for the contract detail screen."""
        stmt = (
            select(
                Chunk.chunk_type,
                # Not labelled "count": a SQLAlchemy Row is tuple-like, so a label
                # colliding with a tuple method (`count`, `index`) resolves to the
                # *method* rather than the value, and `int(row.count)` raises.
                func.count().label("chunk_count"),
                func.coalesce(func.sum(Chunk.token_count), 0).label("token_total"),
            )
            .where(Chunk.contract_id == contract_id, Chunk.project_id == project_id)
            .group_by(Chunk.chunk_type)
        )
        rows = (await self.db.execute(stmt)).all()
        by_type = {
            (row.chunk_type.value if hasattr(row.chunk_type, "value") else str(row.chunk_type)): {
                "count": int(row.chunk_count),
                "tokens": int(row.token_total),
            }
            for row in rows
        }
        return {
            "by_type": by_type,
            "count": sum(item["count"] for item in by_type.values()),
            "total_tokens": sum(item["tokens"] for item in by_type.values()),
        }


__all__ = ["ChunkRepository"]

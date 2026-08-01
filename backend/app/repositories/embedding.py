"""Vector store access (§14).

The methods here are the only path to the embeddings table. Two rules are enforced
structurally rather than by convention:

* **Every query is project-scoped.** A vector search that omits ``project_id``
  returns evidence from contracts the caller has no membership for - the exact
  cross-project leak §1.1 prohibits. ``project_id`` is a required argument on every
  read, and the similarity search additionally filters *before* the ANN scan so the
  pre-filter is part of the plan rather than a post-hoc discard.
* **Duplicate reuse is a lookup, not a guess.** ``existing_hashes`` returns the
  content hashes already embedded under the current version set, so the embedding
  stage can skip provider calls for unchanged text (§14) without ever reusing a
  vector produced by a different model or strategy version.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, delete, func, select

from app.core.enums import EmbeddingLevel
from app.models.embedding import Embedding
from app.repositories.base import ProjectScopedRepository, affected_rows, table_of

#: Rows per INSERT. Vectors are large; batching bounds statement size and memory.
_INSERT_BATCH = 200


@dataclass(slots=True)
class VectorMatch:
    """One similarity hit, with the distance the index actually returned."""

    embedding_id: uuid.UUID
    ref_id: uuid.UUID
    contract_id: uuid.UUID
    level: EmbeddingLevel
    #: Cosine distance in [0, 2]; lower is closer.
    distance: float
    source_text: str | None
    filter_metadata: dict[str, Any]

    @property
    def score(self) -> float:
        """Cosine similarity in [-1, 1], which is what a user-facing score means."""
        return 1.0 - self.distance


class EmbeddingRepository(ProjectScopedRepository[Embedding]):
    model = Embedding

    # =========================================================================
    # Writes
    # =========================================================================
    async def insert_many(self, rows: Sequence[dict[str, Any]], *, validate: bool = True) -> int:
        """Insert vectors, refusing any that are not comparable with the rest.

        Validation is on by default and enforced *here*, at the single write
        boundary, rather than in the stage that happens to call it. A vector from
        a different model is not a bad row that fails loudly - it inserts fine and
        then silently corrupts every similarity search that touches the index, so
        the check belongs somewhere no future caller can forget it.

        ``validate=False`` exists for the reindex path, which is deliberately
        writing a *new* space and has already cleared the old one.
        """
        if not rows:
            return 0

        if validate:
            from app.ai.embedding.validation import EmbeddingValidator

            rows, _report = EmbeddingValidator().validate_batch(rows, strict=True)
            if not rows:
                return 0

        for start in range(0, len(rows), _INSERT_BATCH):
            await self.db.execute(
                table_of(Embedding).insert(), list(rows[start : start + _INSERT_BATCH])
            )
        await self.db.flush()
        return len(rows)

    async def space_census(self, project_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
        """Stored vectors grouped by embedding space.

        Feeds the consistency audit and the reindex planner: "how many vectors
        exist, in how many mutually incomparable spaces".
        """
        stmt = select(
            Embedding.provider,
            Embedding.model,
            Embedding.dim,
            Embedding.embedding_version,
            Embedding.strategy_version,
            # Not `.label("count")`: a Row is a tuple, so `row.count` resolves to
            # `tuple.count` - the method, not this column - and the read silently
            # returns a bound method instead of a number.
            func.count().label("vector_count"),
        ).group_by(
            Embedding.provider,
            Embedding.model,
            Embedding.dim,
            Embedding.embedding_version,
            Embedding.strategy_version,
        )
        if project_id is not None:
            stmt = stmt.where(Embedding.project_id == project_id)

        rows = (await self.db.execute(stmt)).all()
        return [
            {
                "provider": r.provider,
                "model": r.model,
                "dim": r.dim,
                "embedding_version": r.embedding_version,
                "strategy_version": r.strategy_version,
                "count": int(r.vector_count),
            }
            for r in rows
        ]

    async def delete_for_contract_level(
        self, contract_id: uuid.UUID, project_id: uuid.UUID, level: EmbeddingLevel
    ) -> int:
        """Delete one level's vectors.

        Used by selective regeneration: a changed chunking strategy invalidates L3
        without touching the L1 document summary that is still accurate (§25).
        """
        result = await self.db.execute(
            delete(Embedding).where(
                Embedding.contract_id == contract_id,
                Embedding.project_id == project_id,
                Embedding.level == level,
            )
        )
        await self.db.flush()
        return affected_rows(result)

    async def delete_for_refs(self, ref_ids: Sequence[uuid.UUID], project_id: uuid.UUID) -> int:
        if not ref_ids:
            return 0
        result = await self.db.execute(
            delete(Embedding).where(
                Embedding.ref_id.in_(list(ref_ids)),
                Embedding.project_id == project_id,
            )
        )
        await self.db.flush()
        return affected_rows(result)

    # =========================================================================
    # Duplicate detection
    # =========================================================================
    async def existing_hashes(
        self,
        *,
        project_id: uuid.UUID,
        level: EmbeddingLevel,
        model: str,
        embedding_version: str,
        strategy_version: str,
    ) -> dict[str, uuid.UUID]:
        """Content hash → an embedding id already holding that vector.

        Scoped to the project deliberately. Identical clause text across two projects
        would produce an identical vector, but reusing one project's row for another
        would put a foreign ``contract_id`` in the second project's result set.

        ``DISTINCT ON`` rather than ``GROUP BY``: the caller wants *one representative
        row* per hash, not an aggregate of the group. Postgres has no ``min(uuid)``
        aggregate, so expressing "pick one" as ``min(id)`` did not merely read oddly -
        it failed outright with ``UndefinedFunctionError`` and took the whole embedding
        stage down with it. ``DISTINCT ON`` is the operation that was meant all along.

        The ordering picks the **oldest** row, and is total. That matters more than it
        looks: the returned id is used to reuse a stored vector, so an unstable choice
        would make reuse - and therefore the resulting index - non-deterministic
        between runs. ``created_at`` alone is not enough, because rows written in one
        transaction share ``now()``; ``id`` breaks that tie. Ordering UUIDs is fine -
        it is only *aggregating* them that Postgres lacks an operator for.
        """
        stmt = (
            select(Embedding.content_hash, Embedding.id)
            .where(
                Embedding.project_id == project_id,
                Embedding.level == level,
                Embedding.model == model,
                Embedding.embedding_version == embedding_version,
                Embedding.strategy_version == strategy_version,
            )
            .distinct(Embedding.content_hash)
            .order_by(Embedding.content_hash, Embedding.created_at, Embedding.id)
        )
        rows = (await self.db.execute(stmt)).all()
        # One row per hash by construction, so the mapping cannot lose an entry.
        return dict(rows)  # type: ignore[arg-type]

    async def get_vector(self, embedding_id: uuid.UUID, project_id: uuid.UUID) -> Any | None:
        """Fetch a stored vector for reuse, without loading the whole row."""
        stmt = select(Embedding.embedding).where(
            Embedding.id == embedding_id, Embedding.project_id == project_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    # =========================================================================
    # Similarity search
    # =========================================================================
    def _search_stmt(
        self,
        *,
        query_vector: Sequence[float],
        project_ids: Sequence[uuid.UUID],
        level: EmbeddingLevel,
        limit: int,
        contract_ids: Sequence[uuid.UUID] | None,
        metadata_filters: dict[str, Any] | None,
        max_distance: float | None,
        model: str | None,
    ) -> Select[Any]:
        distance = Embedding.embedding.cosine_distance(list(query_vector)).label("distance")
        stmt = select(
            Embedding.id,
            Embedding.ref_id,
            Embedding.contract_id,
            Embedding.level,
            distance,
            Embedding.source_text,
            Embedding.filter_metadata,
        ).where(
            # The isolation boundary, and the first predicate the planner sees.
            Embedding.project_id.in_(list(project_ids)),
            Embedding.level == level,
        )

        if contract_ids:
            stmt = stmt.where(Embedding.contract_id.in_(list(contract_ids)))
        if model:
            # Never compare vectors from two different models: the distances are not
            # in the same space and the ranking would be meaningless.
            stmt = stmt.where(Embedding.model == model)
        if metadata_filters:
            # Metadata-first retrieval: JSONB containment resolves through the GIN
            # index before the ANN scan runs.
            stmt = stmt.where(Embedding.filter_metadata.contains(metadata_filters))
        if max_distance is not None:
            stmt = stmt.where(distance <= max_distance)

        return stmt.order_by(distance).limit(limit)

    async def search(
        self,
        *,
        query_vector: Sequence[float],
        project_ids: Sequence[uuid.UUID],
        level: EmbeddingLevel,
        limit: int = 20,
        contract_ids: Sequence[uuid.UUID] | None = None,
        metadata_filters: dict[str, Any] | None = None,
        max_distance: float | None = None,
        model: str | None = None,
    ) -> list[VectorMatch]:
        """Nearest neighbours within an explicit set of projects.

        ``project_ids`` is plural because a cross-contract question spans the
        projects the *caller is a member of* - never the whole table. An empty list
        returns nothing rather than everything: a caller who resolved no accessible
        projects must not fall through to an unscoped scan.
        """
        if not project_ids or not query_vector:
            return []

        stmt = self._search_stmt(
            query_vector=query_vector,
            project_ids=project_ids,
            level=level,
            limit=limit,
            contract_ids=contract_ids,
            metadata_filters=metadata_filters,
            max_distance=max_distance,
            model=model,
        )
        rows = (await self.db.execute(stmt)).all()
        return [
            VectorMatch(
                embedding_id=row[0],
                ref_id=row[1],
                contract_id=row[2],
                level=row[3],
                distance=float(row[4]),
                source_text=row[5],
                filter_metadata=row[6] or {},
            )
            for row in rows
        ]

    # =========================================================================
    # Reporting
    # =========================================================================
    async def counts_by_level(
        self, contract_id: uuid.UUID, project_id: uuid.UUID
    ) -> dict[str, int]:
        stmt = (
            select(Embedding.level, func.count())
            .where(
                Embedding.contract_id == contract_id,
                Embedding.project_id == project_id,
            )
            .group_by(Embedding.level)
        )
        rows = (await self.db.execute(stmt)).all()
        return {
            (row[0].value if hasattr(row[0], "value") else str(row[0])): int(row[1]) for row in rows
        }


__all__ = ["EmbeddingRepository", "VectorMatch"]

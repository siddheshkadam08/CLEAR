"""Re-run chunking against a stored canonical document, without the pipeline.

Chunking is deterministic given a CDM and a config, which makes the interesting
question cheap to answer: *would a different threshold have kept those chunks?*
Answering it through the pipeline means a full reprocess, a job row and a write
to the chunk table, per attempt - so in practice nobody asks, and thresholds stay
at whatever they were first set to.

This module answers it directly. It reads the CDM artifact the parser already
produced, runs the engine in memory, and returns the diagnostics. Nothing is
written, so it is safe against production data and can be run repeatedly while
sweeping a parameter.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.cdm.models import CanonicalDocument
from app.ai.chunking.engine import ChunkConfig, ChunkingEngine
from app.core.enums import ArtifactKind, ChunkStrategy
from app.core.logging import get_logger
from app.repositories.processing import DocumentArtifactRepository
from app.storage.base import IObjectStorage

logger = get_logger(__name__)


@dataclass(slots=True)
class ReplayOutcome:
    """One chunking run over a stored document."""

    contract_id: uuid.UUID
    strategy: str
    config: dict[str, Any]
    accepted: int
    rejected: int
    diagnostics: dict[str, Any] = field(default_factory=dict)
    statistics: dict[str, Any] = field(default_factory=dict)

    @property
    def acceptance_rate(self) -> float:
        total = self.accepted + self.rejected
        return round(self.accepted / total, 4) if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_id": str(self.contract_id),
            "strategy": self.strategy,
            "config": self.config,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "acceptance_rate": self.acceptance_rate,
            "statistics": self.statistics,
            "diagnostics": self.diagnostics,
        }


class ChunkingReplay:
    """Replays chunking over an already-parsed document. Never writes."""

    def __init__(self, db: AsyncSession, storage: IObjectStorage) -> None:
        self.db = db
        self.storage = storage

    async def load_document(self, contract_id: uuid.UUID) -> CanonicalDocument:
        """The CDM the parser produced, as the chunking stage would read it."""
        pointer = await DocumentArtifactRepository(self.db).current(
            contract_id, ArtifactKind.CANONICAL_DOCUMENT
        )
        if pointer is None:
            raise LookupError(
                f"Contract {contract_id} has no canonical document artifact. "
                "It has not been parsed."
            )
        try:
            payload = await self.storage.get_json(pointer.storage_path)
        except Exception as exc:
            # The database says the artifact exists and object storage disagrees.
            # Worth naming precisely: a bare "Object not found" sends someone
            # looking for a bug in this command, when the actual problem is that
            # the row and the bucket have drifted apart - which is what a restore
            # or a migration that moved one but not the other leaves behind.
            raise LookupError(
                f"Contract {contract_id} has a canonical document artifact recorded at "
                f"'{pointer.storage_path}', but object storage has no such object "
                f"({exc}). The database and the object store have diverged - the "
                "contract needs re-parsing before chunking can be replayed."
            ) from exc
        return CanonicalDocument.model_validate(payload)

    async def run(
        self,
        contract_id: uuid.UUID,
        *,
        strategy: ChunkStrategy | str | None = None,
        max_tokens: int | None = None,
        min_tokens: int | None = None,
        overlap_tokens: int | None = None,
        document: CanonicalDocument | None = None,
    ) -> ReplayOutcome:
        """Chunk one contract with the given overrides and report what happened."""
        document = document or await self.load_document(contract_id)
        config = ChunkConfig()
        if strategy is not None:
            config.strategy = ChunkStrategy(str(strategy))
        for name, value in (
            ("max_tokens", max_tokens),
            ("min_tokens", min_tokens),
            ("overlap_tokens", overlap_tokens),
        ):
            if value is not None:
                setattr(config, name, int(value))

        result = ChunkingEngine().chunk(document, config)
        return ReplayOutcome(
            contract_id=contract_id,
            strategy=config.strategy.value,
            config=config.as_dict(),
            accepted=len(result.chunks),
            rejected=result.validation.rejection_count,
            diagnostics=result.validation.diagnostics(sample_limit=50),
            statistics=result.statistics.as_dict(),
        )

    async def sweep(
        self,
        contract_id: uuid.UUID,
        *,
        strategies: list[ChunkStrategy | str] | None = None,
        min_tokens_values: list[int] | None = None,
    ) -> list[ReplayOutcome]:
        """Run several configurations over the same document and compare.

        The document is loaded once and reused across runs: the comparison is only
        meaningful if every configuration sees identical input, and re-reading it
        per run would be the slow part anyway.
        """
        document = await self.load_document(contract_id)
        outcomes: list[ReplayOutcome] = []

        for strategy in strategies or [None]:  # type: ignore[list-item]
            for min_tokens in min_tokens_values or [None]:  # type: ignore[list-item]
                outcomes.append(
                    await self.run(
                        contract_id,
                        strategy=strategy,
                        min_tokens=min_tokens,
                        document=document,
                    )
                )

        logger.info(
            "chunking_replay_sweep",
            contract_id=str(contract_id),
            runs=len(outcomes),
            best_acceptance=max((o.acceptance_rate for o in outcomes), default=0.0),
        )
        return outcomes


__all__ = ["ChunkingReplay", "ReplayOutcome"]

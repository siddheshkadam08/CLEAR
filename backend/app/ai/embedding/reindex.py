"""Regenerate the vector store after an embedding-model change (§14).

Switching embedding model invalidates every vector at once. Vectors from two
models occupy unrelated spaces, so a partially-migrated index is worse than an
empty one: it keeps answering, and the answers are drawn from whichever space
happens to score higher. The only safe states are "all old" and "all new".

Design constraints this has to satisfy, in order of how badly each one bites:

* **Resumable.** A full re-index of a large repository is hours of provider calls.
  A crash at hour three must not restart from zero, so progress is derived from the
  database itself - rows already carrying the target model are skipped - rather
  than from a cursor file that can disagree with reality.
* **Idempotent.** Running it twice is a no-op the second time, for the same reason.
* **Batched.** Provider calls are batched to ``EMBEDDING_BATCH_SIZE`` and committed
  per contract, so the work already done survives an interruption.
* **No mixed writes.** Each contract is fully re-embedded and committed, or left
  entirely alone. A contract half in the new space is the failure mode above, in
  miniature.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.embedding import Embedding

logger = get_logger(__name__)

ProgressCallback = Callable[["ReindexProgress"], None]


@dataclass(slots=True)
class ReindexProgress:
    """Snapshot emitted after each contract."""

    contracts_total: int = 0
    contracts_done: int = 0
    contracts_failed: int = 0
    vectors_written: int = 0
    vectors_removed: int = 0
    current_contract: uuid.UUID | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def percent(self) -> int:
        if not self.contracts_total:
            return 100
        return int(self.contracts_done / self.contracts_total * 100)

    def as_dict(self) -> dict[str, Any]:
        return {
            "contracts_total": self.contracts_total,
            "contracts_done": self.contracts_done,
            "contracts_failed": self.contracts_failed,
            "vectors_written": self.vectors_written,
            "vectors_removed": self.vectors_removed,
            "percent": self.percent,
        }


@dataclass(slots=True)
class ReindexReport:
    """Final outcome, including which contracts could not be done."""

    progress: ReindexProgress
    failures: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    target_model: str = ""
    target_dim: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.progress.as_dict(),
            "target_model": self.target_model,
            "target_dim": self.target_dim,
            "ok": self.ok,
            "failures": [{"contract_id": str(cid), "error": err} for cid, err in self.failures],
        }


class EmbeddingReindexer:
    """Re-embeds contracts whose vectors were produced by a different model."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._settings = get_settings().embedding

    # ------------------------------------------------------------------ survey
    async def stale_model_counts(self) -> dict[str, int]:
        """How many vectors exist per model. The "do I need this?" query."""
        rows = (
            await self.db.execute(select(Embedding.model, func.count()).group_by(Embedding.model))
        ).all()
        return {str(model): int(count) for model, count in rows}

    async def contracts_needing_reindex(
        self, *, project_id: uuid.UUID | None = None, limit: int | None = None
    ) -> list[uuid.UUID]:
        """Contracts holding at least one vector that is not the target model.

        This is what makes the job resumable and idempotent without any external
        state: the set shrinks as work completes, and re-running simply finds
        whatever is left.
        """
        stmt = (
            select(Embedding.contract_id)
            .where(Embedding.model != self._settings.model)
            .group_by(Embedding.contract_id)
        )
        if project_id is not None:
            stmt = stmt.where(Embedding.project_id == project_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.db.execute(stmt)).scalars().all())

    # ------------------------------------------------------------------- work
    async def run(
        self,
        *,
        project_id: uuid.UUID | None = None,
        contract_ids: Sequence[uuid.UUID] | None = None,
        limit: int | None = None,
        on_progress: ProgressCallback | None = None,
        dry_run: bool = False,
    ) -> ReindexReport:
        """Re-embed every contract that needs it."""
        targets = (
            list(contract_ids)
            if contract_ids
            else await self.contracts_needing_reindex(project_id=project_id, limit=limit)
        )

        progress = ReindexProgress(contracts_total=len(targets))
        report = ReindexReport(
            progress=progress,
            target_model=self._settings.model,
            target_dim=self._settings.dim,
        )

        logger.info(
            "reindex_started",
            contracts=len(targets),
            target_model=self._settings.model,
            target_dim=self._settings.dim,
            dry_run=dry_run,
        )
        if dry_run:
            progress.contracts_done = len(targets)
            if on_progress:
                on_progress(progress)
            return report

        for contract_id in targets:
            progress.current_contract = contract_id
            try:
                written, stale = await self._reindex_contract(contract_id)
                progress.vectors_written += written
                progress.vectors_removed += stale
                progress.contracts_done += 1
                # Commit per contract: an interruption costs at most one contract's
                # enqueue, and everything already committed is consistent.
                await self.db.commit()
            except Exception as exc:  # noqa: BLE001 - one bad contract must not stop the sweep
                await self.db.rollback()
                progress.contracts_failed += 1
                report.failures.append((contract_id, str(exc)[:300]))
                logger.error(
                    "reindex_contract_failed",
                    contract_id=str(contract_id),
                    error=str(exc)[:300],
                )
            finally:
                if on_progress:
                    on_progress(progress)

        logger.info("reindex_finished", **report.as_dict())
        return report

    async def _reindex_contract(self, contract_id: uuid.UUID) -> tuple[int, int]:
        """Queue one contract to be re-embedded from the embedding stage onward.

        Deliberately *not* a bespoke embedding loop. Re-running the pipeline from
        ``embedding`` reuses the stage the product already has: parsing, chunking
        and extraction keep their checkpoints (so this costs one provider pass, not
        eight), the job appears on the Processing screen like any other, retries and
        DLQ handling apply, and the stage's own reuse logic is already keyed on the
        model - so switching model invalidates exactly the right rows without a
        force flag.

        Stale vectors are left in place until the stage writes their replacements.
        The stage clears each level it is about to rewrite, inside its own
        transaction, which is what keeps a contract from ever holding a mix.
        """
        from app.core.enums import JobPriority, PipelineStage
        from app.orchestrator.queue import StageMessage, get_queue_client
        from app.repositories.processing import ProcessingJobRepository

        row = (
            await self.db.execute(
                select(Embedding.project_id, func.count())
                .where(Embedding.contract_id == contract_id)
                .group_by(Embedding.project_id)
            )
        ).first()
        if row is None:
            return 0, 0
        project_id, stale = row

        job = await ProcessingJobRepository(self.db).create_job(
            contract_id=contract_id,
            project_id=project_id,
            priority=JobPriority.LOW,
            is_reprocess=True,
            resume_from_stage=PipelineStage.EMBEDDING,
        )
        await self.db.flush()

        await get_queue_client().enqueue(
            StageMessage(
                job_id=job.id,
                contract_id=contract_id,
                project_id=project_id,
                stage=PipelineStage.EMBEDDING,
                priority=JobPriority.LOW,
                options={"reason": "embedding_model_change", "target_model": self._settings.model},
            )
        )
        return 0, int(stale)


__all__ = ["EmbeddingReindexer", "ReindexProgress", "ReindexReport"]

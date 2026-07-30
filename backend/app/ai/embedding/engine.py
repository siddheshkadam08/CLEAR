"""The embedding engine - the mandatory three-level hierarchy (§14).

Three levels, each answering a different retrieval question:

* **L1 ``document_summary``** - one vector per contract, composed from its summary,
  key topics, parties and agreement type. Used to pick candidate *documents* cheaply
  before descending, so a repository-wide question does not scan every chunk.
* **L2 ``clause``** - one vector per extracted clause. Clause search, clause
  similarity, "show me every contract with a cap like this one".
* **L3 ``chunk``** - one vector per semantic chunk. The evidence retrieval layer for
  RAG answers.

**Composition is not concatenation.** What gets embedded at each level is built
deliberately: an L2 clause vector includes its type and its key attributes alongside
its text, because a bare quotation of a liability clause embeds almost identically to
every other liability clause, and the attributes are what make them distinguishable.

**Duplicate reuse** is the cost control. A content hash plus the version set
identifies a vector exactly; if the text and every relevant version are unchanged,
the existing vector is reused and no provider call is made. Two contracts sharing a
boilerplate confidentiality clause pay for one vector, not two - and re-processing a
contract after a prompt-only change pays for none.

The engine returns rows to insert; the stage handler writes them. It holds no
session, so it can be exercised against fixtures with no database.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.ai.embedding.providers import (
    EmbeddingUsage,
    IEmbeddingProvider,
    content_hash,
    get_embedding_provider,
)
from app.core import metrics
from app.core.config import get_settings
from app.core.enums import EmbeddingLevel
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.core.versions import EMBEDDING_STRATEGY_VERSION

logger = get_logger(__name__)

#: Hard ceiling on the text handed to a provider, in characters. Well inside every
#: supported model's context, and it prevents one pathological chunk from failing a
#: whole batch. Truncation is recorded, never silent.
_MAX_EMBED_CHARS = 24_000

#: Concurrent provider batches. Bounded so one large contract cannot exhaust the
#: provider's rate limit for every other job on the worker.
_MAX_CONCURRENT_BATCHES = 4


@dataclass(slots=True)
class EmbeddingItem:
    """One thing to embed."""

    #: Row this vector represents: a chunk id, a clause id, or the contract id for L1.
    ref_id: uuid.UUID
    level: EmbeddingLevel
    #: The composed text actually sent to the provider.
    text: str
    #: Filterable attributes copied onto the vector row for metadata-first retrieval.
    filter_metadata: dict[str, Any] = field(default_factory=dict)
    token_estimate: int = 0

    @property
    def hash(self) -> str:
        return content_hash(self.text)


@dataclass(slots=True)
class EmbeddingPlan:
    """What one contract needs embedded, per level."""

    items: list[EmbeddingItem] = field(default_factory=list)

    def by_level(self, level: EmbeddingLevel) -> list[EmbeddingItem]:
        return [item for item in self.items if item.level is level]

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.level.value] = counts.get(item.level.value, 0) + 1
        return counts


@dataclass(slots=True)
class LevelOutcome:
    """What happened at one level."""

    level: EmbeddingLevel
    requested: int = 0
    generated: int = 0
    reused: int = 0
    truncated: int = 0
    skipped_empty: int = 0
    batches: int = 0
    input_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "requested": self.requested,
            "generated": self.generated,
            "reused": self.reused,
            "truncated": self.truncated,
            "skipped_empty": self.skipped_empty,
            "batches": self.batches,
            "input_tokens": self.input_tokens,
            "cost_usd": round(self.cost_usd, 8),
            "latency_ms": self.latency_ms,
            "model": self.model,
            "error": self.error,
        }


@dataclass(slots=True)
class EmbeddingRun:
    """The engine's output: rows to insert, plus accounting."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    outcomes: list[LevelOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def generated(self) -> int:
        return sum(outcome.generated for outcome in self.outcomes)

    @property
    def reused(self) -> int:
        return sum(outcome.reused for outcome in self.outcomes)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(outcome.cost_usd for outcome in self.outcomes), 8)

    @property
    def total_tokens(self) -> int:
        return sum(outcome.input_tokens for outcome in self.outcomes)

    @property
    def failed_levels(self) -> list[str]:
        return [outcome.level.value for outcome in self.outcomes if outcome.error]

    def statistics(self) -> dict[str, Any]:
        return {
            "vectors": len(self.rows),
            "generated": self.generated,
            "reused": self.reused,
            "reuse_rate": round(self.reused / max(self.generated + self.reused, 1), 4),
            "input_tokens": self.total_tokens,
            "cost_usd": self.total_cost_usd,
            "by_level": {o.level.value: o.generated + o.reused for o in self.outcomes},
            "failed_levels": self.failed_levels,
        }


class EmbeddingEngine:
    """Generates the three-level vector set for one contract."""

    def __init__(self, provider: IEmbeddingProvider | None = None) -> None:
        self._provider = provider or get_embedding_provider()
        self._settings = get_settings()

    # =========================================================================
    # Composition
    # =========================================================================
    def compose_document_summary(
        self,
        *,
        contract_id: uuid.UUID,
        title: str | None,
        agreement_type: str | None,
        summary: str | None,
        key_topics: list[str],
        parties: list[str],
        filter_metadata: dict[str, Any],
        includes: list[str] | None = None,
    ) -> EmbeddingItem | None:
        """Compose the L1 document vector.

        Deliberately includes the metadata a user would say out loud - the agreement
        type and the parties - because "the Acme MSA" is how people search, and a
        summary alone often names neither. ``includes`` comes from the profile's
        ``embedding_config.summary_includes``, so which facts compose the vector is
        configuration rather than code.
        """
        wanted = set(includes or ["summary", "key_topics", "parties", "agreement_type"])
        parts: list[str] = []

        if title:
            parts.append(title)
        if "agreement_type" in wanted and agreement_type:
            parts.append(f"Agreement type: {agreement_type.replace('_', ' ')}")
        if "parties" in wanted and parties:
            parts.append(f"Parties: {', '.join(parties)}")
        if "summary" in wanted and summary:
            parts.append(summary)
        if "key_topics" in wanted and key_topics:
            parts.append(f"Topics: {', '.join(key_topics)}")

        text = "\n".join(part.strip() for part in parts if part and part.strip())
        if not text.strip():
            return None

        return EmbeddingItem(
            ref_id=contract_id,
            level=EmbeddingLevel.DOCUMENT_SUMMARY,
            text=text,
            filter_metadata=filter_metadata,
        )

    def compose_clause(
        self,
        *,
        clause_id: uuid.UUID,
        clause_type: str,
        title: str | None,
        clause_number: str | None,
        text: str,
        attributes: dict[str, Any],
        filter_metadata: dict[str, Any],
    ) -> EmbeddingItem | None:
        """Compose an L2 clause vector.

        The clause type and its salient attributes are prepended to the text. Without
        them, every liability clause in the repository embeds into nearly the same
        point - they are 90% identical boilerplate - and clause similarity becomes
        useless precisely where it is most wanted. The attributes are what actually
        differ between a 1x cap and an uncapped one.
        """
        if not text.strip():
            return None

        header = [f"Clause type: {clause_type.replace('_', ' ')}"]
        if clause_number:
            header.append(f"Clause {clause_number}")
        if title:
            header.append(title)

        salient = _salient_attributes(attributes)
        if salient:
            header.append(salient)

        return EmbeddingItem(
            ref_id=clause_id,
            level=EmbeddingLevel.CLAUSE,
            text="\n".join([" | ".join(header), text.strip()]),
            filter_metadata={**filter_metadata, "clause_type": clause_type},
        )

    def compose_chunk(
        self,
        *,
        chunk_id: uuid.UUID,
        text: str,
        section_title: str | None,
        clause_number: str | None,
        chunk_type: str,
        filter_metadata: dict[str, Any],
    ) -> EmbeddingItem | None:
        """Compose an L3 chunk vector.

        The section title is prepended because a chunk lifted out of its section
        often loses what it is about - "such notice shall be in writing" is
        meaningless without knowing it sits under Termination.
        """
        if not text.strip():
            return None

        prefix: list[str] = []
        if section_title:
            prefix.append(section_title)
        if clause_number:
            prefix.append(f"Clause {clause_number}")

        composed = f"{' | '.join(prefix)}\n{text.strip()}" if prefix else text.strip()
        return EmbeddingItem(
            ref_id=chunk_id,
            level=EmbeddingLevel.CHUNK,
            text=composed,
            filter_metadata={**filter_metadata, "chunk_type": chunk_type},
        )

    # =========================================================================
    # Execution
    # =========================================================================
    async def run(
        self,
        plan: EmbeddingPlan,
        *,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        existing: dict[EmbeddingLevel, dict[str, uuid.UUID]] | None = None,
        reusable_vectors: dict[uuid.UUID, Any] | None = None,
        profile_version: str | None = None,
        levels: list[EmbeddingLevel] | None = None,
        on_progress: Any = None,
    ) -> EmbeddingRun:
        """Embed a plan, reusing what has not changed.

        ``existing`` maps each level's already-stored content hashes to the embedding
        id holding that vector; ``reusable_vectors`` supplies the vectors themselves.
        Both come from the stage handler, which is the only part that may touch the
        database.

        A level that fails is recorded and the others continue: L3 chunk embeddings
        failing should not cost the L1 vector that makes the contract findable at all.
        """
        run = EmbeddingRun()
        wanted = set(levels or list(EmbeddingLevel))
        existing = existing or {}
        reusable_vectors = reusable_vectors or {}

        for level in EmbeddingLevel:
            if level not in wanted:
                continue
            items = plan.by_level(level)
            if not items:
                continue

            outcome = LevelOutcome(level=level, requested=len(items))
            try:
                rows = await self._embed_level(
                    items,
                    outcome=outcome,
                    contract_id=contract_id,
                    project_id=project_id,
                    existing=existing.get(level, {}),
                    reusable_vectors=reusable_vectors,
                    profile_version=profile_version,
                )
                run.rows.extend(rows)
            except ProviderError as exc:
                outcome.error = str(exc)
                logger.warning(
                    "embedding_level_failed",
                    level=level.value,
                    contract_id=str(contract_id),
                    error=str(exc),
                )
                run.warnings.append(
                    f"{level.value} embeddings failed: {exc}. The other levels were unaffected."
                )
            except Exception as exc:
                outcome.error = str(exc)
                logger.exception("embedding_level_crashed", level=level.value)
                run.warnings.append(f"{level.value} embeddings failed unexpectedly.")

            run.outcomes.append(outcome)
            if on_progress is not None:
                await on_progress(level, outcome)

        logger.info(
            "embedding_run_completed",
            contract_id=str(contract_id),
            vectors=len(run.rows),
            generated=run.generated,
            reused=run.reused,
            tokens=run.total_tokens,
            cost_usd=run.total_cost_usd,
            failed_levels=run.failed_levels,
        )
        return run

    async def _embed_level(
        self,
        items: list[EmbeddingItem],
        *,
        outcome: LevelOutcome,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        existing: dict[str, uuid.UUID],
        reusable_vectors: dict[uuid.UUID, Any],
        profile_version: str | None,
    ) -> list[dict[str, Any]]:
        settings = self._settings.embedding
        rows: list[dict[str, Any]] = []

        # ---- 1. split into reusable and to-generate ---------------------------
        to_generate: list[EmbeddingItem] = []
        seen_hashes: dict[str, list[float]] = {}

        for item in items:
            if not item.text.strip():
                outcome.skipped_empty += 1
                continue

            digest = item.hash
            reuse_id = existing.get(digest)
            vector = reusable_vectors.get(reuse_id) if reuse_id else None
            if vector is not None:
                rows.append(
                    self._row(
                        item,
                        vector=_as_list(vector),
                        contract_id=contract_id,
                        project_id=project_id,
                        model=self._provider.model,
                        profile_version=profile_version,
                    )
                )
                outcome.reused += 1
                metrics.embedding_reused_total.labels(level=item.level.value).inc()
                continue
            to_generate.append(item)

        if not to_generate:
            outcome.model = self._provider.model
            return rows

        # ---- 2. de-duplicate within this batch --------------------------------
        # Two chunks with identical text - boilerplate repeated in a schedule - are
        # embedded once and the vector shared.
        unique: list[EmbeddingItem] = []
        for item in to_generate:
            digest = item.hash
            if digest in seen_hashes:
                continue
            seen_hashes[digest] = []
            unique.append(item)

        duplicates_within = len(to_generate) - len(unique)

        # ---- 3. truncate and batch --------------------------------------------
        payloads: list[str] = []
        for item in unique:
            text = item.text
            if len(text) > _MAX_EMBED_CHARS:
                text = text[:_MAX_EMBED_CHARS]
                outcome.truncated += 1
            payloads.append(text)

        batch_size = max(1, settings.batch_size)
        batches = [
            (index, payloads[index : index + batch_size])
            for index in range(0, len(payloads), batch_size)
        ]
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_BATCHES)

        async def embed_batch(start: int, texts: list[str]) -> tuple[int, Any]:
            async with semaphore:
                return start, await self._provider.embed_many(texts)

        results = await asyncio.gather(*(embed_batch(start, texts) for start, texts in batches))

        # ---- 4. reassemble in order ------------------------------------------
        vectors: list[list[float]] = [[] for _ in payloads]
        usage = EmbeddingUsage()
        for start, result in sorted(results, key=lambda entry: entry[0]):
            for offset, vector in enumerate(result.vectors):
                vectors[start + offset] = vector
            usage = usage + result.usage
            outcome.batches += 1
            outcome.latency_ms += result.latency_ms
            outcome.model = result.model

        for item, vector in zip(unique, vectors, strict=True):
            seen_hashes[item.hash] = vector

        # ---- 5. build rows, including the within-batch duplicates --------------
        for item in to_generate:
            vector = seen_hashes.get(item.hash) or []
            if not vector:
                # Defensive: the provider's own validation should have caught this.
                logger.warning(
                    "embedding_vector_missing",
                    level=item.level.value,
                    ref_id=str(item.ref_id),
                )
                continue
            rows.append(
                self._row(
                    item,
                    vector=vector,
                    contract_id=contract_id,
                    project_id=project_id,
                    model=outcome.model or self._provider.model,
                    profile_version=profile_version,
                )
            )
            outcome.generated += 1
            metrics.embedding_generated_total.labels(
                level=item.level.value, provider=self._provider.name
            ).inc()

        outcome.input_tokens = usage.input_tokens
        outcome.cost_usd = usage.cost_usd(outcome.model or self._provider.model)

        if duplicates_within:
            logger.debug(
                "embedding_batch_deduplicated",
                level=outcome.level.value,
                duplicates=duplicates_within,
            )
        return rows

    def _row(
        self,
        item: EmbeddingItem,
        *,
        vector: list[float],
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        model: str,
        profile_version: str | None,
    ) -> dict[str, Any]:
        """One ``embeddings`` row.

        ``source_text`` is stored so a vector can be explained - "why did this match?"
        is answerable without re-composing the input - and so a re-embed does not have
        to reconstruct it.
        """
        settings = self._settings.embedding
        return {
            "id": uuid.uuid4(),
            "project_id": project_id,
            "contract_id": contract_id,
            "level": item.level,
            "ref_id": item.ref_id,
            "embedding": vector,
            "source_text": item.text[:_MAX_EMBED_CHARS],
            "content_hash": item.hash,
            "token_count": item.token_estimate or max(1, len(item.text) // 4),
            "provider": self._provider.name,
            "model": model,
            "dim": len(vector),
            "embedding_version": settings.version,
            "strategy_version": EMBEDDING_STRATEGY_VERSION,
            "profile_version": profile_version,
            "source_artifact_version": None,
            "filter_metadata": item.filter_metadata,
        }


# =============================================================================
# Helpers
# =============================================================================
#: Attributes worth putting into a clause vector. Chosen because they are what
#: distinguishes two clauses of the same type; a longer list would dilute the text.
_SALIENT_KEYS: tuple[str, ...] = (
    "cap_basis",
    "cap_multiple",
    "cap_amount",
    "has_carve_outs",
    "carve_outs",
    "posture",
    "is_mutual",
    "is_capped",
    "auto_renews",
    "renewal_notice_days",
    "payment_days",
    "notice_days",
    "cure_period_days",
    "governing_law",
    "duration_months",
    "is_exclusive",
    "work_product_owner_side",
    "terminating_side",
)


def _salient_attributes(attributes: dict[str, Any]) -> str:
    """Render the distinguishing attributes as a short phrase."""
    parts: list[str] = []
    for key in _SALIENT_KEYS:
        value = attributes.get(key)
        if value is None or value == [] or value == "":
            continue
        if isinstance(value, list):
            rendered = ", ".join(str(entry).replace("_", " ") for entry in value)
        elif isinstance(value, bool):
            rendered = "yes" if value else "no"
        else:
            rendered = str(value).replace("_", " ")
        parts.append(f"{key.replace('_', ' ')}: {rendered}")
    return "; ".join(parts)


def _as_list(vector: Any) -> list[float]:
    """Coerce a stored vector to a plain float list.

    pgvector may hand back a numpy array; the insert path and the JSON artifact both
    want a list, and a numpy array would serialise unpredictably.
    """
    if isinstance(vector, list):
        return [float(value) for value in vector]
    return [float(value) for value in list(vector)]


__all__ = [
    "EmbeddingEngine",
    "EmbeddingItem",
    "EmbeddingPlan",
    "EmbeddingRun",
    "LevelOutcome",
]

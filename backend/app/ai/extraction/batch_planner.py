"""Group clause categories that read the same evidence into batches.

Pure planning. No LLM, no database, no engine. Given each clause's evidence, it
returns which clauses could share one extraction call - and nothing consumes it
yet, so it can be reasoned about and tested on its own.

**Why grouping is by evidence rather than by topic.** The saving from batching is
the prompt scaffolding sent once instead of N times, and that only pays if the
merged evidence stays small. Two clauses that happen to be about related subjects
but cite different pages produce a union bundle as large as both, so the call gets
bigger while the scaffolding saving stays fixed - and a model asked about two
unrelated clauses over twice the evidence has more room to attach the wrong quote
to the wrong clause. Overlap is the property that predicts both.

**Determinism is a requirement, not a nicety.** The batch plan changes which
prompts are built, so a planner that returned different groupings on identical
input would make extraction irreproducible - and the whole point of the
determinism work preceding this was to remove that. Every ordering here is total:
clauses sort by (priority, key), and ties are impossible because keys are unique.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Clauses per batch. Five is the brief's ceiling, and it is also about where a
#: union schema stops being a meaningful constraint - the more clauses one
#: response object covers, the weaker "this field belongs to that clause" gets.
DEFAULT_MAX_BATCH = 5

#: Jaccard similarity at or above which two clauses are considered to share
#: evidence. At 0.5 the union is at most twice the smaller set, which bounds how
#: much a merge can inflate the prompt.
DEFAULT_MIN_OVERLAP = 0.5


class ClauseEvidence(Protocol):
    """What the planner needs. Deliberately narrower than `ClauseDefinition`.

    A protocol rather than the concrete class so the planner can be tested with
    plain fixtures and never has to import the engine - which is what keeps it
    independent of the thing it will eventually feed.
    """

    key: str
    priority: int
    chunk_ids: frozenset[str]
    evidence_tokens: int


@dataclass(slots=True, frozen=True)
class PlannedClause:
    """One clause, reduced to what planning needs."""

    key: str
    priority: int
    chunk_ids: frozenset[str]
    evidence_tokens: int


@dataclass(slots=True)
class Batch:
    """Clauses that will share one extraction call."""

    clauses: list[PlannedClause] = field(default_factory=list)

    @property
    def keys(self) -> list[str]:
        return [clause.key for clause in self.clauses]

    @property
    def chunk_ids(self) -> frozenset[str]:
        """Union - what the merged prompt would actually carry."""
        union: frozenset[str] = frozenset()
        for clause in self.clauses:
            union |= clause.chunk_ids
        return union

    @property
    def evidence_tokens(self) -> int:
        """Tokens the *union* costs, not the sum.

        Chunks shared between two clauses are sent once, which is the saving
        being planned for. Summing would over-count exactly the overlap that
        justified the merge, and would refuse batches that comfortably fit.

        Estimated by apportioning each clause's token count across its chunks -
        the planner sees chunk ids and totals, not per-chunk text.
        """
        per_chunk: dict[str, float] = {}
        for clause in self.clauses:
            if not clause.chunk_ids:
                continue
            share = clause.evidence_tokens / len(clause.chunk_ids)
            for chunk_id in clause.chunk_ids:
                # Max, not sum: the same chunk contributes its text once, and two
                # clauses may estimate it slightly differently.
                per_chunk[chunk_id] = max(per_chunk.get(chunk_id, 0.0), share)
        return round(sum(per_chunk.values()))

    @property
    def is_singleton(self) -> bool:
        return len(self.clauses) == 1


@dataclass(slots=True)
class BatchPlan:
    """The full grouping, plus what it would save."""

    batches: list[Batch] = field(default_factory=list)

    @property
    def clause_count(self) -> int:
        return sum(len(batch.clauses) for batch in self.batches)

    @property
    def call_count(self) -> int:
        return len(self.batches)

    @property
    def size_distribution(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for batch in self.batches:
            size = len(batch.clauses)
            counts[size] = counts.get(size, 0) + 1
        return dict(sorted(counts.items()))

    def calls_saved(self) -> int:
        """Calls the per-clause pipeline would have made, minus these."""
        return self.clause_count - self.call_count

    def as_dict(self) -> dict[str, object]:
        return {
            "clauses": self.clause_count,
            "calls": self.call_count,
            "calls_saved": self.calls_saved(),
            "size_distribution": self.size_distribution,
            "batches": [
                {
                    "keys": batch.keys,
                    "chunks": len(batch.chunk_ids),
                    "evidence_tokens": batch.evidence_tokens,
                }
                for batch in self.batches
            ],
        }


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Overlap of two evidence sets.

    Jaccard rather than containment, deliberately. Containment
    (``|A∩B| / min(|A|,|B|)``) is asymmetric: a clause citing one chunk is 100%
    contained in a clause citing twenty, so it would be merged into a bundle
    twenty times its size - inflating the prompt for no shared evidence. Jaccard
    is symmetric and bounds the union, which is the quantity that actually
    decides whether a merge is worth making.
    """
    if not left and not right:
        # Two clauses with no evidence are not similar; they are both empty. The
        # engine skips a clause with no bundle entirely, so treating them as
        # identical would batch things that will never be sent.
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def plan_batches(
    clauses: Sequence[ClauseEvidence],
    *,
    max_batch: int = DEFAULT_MAX_BATCH,
    min_overlap: float = DEFAULT_MIN_OVERLAP,
    evidence_budget: int,
) -> BatchPlan:
    """Group clauses into batches. Deterministic for a given input.

    **Algorithm: greedy seeded clustering.** Clauses are ordered by (priority,
    key). The first unassigned clause seeds a batch; the remaining clauses are
    scanned in the same order and joined if they overlap the *seed* by at least
    ``min_overlap`` and the union stays inside the budget. Repeat until none are
    left.

    Two simpler-sounding alternatives were rejected:

    * **Agglomerative clustering** (repeatedly merge the closest pair) gives
      tighter groups but is O(n³) without a heap, needs a tie-break rule at every
      merge to stay deterministic, and produces clusters whose membership shifts
      when one clause's evidence changes slightly. Instability is a real cost
      here: the plan decides prompts, so a small evidence change silently
      re-groups half the document.
    * **Graph community detection** over an overlap graph is a better fit for the
      shape of the problem and is not worth it at n=30. Most implementations are
      randomised, and making one deterministic is more work than the grouping is
      worth.

    Greedy seeded is the simplest thing that satisfies every stated constraint,
    and its output changes only where the input changed.

    **Overlap is measured against the seed, not the running union.** Against the
    union, each addition widens the target and lets the batch drift - clause A
    joins B, C joins the A∪B union while sharing nothing with A, and the batch
    ends up containing two clauses with no common evidence at all. Anchoring to
    the seed keeps every member genuinely related to one reference point, which
    is what "never merge unrelated evidence" has to mean.
    """
    if max_batch < 1:
        raise ValueError("max_batch must be at least 1")
    if not 0.0 <= min_overlap <= 1.0:
        raise ValueError("min_overlap must be between 0 and 1")

    ordered = sorted(
        (
            PlannedClause(
                key=str(clause.key),
                priority=int(clause.priority),
                chunk_ids=frozenset(clause.chunk_ids),
                evidence_tokens=int(clause.evidence_tokens),
            )
            for clause in clauses
        ),
        key=lambda item: (item.priority, item.key),
    )

    plan = BatchPlan()
    assigned: set[str] = set()

    for seed in ordered:
        if seed.key in assigned:
            continue

        batch = Batch(clauses=[seed])
        assigned.add(seed.key)

        # A seed that already exceeds the budget still gets its own batch: the
        # per-clause pipeline would have sent it alone anyway, and refusing to
        # plan it would drop a clause from the document.
        for candidate in ordered:
            if len(batch.clauses) >= max_batch:
                break
            if candidate.key in assigned:
                continue
            if jaccard(seed.chunk_ids, candidate.chunk_ids) < min_overlap:
                continue

            trial = Batch(clauses=[*batch.clauses, candidate])
            if trial.evidence_tokens > evidence_budget:
                # Over budget with this one, but a later, smaller clause may
                # still fit - so continue rather than closing the batch.
                continue

            batch = trial
            assigned.add(candidate.key)

        plan.batches.append(batch)

    logger.info(
        "extraction_batch_plan",
        clauses=plan.clause_count,
        calls=plan.call_count,
        saved=plan.calls_saved(),
        distribution=plan.size_distribution,
    )
    return plan


__all__ = [
    "DEFAULT_MAX_BATCH",
    "DEFAULT_MIN_OVERLAP",
    "Batch",
    "BatchPlan",
    "ClauseEvidence",
    "PlannedClause",
    "jaccard",
    "plan_batches",
]

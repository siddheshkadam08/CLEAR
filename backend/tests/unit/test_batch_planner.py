"""The batch planner's guarantees.

Two of these matter more than the rest and are worth stating plainly.

**Every clause appears exactly once.** A planner that drops a clause silently
removes a term from the contract record, and nothing downstream would notice -
the extraction simply would not contain it. Several tests assert conservation
directly rather than inferring it from batch counts.

**Output is deterministic.** The plan decides which prompts get built, so a
planner that grouped differently on identical input would reintroduce exactly the
irreproducibility the determinism work removed.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.ai.extraction.batch_planner import (
    DEFAULT_MAX_BATCH,
    Batch,
    BatchPlan,
    PlannedClause,
    jaccard,
    plan_batches,
)

BUDGET = 9_000


@dataclass(slots=True)
class Fixture:
    """A clause reduced to what planning needs - the protocol, not the engine."""

    key: str
    priority: int
    chunk_ids: frozenset[str]
    evidence_tokens: int


def clause(key: str, chunks: str, *, priority: int = 1, tokens: int = 600) -> Fixture:
    """`chunks` is a compact spelling: "abc" means chunks a, b and c."""
    return Fixture(key, priority, frozenset(chunks), tokens)


def keys_of(plan: BatchPlan) -> list[list[str]]:
    return [batch.keys for batch in plan.batches]


class TestOverlapMeasure:
    def test_identical_sets_are_one(self) -> None:
        assert jaccard(frozenset("abc"), frozenset("abc")) == 1.0

    def test_disjoint_sets_are_zero(self) -> None:
        assert jaccard(frozenset("ab"), frozenset("cd")) == 0.0

    def test_it_is_symmetric(self) -> None:
        """Containment is not, which is why it was rejected: a one-chunk clause
        is 100% contained in a twenty-chunk one and would be merged into a bundle
        twenty times its size."""
        left, right = frozenset("ab"), frozenset("abcd")
        assert jaccard(left, right) == jaccard(right, left)

    def test_two_empty_sets_are_not_similar(self) -> None:
        """They are both empty, not alike. The engine skips a clause with no
        bundle, so batching them would group calls that never get sent."""
        assert jaccard(frozenset(), frozenset()) == 0.0

    def test_exactly_half(self) -> None:
        # |∩| = 2 ("ab"), |∪| = 4 ("abcd")
        assert jaccard(frozenset("abc"), frozenset("abd")) == pytest.approx(0.5)


class TestGrouping:
    def test_identical_evidence_batches_together(self) -> None:
        plan = plan_batches(
            [clause("a", "xyz"), clause("b", "xyz"), clause("c", "xyz")],
            evidence_budget=BUDGET,
        )

        assert keys_of(plan) == [["a", "b", "c"]]
        assert plan.call_count == 1

    def test_no_overlap_stays_separate(self) -> None:
        plan = plan_batches(
            [clause("a", "xy"), clause("b", "pq"), clause("c", "mn")],
            evidence_budget=BUDGET,
        )

        assert keys_of(plan) == [["a"], ["b"], ["c"]]
        assert all(batch.is_singleton for batch in plan.batches)

    def test_partial_overlap_below_the_floor_stays_separate(self) -> None:
        # |∩|=1, |∪|=5 -> 0.2
        plan = plan_batches(
            [clause("a", "abcd"), clause("b", "ae")], evidence_budget=BUDGET
        )

        assert keys_of(plan) == [["a"], ["b"]]

    def test_overlap_of_exactly_fifty_percent_is_included(self) -> None:
        """The boundary is inclusive - `>= min_overlap`, not `>`."""
        plan = plan_batches(
            [clause("a", "abc"), clause("b", "abd")], evidence_budget=BUDGET
        )

        assert keys_of(plan) == [["a", "b"]]

    def test_a_singleton_clause_still_gets_a_batch(self) -> None:
        """It would have been its own call anyway; dropping it would lose the
        clause entirely."""
        plan = plan_batches([clause("lonely", "z")], evidence_budget=BUDGET)

        assert keys_of(plan) == [["lonely"]]

    def test_overlap_is_measured_against_the_seed_not_the_union(self) -> None:
        """Otherwise batches drift: `b` joins `a`, then `c` matches the widened
        a∪b union while sharing nothing with `a`, and the batch ends up holding
        two clauses with no common evidence at all.
        """
        plan = plan_batches(
            [
                clause("a", "abcd", priority=1),
                clause("b", "abce", priority=2),  # 0.6 vs seed - joins
                clause("c", "efgh", priority=3),  # 0.0 vs seed - must not join
            ],
            evidence_budget=BUDGET,
        )

        assert keys_of(plan) == [["a", "b"], ["c"]]


class TestLimits:
    def test_batch_size_is_capped(self) -> None:
        plan = plan_batches(
            [clause(k, "xyz") for k in "abcdefg"], evidence_budget=BUDGET
        )

        assert all(len(batch.clauses) <= DEFAULT_MAX_BATCH for batch in plan.batches)
        assert keys_of(plan) == [["a", "b", "c", "d", "e"], ["f", "g"]]

    def test_the_budget_is_never_exceeded(self) -> None:
        """Distinct chunks, so the union grows with every member."""
        plan = plan_batches(
            [clause(k, f"x{k}", tokens=400) for k in "abcd"],
            min_overlap=0.0,  # force them to be candidates
            evidence_budget=600,
        )

        for batch in plan.batches:
            assert batch.evidence_tokens <= 600

    def test_a_clause_over_budget_alone_is_still_planned(self) -> None:
        """The per-clause pipeline would have sent it; refusing to plan it would
        drop a clause from the document."""
        plan = plan_batches([clause("huge", "xyz", tokens=50_000)], evidence_budget=1_000)

        assert keys_of(plan) == [["huge"]]

    def test_a_later_smaller_clause_can_still_join(self) -> None:
        """A candidate that does not fit must not close the batch - the scan
        continues, because a smaller one further down may."""
        # Priorities pin which clause seeds: ordering is (priority, key), so
        # without them "big" would sort first alphabetically and seed the batch.
        plan = plan_batches(
            [
                clause("seed", "xy", priority=1, tokens=100),
                clause("big", "xy", priority=2, tokens=10_000),
                clause("small", "xy", priority=3, tokens=100),
            ],
            evidence_budget=1_000,
        )

        assert plan.batches[0].keys == ["seed", "small"]
        assert plan.batches[1].keys == ["big"], "the oversized clause is planned alone"

    def test_shared_chunks_are_counted_once(self) -> None:
        """Summing per-clause totals would over-count exactly the overlap that
        justified the merge, and refuse batches that comfortably fit."""
        batch = Batch(
            clauses=[
                PlannedClause("a", 1, frozenset("xy"), 600),
                PlannedClause("b", 2, frozenset("xy"), 600),
            ]
        )

        assert batch.evidence_tokens == 600, "not 1200"

    @pytest.mark.parametrize("bad", [0, -1])
    def test_an_impossible_batch_size_is_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match="max_batch"):
            plan_batches([clause("a", "x")], max_batch=bad, evidence_budget=BUDGET)

    @pytest.mark.parametrize("bad", [-0.1, 1.1])
    def test_an_impossible_overlap_is_rejected(self, bad: float) -> None:
        with pytest.raises(ValueError, match="min_overlap"):
            plan_batches([clause("a", "x")], min_overlap=bad, evidence_budget=BUDGET)


class TestConservation:
    """A dropped clause is a missing contract term that nothing downstream
    would notice."""

    def test_every_clause_appears_exactly_once(self) -> None:
        clauses = [
            clause("a", "xy"), clause("b", "xy"), clause("c", "pq"),
            clause("d", "pqr"), clause("e", "zz"), clause("f", "xy"),
        ]

        plan = plan_batches(clauses, evidence_budget=BUDGET)
        planned = [key for batch in plan.batches for key in batch.keys]

        assert sorted(planned) == sorted(c.key for c in clauses)
        assert len(planned) == len(set(planned)), "no clause may be duplicated"

    def test_conservation_holds_under_a_tight_budget(self) -> None:
        clauses = [clause(k, f"x{k}", tokens=5_000) for k in "abcdef"]

        plan = plan_batches(clauses, evidence_budget=10, min_overlap=0.0)

        assert plan.clause_count == len(clauses)

    def test_an_empty_input_plans_nothing(self) -> None:
        plan = plan_batches([], evidence_budget=BUDGET)

        assert plan.batches == []
        assert plan.calls_saved() == 0

    def test_clauses_with_no_evidence_are_not_merged(self) -> None:
        """They score 0.0 against each other, so each is planned alone - which
        matches the engine, where an empty bundle short-circuits without a call.
        """
        plan = plan_batches(
            [clause("a", ""), clause("b", ""), clause("c", "")], evidence_budget=BUDGET
        )

        assert plan.call_count == 3


class TestDeterminism:
    def test_the_same_input_gives_the_same_plan(self) -> None:
        clauses = [clause(k, "xy" if k in "abc" else "pq") for k in "abcdef"]

        first = keys_of(plan_batches(clauses, evidence_budget=BUDGET))
        second = keys_of(plan_batches(clauses, evidence_budget=BUDGET))

        assert first == second

    def test_input_order_does_not_change_the_plan(self) -> None:
        """Clauses arrive from a database query; a plan that depended on row
        order would change when an index did."""
        clauses = [clause(k, "xy") for k in "abcde"]

        forward = keys_of(plan_batches(clauses, evidence_budget=BUDGET))
        backward = keys_of(plan_batches(list(reversed(clauses)), evidence_budget=BUDGET))

        assert forward == backward

    def test_priority_order_is_preserved(self) -> None:
        """Extraction runs in Clause Master priority order so a run cut short has
        still produced the terms that matter most. Batching must not reorder it."""
        clauses = [
            clause("low", "xy", priority=9),
            clause("high", "pq", priority=1),
            clause("mid", "mn", priority=5),
        ]

        assert keys_of(plan_batches(clauses, evidence_budget=BUDGET)) == [
            ["high"],
            ["mid"],
            ["low"],
        ]

    def test_equal_priorities_break_ties_on_key(self) -> None:
        clauses = [clause(k, f"{k}{k}", priority=1) for k in "cab"]

        assert keys_of(plan_batches(clauses, evidence_budget=BUDGET)) == [
            ["a"],
            ["b"],
            ["c"],
        ]


class TestReporting:
    def test_the_plan_reports_what_it_would_save(self) -> None:
        plan = plan_batches([clause(k, "xyz") for k in "abcde"], evidence_budget=BUDGET)

        assert plan.clause_count == 5
        assert plan.call_count == 1
        assert plan.calls_saved() == 4

    def test_the_size_distribution_is_reported(self) -> None:
        clauses = [clause(k, "xy") for k in "abc"] + [clause("z", "pq")]

        assert plan_batches(clauses, evidence_budget=BUDGET).size_distribution == {1: 1, 3: 1}

    def test_the_plan_serialises(self) -> None:
        payload = plan_batches([clause("a", "xy")], evidence_budget=BUDGET).as_dict()

        assert payload["clauses"] == 1
        assert payload["batches"][0]["keys"] == ["a"]


class TestIndependence:
    def test_the_planner_does_not_import_the_engine_or_a_provider(self) -> None:
        """It must be reasonable about, and testable, without either. An engine
        import would also make this module unusable from a test that has no
        database."""
        import inspect

        from app.ai.extraction import batch_planner

        source = inspect.getsource(batch_planner)
        offenders = [
            line.strip()
            for line in source.splitlines()
            if line.startswith(("import ", "from "))
            and any(word in line for word in ("engine", "provider", "openai", "session"))
        ]

        assert offenders == [], f"planner reaches into production machinery: {offenders}"

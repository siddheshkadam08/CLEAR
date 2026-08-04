"""``RETRIEVAL_MIN_SIMILARITY`` has to actually reach retrieval.

It did not. The per-level fields were typed ``float | None`` - the ``None`` being
the "inherit from the base" sentinel - but declared with eager defaults of
0.35/0.45/0.40, so the sentinel never occurred and the base was unreachable. The
setting looked live from every angle: documented in ``.env.example``, parsed,
range-validated, reported correctly by ``settings.retrieval.min_similarity``, and
written into ``profile.thresholds`` by the seed. Only the code that filters on it
ignored it, so lowering it to admit a 0.42-scoring chunk changed nothing and the
Copilot kept answering "no relevant content found".

These tests pin the resolution order rather than the numbers, so retuning a
default is a one-line change here and a silent regression is not possible.

The planner class at the bottom is the part that matters: a floor that resolves
correctly in ``config`` but is not the one retrieval filters on would reproduce
the original bug exactly.
"""

from __future__ import annotations

import pytest

from app.ai.retrieval.planner import RetrievalPlanner, RetrievalStrategy
from app.core.config import RetrievalSettings
from app.core.enums import EmbeddingLevel, SearchScope

#: The tuned defaults. Unequal on purpose - the floor has to suit the unit of
#: text, and a short formulaic clause needs a stricter one than a long summary.
DEFAULT_DOCUMENT = 0.35
DEFAULT_CLAUSE = 0.45
DEFAULT_CHUNK = 0.40

ALL_KEYS = (
    "RETRIEVAL_MIN_SIMILARITY",
    "RETRIEVAL_MIN_SIMILARITY_DOCUMENT",
    "RETRIEVAL_MIN_SIMILARITY_CLAUSE",
    "RETRIEVAL_MIN_SIMILARITY_CHUNK",
    "RETRIEVAL_DOCUMENT_MIN_SIMILARITY",
    "RETRIEVAL_CLAUSE_MIN_SIMILARITY",
    "RETRIEVAL_CHUNK_MIN_SIMILARITY",
)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Build settings from a known-empty environment.

    The developer running these may well have `RETRIEVAL_MIN_SIMILARITY` set in
    their shell - `.env` is loaded into the process environment by
    `scripts/dev-local.ps1` - and inheriting it would make "no env vars" untrue
    and the first test vacuous.
    """

    def build(**values: str) -> RetrievalSettings:
        for key in ALL_KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        return RetrievalSettings()

    return build


def floors(settings: RetrievalSettings) -> tuple[float, float, float]:
    return (
        settings.similarity_floor(EmbeddingLevel.DOCUMENT_SUMMARY.value),
        settings.similarity_floor(EmbeddingLevel.CLAUSE.value),
        settings.similarity_floor(EmbeddingLevel.CHUNK.value),
    )


class TestNothingConfigured:
    def test_the_tuned_per_level_defaults_apply(self, env) -> None:
        """The regression guard for every existing deployment.

        Nobody sets these variables today, so this triple is current production
        behaviour and the fix is only safe if it survives untouched.
        """
        assert floors(env()) == (DEFAULT_DOCUMENT, DEFAULT_CLAUSE, DEFAULT_CHUNK)

    def test_the_defaults_are_not_all_equal(self, env) -> None:
        """Guards the reason tier 3 exists at all.

        If someone "simplifies" the fallback to the base value, the three levels
        collapse onto one number and this fails rather than quietly retuning
        retrieval.
        """
        document, clause, chunk = floors(env())
        assert len({document, clause, chunk}) == 3


class TestBaseIsHonoured:
    def test_the_base_moves_every_level(self, env) -> None:
        """The reported bug, in one line."""
        assert floors(env(RETRIEVAL_MIN_SIMILARITY="0.25")) == (0.25, 0.25, 0.25)

    def test_a_base_equal_to_its_own_default_still_counts_as_set(self, env) -> None:
        """Explicitness is the test, not inequality.

        Resolution asks whether the operator supplied the variable, not whether
        the value differs from the default. Comparing values instead would make
        `RETRIEVAL_MIN_SIMILARITY=0.40` - a perfectly reasonable way to flatten
        all three levels onto one number - silently do nothing.
        """
        assert floors(env(RETRIEVAL_MIN_SIMILARITY="0.40")) == (0.40, 0.40, 0.40)

    @pytest.mark.parametrize("score", [0.4239])
    def test_a_real_hit_survives_the_lowered_floor(self, env, score: float) -> None:
        """The reported symptom: "What are the payment terms?" scored 0.4239.

        Above the base of 0.25 the operator set, below the 0.45 clause floor that
        overrode it, so the chunk was dropped and the answer came back empty.
        """
        settings = env(RETRIEVAL_MIN_SIMILARITY="0.25")

        assert all(score >= floor for floor in floors(settings))
        # ... and it really was excluded before, which is why the report exists.
        assert score < DEFAULT_CLAUSE


class TestPerLevelOverrides:
    def test_a_level_override_beats_the_base(self, env) -> None:
        settings = env(
            RETRIEVAL_MIN_SIMILARITY="0.25", RETRIEVAL_CLAUSE_MIN_SIMILARITY="0.50"
        )

        assert floors(settings) == (0.25, 0.50, 0.25)

    def test_both_spellings_work(self, env) -> None:
        """`RETRIEVAL_MIN_SIMILARITY_CLAUSE` is what the deployed config uses;
        `RETRIEVAL_CLAUSE_MIN_SIMILARITY` is what people write from memory.
        Accepting only one recreates this whole class of bug."""
        legacy = env(RETRIEVAL_MIN_SIMILARITY="0.25", RETRIEVAL_MIN_SIMILARITY_CLAUSE="0.50")
        natural = env(RETRIEVAL_MIN_SIMILARITY="0.25", RETRIEVAL_CLAUSE_MIN_SIMILARITY="0.50")

        assert floors(legacy) == floors(natural) == (0.25, 0.50, 0.25)

    def test_a_level_override_alone_leaves_the_others_at_their_defaults(self, env) -> None:
        """No base set, so the other two levels must not move."""
        settings = env(RETRIEVAL_CHUNK_MIN_SIMILARITY="0.10")

        assert floors(settings) == (DEFAULT_DOCUMENT, DEFAULT_CLAUSE, 0.10)

    def test_every_level_can_be_set_independently(self, env) -> None:
        settings = env(
            RETRIEVAL_DOCUMENT_MIN_SIMILARITY="0.11",
            RETRIEVAL_CLAUSE_MIN_SIMILARITY="0.22",
            RETRIEVAL_CHUNK_MIN_SIMILARITY="0.33",
        )

        assert floors(settings) == (0.11, 0.22, 0.33)


class TestBlankIsUnset:
    """Compose forwards an unset variable as the empty string, not as nothing.

    `FOO: ${FOO:-}` yields `FOO=`. Before the fix that aborted startup on a float
    parse of ''; the subtler risk is a blank counting as "explicitly set" and
    flattening the per-level defaults, which is the original bug wearing a hat.
    """

    def test_a_blank_base_does_not_start_a_fight_with_the_defaults(self, env) -> None:
        assert floors(env(RETRIEVAL_MIN_SIMILARITY="")) == (
            DEFAULT_DOCUMENT,
            DEFAULT_CLAUSE,
            DEFAULT_CHUNK,
        )

    def test_whitespace_counts_as_blank(self, env) -> None:
        assert floors(env(RETRIEVAL_MIN_SIMILARITY="  ")) == (
            DEFAULT_DOCUMENT,
            DEFAULT_CLAUSE,
            DEFAULT_CHUNK,
        )

    def test_a_blank_level_falls_through_to_the_base(self, env) -> None:
        settings = env(RETRIEVAL_MIN_SIMILARITY="0.30", RETRIEVAL_MIN_SIMILARITY_CLAUSE="")

        assert floors(settings) == (0.30, 0.30, 0.30)

    def test_blanks_do_not_stop_a_real_value_beside_them(self, env) -> None:
        settings = env(RETRIEVAL_MIN_SIMILARITY="", RETRIEVAL_CLAUSE_MIN_SIMILARITY="0.5")

        assert floors(settings) == (DEFAULT_DOCUMENT, 0.5, DEFAULT_CHUNK)


class TestValidationStillApplies:
    """Honouring the value must not mean accepting any value."""

    @pytest.mark.parametrize("bad", ["1.5", "-0.1"])
    def test_a_floor_outside_zero_to_one_is_rejected(self, env, bad: str) -> None:
        with pytest.raises(ValueError):
            env(RETRIEVAL_MIN_SIMILARITY=bad)

    def test_a_non_numeric_floor_is_rejected(self, env) -> None:
        with pytest.raises(ValueError):
            env(RETRIEVAL_MIN_SIMILARITY="tight")


class TestThePlannerUsesTheResolvedFloor:
    """Resolution in `config` is worth nothing if the planner reads elsewhere.

    `LevelBudget.min_similarity` becomes `max_distance = 1.0 - min_similarity` in
    the engine, so this is the last point at which the setting can be lost.
    """

    def _budgets(self, settings: RetrievalSettings):
        """Drive the real planner, with only its settings swapped.

        `_settings` is assigned from `get_settings()` in `__init__`; replacing it
        afterwards exercises the genuine budget code rather than a copy of it.
        """
        planner = RetrievalPlanner()
        planner._settings = settings  # type: ignore[assignment]
        return planner._level_budgets(  # type: ignore[attr-defined]
            RetrievalStrategy.HYBRID, SearchScope.PROJECT, []
        )

    def test_defaults_reach_the_budgets(self, env) -> None:
        by_level = {b.level: b.min_similarity for b in self._budgets(env())}

        assert by_level[EmbeddingLevel.DOCUMENT_SUMMARY] == DEFAULT_DOCUMENT
        assert by_level[EmbeddingLevel.CLAUSE] == DEFAULT_CLAUSE
        assert by_level[EmbeddingLevel.CHUNK] == DEFAULT_CHUNK

    def test_the_base_reaches_the_budgets(self, env) -> None:
        budgets = self._budgets(env(RETRIEVAL_MIN_SIMILARITY="0.25"))

        assert [b.min_similarity for b in budgets] == [0.25] * len(budgets)

    def test_an_override_reaches_the_budgets(self, env) -> None:
        settings = env(
            RETRIEVAL_MIN_SIMILARITY="0.25", RETRIEVAL_CLAUSE_MIN_SIMILARITY="0.50"
        )

        by_level = {b.level: b.min_similarity for b in self._budgets(settings)}

        assert by_level[EmbeddingLevel.CLAUSE] == 0.50
        assert by_level[EmbeddingLevel.CHUNK] == 0.25


class TestEffectiveFloorsAreReportable:
    def test_it_reports_what_retrieval_will_use(self, env) -> None:
        """The benchmark snapshot records the fields, which are now mostly
        `None` - without this it would stop saying what a run actually ran."""
        settings = env(RETRIEVAL_MIN_SIMILARITY="0.25", RETRIEVAL_CLAUSE_MIN_SIMILARITY="0.50")

        assert settings.effective_similarity_floors() == {
            "document_summary": 0.25,
            "clause": 0.50,
            "chunk": 0.25,
        }

    def test_every_level_the_planner_asks_for_is_covered(self, env) -> None:
        reported = env().effective_similarity_floors()

        for level in (
            EmbeddingLevel.DOCUMENT_SUMMARY,
            EmbeddingLevel.CLAUSE,
            EmbeddingLevel.CHUNK,
        ):
            assert level.value in reported

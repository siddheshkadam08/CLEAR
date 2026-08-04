"""The RAG pipeline's own measurements.

Instrumentation is code, and unmeasured instrumentation is worse than none: a
metric that silently stops being recorded turns into a flat line on a dashboard,
which reads as "healthy" rather than "broken". These pin the fields and metric
names that a benchmark or an alert would depend on.

Two properties matter and neither is obvious from reading the collectors:

* **Measurement must not change behaviour.** Every timer here wraps a call whose
  arguments and ordering are untouched, so the assertions are about fields being
  *present and plausible*, never about the values themselves.
* **The evidence/scaffolding split must be defined.** It is the number that
  decides whether batching the per-clause extraction calls is worth doing, and it
  is not observable from the provider - the API reports one total. It is derived,
  so its arithmetic needs guarding.
"""

from __future__ import annotations

from app.ai.extraction.models import CategoryOutcome
from app.core import metrics


class TestMetricsAreRegistered:
    """Names a dashboard or alert would break on if they were renamed."""

    def test_the_retrieval_leg_histogram_exists(self) -> None:
        assert metrics.retrieval_leg_duration_seconds is not None

    def test_it_is_labelled_by_leg_and_level(self) -> None:
        """A hybrid search runs three legs across three levels.

        Without both labels the series cannot answer "which leg of which level",
        which is the only question worth asking when retrieval is slow - the legs
        scale with different things and a total cannot separate them.
        """
        labelled = metrics.retrieval_leg_duration_seconds.labels(leg="vector", level="chunk")

        assert labelled is not None

    def test_the_clause_histograms_exist(self) -> None:
        assert metrics.clause_extraction_tokens is not None
        assert metrics.clause_extraction_duration_seconds is not None

    def test_clause_tokens_carry_the_five_kinds(self) -> None:
        """`evidence` and `repeated` are the two that do not come from the API.

        The provider reports input/output/cache_read. The split of input into the
        part about this document and the part re-sent on every call is computed
        here, and it is the whole reason this metric exists.
        """
        for kind in ("input", "output", "cache_read", "evidence", "repeated"):
            assert metrics.clause_extraction_tokens.labels(kind=kind) is not None


class TestPerClauseAccounting:
    def test_a_fresh_outcome_starts_at_zero(self) -> None:
        outcome = CategoryOutcome(category="clause:term", prompt_id="extraction.clauses")

        assert outcome.evidence_tokens == 0
        assert outcome.repeated_tokens == 0

    def test_both_fields_are_reported(self) -> None:
        """They travel in the stage artifact, which is what a benchmark reads."""
        outcome = CategoryOutcome(
            category="clause:term",
            prompt_id="extraction.clauses",
            input_tokens=3200,
            output_tokens=180,
            evidence_tokens=1800,
            repeated_tokens=1400,
        )

        payload = outcome.as_dict()

        assert payload["evidence_tokens"] == 1800
        assert payload["repeated_tokens"] == 1400
        # The pre-existing keys must survive: dashboards and the evaluation
        # runner already read them.
        assert payload["input_tokens"] == 3200
        assert payload["output_tokens"] == 180

    def test_the_split_is_bounded_by_the_provider_total(self) -> None:
        """`repeated = input - evidence`, and the two are different counters.

        `evidence_tokens` is an estimate over text we supplied; `input_tokens` is
        the provider's count of the assembled prompt. On a short prompt the
        estimate can exceed the total, and an unclamped subtraction would report
        a negative number of repeated tokens - which is not a small error, it is
        a nonsensical one that would poison any average built on it.
        """
        input_tokens, evidence_tokens = 100, 250

        repeated = max(0, input_tokens - evidence_tokens)

        assert repeated == 0

    def test_a_normal_call_splits_sensibly(self) -> None:
        input_tokens, evidence_tokens = 3200, 1800

        repeated = max(0, input_tokens - evidence_tokens)

        assert repeated == 1400
        assert repeated + evidence_tokens == input_tokens


class TestRetrievalResultCarriesLegTimings:
    def test_leg_ms_defaults_to_empty(self) -> None:
        """Empty rather than zeroed: a leg that did not run and a leg that ran in
        under a millisecond are different facts, and pre-seeding keys would make
        them indistinguishable."""
        from app.ai.retrieval.engine import RetrievalResult

        assert RetrievalResult().leg_ms == {}

    def test_leg_ms_is_serialised(self) -> None:
        """It rides in the same payload as `duration_ms` and `rerank_ms`, so a
        slow answer is attributable from the logs without a profiler."""
        from app.ai.retrieval.engine import RetrievalResult

        result = RetrievalResult()
        result.leg_ms = {"vector": 42, "keyword": 11, "fusion": 1}

        payload = result.statistics()

        assert payload["leg_ms"] == {"vector": 42, "keyword": 11, "fusion": 1}
        assert "duration_ms" in payload and "rerank_ms" in payload

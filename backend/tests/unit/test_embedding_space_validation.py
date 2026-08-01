"""Embedding space compatibility.

The regression: a provider switch left mock vectors and real vectors in one
table. Nothing errored - similarity search simply compared coordinates from two
unrelated spaces and ranked the results by a meaningless number.
"""

from __future__ import annotations

import pytest

from app.ai.embedding.validation import (
    EmbeddingSpace,
    EmbeddingValidator,
    ValidationReport,
)
from app.core.errors import ValidationError


def _space(**over: object) -> EmbeddingSpace:
    base: dict[str, object] = {
        "provider": "openai",
        "model": "nvidia/nemotron-3-embed-1b",
        "dim": 8,
        "embedding_version": "v1",
        "strategy_version": "1.0.0",
    }
    base.update(over)
    return EmbeddingSpace(**base)  # type: ignore[arg-type]


def _row(space: EmbeddingSpace, *, vector_len: int | None = None) -> dict[str, object]:
    length = space.dim if vector_len is None else vector_len
    return {
        "ref_id": "11111111-1111-1111-1111-111111111111",
        "embedding": [0.1] * length,
        **space.as_dict(),
    }


class TestSpaceIdentity:
    def test_same_configuration_is_the_same_space(self) -> None:
        assert _space() == _space()

    def test_a_different_model_is_a_different_space(self) -> None:
        assert _space() != _space(model="other-model")

    def test_a_different_dimension_is_a_different_space(self) -> None:
        assert _space() != _space(dim=1024)

    def test_strategy_version_participates_in_identity(self) -> None:
        """Changing the composed input moves vectors without changing the model."""
        assert _space() != _space(strategy_version="2.0.0")

    def test_label_is_human_readable(self) -> None:
        assert _space().label == "openai:nvidia/nemotron-3-embed-1b@8/v1"


class TestValidateBatch:
    def test_matching_rows_are_accepted(self) -> None:
        expected = _space()
        rows = [_row(expected) for _ in range(3)]
        keep, report = EmbeddingValidator(expected).validate_batch(rows)
        assert len(keep) == 3
        assert report.ok and report.accepted == 3

    def test_foreign_model_is_rejected(self) -> None:
        """The exact production failure: mock vectors beside real ones."""
        expected = _space()
        rows = [_row(expected), _row(_space(provider="mock", model="mock-model"))]
        with pytest.raises(ValidationError) as err:
            EmbeddingValidator(expected).validate_batch(rows, strict=True)
        assert "embedding space" in str(err.value).lower()

    def test_dimension_mismatch_is_rejected(self) -> None:
        expected = _space()
        with pytest.raises(ValidationError):
            EmbeddingValidator(expected).validate_batch([_row(_space(dim=1024))])

    def test_vector_length_is_checked_against_the_payload(self) -> None:
        """`dim` is metadata the caller supplied; the list is what gets indexed."""
        expected = _space()
        bad = _row(expected, vector_len=expected.dim - 1)
        with pytest.raises(ValidationError):
            EmbeddingValidator(expected).validate_batch([bad])

    def test_missing_vector_is_rejected(self) -> None:
        expected = _space()
        row = _row(expected)
        row["embedding"] = None
        with pytest.raises(ValidationError):
            EmbeddingValidator(expected).validate_batch([row])

    def test_non_strict_returns_the_good_rows_and_reports(self) -> None:
        expected = _space()
        rows = [_row(expected), _row(_space(dim=1024))]
        keep, report = EmbeddingValidator(expected).validate_batch(rows, strict=False)
        assert len(keep) == 1
        assert report.rejected == 1
        assert "dimension_mismatch" in report.reasons

    def test_report_records_why_and_samples(self) -> None:
        report = ValidationReport()
        validator = EmbeddingValidator(_space())
        validator.validate_row(_row(_space(model="x")), report)
        assert report.reasons == {"foreign_space": 1}
        assert report.rejected_samples[0]["reason"] == "foreign_space"


class TestActiveSpaceFollowsTheProvider:
    """The provider stamps the row, so the provider defines the expected space."""

    def test_mock_provider_rows_are_accepted(self) -> None:
        """Regression: reading `model` from settings rejected every mock vector.

        The mock provider prefixes `mock-`; comparing against the raw setting made
        validation reject rows that were entirely correct for that provider.
        """
        from app.ai.embedding.providers import get_embedding_provider

        provider = get_embedding_provider()
        active = EmbeddingSpace.active()
        assert active.provider == provider.name
        assert active.model == provider.model

        row = {
            "ref_id": "22222222-2222-2222-2222-222222222222",
            "embedding": [0.0] * active.dim,
            **active.as_dict(),
        }
        keep, report = EmbeddingValidator().validate_batch([row])
        assert len(keep) == 1 and report.ok

    def test_mock_provider_names_one_space_on_both_write_paths(self) -> None:
        """Generated and reused rows must agree, or reuse silently stops matching."""
        import asyncio

        from app.ai.embedding.providers import MockEmbeddingProvider

        provider = MockEmbeddingProvider()
        result = asyncio.run(provider.embed_many(["some clause text"]))
        assert result.model == provider.model
        assert provider.model.startswith("mock-")


class TestEngineRowsSurviveTheGate:
    """The gate is on the insert path, so real engine output has to pass it.

    Without this, a validator that is subtly stricter than the writer would not
    fail a unit test - it would fail every ingest in production.
    """

    @pytest.mark.asyncio
    async def test_generated_rows_validate(self) -> None:
        import uuid as _uuid

        from app.ai.embedding.engine import EmbeddingEngine, EmbeddingItem, EmbeddingPlan
        from app.ai.embedding.providers import MockEmbeddingProvider
        from app.core.enums import EmbeddingLevel

        plan = EmbeddingPlan(
            items=[
                EmbeddingItem(
                    ref_id=_uuid.uuid4(),
                    level=EmbeddingLevel.CHUNK,
                    text=f"clause text number {n}",
                )
                for n in range(3)
            ]
        )
        run = await EmbeddingEngine(provider=MockEmbeddingProvider()).run(
            plan, contract_id=_uuid.uuid4(), project_id=_uuid.uuid4()
        )
        assert run.rows

        keep, report = EmbeddingValidator().validate_batch(run.rows, strict=True)
        assert len(keep) == len(run.rows) and report.ok

    @pytest.mark.asyncio
    async def test_reused_rows_validate_identically(self) -> None:
        """The reuse path stamps provenance separately; it must agree with generate."""
        import uuid as _uuid

        from app.ai.embedding.engine import EmbeddingEngine, EmbeddingItem, EmbeddingPlan
        from app.ai.embedding.providers import MockEmbeddingProvider
        from app.core.enums import EmbeddingLevel

        item = EmbeddingItem(
            ref_id=_uuid.uuid4(), level=EmbeddingLevel.CHUNK, text="a reused clause"
        )
        stored_id = _uuid.uuid4()
        active = EmbeddingSpace.active()

        run = await EmbeddingEngine(provider=MockEmbeddingProvider()).run(
            EmbeddingPlan(items=[item]),
            contract_id=_uuid.uuid4(),
            project_id=_uuid.uuid4(),
            existing={EmbeddingLevel.CHUNK: {item.hash: stored_id}},
            reusable_vectors={stored_id: [0.0] * active.dim},
        )
        assert run.rows and run.reused == 1

        keep, report = EmbeddingValidator().validate_batch(run.rows, strict=True)
        assert len(keep) == 1 and report.ok


class TestStoreAudit:
    def test_single_space_is_consistent(self) -> None:
        expected = _space()
        audit = EmbeddingValidator(expected).audit_rows([{**expected.as_dict(), "count": 120}])
        assert audit.is_consistent
        assert audit.incompatible_rows == 0

    def test_mixed_spaces_are_flagged_with_counts(self) -> None:
        expected = _space()
        audit = EmbeddingValidator(expected).audit_rows(
            [
                {**expected.as_dict(), "count": 100},
                {**_space(provider="mock", model="mock-model").as_dict(), "count": 49},
            ]
        )
        assert not audit.is_consistent
        assert audit.incompatible_rows == 49
        assert len(audit.foreign_spaces) == 1

    def test_audit_serialises_for_diagnostics(self) -> None:
        expected = _space()
        audit = EmbeddingValidator(expected).audit_rows([{**expected.as_dict(), "count": 1}])
        payload = audit.as_dict()
        assert payload["consistent"] is True
        assert payload["active_space"] == expected.label

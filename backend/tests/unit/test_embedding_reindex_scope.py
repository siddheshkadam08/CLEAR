"""What the reindexer considers stale.

Detection used to compare the model name alone. A dimension change or a strategy
bump produced vectors that could not be compared with the existing ones, and the
reindexer reported nothing to do - so the mixed index stayed mixed.
"""

from __future__ import annotations

from app.ai.embedding.reindex import EmbeddingReindexer
from app.ai.embedding.validation import EmbeddingSpace


def _predicate_sql() -> str:
    reindexer = EmbeddingReindexer.__new__(EmbeddingReindexer)  # no DB needed
    return str(reindexer._stale_predicate().compile(compile_kwargs={"literal_binds": True}))


class TestStalePredicate:
    def test_every_field_of_the_space_participates(self) -> None:
        """Anything short of all five leaves incomparable vectors in place."""
        sql = _predicate_sql()
        for column in ("model", "provider", "dim", "embedding_version", "strategy_version"):
            assert column in sql, f"{column} is not part of the staleness check"

    def test_it_matches_the_space_identity_exactly(self) -> None:
        """The predicate and EmbeddingSpace must not drift apart."""
        sql = _predicate_sql()
        for field_name in EmbeddingSpace.__dataclass_fields__:
            assert field_name in sql

    def test_the_target_space_is_the_active_one(self) -> None:
        reindexer = EmbeddingReindexer.__new__(EmbeddingReindexer)
        assert reindexer.target == EmbeddingSpace.active()


class TestCliSurface:
    def test_reindex_accepts_all_and_batch_size(self) -> None:
        import inspect

        from app.cli import reindex_embeddings

        params = inspect.signature(reindex_embeddings).parameters
        assert "all_contracts" in params
        assert "batch_size" in params
        assert params["all_contracts"].default.default is False

    def test_run_threads_include_current_through(self) -> None:
        import inspect

        params = inspect.signature(EmbeddingReindexer.run).parameters
        assert params["include_current"].default is False
        assert params["batch_size"].default > 0

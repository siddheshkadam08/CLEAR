"""Who may skip the document-summary level, and who may not.

L1 ranks *documents*. When the caller has already named the contracts there is
nothing left to rank, so the level costs ~110ms and returns a candidate list the
plan already holds. Skipping it is free for an answering caller: L1 is excluded
from `answerable_similarity` by design, so a document summary can never support
an answer.

It is **not** free for `/search`, whose results are read directly rather than
answered from. There the summary is a legitimate row, and skipping L1 removes
something the reader came to see - measured as hits falling 3 to 2 on the same
query.

Hence `for_answer`, and hence these tests: the saving is taken where it is free
and declined where it is not, and a flipped default would change `/search`
silently.
"""

from __future__ import annotations

import uuid

import pytest

from app.ai.retrieval.planner import RetrievalPlanner
from app.core.enums import EmbeddingLevel, SearchMode, SearchScope

PROJECT = uuid.uuid4()
CONTRACT = uuid.uuid4()
#: Routes to HYBRID, which is the strategy that includes L1.
FINANCIAL_QUERY = "What are the payment terms?"


def levels_for(**kwargs: object) -> list[EmbeddingLevel]:
    plan = RetrievalPlanner().plan(
        FINANCIAL_QUERY,
        project_ids=[PROJECT],
        mode=SearchMode.HYBRID,
        **kwargs,  # type: ignore[arg-type]
    )
    return [budget.level for budget in plan.levels]


class TestTheAnsweringPathSkipsL1:
    def test_named_contracts_drop_the_summary_level(self) -> None:
        levels = levels_for(
            scope=SearchScope.CONTRACT, contract_ids=[CONTRACT], for_answer=True
        )

        assert EmbeddingLevel.DOCUMENT_SUMMARY not in levels

    def test_the_answering_levels_survive(self) -> None:
        """Dropping L1 must not touch the levels an answer is built from."""
        levels = levels_for(
            scope=SearchScope.CONTRACT, contract_ids=[CONTRACT], for_answer=True
        )

        assert EmbeddingLevel.CLAUSE in levels
        assert EmbeddingLevel.CHUNK in levels

    def test_without_named_contracts_l1_still_runs(self) -> None:
        """`for_answer` alone is not permission to skip: with no contract named,
        L1 is doing the job it exists for - deciding which documents to read."""
        levels = levels_for(scope=SearchScope.PROJECT, for_answer=True)

        assert EmbeddingLevel.DOCUMENT_SUMMARY in levels


class TestTheBrowsePathKeepsL1:
    def test_search_keeps_the_summary_even_with_named_contracts(self) -> None:
        """`/search` does not pass `for_answer`. This is the regression guard for
        the row that disappeared when the skip was applied to every caller."""
        levels = levels_for(scope=SearchScope.CONTRACT, contract_ids=[CONTRACT])

        assert EmbeddingLevel.DOCUMENT_SUMMARY in levels

    def test_the_default_is_the_browse_behaviour(self) -> None:
        """Opt-in, not opt-out. A caller that has not thought about it gets the
        behaviour that loses nothing."""
        import inspect

        signature = inspect.signature(RetrievalPlanner.plan)

        assert signature.parameters["for_answer"].default is False

    @pytest.mark.parametrize("scope", [SearchScope.PROJECT, SearchScope.APPLICATION])
    def test_wider_scopes_are_untouched(self, scope: SearchScope) -> None:
        assert EmbeddingLevel.DOCUMENT_SUMMARY in levels_for(scope=scope)


class TestOnlyTheCopilotOptsIn:
    def test_the_copilot_passes_for_answer(self) -> None:
        import inspect

        from app.services import copilot

        source = inspect.getsource(copilot)

        assert "for_answer=True" in source

    def test_search_does_not(self) -> None:
        """Pinned against the source because the difference between the two paths
        is a single keyword, and adding it to `/search` would reintroduce the
        behaviour change this scoping exists to prevent."""
        import inspect

        from app.api.v1 import search

        assert "for_answer=True" not in inspect.getsource(search)

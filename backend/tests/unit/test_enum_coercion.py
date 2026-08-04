"""Request enums arrive as bare strings, and the code that reads `.value` must cope.

Every request schema sets ``use_enum_values=True``. That is a deliberate choice -
it keeps serialised payloads plain - but it means an annotation of
``ResponseFormat`` or ``SearchScope`` on a request model does *not* guarantee an
enum member at runtime. Pydantic stores the value, so an explicitly supplied field
is a ``str``.

Two production paths then called ``.value`` on it and raised
``AttributeError: 'str' object has no attribute 'value'``, returning 500:

* ``PromptOrchestrator.build`` - any answer with a chosen ``response_format``
* ``RetrievalPlanner.plan``   - any search with a named ``scope`` or ``mode``

Both only failed when the caller *supplied* the field. Leaving it unset took a
default that was already an enum member, so the endpoints looked healthy under
any test that did not pass one - which is exactly what every existing test did.
"""

from __future__ import annotations

import uuid

import pytest

from app.ai.rag.orchestrator import PromptOrchestrator
from app.ai.retrieval.context import ContextPackage
from app.ai.retrieval.planner import RetrievalPlanner
from app.core.enums import QueryIntent, ResponseFormat, SearchMode, SearchScope


def package() -> ContextPackage:
    return ContextPackage(query="What is the limitation of liability?", intent=QueryIntent.CLAUSE_LOOKUP)


class TestPromptOrchestratorAcceptsRawStrings:
    def test_a_string_response_format_is_coerced(self) -> None:
        prompt = PromptOrchestrator().build(package(), response_format="executive_summary")

        assert prompt.response_format is ResponseFormat.EXECUTIVE_SUMMARY
        # The attribute the RAG engine and the metrics label both read.
        assert prompt.response_format.value == "executive_summary"

    def test_an_enum_response_format_still_works(self) -> None:
        prompt = PromptOrchestrator().build(package(), response_format=ResponseFormat.RISK_REPORT)

        assert prompt.response_format is ResponseFormat.RISK_REPORT

    def test_the_default_is_an_enum(self) -> None:
        """The path that never broke - asserted so a fix cannot regress it."""
        prompt = PromptOrchestrator().build(package())

        assert isinstance(prompt.response_format, ResponseFormat)

    def test_the_prompt_serialises(self) -> None:
        """`as_audit` reads `.value`; a raw string made it raise rather than return."""
        prompt = PromptOrchestrator().build(package(), response_format="risk_report")

        assert prompt.as_audit()["response_format"] == "risk_report"


class TestRetrievalPlannerAcceptsRawStrings:
    def plan_with(self, **kwargs: object):
        return RetrievalPlanner().plan(
            "What is the limitation of liability?",
            project_ids=[uuid.uuid4()],
            **kwargs,  # type: ignore[arg-type]
        )

    def test_a_string_scope_is_coerced(self) -> None:
        plan = self.plan_with(scope="contract", contract_ids=[uuid.uuid4()])

        assert plan.scope is SearchScope.CONTRACT

    def test_a_string_mode_is_coerced(self) -> None:
        plan = self.plan_with(mode="keyword")

        assert plan.mode is SearchMode.KEYWORD

    def test_the_plan_serialises(self) -> None:
        """`as_dict` reads `.value` off both, which is where the 500 came from."""
        payload = self.plan_with(scope="project", mode="semantic").as_dict()

        assert payload["scope"] == "project"
        assert payload["mode"] == "semantic"

    @pytest.mark.parametrize("bad", ["not_a_scope", "APPLICATION"])
    def test_an_unknown_scope_is_rejected(self, bad: str) -> None:
        """Coercion must not quietly accept nonsense - the values are case
        sensitive, and a silent fallback would hide a real routing mistake."""
        with pytest.raises(ValueError):
            self.plan_with(scope=bad)

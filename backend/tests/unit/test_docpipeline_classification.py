"""Document-type classification: the window, the taxonomy, and the fallback."""

from __future__ import annotations

from typing import Any

import pytest

from app.ai.docpipeline.classification import (
    CLASSIFICATION_PAGE_WINDOW,
    DocumentTypeClassifier,
)
from app.ai.docpipeline.source import PageContent, Paragraph
from app.ai.rag.providers import InferenceResult, StructuredResult, TokenUsage
from app.ai.routing import LLMTask

DOC_TYPES = [
    "Addendum",
    "Contract cum Order Form",
    "License Agreement",
    "MSA",
    "NDA",
    "Others",
]


class StubProvider:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.prompt = ""
        self.system = ""
        self.purpose = ""
        self.schema: dict[str, Any] = {}

    async def generate_structured(
        self, *, system: str, prompt: str, schema: dict, purpose: str = "", **_: Any
    ) -> StructuredResult:
        self.system, self.prompt, self.schema, self.purpose = system, prompt, schema, purpose
        return StructuredResult(
            data=self._data,
            inference=InferenceResult(
                text="", model="stub-model", usage=TokenUsage(), stop_reason="stop"
            ),
        )


def _pages(count: int) -> list[PageContent]:
    return [
        PageContent(
            page_number=number,
            paragraphs=(
                Paragraph(
                    page_number=number,
                    index=1,
                    role=None,
                    content=f"text of page {number}",
                    polygon=(),
                ),
            ),
            source_file=None,  # type: ignore[arg-type]
        )
        for number in range(1, count + 1)
    ]


@pytest.mark.asyncio
async def test_only_the_first_five_pages_reach_the_prompt() -> None:
    provider = StubProvider({"document_type": "MSA", "confidence": 0.9, "reason": "framework"})

    result = await DocumentTypeClassifier(provider).classify(_pages(28), DOC_TYPES)

    assert result.pages_used == CLASSIFICATION_PAGE_WINDOW
    assert "text of page 5" in provider.prompt
    assert "text of page 6" not in provider.prompt


@pytest.mark.asyncio
async def test_a_short_document_uses_every_page_it_has() -> None:
    provider = StubProvider(
        {"document_type": "NDA", "confidence": 0.8, "reason": "confidentiality"}
    )

    result = await DocumentTypeClassifier(provider).classify(_pages(3), DOC_TYPES)

    assert result.pages_used == 3


@pytest.mark.parametrize("label", DOC_TYPES)
@pytest.mark.asyncio
async def test_every_taxonomy_label_round_trips(label: str) -> None:
    """The label is the join key for the clause lookup, so it must survive verbatim."""
    provider = StubProvider({"document_type": label, "confidence": 0.7, "reason": "because"})

    result = await DocumentTypeClassifier(provider).classify(_pages(5), DOC_TYPES)

    assert result.doc_type == label
    assert not result.fell_back


@pytest.mark.asyncio
async def test_off_taxonomy_label_falls_back_to_others() -> None:
    """An invented label would join to zero clauses and read as 'nothing found'."""
    provider = StubProvider(
        {"document_type": "Purchase Order", "confidence": 0.6, "reason": "guessing"}
    )

    result = await DocumentTypeClassifier(provider).classify(_pages(5), DOC_TYPES)

    assert result.doc_type == "Others"
    assert result.fell_back


@pytest.mark.asyncio
async def test_case_differences_are_tolerated() -> None:
    provider = StubProvider({"document_type": "msa", "confidence": 0.9, "reason": "ok"})

    result = await DocumentTypeClassifier(provider).classify(_pages(5), DOC_TYPES)

    assert result.doc_type == "MSA"
    assert not result.fell_back


@pytest.mark.asyncio
async def test_confidence_is_clamped_and_survives_rubbish() -> None:
    provider = StubProvider({"document_type": "MSA", "confidence": "not a number", "reason": ""})

    result = await DocumentTypeClassifier(provider).classify(_pages(5), DOC_TYPES)

    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_the_call_is_routed_by_task_not_by_model_name() -> None:
    """Routing by task is what pins this to the cheap tier; a model name here
    would also break the repo-wide test that forbids them outside config."""
    provider = StubProvider({"document_type": "MSA", "confidence": 0.9, "reason": "ok"})

    await DocumentTypeClassifier(provider).classify(_pages(5), DOC_TYPES)

    assert provider.purpose == LLMTask.DOCUMENT_CLASSIFICATION.value


@pytest.mark.asyncio
async def test_schema_enum_is_exactly_the_supplied_taxonomy() -> None:
    provider = StubProvider({"document_type": "MSA", "confidence": 0.9, "reason": "ok"})

    await DocumentTypeClassifier(provider).classify(_pages(5), DOC_TYPES)

    assert provider.schema["properties"]["document_type"]["enum"] == DOC_TYPES


@pytest.mark.asyncio
async def test_no_pages_is_an_error_not_a_guess() -> None:
    provider = StubProvider({"document_type": "MSA", "confidence": 0.9, "reason": "ok"})

    with pytest.raises(ValueError, match="No pages"):
        await DocumentTypeClassifier(provider).classify([], DOC_TYPES)

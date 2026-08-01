"""Paragraphs grouped into the sections the evidence selector can actually use.

Every one of these pins a property that `EvidenceSelector` depends on. The first
is not a style preference: the stage originally emitted one chunk per paragraph
and left `token_count` at its default of zero, and since all thirty seeded
Clause Master rules carry ``min_tokens: 15``, every chunk was discarded before it
was scored. The stage reported success and extracted nothing.
"""

from __future__ import annotations

from app.ai.docpipeline.source import Paragraph
from app.ai.extraction.evidence import EvidenceSelector
from app.core.enums import ChunkType
from app.orchestrator.stages.extraction import _RULE_MIN_TOKENS, _section_chunks
from app.schemas.common import BoundingBox

BODY = (
    "Neither party shall be liable for any indirect, incidental or consequential "
    "damages arising out of or relating to this Agreement, and the aggregate "
    "liability of either party shall not exceed the fees paid in the twelve "
    "months preceding the claim."
)


def para(page: int, index: int, role: str | None, content: str) -> Paragraph:
    return Paragraph(
        page_number=page,
        index=index,
        role=role,
        content=content,
        polygon=(1.0, 1.0, 2.0, 1.0, 2.0, 2.0, 1.0, 2.0),
    )


def test_a_section_clears_the_min_tokens_floor_every_rule_applies() -> None:
    """The regression the whole module exists for."""
    chunks = _section_chunks(
        [
            para(3, 1, "sectionHeading", "9.3 Limitation of Liability"),
            para(3, 2, None, BODY),
        ]
    )

    assert len(chunks) == 1
    assert chunks[0].token_count >= _RULE_MIN_TOKENS
    assert chunks[0].token_count > 0  # the actual bug: it defaulted to 0


def test_the_heading_survives_because_matching_is_built_on_it() -> None:
    """`heading_patterns` and the title-keyword bonus both read `chunk.heading`."""
    chunk = _section_chunks(
        [
            para(3, 1, "sectionHeading", "9.3 Limitation of Liability"),
            para(3, 2, None, BODY),
        ]
    )[0]

    assert chunk.section_title == "9.3 Limitation of Liability"
    assert chunk.clause_number == "9.3"
    assert "limitation of liability" in chunk.heading.lower()
    assert chunk.chunk_type is ChunkType.SECTION


def test_the_selector_finds_a_section_it_would_have_dropped_as_paragraphs() -> None:
    """End to end against the real selector, with a real rule shape."""
    chunks = _section_chunks(
        [
            para(3, 1, "sectionHeading", "9.3 Limitation of Liability"),
            para(3, 2, None, BODY),
        ]
    )
    bundle = EvidenceSelector(chunks).select(
        category="limitation_of_liability",
        rule={
            "keywords": ["aggregate liability", "shall not exceed", "consequential damages"],
            "heading_patterns": ["limitation of liability"],
            "min_tokens": _RULE_MIN_TOKENS,
        },
    )

    assert bundle.scored, "the selector found no evidence - the zero-clause bug is back"
    assert bundle.excluded == 0


def test_geometry_and_pages_span_the_whole_section() -> None:
    """Evidence has to stay pinned to a place in the document."""
    chunk = _section_chunks(
        [
            para(3, 1, "sectionHeading", "9.3 Limitation of Liability"),
            para(3, 2, None, BODY),
            para(4, 1, None, "This Section 9.3 survives termination."),
        ]
    )[0]

    assert (chunk.page_start, chunk.page_end) == (3, 4)
    # One merged rectangle per page, not one per paragraph: the viewer draws
    # every box it is handed, and three paragraphs on page 3 would otherwise
    # render as three separate strips.
    assert [b["page_number"] for b in chunk.bounding_boxes] == [3, 4]


def test_geometry_is_the_shape_the_api_validates_against() -> None:
    """The regression that made GET /knowledge a 500 for every contract.

    The stage wrote the document pipeline's `{page, polygon}` inches into a
    column the API parses as `BoundingBox`, and `_boxes()` raised on all 52
    clauses at once - so Contract Detail lost every tab, not just a highlight.
    """
    chunk = _section_chunks(
        [
            para(3, 1, "sectionHeading", "9.3 Limitation of Liability"),
            para(3, 2, None, BODY),
        ]
    )[0]

    assert chunk.bounding_boxes, "a paragraph with a polygon must yield a box"
    for raw in chunk.bounding_boxes:
        box = BoundingBox(**raw)  # verbatim what app/api/v1/knowledge.py does
        # Page size must travel with the box: PdfViewer scales by x / page_width
        # and silently draws nothing when it is absent.
        assert box.page_width and box.page_height
        assert box.width > 0 and box.height > 0


def test_a_paragraph_without_geometry_is_kept_as_text() -> None:
    """Real text worth extracting, simply not pointable-at."""
    chunks = _section_chunks(
        [
            Paragraph(page_number=1, index=1, role="sectionHeading",
                      content="1. Definitions", polygon=()),
            Paragraph(page_number=1, index=2, role=None, content=BODY, polygon=()),
        ]
    )

    assert len(chunks) == 1
    assert chunks[0].bounding_boxes == []
    assert BODY[:40] in chunks[0].text


def test_furniture_is_dropped_rather_than_matched() -> None:
    """A running head matches keywords as readily as clause text does."""
    chunks = _section_chunks(
        [
            para(3, 0, "pageHeader", "Limitation of Liability - Confidential"),
            para(3, 1, "sectionHeading", "9.3 Limitation of Liability"),
            para(3, 2, None, BODY),
            para(3, 9, "pageNumber", "3"),
        ]
    )

    assert len(chunks) == 1
    assert "Confidential" not in chunks[0].text


def test_text_before_the_first_heading_is_kept() -> None:
    """Parties and dates live in the recitals, which no heading introduces."""
    chunks = _section_chunks(
        [
            para(1, 1, None, "This Agreement is made between Acme Inc. and Beta LLC."),
            para(1, 2, "sectionHeading", "1. Definitions"),
            para(1, 3, None, BODY),
        ]
    )

    assert len(chunks) == 2
    assert chunks[0].section_title is None
    assert "Acme Inc." in chunks[0].text
    assert chunks[0].chunk_type is ChunkType.PARAGRAPH


def test_an_oversized_section_splits_and_each_part_keeps_the_heading() -> None:
    """A split half is still matchable by heading, and still reads as its section."""
    chunks = _section_chunks(
        [para(2, 1, "sectionHeading", "12. Services")]
        + [para(2, i, None, BODY) for i in range(2, 40)]
    )

    assert len(chunks) > 1
    assert all(c.section_title == "12. Services" for c in chunks)
    assert all(c.token_count >= _RULE_MIN_TOKENS for c in chunks)
    # Reading order is contiguous, so ties break in document order.
    assert [c.reading_order for c in chunks] == list(range(1, len(chunks) + 1))


def test_split_parts_get_distinct_ids_or_the_evidence_is_dropped() -> None:
    """Every part shares the heading paragraph, so every part shared its ref.

    `EvidenceBundle.chunks` dedupes on `chunk_id`, so all but one part of a long
    section vanished from every bundle it was selected into - and long sections
    are where liability caps and indemnities live.
    """
    chunks = _section_chunks(
        [para(2, 1, "sectionHeading", "12. Services")]
        + [para(2, i, None, BODY) for i in range(2, 40)]
    )
    ids = [c.chunk_id for c in chunks]

    assert len(ids) > 1
    assert len(set(ids)) == len(ids), f"duplicate chunk ids: {ids}"


def test_an_unsplit_section_keeps_its_bare_ref() -> None:
    """The id goes into the prompt and the model must echo it back verbatim."""
    chunk = _section_chunks(
        [
            para(3, 1, "sectionHeading", "9.3 Limitation of Liability"),
            para(3, 2, None, BODY),
        ]
    )[0]

    assert chunk.chunk_id == "3.1"

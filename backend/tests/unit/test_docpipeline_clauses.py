"""Clause detection: heading matching, the chunk loop, and its invariants.

Every test drives a stub provider, so none of these touch the network.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from app.ai.docpipeline.clauses import (
    MAX_REPAIR_PARAGRAPHS,
    ClauseDetector,
    _clean_ref,
    _normalise,
    _windows,
)
from app.ai.docpipeline.mapping import ClauseSpec
from app.ai.docpipeline.source import PageContent, Paragraph
from app.ai.rag.providers import InferenceResult, StructuredResult, TokenUsage

BOX = (1.0, 1.0, 2.0, 1.0, 2.0, 2.0, 1.0, 2.0)


class StubProvider:
    """Returns queued payloads in order, recording what it was asked."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def generate_structured(
        self, *, system: str, prompt: str, schema: dict, purpose: str = "", **_: Any
    ) -> StructuredResult:
        self.systems.append(system)
        self.prompts.append(prompt)
        data = self._responses.pop(0) if self._responses else {}
        return StructuredResult(
            data=data,
            inference=InferenceResult(
                text="", model="stub", usage=TokenUsage(), stop_reason="stop"
            ),
        )


def _page(number: int, entries: Sequence[tuple[str, str | None]]) -> PageContent:
    paragraphs = tuple(
        Paragraph(page_number=number, index=index + 1, role=role, content=content, polygon=BOX)
        for index, (content, role) in enumerate(entries)
    )
    return PageContent(page_number=number, paragraphs=paragraphs, source_file=None)  # type: ignore[arg-type]


def _specs(*names: str) -> list[ClauseSpec]:
    return [ClauseSpec(clause=name, description=f"what {name} covers") for name in names]


# ------------------------------------------------------------------ helpers
def test_normalise_strips_numbering_and_punctuation() -> None:
    assert _normalise("6. INSPECTION & AUDIT") == "inspection audit"
    assert _normalise("15. FORCE MAJEURE") == "force majeure"
    assert _normalise("Governing law") == "governing law"


def test_clean_ref_accepts_the_bracketed_form() -> None:
    """The extract is printed as '[9.3] text', so models copy the brackets back.

    Treating '[9.3]' as an invented ref discards every correct answer and looks
    exactly like finding nothing.
    """
    assert _clean_ref("[18.19]") == "18.19"
    assert _clean_ref(" 18.19 ") == "18.19"


# ------------------------------------------------------------------ pass A
@pytest.mark.asyncio
async def test_exact_heading_match_costs_no_call() -> None:
    pages = [
        _page(1, [("15. FORCE MAJEURE", "sectionHeading"), ("15.1 A Force Majeure Event ...", None)])
    ]
    provider = StubProvider([])

    result = await ClauseDetector(provider).detect(pages, _specs("Force majeure"))

    assert result.heading_exact == 1
    assert provider.prompts == []
    assert result.detected[0].method == "heading:exact"


@pytest.mark.asyncio
async def test_one_heading_may_satisfy_several_clauses() -> None:
    """'TERMS AND TERMINATION' governs the term and both terminations.

    Keeping only one match reports the others as absent from a document that
    plainly contains them.
    """
    pages = [
        _page(
            1,
            [
                ("7. TERMS AND TERMINATION", "sectionHeading"),
                ("7.1 This Agreement is effective from ...", None),
            ],
        )
    ]
    provider = StubProvider(
        [
            {
                "matches": [
                    {"heading_ref": "1.1", "clause": "Term/duration"},
                    {"heading_ref": "1.1", "clause": "Termination for cause"},
                    {"heading_ref": "1.1", "clause": "Termination for convenience"},
                ]
            }
        ]
    )
    specs = _specs("Term/duration", "Termination for cause", "Termination for convenience")

    result = await ClauseDetector(provider).detect(pages, specs)

    assert {item.clause for item in result.detected} == {spec.clause for spec in specs}
    assert result.not_found == []


@pytest.mark.asyncio
async def test_heading_extent_runs_to_the_next_heading_across_pages() -> None:
    pages = [
        _page(9, [("6. INSPECTION & AUDIT", "sectionHeading"), ("6.1 audit rights ...", None)]),
        _page(10, [("6.2 more audit ...", None), ("7. TERMINATION", "sectionHeading")]),
    ]
    provider = StubProvider([{"matches": [{"heading_ref": "9.1", "clause": "Audit rights"}]}])

    result = await ClauseDetector(provider).detect(pages, _specs("Audit rights"))

    (clause,) = result.detected
    assert clause.page_numbers == [9, 10]
    assert len(clause.paragraphs) == 3  # heading + 6.1 + 6.2, stopping at "7."
    # One box per page, not one box spanning both: a single rectangle over pages
    # 9 and 10 exists on neither of them.
    assert len(clause.polygon) == 8 * len(clause.page_numbers) == 16


# ------------------------------------------------------------------ pass B
@pytest.mark.asyncio
async def test_chunk_refs_are_validated_against_the_chunk() -> None:
    """Refs the model invented are dropped; ones it merely bracketed are kept."""
    pages = [_page(1, [("Payment shall be made within 30 days.", None)])] + [
        _page(number, [("filler " * 40, None)]) for number in range(2, 5)
    ]
    provider = StubProvider(
        [
            {
                "clauses": [
                    {"clause": "Payment terms", "paragraph_refs": ["[1.1]"]},
                    {"clause": "Non-solicit", "paragraph_refs": ["99.99"]},
                ]
            }
        ]
    )

    result = await ClauseDetector(provider).detect(
        pages, _specs("Payment terms", "Non-solicit"), chunk_pages=4, chunk_overlap=0, concurrency=1
    )

    assert [item.clause for item in result.detected] == ["Payment terms"]
    assert result.not_found == ["Non-solicit"]


@pytest.mark.asyncio
async def test_outstanding_clause_forces_every_chunk_to_run() -> None:
    """A clause only present on the last page is still found.

    This is the carry-forward rule: not finding a clause in one chunk is not a
    reason to stop looking for it.
    """
    pages = [_page(number, [(f"page {number} " + "filler " * 40, None)]) for number in range(1, 13)]
    provider = StubProvider(
        [
            {"clauses": []},  # pages 1-4
            {"clauses": []},  # pages 5-8
            {"clauses": [{"clause": "Non-solicit", "paragraph_refs": ["12.1"]}]},  # pages 9-12
        ]
    )

    result = await ClauseDetector(provider).detect(pages, _specs("Non-solicit"), chunk_pages=4, chunk_overlap=0, concurrency=1)

    assert [item.clause for item in result.detected] == ["Non-solicit"]
    assert result.llm_chunk_calls == 3
    assert result.pages_never_read == []


@pytest.mark.asyncio
async def test_early_stop_leaves_later_pages_unread() -> None:
    pages = [_page(number, [(f"page {number} " + "filler " * 40, None)]) for number in range(1, 13)]
    provider = StubProvider(
        [{"clauses": [{"clause": "Non-solicit", "paragraph_refs": ["1.1"]}]}]
    )

    result = await ClauseDetector(provider).detect(pages, _specs("Non-solicit"), chunk_pages=4, chunk_overlap=0, concurrency=1)

    assert result.early_stopped
    assert result.pages_never_read == [5, 6, 7, 8, 9, 10, 11, 12]
    assert result.llm_chunk_calls == 1


@pytest.mark.asyncio
async def test_never_read_and_not_found_are_mutually_exclusive() -> None:
    """The invariant the whole loop exists to hold.

    Reporting a clause absent while pages went unread would be a statement about
    the search printed as a statement about the contract.
    """
    pages = [_page(number, [(f"page {number} " + "filler " * 40, None)]) for number in range(1, 13)]
    provider = StubProvider(
        [
            {"clauses": [{"clause": "Payment terms", "paragraph_refs": ["1.1"]}]},
            {"clauses": []},
            {"clauses": []},
        ]
    )

    result = await ClauseDetector(provider).detect(
        pages, _specs("Payment terms", "Non-solicit"), chunk_pages=4, chunk_overlap=0, concurrency=1
    )

    assert result.not_found == ["Non-solicit"]
    assert result.pages_never_read == []
    assert not (result.not_found and result.pages_never_read)


@pytest.mark.asyncio
async def test_thin_chunk_is_skipped_without_a_call() -> None:
    """A stamp-paper cover is reached and evaluated, not left unread."""
    pages = [_page(1, [("Rs. 500", None)]), _page(2, [("INDIA NON JUDICIAL", None)])]
    provider = StubProvider([{"matches": []}])

    result = await ClauseDetector(provider).detect(pages, _specs("Payment terms"), chunk_pages=4, chunk_overlap=0, concurrency=1)

    (chunk,) = result.chunk_outcomes
    assert chunk.skipped
    assert "floor" in chunk.skip_reason
    assert result.llm_chunk_calls == 0
    assert result.pages_never_read == []  # skipped is not unread


@pytest.mark.asyncio
async def test_boundary_repair_is_capped() -> None:
    """Extending past a chunk edge must not swallow an entire schedule.

    Uncapped, a clause landing on the last paragraph of a chunk ran to the next
    heading - 56 paragraphs across five pages, in a document whose annexures
    carry no headings.
    """
    pages = [_page(1, [("clause text " * 20, None)])] + [
        _page(number, [(f"unheaded schedule line {index}", None) for index in range(12)])
        for number in range(2, 9)
    ]
    provider = StubProvider(
        [{"clauses": [{"clause": "Payment terms", "paragraph_refs": ["4.12"]}]}]
    )

    result = await ClauseDetector(provider).detect(pages, _specs("Payment terms"), chunk_pages=4, chunk_overlap=0, concurrency=1)

    (clause,) = result.detected
    assert clause.boundary_repaired
    assert len(clause.paragraphs) <= 1 + MAX_REPAIR_PARAGRAPHS
    assert max(clause.page_numbers) <= 5  # never runs past the following page


@pytest.mark.asyncio
async def test_provider_failure_does_not_end_the_run() -> None:
    """One bad chunk must not lose the clauses the other passes found."""

    class Exploding(StubProvider):
        async def generate_structured(self, **kwargs: Any) -> StructuredResult:
            if self._responses:
                return await super().generate_structured(**kwargs)
            raise RuntimeError("gateway is having a moment")

    pages = [_page(1, [("15. FORCE MAJEURE", "sectionHeading"), ("15.1 ..." + "x" * 300, None)])]
    provider = Exploding([])

    result = await ClauseDetector(provider).detect(
        pages, _specs("Force majeure", "Non-solicit"), chunk_pages=4, chunk_overlap=0, concurrency=1
    )

    assert [item.clause for item in result.detected] == ["Force majeure"]
    assert result.not_found == ["Non-solicit"]


# ----------------------------------------------------------------- windowing
def _pages(count: int) -> list[PageContent]:
    return [_page(number, [("text", None)]) for number in range(1, count + 1)]


def test_windows_without_overlap_partition_the_document() -> None:
    windows = _windows(_pages(12), 4, 0)

    assert [[page.page_number for page in window] for window in windows] == [
        [1, 2, 3, 4],
        [5, 6, 7, 8],
        [9, 10, 11, 12],
    ]


def test_windows_with_overlap_re_read_the_seam() -> None:
    """A clause cut in half by a boundary is whole in the next window."""
    windows = _windows(_pages(12), 4, 1)

    assert [[page.page_number for page in window] for window in windows] == [
        [1, 2, 3, 4],
        [4, 5, 6, 7],
        [7, 8, 9, 10],
        [10, 11, 12],
    ]


def test_windows_do_not_run_off_the_end() -> None:
    """The last window reaches the end; stepping again would re-read its tail."""
    windows = _windows(_pages(10), 10, 1)

    assert len(windows) == 1
    assert [page.page_number for page in windows[0]] == list(range(1, 11))


@pytest.mark.asyncio
async def test_overlap_does_not_double_count_pages_as_unread() -> None:
    """A page carried by overlap has been read, even if a later window holds it."""
    pages = [_page(number, [(f"page {number} " + "filler " * 40, None)]) for number in range(1, 13)]
    provider = StubProvider([{"clauses": [{"clause": "Non-solicit", "paragraph_refs": ["1.1"]}]}])

    result = await ClauseDetector(provider).detect(
        pages, _specs("Non-solicit"), chunk_pages=4, chunk_overlap=1, concurrency=1
    )

    # Window 1 covered pages 1-4; pages 5-12 were never reached. Page 4 is in
    # the next window too, and must not be reported unread on that account.
    assert result.pages_never_read == [5, 6, 7, 8, 9, 10, 11, 12]


@pytest.mark.asyncio
async def test_a_wave_searches_windows_concurrently() -> None:
    """Windows in one wave share an outstanding list and run together.

    Sequential windows spent 30-100s each in round trips; a wave collapses that
    to one round trip for the whole wave.
    """
    pages = [_page(number, [(f"page {number} " + "filler " * 40, None)]) for number in range(1, 13)]
    provider = StubProvider(
        [
            {"clauses": []},
            {"clauses": []},
            {"clauses": [{"clause": "Non-solicit", "paragraph_refs": ["9.1"]}]},
        ]
    )

    result = await ClauseDetector(provider).detect(
        pages, _specs("Non-solicit"), chunk_pages=4, chunk_overlap=0, concurrency=3
    )

    assert [item.clause for item in result.detected] == ["Non-solicit"]
    assert result.llm_chunk_calls == 3
    assert result.pages_never_read == []


@pytest.mark.asyncio
async def test_first_window_in_a_wave_wins_a_duplicate_clause() -> None:
    """Two windows in one wave may both claim a clause; the earlier one keeps it."""
    pages = [_page(number, [(f"page {number} " + "filler " * 40, None)]) for number in range(1, 9)]
    provider = StubProvider(
        [
            {"clauses": [{"clause": "Non-solicit", "paragraph_refs": ["1.1"]}]},
            {"clauses": [{"clause": "Non-solicit", "paragraph_refs": ["5.1"]}]},
        ]
    )

    result = await ClauseDetector(provider).detect(
        pages, _specs("Non-solicit"), chunk_pages=4, chunk_overlap=0, concurrency=2
    )

    assert len(result.detected) == 1
    assert result.detected[0].page_numbers == [1]


# ------------------------------------------------- phase 2: extents & geometry
@pytest.mark.asyncio
async def test_extent_stops_at_the_signature_block() -> None:
    """A clause must not swallow the execution block.

    Closing pages carry no heading between the last clause and the signatures,
    so "run to the next heading" ran straight through them and embedded
    `IN WITNESS WHEREOF ... Digitally signed by ... Date: 2025.12.26` as clause
    text.
    """
    pages = [
        _page(
            16,
            [
                ("17 OTHER TERMS", "sectionHeading"),
                ("17.4 Assignment: the Parties shall not assign ...", None),
                ("IN WITNESS WHEREOF, the Parties hereto have executed this agreement", None),
                ("Signed and delivered by the within named", None),
                ("YOGENDRA SINGH", None),
            ],
        )
    ]
    provider = StubProvider(
        [{"matches": [{"heading_ref": "16.1", "clause": "Assignment/change of control"}]}]
    )

    result = await ClauseDetector(provider).detect(
        pages, _specs("Assignment/change of control")
    )

    (clause,) = result.detected
    assert len(clause.paragraphs) == 2  # heading + 17.4, stopping before the block
    assert "witness whereof" not in clause.textcontent.lower()
    assert "signed and delivered" not in clause.textcontent.lower()


@pytest.mark.asyncio
async def test_extent_stops_at_page_furniture() -> None:
    pages = [
        _page(
            5,
            [
                ("11. GOVERNING LAW", "sectionHeading"),
                ("11.1 This agreement is governed by the laws of India.", None),
                ("Page 5 of 28", "pageFooter"),
                ("stray text after the footer", None),
            ],
        )
    ]
    provider = StubProvider([{"matches": [{"heading_ref": "5.1", "clause": "Governing law"}]}])

    result = await ClauseDetector(provider).detect(pages, _specs("Governing law"))

    (clause,) = result.detected
    assert len(clause.paragraphs) == 2
    assert "stray text" not in clause.textcontent


def test_signature_detection_does_not_fire_on_clause_prose() -> None:
    """A clause discussing signing authority is still clause text."""
    ordinary = Paragraph(
        page_number=3,
        index=1,
        role=None,
        content=(
            "Each Party represents that the person executing this Agreement on its "
            "behalf has been duly authorised to do so."
        ),
        polygon=BOX,
    )
    closing = Paragraph(
        page_number=17,
        index=1,
        role=None,
        content="IN WITNESS WHEREOF, the Parties hereto have executed this agreement",
        polygon=BOX,
    )

    assert not ordinary.is_signature_block
    assert closing.is_signature_block


def test_polygon_has_one_box_per_page() -> None:
    from app.ai.docpipeline.source import pages_with_geometry, per_page_boxes

    paragraphs = [
        Paragraph(16, 1, None, "a", (1.0, 1.0, 3.0, 1.0, 3.0, 2.0, 1.0, 2.0)),
        Paragraph(16, 2, None, "b", (1.0, 4.0, 5.0, 4.0, 5.0, 6.0, 1.0, 6.0)),
        Paragraph(17, 1, None, "c", (2.0, 1.0, 4.0, 1.0, 4.0, 3.0, 2.0, 3.0)),
    ]

    boxes = per_page_boxes(paragraphs)
    pages = pages_with_geometry(paragraphs)

    assert pages == [16, 17]
    assert len(boxes) == 8 * len(pages) == 16
    # Page 16's box covers both of its paragraphs and nothing from page 17.
    assert boxes[0:8] == [1.0, 1.0, 5.0, 1.0, 5.0, 6.0, 1.0, 6.0]
    assert boxes[8:16] == [2.0, 1.0, 4.0, 1.0, 4.0, 3.0, 2.0, 3.0]


def test_pages_with_geometry_skips_pages_that_have_none() -> None:
    """Arrays stay aligned when a page contributes no box."""
    from app.ai.docpipeline.source import pages_with_geometry, per_page_boxes

    paragraphs = [
        Paragraph(1, 1, None, "has geometry", BOX),
        Paragraph(2, 1, None, "no geometry", ()),
    ]

    assert pages_with_geometry(paragraphs) == [1]
    assert len(per_page_boxes(paragraphs)) == 8


# ------------------------------------------------- phase 2: heading behaviour
@pytest.mark.asyncio
async def test_heading_union_takes_a_clause_only_the_second_sample_found() -> None:
    """Recall, not precision: the same call returned 9, 11 and 13 across runs."""
    pages = [
        _page(
            1,
            [
                ("7. TERMS AND TERMINATION", "sectionHeading"),
                ("7.1 effective from ...", None),
                ("14. NOTICES", "sectionHeading"),
                ("14.1 all notices ...", None),
            ],
        )
    ]
    provider = StubProvider(
        [
            {"matches": [{"heading_ref": "1.1", "clause": "Term/duration"}]},
            {"matches": [{"heading_ref": "1.3", "clause": "Notice requirements"}]},
        ]
    )

    result = await ClauseDetector(provider).detect(
        pages, _specs("Term/duration", "Notice requirements")
    )

    assert {item.clause for item in result.detected} == {"Term/duration", "Notice requirements"}


@pytest.mark.asyncio
async def test_shared_section_is_split_so_clauses_differ() -> None:
    """Three clauses under one heading must not store identical text.

    Identical text means identical vectors, and three rows a retrieval query
    cannot tell apart.
    """
    pages = [
        _page(
            10,
            [
                ("7. TERMS AND TERMINATION", "sectionHeading"),
                ("7.1 This Agreement is effective for three years.", None),
                ("7.2 Either Party may terminate for material breach.", None),
            ],
        )
    ]
    provider = StubProvider(
        [
            {
                "matches": [
                    {"heading_ref": "10.1", "clause": "Term/duration"},
                    {"heading_ref": "10.1", "clause": "Termination for cause"},
                ]
            },
            {"matches": []},
            {
                "assignments": [
                    {"clause": "Term/duration", "paragraph_refs": ["10.1", "10.2"]},
                    {"clause": "Termination for cause", "paragraph_refs": ["10.3"]},
                ]
            },
        ]
    )

    result = await ClauseDetector(provider).detect(
        pages, _specs("Term/duration", "Termination for cause")
    )

    texts = {item.clause: item.textcontent for item in result.detected}
    assert len(texts) == 2
    assert texts["Term/duration"] != texts["Termination for cause"]
    assert "three years" in texts["Term/duration"]
    assert "material breach" in texts["Termination for cause"]


@pytest.mark.asyncio
async def test_a_failed_split_falls_back_to_the_shared_extent() -> None:
    """Wrong-but-usable beats missing: the clause still gets its section."""
    pages = [
        _page(
            10,
            [
                ("7. TERMS AND TERMINATION", "sectionHeading"),
                ("7.1 This Agreement is effective for three years.", None),
            ],
        )
    ]
    provider = StubProvider(
        [
            {
                "matches": [
                    {"heading_ref": "10.1", "clause": "Term/duration"},
                    {"heading_ref": "10.1", "clause": "Termination for cause"},
                ]
            },
            {"matches": []},
            {"assignments": [{"clause": "Term/duration", "paragraph_refs": ["10.1"]}]},
        ]
    )

    result = await ClauseDetector(provider).detect(
        pages, _specs("Term/duration", "Termination for cause")
    )

    assert len(result.detected) == 2
    assert all("three years" in item.textcontent for item in result.detected)

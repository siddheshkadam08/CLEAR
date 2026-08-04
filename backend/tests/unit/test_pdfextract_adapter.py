"""The local extractor as a first-class parser.

`pdfextract` is not a fallback: it emits the same Azure ``prebuilt-layout``
payloads the iDoc service does, which is what lets the document pipeline - which
reads those raw payloads rather than the normalised document - run unchanged
against either. These tests pin the two things that make that true: the payload
division, and the fact that the adapter inherits iDoc's mapping rather than
carrying a second copy of it.

None of them need the extractor installed. The subprocess boundary is exercised
only in its failure modes, which is where the interesting behaviour is anyway.
"""

from __future__ import annotations

import pytest

from app.ai.parsers.idoc_adapter import IDocParser
from app.ai.parsers.pdfextract_adapter import PdfTextExtractorParser, split_pages
from app.core.enums import FileType


def page(number: int, *paragraphs: dict) -> dict:
    return {
        "pageNumber": number,
        "unit": "inch",
        "width": 8.5,
        "height": 11.0,
        "words": [],
        "lines": [],
    }


def paragraph(number: int, content: str, role: str | None = None) -> dict:
    entry: dict = {
        "content": content,
        "boundingRegions": [{"pageNumber": number, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
    }
    if role:
        entry["role"] = role
    return entry


def analyze_result(pages: list[dict], paragraphs: list[dict]) -> dict:
    return {
        "analyzeResult": {
            "modelId": "prebuilt-layout",
            "apiVersion": "2024-11-30",
            "pages": pages,
            "paragraphs": paragraphs,
        }
    }


# =============================================================================
# Splitting one document into per-page payloads
# =============================================================================
class TestSplitPages:
    def test_each_page_becomes_one_payload(self) -> None:
        adi = analyze_result(
            [page(1), page(2), page(3)],
            [paragraph(1, "one"), paragraph(2, "two"), paragraph(3, "three")],
        )

        payloads = split_pages(adi)

        assert len(payloads) == 3
        assert [p["pages"][0]["pageNumber"] for p in payloads] == [1, 2, 3]

    def test_a_payload_carries_only_its_own_paragraphs(self) -> None:
        # The whole point of the split. `source._build_page` reads
        # `payload["paragraphs"]` as that page's text, so leaking another page's
        # content in would attribute clauses to the wrong page.
        adi = analyze_result(
            [page(1), page(2)],
            [paragraph(1, "first"), paragraph(2, "second"), paragraph(2, "also second")],
        )

        payloads = split_pages(adi)

        assert [p["content"] for p in payloads[0]["paragraphs"]] == ["first"]
        assert [p["content"] for p in payloads[1]["paragraphs"]] == ["second", "also second"]

    def test_the_page_is_its_payloads_first_entry(self) -> None:
        # `_page_of` and `source._page_data` both read `pages[0]` for dimensions
        # and unit; a payload listing every page would describe the wrong one.
        payloads = split_pages(analyze_result([page(1), page(2)], []))

        for payload in payloads:
            assert len(payload["pages"]) == 1

    def test_a_paragraph_with_no_page_is_dropped_not_guessed(self) -> None:
        # Placing it on page 1 would give a clause a citation pointing at a page
        # it is not on, which is worse here than the paragraph being absent.
        orphan = {"content": "nowhere"}
        adi = analyze_result([page(1)], [paragraph(1, "real"), orphan])

        payloads = split_pages(adi)

        assert [p["content"] for p in payloads[0]["paragraphs"]] == ["real"]

    def test_a_page_with_no_paragraphs_still_appears(self) -> None:
        # A blank or image-only page is part of the document; dropping it would
        # renumber every page after it.
        payloads = split_pages(analyze_result([page(1), page(2)], [paragraph(2, "text")]))

        assert len(payloads) == 2
        assert payloads[0]["paragraphs"] == []

    def test_it_accepts_an_unwrapped_result(self) -> None:
        """Some exports omit the `analyzeResult` envelope."""
        unwrapped = {"pages": [page(1)], "paragraphs": [paragraph(1, "hello")]}

        payloads = split_pages(unwrapped)

        assert len(payloads) == 1
        assert payloads[0]["paragraphs"][0]["content"] == "hello"

    def test_roles_survive_the_split(self) -> None:
        # `sectionHeading` is what the clause detector's first pass keys on, so
        # losing it here would silently halve the pipeline's accuracy.
        adi = analyze_result([page(1)], [paragraph(1, "DEFINITIONS", role="sectionHeading")])

        assert split_pages(adi)[0]["paragraphs"][0]["role"] == "sectionHeading"


# =============================================================================
# The adapter
# =============================================================================
class TestAdapter:
    def test_it_reuses_idocs_mapping_rather_than_copying_it(self) -> None:
        """The payload shapes are identical, so the conversion must be shared.

        Two implementations of the ADI -> CDM mapping would drift, and the
        coordinate maths is the part where drift is hardest to notice.
        """
        assert issubclass(PdfTextExtractorParser, IDocParser)

    def test_it_has_its_own_cache_namespace(self) -> None:
        # `_payloads` keys the object cache and fixture store on this name. Were
        # it left as "idoc", a document parsed locally would be served back to a
        # deployment that believed it came from the service.
        assert PdfTextExtractorParser().capabilities.name == "pdfextract"

    def test_it_is_local_not_remote(self) -> None:
        assert PdfTextExtractorParser().capabilities.is_remote is False

    def test_it_reports_layout_capabilities(self) -> None:
        # Claimed because the ADI payload really carries them - roles, polygons
        # and tables all survive the export.
        capabilities = PdfTextExtractorParser().capabilities
        assert capabilities.supports_coordinates
        assert capabilities.supports_sections
        assert capabilities.supports(FileType.PDF)
        assert not capabilities.supports(FileType.DOCX)

    @pytest.mark.asyncio
    async def test_health_is_false_without_a_configured_checkout(
        self, settings_env: object
    ) -> None:
        from app.core.config import get_settings

        get_settings.cache_clear()
        assert await PdfTextExtractorParser().health() is False

    def test_the_registry_can_build_it(self) -> None:
        from app.ai.parsers.registry import get_parser_by_name

        assert isinstance(get_parser_by_name("pdfextract"), PdfTextExtractorParser)

    def test_it_is_a_pdf_fallback_ahead_of_pymupdf(self) -> None:
        """Order matters: only these two produce the layout JSON.

        `pymupdf` returns a normalised document and writes no layout payloads, so
        a PDF that falls through to it cannot run the document pipeline at all.
        """
        from app.ai.parsers.registry import _FALLBACKS

        chain = _FALLBACKS[FileType.PDF]
        assert chain.index("pdfextract") < chain.index("pymupdf")

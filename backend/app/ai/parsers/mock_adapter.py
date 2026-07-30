"""Deterministic mock parser.

Produces a structurally complete :class:`~app.ai.cdm.models.NormalizedDocument` -
sections, paragraphs, a table, a list, coordinates, reading order - without any
parsing dependency. That makes the whole pipeline runnable in CI and in tests, and
it is what lets a developer exercise chunking, extraction and retrieval on a laptop
with no PDF libraries installed.

Deterministic on purpose: output depends only on the file hash, so a test that
asserts on chunk counts or clause positions stays stable.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.ai.cdm.models import (
    ContentBlock,
    Coordinates,
    DocumentList,
    DocumentMetadata,
    HeaderFooter,
    ListItem,
    NormalizedDocument,
    Page,
    Paragraph,
    QualityMetrics,
    Section,
    Signature,
    Table,
    TableCell,
)
from app.ai.parsers.base import IDocumentParser, ParserCapabilities, ParseRequest
from app.core.enums import ContentBlockType, FileType
from app.core.versions import PARSER_FRAMEWORK_VERSION

_PAGE_WIDTH = 612.0
_PAGE_HEIGHT = 792.0
_LINE_HEIGHT = 14.0

#: A small but realistic contract skeleton: numbered sections whose headings match
#: the Clause Master's heading patterns, so classification and extraction have
#: something meaningful to find.
_TEMPLATE: list[tuple[str, str, list[str]]] = [
    (
        "1",
        "Term",
        [
            'This Agreement shall commence on 1 January 2026 (the "Effective Date") '
            "and shall continue for an initial term of twenty-four (24) months, "
            "expiring on 31 December 2027 unless earlier terminated in accordance "
            "with Section 4.",
        ],
    ),
    (
        "2",
        "Payment Terms",
        [
            "Customer shall pay all undisputed invoices within thirty (30) days of "
            "the invoice date. All amounts are stated in USD and are exclusive of "
            "applicable taxes.",
            "Late payments shall accrue interest at 1.5% per month from the due date "
            "until paid in full.",
        ],
    ),
    (
        "3",
        "Confidentiality",
        [
            "Each party shall hold the other party's Confidential Information in "
            "strict confidence and shall not disclose it to any third party without "
            "prior written consent. These obligations are mutual and shall survive "
            "termination of this Agreement for a period of five (5) years.",
        ],
    ),
    (
        "4",
        "Termination",
        [
            "Either party may terminate this Agreement for convenience upon sixty "
            "(60) days' prior written notice to the other party.",
            "Either party may terminate this Agreement immediately upon written "
            "notice if the other party commits a material breach and fails to cure "
            "that breach within thirty (30) days of receiving notice of it.",
        ],
    ),
    (
        "5",
        "Limitation of Liability",
        [
            "Except as set out below, each party's total aggregate liability arising "
            "out of or in connection with this Agreement shall not exceed two (2) "
            "times the total fees paid by Customer in the twelve (12) months "
            "preceding the event giving rise to the claim.",
            "Nothing in this Agreement shall limit either party's liability for "
            "breach of confidentiality, infringement of intellectual property "
            "rights, gross negligence or wilful misconduct.",
        ],
    ),
    (
        "6",
        "Indemnification",
        [
            "Supplier shall defend, indemnify and hold harmless Customer against any "
            "third-party claim alleging that the Services infringe that third "
            "party's intellectual property rights.",
        ],
    ),
    (
        "7",
        "Intellectual Property",
        [
            "Customer shall own all right, title and interest in the Deliverables "
            "created under this Agreement. Each party retains all right, title and "
            "interest in its pre-existing intellectual property.",
        ],
    ),
    (
        "8",
        "Governing Law",
        [
            "This Agreement shall be governed by and construed in accordance with "
            "the laws of the State of Delaware, United States, without regard to its "
            "conflict of laws principles.",
        ],
    ),
]


class MockParser(IDocumentParser):
    """Deterministic parser for tests, CI and dependency-free local development."""

    @property
    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities(
            name="mock",
            version="1.0.0",
            supported_types=frozenset({FileType.PDF, FileType.DOCX}),
            supports_coordinates=True,
            supports_tables=True,
            supports_sections=True,
            supports_lists=True,
            supports_images=False,
            supports_ocr=False,
            supports_signatures=True,
            is_remote=False,
        )

    async def health(self) -> bool:
        return True

    async def page_count(self, request: ParseRequest) -> int | None:
        return 3

    async def parse(self, request: ParseRequest) -> NormalizedDocument:
        sections: list[Section] = []
        paragraphs: list[Paragraph] = []
        blocks_by_page: dict[int, list[ContentBlock]] = {1: [], 2: [], 3: []}
        order = 0
        y = 90.0
        page_number = 1

        # Three sections per page. Deterministic rather than height-driven so the
        # fixture reliably spans all three pages - the chunker's cross-page and
        # per-page grouping logic needs real page boundaries to exercise.
        sections_per_page = 3

        for index, (number, title, body) in enumerate(_TEMPLATE):
            expected_page = min(3, index // sections_per_page + 1)
            if expected_page != page_number:
                page_number = expected_page
                y = 90.0

            section = Section(
                section_id=f"sec-{len(sections)}",
                title=title,
                level=1,
                parent_section=None,
                start_page=page_number,
                end_page=page_number,
                number=number,
                order=len(sections),
                coordinates=Coordinates(
                    page_number=page_number,
                    x=72.0,
                    y=y,
                    width=400.0,
                    height=_LINE_HEIGHT,
                    page_width=_PAGE_WIDTH,
                    page_height=_PAGE_HEIGHT,
                ),
            )
            sections.append(section)

            blocks_by_page[page_number].append(
                ContentBlock(
                    block_id=f"p{page_number}-h{len(sections)}",
                    block_type=ContentBlockType.HEADING,
                    order=len(blocks_by_page[page_number]),
                    text=f"{number}. {title}",
                    level=1,
                    section_id=section.section_id,
                    coordinates=section.coordinates,
                )
            )
            order += 1
            y += _LINE_HEIGHT * 1.6

            for text in body:
                lines = max(1, len(text) // 90)
                height = _LINE_HEIGHT * lines
                coordinates = Coordinates(
                    page_number=page_number,
                    x=72.0,
                    y=y,
                    width=468.0,
                    height=height,
                    page_width=_PAGE_WIDTH,
                    page_height=_PAGE_HEIGHT,
                )
                paragraphs.append(
                    Paragraph(
                        paragraph_id=f"para-{len(paragraphs)}",
                        text=text,
                        page_number=page_number,
                        section_id=section.section_id,
                        coordinates=coordinates,
                        reading_order=order,
                    )
                )
                blocks_by_page[page_number].append(
                    ContentBlock(
                        block_id=f"p{page_number}-b{len(blocks_by_page[page_number])}",
                        block_type=ContentBlockType.PARAGRAPH,
                        order=len(blocks_by_page[page_number]),
                        text=text,
                        section_id=section.section_id,
                        coordinates=coordinates,
                    )
                )
                order += 1
                y += height + 8

        # A fee table, so table-preserving chunking has something to preserve.
        fee_table = Table(
            table_id="p2-t0",
            page_number=2,
            rows=3,
            columns=3,
            cells=[
                TableCell(row=0, col=0, text="Service", is_header=True),
                TableCell(row=0, col=1, text="Rate", is_header=True),
                TableCell(row=0, col=2, text="Currency", is_header=True),
                TableCell(row=1, col=0, text="Implementation"),
                TableCell(row=1, col=1, text="50,000"),
                TableCell(row=1, col=2, text="USD"),
                TableCell(row=2, col=0, text="Annual support"),
                TableCell(row=2, col=1, text="12,000"),
                TableCell(row=2, col=2, text="USD"),
            ],
            caption="Schedule A - Fees",
            section_id="sec-1",
            coordinates=Coordinates(
                page_number=2,
                x=72.0,
                y=400.0,
                width=468.0,
                height=90.0,
                page_width=_PAGE_WIDTH,
                page_height=_PAGE_HEIGHT,
            ),
            reading_order=order,
        )
        order += 1

        carve_out_list = DocumentList(
            list_id="list-0",
            page_number=2,
            ordered=False,
            items=[
                ListItem(text="breach of confidentiality obligations"),
                ListItem(text="infringement of intellectual property rights"),
                ListItem(text="gross negligence or wilful misconduct"),
            ],
            section_id="sec-4",
            coordinates=Coordinates(
                page_number=2,
                x=90.0,
                y=520.0,
                width=440.0,
                height=54.0,
                page_width=_PAGE_WIDTH,
                page_height=_PAGE_HEIGHT,
            ),
            reading_order=order,
        )
        order += 1

        return NormalizedDocument(
            metadata=DocumentMetadata(
                document_id=request.document_id,
                organization_id=request.organization_id,
                project_id=request.project_id,
                file_name=request.file_name,
                storage_path=request.storage_path,
                file_type=request.file_type.value,
                file_size=request.size,
                hash=request.file_hash,
                language="en",
                parser_name="mock",
                parser_version=PARSER_FRAMEWORK_VERSION,
                adapter_version="1.0.0",
                created_at=datetime.now(UTC).isoformat(),
            ),
            pages=[
                Page(
                    page_number=number,
                    width=_PAGE_WIDTH,
                    height=_PAGE_HEIGHT,
                    reading_order=0,
                    content_blocks=blocks,
                    text_char_count=sum(len(block.text) for block in blocks),
                )
                for number, blocks in sorted(blocks_by_page.items())
            ],
            sections=sections,
            paragraphs=paragraphs,
            tables=[fee_table],
            lists=[carve_out_list],
            images=[],
            signatures=[
                Signature(
                    signature_id="sig-0",
                    page_number=3,
                    signatory_name="Jordan Ellis",
                    signatory_title="Chief Operating Officer",
                    party="Supplier",
                    date="2026-01-01",
                    raw_text="IN WITNESS WHEREOF the parties have executed this Agreement.",
                    coordinates=Coordinates(
                        page_number=3,
                        x=72.0,
                        y=600.0,
                        width=300.0,
                        height=60.0,
                        page_width=_PAGE_WIDTH,
                        page_height=_PAGE_HEIGHT,
                    ),
                )
            ],
            headers=[
                HeaderFooter(text="Master Services Agreement", page_number=number, kind="header")
                for number in (1, 2, 3)
            ],
            footers=[
                HeaderFooter(text=f"Page {number} of 3", page_number=number, kind="footer")
                for number in (1, 2, 3)
            ],
            quality=QualityMetrics(coordinate_coverage=1.0),
        )


__all__ = ["MockParser"]

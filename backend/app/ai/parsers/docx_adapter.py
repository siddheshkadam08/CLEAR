"""DOCX parser adapter (python-docx).

DOCX has no page model - pagination is a rendering decision made by Word, not
something stored in the file. This adapter therefore synthesises page numbers by
accumulating estimated content height, which keeps the CDM's coordinate contract
satisfiable for DOCX without pretending to a precision the format does not have.

The tradeoff is stated plainly in the quality metrics: coordinates for DOCX are
**estimated**, so the viewer positions a highlight approximately. That is better
than the alternative (no coordinates, so no evidence highlighting at all), and the
`coordinates_estimated` warning tells the UI to say so.

Structure, by contrast, is genuinely reliable: DOCX carries real heading styles,
real tables and real list numbering, so sections and hierarchy are read directly
rather than inferred from font size.
"""

from __future__ import annotations

import asyncio
import io
import re
from datetime import UTC, datetime
from typing import Any

from app.ai.cdm.models import (
    ContentBlock,
    Coordinates,
    DocumentList,
    DocumentMetadata,
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
from app.core.errors import CorruptedDocumentError, ParserError
from app.core.logging import get_logger
from app.core.versions import PARSER_ADAPTER_VERSIONS, PARSER_FRAMEWORK_VERSION

logger = get_logger(__name__)

# US Letter at 72 dpi, matching the PDF adapter so both produce coordinates in the
# same space and the viewer needs no per-format special casing.
_PAGE_WIDTH = 612.0
_PAGE_HEIGHT = 792.0
_MARGIN = 72.0
_LINE_HEIGHT = 14.0
_CHARS_PER_LINE = 95
_CONTENT_HEIGHT = _PAGE_HEIGHT - (2 * _MARGIN)

_HEADING_STYLE = re.compile(r"^heading\s*(\d+)$", re.IGNORECASE)
_NUMBER_PREFIX = re.compile(r"^((?:\d+|[IVXLC]+|[A-Z])(?:\.\d+)*\.?)\s+(\S.*)$")


class DocxParser(IDocumentParser):
    @property
    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities(
            name="docx",
            version=PARSER_ADAPTER_VERSIONS.get("docx", PARSER_FRAMEWORK_VERSION),
            supported_types=frozenset({FileType.DOCX}),
            # Estimated rather than exact - see the module docstring.
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
        try:
            import docx  # noqa: F401

            return True
        except ImportError:
            return False

    async def parse(self, request: ParseRequest) -> NormalizedDocument:
        try:
            return await asyncio.to_thread(self._parse_sync, request)
        except CorruptedDocumentError:
            raise
        except ImportError as exc:
            raise ParserError(
                "DOCX parsing requires the 'parsers' extra: pip install '.[parsers]'",
                stage="parser",
            ) from exc
        except Exception as exc:
            raise ParserError(f"The DOCX file could not be parsed: {exc}") from exc

    # =========================================================================
    def _parse_sync(self, request: ParseRequest) -> NormalizedDocument:
        import docx
        from docx.document import Document as DocxDocument
        from docx.oxml.ns import qn
        from docx.table import Table as DocxTable
        from docx.text.paragraph import Paragraph as DocxParagraph

        try:
            document: DocxDocument = docx.Document(io.BytesIO(request.content))
        except Exception as exc:
            raise CorruptedDocumentError(
                "The DOCX file could not be opened - it may be corrupted.", stage="parser"
            ) from exc

        sections: list[Section] = []
        paragraphs: list[Paragraph] = []
        tables: list[Table] = []
        lists: list[DocumentList] = []
        blocks_by_page: dict[int, list[ContentBlock]] = {}

        state = _Layout()
        current_section: Section | None = None
        pending_list: list[tuple[str, int]] = []
        order = 0

        def flush_list() -> None:
            nonlocal pending_list
            if len(pending_list) < 2:
                pending_list = []
                return
            first_page, first_y = state.page, state.y
            items = [
                ListItem(text=text, level=level, coordinates=None) for text, level in pending_list
            ]
            lists.append(
                DocumentList(
                    list_id=f"list-{len(lists)}",
                    page_number=first_page,
                    ordered=False,
                    items=items,
                    section_id=current_section.section_id if current_section else None,
                    coordinates=Coordinates(
                        page_number=first_page,
                        x=_MARGIN + 18,
                        y=first_y,
                        width=_PAGE_WIDTH - 2 * _MARGIN - 18,
                        height=_LINE_HEIGHT * len(items),
                        page_width=_PAGE_WIDTH,
                        page_height=_PAGE_HEIGHT,
                    ),
                    reading_order=order,
                )
            )
            pending_list = []

        # Walk the body in document order. python-docx exposes paragraphs and tables
        # as separate collections, so the XML body is iterated directly to keep the
        # true interleaved reading order - which chunking depends on.
        for element in document.element.body.iterchildren():
            if element.tag == qn("w:p"):
                paragraph = DocxParagraph(element, document)
                text = paragraph.text.strip()
                if not text:
                    continue

                style_name = (paragraph.style.name if paragraph.style else "") or ""
                heading_match = _HEADING_STYLE.match(style_name.strip())
                is_list = self._is_list_paragraph(paragraph, style_name)

                if heading_match or (style_name.lower() == "title"):
                    flush_list()
                    level = int(heading_match.group(1)) if heading_match else 1
                    number_match = _NUMBER_PREFIX.match(text)
                    coords = state.advance(1)
                    current_section = Section(
                        section_id=f"sec-{len(sections)}",
                        title=(number_match.group(2) if number_match else text)[:500],
                        level=level,
                        parent_section=self._parent_for(sections, level),
                        start_page=coords.page_number,
                        end_page=coords.page_number,
                        number=number_match.group(1) if number_match else None,
                        order=len(sections),
                        coordinates=coords,
                    )
                    sections.append(current_section)
                    blocks_by_page.setdefault(coords.page_number, []).append(
                        ContentBlock(
                            block_id=f"p{coords.page_number}-h{len(sections)}",
                            block_type=ContentBlockType.HEADING,
                            order=len(blocks_by_page[coords.page_number]),
                            text=text,
                            level=level,
                            section_id=current_section.section_id,
                            coordinates=coords,
                        )
                    )
                    order += 1
                    continue

                if is_list:
                    pending_list.append((text, self._list_level(paragraph)))
                    continue

                flush_list()
                line_count = max(1, len(text) // _CHARS_PER_LINE + 1)
                coords = state.advance(line_count)

                paragraphs.append(
                    Paragraph(
                        paragraph_id=f"para-{len(paragraphs)}",
                        text=text,
                        page_number=coords.page_number,
                        section_id=current_section.section_id if current_section else None,
                        coordinates=coords,
                        reading_order=order,
                        style=style_name or None,
                    )
                )
                blocks_by_page.setdefault(coords.page_number, []).append(
                    ContentBlock(
                        block_id=f"p{coords.page_number}-b{len(blocks_by_page[coords.page_number])}",
                        block_type=ContentBlockType.PARAGRAPH,
                        order=len(blocks_by_page[coords.page_number]),
                        text=text,
                        section_id=current_section.section_id if current_section else None,
                        coordinates=coords,
                    )
                )
                order += 1

                if current_section is not None and coords.page_number > current_section.end_page:
                    sections[-1] = current_section.model_copy(
                        update={"end_page": coords.page_number}
                    )
                    current_section = sections[-1]

            elif element.tag == qn("w:tbl"):
                flush_list()
                docx_table = DocxTable(element, document)
                table = self._convert_table(
                    docx_table,
                    index=len(tables),
                    state=state,
                    section_id=current_section.section_id if current_section else None,
                    order=order,
                )
                if table is not None:
                    tables.append(table)
                    order += 1

        flush_list()

        page_numbers = sorted(blocks_by_page) or [1]
        pages = [
            Page(
                page_number=number,
                width=_PAGE_WIDTH,
                height=_PAGE_HEIGHT,
                reading_order=0,
                content_blocks=blocks_by_page.get(number, []),
                text_char_count=sum(len(block.text) for block in blocks_by_page.get(number, [])),
            )
            for number in range(1, max(page_numbers) + 1)
        ]

        signatures = self._detect_signatures(paragraphs)
        core = document.core_properties

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
                language=request.language_hint or (core.language or None),
                parser_name="docx",
                parser_version=PARSER_FRAMEWORK_VERSION,
                adapter_version=self.capabilities.version,
                created_at=datetime.now(UTC).isoformat(),
            ),
            pages=pages,
            sections=sections,
            paragraphs=paragraphs,
            tables=tables,
            lists=lists,
            images=[],
            signatures=signatures,
            headers=[],
            footers=[],
            quality=QualityMetrics(
                coordinate_coverage=1.0,
                warnings=[
                    "DOCX has no page model; page numbers and coordinates are "
                    "estimated from content flow, so highlight positions are "
                    "approximate.",
                ],
            ),
        )

    # =========================================================================
    # Helpers
    # =========================================================================
    @staticmethod
    def _is_list_paragraph(paragraph: Any, style_name: str) -> bool:
        """Detect a list item from the style name or real numbering properties."""
        if "list" in style_name.lower() or "bullet" in style_name.lower():
            return True
        try:
            from docx.oxml.ns import qn

            numbering = paragraph._p.find(qn("w:pPr"))
            return numbering is not None and numbering.find(qn("w:numPr")) is not None
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _list_level(paragraph: Any) -> int:
        try:
            from docx.oxml.ns import qn

            properties = paragraph._p.find(qn("w:pPr"))
            if properties is None:
                return 1
            numbering = properties.find(qn("w:numPr"))
            if numbering is None:
                return 1
            level = numbering.find(qn("w:ilvl"))
            if level is None:
                return 1
            return int(level.get(qn("w:val"), "0")) + 1
        except Exception:  # noqa: BLE001
            return 1

    @staticmethod
    def _parent_for(sections: list[Section], level: int) -> str | None:
        for section in reversed(sections):
            if section.level < level:
                return section.section_id
        return None

    def _convert_table(
        self,
        docx_table: Any,
        *,
        index: int,
        state: _Layout,
        section_id: str | None,
        order: int,
    ) -> Table | None:
        cells: list[TableCell] = []
        row_count = 0
        column_count = 0

        try:
            for row_index, row in enumerate(docx_table.rows):
                row_count += 1
                column_count = max(column_count, len(row.cells))
                for col_index, cell in enumerate(row.cells):
                    cells.append(
                        TableCell(
                            row=row_index,
                            col=col_index,
                            text=cell.text.strip(),
                            is_header=row_index == 0,
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug("docx_table_conversion_failed", index=index, error=str(exc))
            return None

        if not cells:
            return None

        coords = state.advance(row_count + 1)
        return Table(
            table_id=f"tbl-{index}",
            page_number=coords.page_number,
            rows=row_count,
            columns=column_count,
            cells=cells,
            section_id=section_id,
            coordinates=coords,
            reading_order=order,
        )

    def _detect_signatures(self, paragraphs: list[Paragraph]) -> list[Signature]:
        markers = ("in witness whereof", "signature", "signed by", "authorised signatory")
        signatures: list[Signature] = []
        for index, paragraph in enumerate(paragraphs):
            if not any(marker in paragraph.text.lower() for marker in markers):
                continue
            window = paragraphs[index : index + 6]
            joined = "\n".join(item.text for item in window)
            name = re.search(r"(?:name)\s*[:\-]\s*(.+)", joined, re.IGNORECASE)
            title = re.search(r"(?:title|designation)\s*[:\-]\s*(.+)", joined, re.IGNORECASE)
            date = re.search(r"(?:date|dated)\s*[:\-]\s*(.+)", joined, re.IGNORECASE)
            signatures.append(
                Signature(
                    signature_id=f"sig-{len(signatures)}",
                    page_number=paragraph.page_number,
                    coordinates=paragraph.coordinates,
                    signatory_name=name.group(1).strip()[:255] if name else None,
                    signatory_title=title.group(1).strip()[:255] if title else None,
                    date=date.group(1).strip()[:64] if date else None,
                    raw_text=joined[:2000],
                )
            )
        return signatures


class _Layout:
    """Tracks synthesised page position while walking a DOCX body.

    DOCX carries no pagination, so position is accumulated from estimated content
    height and rolled onto a new page when the content area fills.
    """

    __slots__ = ("page", "y")

    def __init__(self) -> None:
        self.page = 1
        self.y = _MARGIN

    def advance(self, line_count: int) -> Coordinates:
        height = _LINE_HEIGHT * max(1, line_count)
        if self.y + height > _MARGIN + _CONTENT_HEIGHT:
            self.page += 1
            self.y = _MARGIN

        coords = Coordinates(
            page_number=self.page,
            x=_MARGIN,
            y=self.y,
            width=_PAGE_WIDTH - 2 * _MARGIN,
            height=height,
            page_width=_PAGE_WIDTH,
            page_height=_PAGE_HEIGHT,
        )
        self.y += height + 6
        return coords


__all__ = ["DocxParser"]

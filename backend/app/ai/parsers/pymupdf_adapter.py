"""PyMuPDF parser adapter.

Local, dependency-light and always available, which makes it the platform's
fallback for PDFs. It extracts real coordinates from the PDF text layer, detects
headings from font size, groups blocks into sections, and routes pages with no
extractable text through OCR.

Coordinate note: PyMuPDF reports rectangles in top-left origin page space, which is
what a browser canvas expects, so no flip is needed before the viewer draws them.
"""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.ai.cdm.models import (
    ContentBlock,
    Coordinates,
    DocumentList,
    DocumentMetadata,
    HeaderFooter,
    Image,
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
from app.core.config import get_settings
from app.core.enums import ContentBlockType, FileType
from app.core.errors import CorruptedDocumentError, ParserError
from app.core.logging import get_logger
from app.core.versions import PARSER_ADAPTER_VERSIONS, PARSER_FRAMEWORK_VERSION

logger = get_logger(__name__)

#: A line whose font size exceeds the body median by this factor is a heading.
_HEADING_SIZE_RATIO = 1.15

#: Fraction of page height treated as header/footer bands.
_HEADER_BAND = 0.07
_FOOTER_BAND = 0.93

#: Markers that identify a numbered/lettered list item.
_LIST_MARKERS = ("•", "◦", "▪", "-", "–", "*")


@dataclass(slots=True)
class _Line:
    """One text line with its geometry - the unit this adapter reasons about.

    ``page_width`` / ``page_height`` travel with every line, and must. The viewer
    scales a highlight by ``x / page_width``, so a box without them cannot be
    drawn - and because ``Coordinates.to_dict`` serialises with
    ``exclude_none=True``, the keys do not arrive as null, they are absent
    entirely. The box then validates as a ``BoundingBox`` (both fields are
    optional there), reaches the browser intact, and silently renders nothing.

    That was the bug: for a PyMuPDF-parsed document the Evidence button appeared,
    the pane switched, the viewer jumped to the right page, and no highlight was
    drawn. Carrying the dimensions on the line is what makes it structural rather
    than something each call site has to remember.
    """

    text: str
    size: float
    bold: bool
    bbox: tuple[float, float, float, float]
    page: int
    order: int
    page_width: float | None = None
    page_height: float | None = None


class PyMuPdfParser(IDocumentParser):
    @property
    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities(
            name="pymupdf",
            version=PARSER_ADAPTER_VERSIONS.get("pymupdf", PARSER_FRAMEWORK_VERSION),
            supported_types=frozenset({FileType.PDF}),
            supports_coordinates=True,
            supports_tables=True,
            supports_sections=True,
            supports_lists=True,
            supports_images=True,
            supports_ocr=True,
            supports_signatures=True,
            is_remote=False,
        )

    async def health(self) -> bool:
        try:
            import fitz  # noqa: F401

            return True
        except ImportError:
            return False

    async def page_count(self, request: ParseRequest) -> int | None:
        def _count() -> int | None:
            try:
                import fitz

                with fitz.open(stream=request.content, filetype="pdf") as document:
                    return int(document.page_count)
            except Exception:  # noqa: BLE001
                return None

        return await asyncio.to_thread(_count)

    async def parse(self, request: ParseRequest) -> NormalizedDocument:
        """Parse a PDF. CPU-bound work runs in a thread to keep the loop free."""
        try:
            document = await asyncio.to_thread(self._parse_sync, request)
        except CorruptedDocumentError:
            raise
        except ImportError as exc:
            raise ParserError(
                "PyMuPDF is not installed: pip install '.[parsers]'", stage="parser"
            ) from exc
        except Exception as exc:
            raise ParserError(f"PyMuPDF failed to parse the document: {exc}") from exc

        return document

    # =========================================================================
    # Synchronous parse
    # =========================================================================
    def _parse_sync(self, request: ParseRequest) -> NormalizedDocument:
        import fitz

        settings = get_settings()

        try:
            pdf = fitz.open(stream=request.content, filetype="pdf")
        except Exception as exc:
            raise CorruptedDocumentError(
                "The PDF could not be opened - it may be corrupted or encrypted.",
                stage="parser",
            ) from exc

        with pdf:
            if pdf.needs_pass:
                raise CorruptedDocumentError(
                    "The PDF is password protected and cannot be parsed.", stage="parser"
                )
            if pdf.page_count == 0:
                raise CorruptedDocumentError("The PDF contains no pages.", stage="parser")

            pages: list[Page] = []
            lines: list[_Line] = []
            tables: list[Table] = []
            images: list[Image] = []
            headers: list[HeaderFooter] = []
            footers: list[HeaderFooter] = []
            corrupted: list[int] = []
            ocr_pages: list[int] = []
            ocr_confidences: list[float] = []
            global_order = 0

            for page_index in range(pdf.page_count):
                page_number = page_index + 1
                try:
                    page = pdf.load_page(page_index)
                except Exception:  # noqa: BLE001
                    corrupted.append(page_number)
                    continue

                rect = page.rect
                page_width, page_height = float(rect.width), float(rect.height)

                page_lines, blocks, page_chars, page_start_order = self._extract_page_lines(
                    page,
                    page_number,
                    page_width,
                    page_height,
                    global_order,
                    headers,
                    footers,
                )

                # A page with almost no text layer is scanned; recover it with OCR
                # rather than silently contributing nothing to extraction.
                is_scanned = page_chars < settings.parser.ocr_scanned_page_char_threshold
                if is_scanned and request.allow_ocr and settings.parser.ocr_enabled:
                    ocr_lines, confidence = self._ocr_page(
                        page, page_number, page_width, page_height, len(page_lines)
                    )
                    if ocr_lines:
                        page_lines.extend(ocr_lines)
                        blocks.extend(
                            ContentBlock(
                                block_id=f"p{page_number}-ocr-{index}",
                                block_type=ContentBlockType.PARAGRAPH,
                                order=len(blocks) + index,
                                text=line.text,
                                coordinates=self._coords(
                                    line.bbox, page_number, page_width, page_height
                                ),
                                from_ocr=True,
                            )
                            for index, line in enumerate(ocr_lines)
                        )
                        ocr_pages.append(page_number)
                        if confidence is not None:
                            ocr_confidences.append(confidence)

                lines.extend(page_lines)
                global_order = page_start_order + len(blocks)

                tables.extend(self._extract_tables(page, page_number, page_width, page_height))
                images.extend(self._extract_images(page, page_number, page_width, page_height))

                pages.append(
                    Page(
                        page_number=page_number,
                        width=page_width,
                        height=page_height,
                        rotation=int(page.rotation or 0),
                        reading_order=page_start_order,
                        content_blocks=blocks,
                        text_char_count=page_chars,
                        is_scanned=is_scanned,
                    )
                )

            # The PDF's own metadata dictionary. Values are coerced to strings and
            # empty ones dropped: PyMuPDF returns None for absent keys, and a map of
            # nulls is noise in an artifact that gets stored and diffed.
            source_metadata = {
                key: str(value).strip()
                for key, value in (pdf.metadata or {}).items()
                if value not in (None, "")
            }

        # --- structure ---------------------------------------------------------
        body_size = self._body_font_size(lines)
        sections, paragraphs, doc_lists = self._build_structure(lines, body_size)
        signatures = self._detect_signatures(lines, pages)

        quality = QualityMetrics(
            ocr_confidence=round(statistics.fmean(ocr_confidences), 4) if ocr_confidences else None,
            corrupted_pages=corrupted,
            scanned_pages=ocr_pages,
            warnings=([f"{len(corrupted)} page(s) could not be read."] if corrupted else []),
        )

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
                language=request.language_hint,
                parser_name="pymupdf",
                parser_version=PARSER_FRAMEWORK_VERSION,
                adapter_version=self.capabilities.version,
                created_at=datetime.now(UTC).isoformat(),
                ocr_applied=bool(ocr_pages),
                ocr_engine=get_settings().parser.ocr_engine if ocr_pages else None,
                source_metadata=source_metadata,
            ),
            pages=pages,
            sections=sections,
            paragraphs=paragraphs,
            tables=tables,
            lists=doc_lists,
            images=images,
            signatures=signatures,
            headers=headers,
            footers=footers,
            quality=quality,
        )

    # =========================================================================
    # Page extraction
    # =========================================================================
    def _extract_page_lines(
        self,
        page: Any,
        page_number: int,
        page_width: float,
        page_height: float,
        start_order: int,
        headers: list[HeaderFooter],
        footers: list[HeaderFooter],
    ) -> tuple[list[_Line], list[ContentBlock], int, int]:
        """Extract lines and blocks from one page, splitting off page furniture."""
        lines: list[_Line] = []
        blocks: list[ContentBlock] = []
        char_count = 0
        order = start_order

        raw = page.get_text("dict")
        for block in raw.get("blocks", []):
            if block.get("type") != 0:  # 0 = text
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = "".join(span.get("text", "") for span in spans).strip()
                if not text:
                    continue

                bbox = tuple(float(value) for value in line.get("bbox", (0, 0, 0, 0)))
                sizes = [float(span.get("size", 0)) for span in spans if span.get("size")]
                size = max(sizes) if sizes else 0.0
                bold = any("bold" in str(span.get("font", "")).lower() for span in spans)
                char_count += len(text)

                # Header/footer bands: repeating furniture is captured but excluded
                # from the semantic body (§8).
                relative_y = bbox[1] / page_height if page_height else 0
                if relative_y < _HEADER_BAND:
                    headers.append(
                        HeaderFooter(
                            text=text,
                            page_number=page_number,
                            coordinates=self._coords(bbox, page_number, page_width, page_height),
                            kind="header",
                        )
                    )
                    continue
                if relative_y > _FOOTER_BAND:
                    footers.append(
                        HeaderFooter(
                            text=text,
                            page_number=page_number,
                            coordinates=self._coords(bbox, page_number, page_width, page_height),
                            kind="footer",
                        )
                    )
                    continue

                lines.append(
                    _Line(
                        text=text,
                        size=size,
                        bold=bold,
                        bbox=bbox,  # type: ignore[arg-type]
                        page=page_number,
                        order=order,
                        page_width=page_width,
                        page_height=page_height,
                    )
                )
                blocks.append(
                    ContentBlock(
                        block_id=f"p{page_number}-b{len(blocks)}",
                        block_type=ContentBlockType.PARAGRAPH,
                        order=len(blocks),
                        text=text,
                        coordinates=self._coords(bbox, page_number, page_width, page_height),
                    )
                )
                order += 1

        return lines, blocks, char_count, start_order

    def _extract_tables(
        self, page: Any, page_number: int, page_width: float, page_height: float
    ) -> list[Table]:
        """Extract tables using PyMuPDF's table finder.

        Cell coordinates are preserved so a value inside a payment schedule can be
        highlighted, not just the table as a whole.
        """
        tables: list[Table] = []
        try:
            found = page.find_tables()
        except Exception as exc:  # noqa: BLE001 - table detection is best-effort
            logger.debug("table_detection_failed", page=page_number, error=str(exc))
            return tables

        for index, table in enumerate(getattr(found, "tables", []) or []):
            try:
                grid = table.extract()
            except Exception as exc:  # noqa: BLE001 - one bad table must not lose the page
                logger.debug(
                    "table_extraction_failed",
                    page=page_number,
                    table_index=index,
                    error=str(exc),
                )
                continue
            if not grid:
                continue

            cells: list[TableCell] = []
            for row_index, row in enumerate(grid):
                for col_index, value in enumerate(row):
                    cells.append(
                        TableCell(
                            row=row_index,
                            col=col_index,
                            text=str(value or "").strip(),
                            is_header=row_index == 0,
                        )
                    )

            tables.append(
                Table(
                    table_id=f"p{page_number}-t{index}",
                    page_number=page_number,
                    rows=len(grid),
                    columns=max((len(row) for row in grid), default=0),
                    cells=cells,
                    coordinates=self._coords(
                        tuple(float(v) for v in table.bbox),  # type: ignore[arg-type]
                        page_number,
                        page_width,
                        page_height,
                    ),
                    reading_order=0,
                )
            )
        return tables

    def _extract_images(
        self, page: Any, page_number: int, page_width: float, page_height: float
    ) -> list[Image]:
        images: list[Image] = []
        try:
            for index, info in enumerate(page.get_image_info() or []):
                bbox = info.get("bbox")
                images.append(
                    Image(
                        image_id=f"p{page_number}-i{index}",
                        page_number=page_number,
                        coordinates=self._coords(
                            tuple(float(v) for v in bbox), page_number, page_width, page_height
                        )
                        if bbox
                        else None,
                        width=float(info.get("width", 0)) or None,
                        height=float(info.get("height", 0)) or None,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("image_extraction_failed", page=page_number, error=str(exc))
        return images

    def _ocr_page(
        self,
        page: Any,
        page_number: int,
        page_width: float,
        page_height: float,
        existing_lines: int,
    ) -> tuple[list[_Line], float | None]:
        """OCR a scanned page.

        Coordinates come back in image pixels and are scaled to page space, so an OCR
        highlight lands in the same coordinate system as a text-layer highlight.
        """
        from app.ai.parsers.ocr import ocr_page_image

        settings = get_settings()
        try:
            matrix_scale = settings.parser.ocr_dpi / 72.0
            import fitz

            pixmap = page.get_pixmap(matrix=fitz.Matrix(matrix_scale, matrix_scale))
            image_bytes = pixmap.tobytes("png")
        except Exception as exc:  # noqa: BLE001
            logger.warning("ocr_render_failed", page=page_number, error=str(exc))
            return [], None

        results, confidence = ocr_page_image(image_bytes)
        if not results:
            return [], confidence

        scale_x = page_width / pixmap.width if pixmap.width else 1.0
        scale_y = page_height / pixmap.height if pixmap.height else 1.0

        lines = [
            _Line(
                text=item.text,
                size=0.0,
                bold=False,
                bbox=(
                    item.left * scale_x,
                    item.top * scale_y,
                    (item.left + item.width) * scale_x,
                    (item.top + item.height) * scale_y,
                ),
                page=page_number,
                order=existing_lines + index,
                page_width=page_width,
                page_height=page_height,
            )
            for index, item in enumerate(results)
        ]
        return lines, confidence

    # =========================================================================
    # Structure inference
    # =========================================================================
    @staticmethod
    def _body_font_size(lines: list[_Line]) -> float:
        """Median font size, used as the body-text baseline for heading detection."""
        sizes = [line.size for line in lines if line.size > 0]
        return statistics.median(sizes) if sizes else 10.0

    def _build_structure(
        self, lines: list[_Line], body_size: float
    ) -> tuple[list[Section], list[Paragraph], list[DocumentList]]:
        """Group lines into sections, paragraphs and lists.

        Heading detection combines three weak signals - font size, boldness, and a
        leading clause number - because no single one is reliable across the variety
        of contract layouts in a real repository.
        """
        import re

        heading_pattern = re.compile(r"^((?:\d+|[IVXLC]+|[A-Z])(?:\.\d+)*\.?)\s+(\S.*)$")

        sections: list[Section] = []
        paragraphs: list[Paragraph] = []
        doc_lists: list[DocumentList] = []

        current_section: Section | None = None
        buffer: list[_Line] = []
        pending_list: list[_Line] = []

        def flush_paragraph() -> None:
            nonlocal buffer
            if not buffer:
                return
            text = " ".join(line.text for line in buffer).strip()
            if text:
                boxes = [self._coords_from_line(line) for line in buffer]
                merged = Coordinates.merge([box for box in boxes if box is not None])
                paragraphs.append(
                    Paragraph(
                        paragraph_id=f"para-{len(paragraphs)}",
                        text=text,
                        page_number=buffer[0].page,
                        section_id=current_section.section_id if current_section else None,
                        coordinates=merged,
                        reading_order=buffer[0].order,
                        # A paragraph starting on a later page than it ends is a
                        # continuation; chunking uses this to keep a clause whole.
                        is_continuation=buffer[0].page != buffer[-1].page,
                    )
                )
            buffer = []

        def flush_list() -> None:
            nonlocal pending_list
            if len(pending_list) < 2:
                # A single bullet is a paragraph, not a list.
                buffer.extend(pending_list)
                pending_list = []
                return
            boxes = [self._coords_from_line(line) for line in pending_list]
            merged = Coordinates.merge([box for box in boxes if box is not None])
            doc_lists.append(
                DocumentList(
                    list_id=f"list-{len(doc_lists)}",
                    page_number=pending_list[0].page,
                    ordered=not pending_list[0].text.lstrip().startswith(_LIST_MARKERS),
                    items=[
                        ListItem(
                            text=line.text.lstrip("".join(_LIST_MARKERS) + " ").strip(),
                            coordinates=self._coords_from_line(line),
                        )
                        for line in pending_list
                    ],
                    section_id=current_section.section_id if current_section else None,
                    coordinates=merged,
                    reading_order=pending_list[0].order,
                )
            )
            pending_list = []

        # Enumerated list markers. Deliberately excludes a bare "1." - that form is
        # ambiguous with a numbered section heading ("1. Term"), so the heading test
        # is evaluated first and only unclaimed lines reach the list test.
        enumerated = re.compile(
            r"^\(\s*[a-z0-9ivx]{1,4}\s*\)\s|^[a-z]{1,2}[\).]\s|^[ivxlcdm]{1,5}[\).]\s"
        )

        for line in lines:
            stripped = line.text.strip()
            match = heading_pattern.match(stripped)

            # Heading first: a larger font, bold-and-short, or a section number
            # followed by a short title. Any of these outranks list detection.
            looks_like_heading = (
                (line.size >= body_size * _HEADING_SIZE_RATIO)
                or (line.bold and len(stripped) < 120)
                or (match is not None and len(stripped) < 120)
            )

            is_list_item = not looks_like_heading and (
                stripped.startswith(_LIST_MARKERS) or bool(enumerated.match(stripped))
            )

            if looks_like_heading:
                flush_list()
                flush_paragraph()
                number = match.group(1) if match else None
                title = match.group(2) if match else stripped
                level = (number.count(".") + 1) if number else 1
                current_section = Section(
                    section_id=f"sec-{len(sections)}",
                    title=title[:500],
                    level=level,
                    parent_section=self._parent_for(sections, level),
                    start_page=line.page,
                    end_page=line.page,
                    number=number,
                    order=len(sections),
                    coordinates=self._coords_from_line(line),
                )
                sections.append(current_section)
                continue

            if is_list_item:
                flush_paragraph()
                pending_list.append(line)
                continue

            flush_list()
            buffer.append(line)

            if current_section is not None and line.page > current_section.end_page:
                # Sections are frozen models, so extend by replacement.
                sections[-1] = current_section.model_copy(update={"end_page": line.page})
                current_section = sections[-1]

        flush_list()
        flush_paragraph()

        return sections, paragraphs, doc_lists

    @staticmethod
    def _parent_for(sections: list[Section], level: int) -> str | None:
        """Nearest preceding section at a shallower level."""
        for section in reversed(sections):
            if section.level < level:
                return section.section_id
        return None

    def _coords_from_line(self, line: _Line) -> Coordinates | None:
        """The line's box, *with* its page dimensions.

        The dimensions are what makes the box drawable: `PdfViewer` scales a
        highlight by ``x / page_width``, and returns null rather than guessing when
        that is missing. This method used to omit them - and because
        ``Coordinates.to_dict`` serialises with ``exclude_none=True``, they were not
        even present as nulls to notice. The box validated as a `BoundingBox`
        (both fields optional there), travelled through chunking into every clause,
        risk and date, reached the browser, and drew nothing.

        Its sibling `_coords` always passed them; this path is every paragraph,
        list item, heading and signature, so it is the one that mattered.
        """
        return Coordinates(
            page_number=line.page,
            x=line.bbox[0],
            y=line.bbox[1],
            width=max(line.bbox[2] - line.bbox[0], 0),
            height=max(line.bbox[3] - line.bbox[1], 0),
            page_width=line.page_width,
            page_height=line.page_height,
        )

    @staticmethod
    def _coords(
        # A sequence, not a fixed 4-tuple: PyMuPDF hands back `Rect`/`tuple[float, ...]`
        # and pinning the width here only forces a cast at every call site.
        bbox: Sequence[float],
        page_number: int,
        page_width: float,
        page_height: float,
    ) -> Coordinates:
        return Coordinates(
            page_number=page_number,
            x=bbox[0],
            y=bbox[1],
            width=max(bbox[2] - bbox[0], 0),
            height=max(bbox[3] - bbox[1], 0),
            page_width=page_width,
            page_height=page_height,
        )

    def _detect_signatures(self, lines: list[_Line], pages: list[Page]) -> list[Signature]:
        """Find signature blocks.

        Signatory names and execution dates are extraction targets, and the region is
        excluded from semantic chunks, so it is worth identifying structurally.
        """
        import re

        markers = (
            "in witness whereof",
            "signature",
            "signed by",
            "authorised signatory",
            "authorized signatory",
            "/s/",
            "by:",
        )
        name_pattern = re.compile(r"(?:name|printed name)\s*[:\-]\s*(.+)", re.IGNORECASE)
        title_pattern = re.compile(r"(?:title|designation)\s*[:\-]\s*(.+)", re.IGNORECASE)
        date_pattern = re.compile(r"(?:date|dated)\s*[:\-]\s*(.+)", re.IGNORECASE)

        signatures: list[Signature] = []
        last_pages = {page.page_number for page in pages[-3:]} if pages else set()

        for index, line in enumerate(lines):
            lowered = line.text.lower()
            if not any(marker in lowered for marker in markers):
                continue
            # Signature blocks live at the end of a contract; a mid-document mention
            # of "signature" is usually prose about signing, not a block.
            if line.page not in last_pages and "in witness whereof" not in lowered:
                continue

            window = lines[index : index + 8]
            joined = "\n".join(item.text for item in window)
            name = name_pattern.search(joined)
            title = title_pattern.search(joined)
            date = date_pattern.search(joined)

            signatures.append(
                Signature(
                    signature_id=f"sig-{len(signatures)}",
                    page_number=line.page,
                    coordinates=self._coords_from_line(line),
                    signatory_name=name.group(1).strip()[:255] if name else None,
                    signatory_title=title.group(1).strip()[:255] if title else None,
                    date=date.group(1).strip()[:64] if date else None,
                    raw_text=joined[:2000],
                )
            )

        return signatures


__all__ = ["PyMuPdfParser"]

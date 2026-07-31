"""Canonical Document Model (§8) - the single internal document representation.

Every parser emits a :class:`NormalizedDocument`; the CDM builder turns that into a
:class:`CanonicalDocument`. Downstream code consumes **only** the CDM, which is what
makes the platform parser-agnostic: switching Docling for Azure Document
Intelligence changes an adapter, not chunking, extraction, embedding or search.

Frozen rules encoded here:

* Coordinates are mandatory on every positioned element. They are what powers PDF
  highlighting, evidence display and clause navigation, so a parser that cannot
  supply them is degrading the product, not just omitting a field.
* Reading order is global and explicit, so chunking is deterministic.
* Headers and footers are captured but held separately - they repeat on every page
  and would pollute semantic chunks.
* The CDM is immutable once built.
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.core.enums import ContentBlockType
from app.core.versions import CDM_VERSION


class CdmBase(BaseModel):
    """Base for CDM nodes. Frozen: the CDM is immutable after generation."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=False,
        ser_json_bytes="base64",
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_computed_fields(cls, data: Any) -> Any:
        """Allow a dumped node to be loaded back.

        ``model_dump()`` emits computed fields (``Page.block_count``,
        ``QualityMetrics.is_degraded``), and ``extra="forbid"`` then rejected them
        on the way back in - so every CDM artifact written to object storage was
        unreadable, and the enrichment stage failed on the artifact the parser
        stage had just written. Dropping exactly the computed names keeps
        ``forbid`` meaningful for genuinely unknown keys, which is what it is for.
        """
        if isinstance(data, dict):
            computed = cls.model_computed_fields
            if computed and any(name in data for name in computed):
                return {key: value for key, value in data.items() if key not in computed}
        return data


class Coordinates(CdmBase):
    """A rectangle on a page, in the page's own coordinate space.

    Stored in raw page units rather than normalised fractions so the viewer can
    scale to any zoom without accumulating rounding drift, and so a coordinate can
    be checked against the page dimensions it came from.
    """

    page_number: int = Field(ge=1)
    x: float
    y: float
    width: float = Field(ge=0)
    height: float = Field(ge=0)
    #: Page size the coordinates were measured against - required for correct scaling.
    page_width: float | None = None
    page_height: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)

    @classmethod
    def merge(cls, boxes: list[Coordinates]) -> Coordinates | None:
        """Bounding box enclosing several boxes on the same page.

        Used when a clause spans multiple lines: the viewer draws one highlight per
        page rather than one per text run.
        """
        if not boxes:
            return None
        page = boxes[0].page_number
        same_page = [box for box in boxes if box.page_number == page]
        left = min(box.x for box in same_page)
        top = min(box.y for box in same_page)
        right = max(box.x + box.width for box in same_page)
        bottom = max(box.y + box.height for box in same_page)
        return cls(
            page_number=page,
            x=left,
            y=top,
            width=right - left,
            height=bottom - top,
            page_width=same_page[0].page_width,
            page_height=same_page[0].page_height,
        )

    @classmethod
    def group_by_page(cls, boxes: list[Coordinates]) -> list[Coordinates]:
        """One merged box per page - what a cross-page clause highlight needs."""
        by_page: dict[int, list[Coordinates]] = {}
        for box in boxes:
            by_page.setdefault(box.page_number, []).append(box)
        merged = [cls.merge(page_boxes) for page_boxes in by_page.values()]
        return [box for box in merged if box is not None]


class DocumentMetadata(CdmBase):
    """Identity and provenance of the parsed document."""

    document_id: str
    organization_id: str
    project_id: str
    file_name: str
    storage_path: str
    file_type: str
    file_size: int
    hash: str
    language: str | None = None
    #: Which parser produced the normalized document, and at what version. Stamped
    #: on every artifact so an extraction is reproducible (§25).
    parser_name: str
    parser_version: str
    adapter_version: str
    cdm_version: str = CDM_VERSION
    created_at: str
    #: True when OCR contributed text, so downstream confidence can be tempered.
    ocr_applied: bool = False
    ocr_engine: str | None = None
    #: The document's *own* embedded metadata, as the file declares it - a PDF's
    #: ``/Title``, ``/Author``, ``/Producer``, ``/CreationDate``. Kept because it is
    #: independent evidence: ``/Title`` is frequently the agreement's real title when
    #: the first page is a cover sheet, and ``/Producer`` distinguishes a digital
    #: export from a scan, which is a parse-quality signal. Stored as a free-form map
    #: because every format names these differently and none of them are trustworthy
    #: enough to promote to typed columns.
    source_metadata: dict[str, str] = Field(default_factory=dict)


class DocumentStatistics(CdmBase):
    total_pages: int = 0
    paragraph_count: int = 0
    table_count: int = 0
    image_count: int = 0
    list_count: int = 0
    section_count: int = 0
    word_count: int = 0
    char_count: int = 0
    signature_count: int = 0


class ContentBlock(CdmBase):
    """One positioned element on a page, in reading order."""

    block_id: str
    block_type: ContentBlockType
    #: Index within the page. The global order is on :class:`CanonicalDocument`.
    order: int
    text: str = ""
    coordinates: Coordinates | None = None
    #: Heading depth for ``heading`` blocks.
    level: int | None = None
    section_id: str | None = None
    confidence: float | None = None
    #: Set when this block's text came from OCR rather than the text layer.
    from_ocr: bool = False


class Page(CdmBase):
    page_number: int = Field(ge=1)
    width: float
    height: float
    rotation: int = 0
    #: Global reading-order index of this page's first block.
    reading_order: int = 0
    content_blocks: list[ContentBlock] = Field(default_factory=list)
    #: Extractable characters found on the page. Below the configured threshold the
    #: page is treated as scanned and routed through OCR.
    text_char_count: int = 0
    is_scanned: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def block_count(self) -> int:
        return len(self.content_blocks)


class Section(CdmBase):
    """A logical section of the document - the spine chunking follows."""

    section_id: str
    title: str
    level: int = 1
    parent_section: str | None = None
    start_page: int = 1
    end_page: int = 1
    #: Numbering as printed ("11.2"), preserved for citation.
    number: str | None = None
    order: int = 0
    coordinates: Coordinates | None = None


class Paragraph(CdmBase):
    paragraph_id: str
    text: str
    page_number: int
    section_id: str | None = None
    coordinates: Coordinates | None = None
    reading_order: int = 0
    #: True when this paragraph continues one that began on the previous page.
    is_continuation: bool = False
    style: str | None = None


class TableCell(CdmBase):
    row: int
    col: int
    text: str = ""
    row_span: int = 1
    col_span: int = 1
    is_header: bool = False
    coordinates: Coordinates | None = None


class Table(CdmBase):
    """A table, kept structurally intact.

    Chunking never splits a table row: a payment schedule split mid-row produces
    text that reads as a different obligation than the document states.
    """

    table_id: str
    page_number: int
    rows: int = 0
    columns: int = 0
    cells: list[TableCell] = Field(default_factory=list)
    caption: str | None = None
    section_id: str | None = None
    coordinates: Coordinates | None = None
    reading_order: int = 0
    #: Set when the table continues across a page break.
    continues_from: str | None = None

    def to_markdown(self) -> str:
        """Render as markdown - the form the LLM and embeddings consume.

        Markdown preserves the row/column relationships in plain text, so a model
        reading a chunk still sees which value belongs to which column.
        """
        if not self.cells:
            return ""
        grid: dict[int, dict[int, str]] = {}
        for cell in self.cells:
            grid.setdefault(cell.row, {})[cell.col] = cell.text.replace("|", "\\|").strip()

        ordered_rows = sorted(grid)
        if not ordered_rows:
            return ""
        width = max((max(row.keys()) + 1) for row in grid.values())

        lines: list[str] = []
        for index, row_index in enumerate(ordered_rows):
            row = grid[row_index]
            cells = [row.get(col, "") for col in range(width)]
            lines.append("| " + " | ".join(cells) + " |")
            if index == 0:
                lines.append("|" + "|".join([" --- "] * width) + "|")
        return "\n".join(lines)


class ListItem(CdmBase):
    text: str
    level: int = 1
    marker: str | None = None
    coordinates: Coordinates | None = None


class DocumentList(CdmBase):
    """An ordered or bulleted list, preserved whole.

    Lists in contracts are usually enumerated obligations or carve-outs; splitting
    one changes its meaning.
    """

    list_id: str
    page_number: int
    ordered: bool = False
    items: list[ListItem] = Field(default_factory=list)
    section_id: str | None = None
    coordinates: Coordinates | None = None
    reading_order: int = 0

    def to_text(self) -> str:
        lines = []
        for index, item in enumerate(self.items, start=1):
            marker = item.marker or (f"{index}." if self.ordered else "-")
            lines.append(f"{'  ' * (item.level - 1)}{marker} {item.text}")
        return "\n".join(lines)


class Image(CdmBase):
    image_id: str
    page_number: int
    coordinates: Coordinates | None = None
    caption: str | None = None
    #: Text recovered from the image by OCR, when applied.
    ocr_text: str | None = None
    width: float | None = None
    height: float | None = None


class Signature(CdmBase):
    """A detected signature block.

    Worth modelling separately: signatory names and execution dates are extraction
    targets, and the region is excluded from semantic chunks.
    """

    signature_id: str
    page_number: int
    coordinates: Coordinates | None = None
    signatory_name: str | None = None
    signatory_title: str | None = None
    party: str | None = None
    date: str | None = None
    raw_text: str | None = None


class HeaderFooter(CdmBase):
    """Repeating page furniture. Captured, but never chunked.

    A running header on 150 pages would otherwise contribute 150 near-identical
    chunks and dilute every retrieval.
    """

    text: str
    page_number: int
    coordinates: Coordinates | None = None
    kind: str = "header"


class Footnote(CdmBase):
    footnote_id: str
    page_number: int
    text: str
    marker: str | None = None
    coordinates: Coordinates | None = None


class CrossReference(CdmBase):
    """An in-document reference, e.g. "subject to Section 11.2"."""

    reference_id: str
    source_page: int
    text: str
    target_label: str | None = None
    target_section_id: str | None = None
    coordinates: Coordinates | None = None


class ReadingOrderEntry(CdmBase):
    """One entry in the global reading order.

    The deterministic sequence chunking walks. Because it is explicit, re-chunking
    the same document always produces the same chunk boundaries.
    """

    index: int
    block_id: str
    block_type: ContentBlockType
    page_number: int
    section_id: str | None = None
    #: Id of the paragraph/table/list this entry points at.
    element_id: str | None = None


class QualityMetrics(CdmBase):
    """Parse quality. Surfaced rather than hidden.

    A document parsed with 60% OCR confidence produces less reliable extractions,
    and the platform's explainability promise means saying so.
    """

    ocr_confidence: float | None = None
    missing_text: bool = False
    empty_pages: list[int] = Field(default_factory=list)
    corrupted_pages: list[int] = Field(default_factory=list)
    scanned_pages: list[int] = Field(default_factory=list)
    #: Fraction of positioned elements that carry coordinates. Below 1.0, some
    #: evidence cannot be highlighted.
    coordinate_coverage: float = 1.0
    warnings: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_degraded(self) -> bool:
        """True when downstream confidence should be tempered."""
        return bool(
            self.missing_text
            or self.corrupted_pages
            or self.coordinate_coverage < 0.9
            or (self.ocr_confidence is not None and self.ocr_confidence < 0.7)
        )


class NormalizedDocument(CdmBase):
    """A parser adapter's output.

    The only thing an adapter may return. No parser-specific JSON travels past this
    boundary (§9), which is the whole point of the abstraction.
    """

    metadata: DocumentMetadata
    pages: list[Page] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    paragraphs: list[Paragraph] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    lists: list[DocumentList] = Field(default_factory=list)
    images: list[Image] = Field(default_factory=list)
    signatures: list[Signature] = Field(default_factory=list)
    headers: list[HeaderFooter] = Field(default_factory=list)
    footers: list[HeaderFooter] = Field(default_factory=list)
    footnotes: list[Footnote] = Field(default_factory=list)
    quality: QualityMetrics = Field(default_factory=QualityMetrics)


class CanonicalDocument(CdmBase):
    """The immutable canonical representation. The only input downstream stages read."""

    metadata: DocumentMetadata
    statistics: DocumentStatistics
    pages: list[Page] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    paragraphs: list[Paragraph] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    lists: list[DocumentList] = Field(default_factory=list)
    images: list[Image] = Field(default_factory=list)
    signatures: list[Signature] = Field(default_factory=list)
    headers: list[HeaderFooter] = Field(default_factory=list)
    footers: list[HeaderFooter] = Field(default_factory=list)
    footnotes: list[Footnote] = Field(default_factory=list)
    references: list[CrossReference] = Field(default_factory=list)
    #: Global block ordering - chunking's deterministic input.
    reading_order: list[ReadingOrderEntry] = Field(default_factory=list)
    quality_metrics: QualityMetrics = Field(default_factory=QualityMetrics)

    @model_validator(mode="after")
    def _validate_integrity(self) -> Self:
        """Reject a CDM that would break downstream guarantees.

        Cheaper to fail here, once, than to discover mid-extraction that a section
        reference dangles or the reading order has gaps.
        """
        section_ids = {section.section_id for section in self.sections}
        for paragraph in self.paragraphs:
            if paragraph.section_id and paragraph.section_id not in section_ids:
                raise ValueError(
                    f"Paragraph {paragraph.paragraph_id} references unknown section "
                    f"{paragraph.section_id}"
                )
        indexes = [entry.index for entry in self.reading_order]
        if indexes and sorted(indexes) != list(range(len(indexes))):
            raise ValueError("reading_order indexes must be a contiguous 0-based sequence")
        return self

    # ------------------------------------------------------------------ access
    def section(self, section_id: str) -> Section | None:
        return next((s for s in self.sections if s.section_id == section_id), None)

    def paragraphs_in(self, section_id: str) -> list[Paragraph]:
        return [p for p in self.paragraphs if p.section_id == section_id]

    def tables_in(self, section_id: str) -> list[Table]:
        return [t for t in self.tables if t.section_id == section_id]

    def lists_in(self, section_id: str) -> list[DocumentList]:
        return [item for item in self.lists if item.section_id == section_id]

    def page(self, page_number: int) -> Page | None:
        return next((p for p in self.pages if p.page_number == page_number), None)

    def child_sections(self, section_id: str | None) -> list[Section]:
        return sorted(
            (s for s in self.sections if s.parent_section == section_id),
            key=lambda s: s.order,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def full_text(self) -> str:
        """Document text in reading order, excluding page furniture.

        Headers, footers and page numbers are omitted deliberately - see
        :class:`HeaderFooter`.
        """
        parts: list[str] = []
        by_id: dict[str, Any] = {}
        for paragraph in self.paragraphs:
            by_id[paragraph.paragraph_id] = paragraph
        for table in self.tables:
            by_id[table.table_id] = table
        for item in self.lists:
            by_id[item.list_id] = item

        for entry in sorted(self.reading_order, key=lambda e: e.index):
            if entry.block_type in {
                ContentBlockType.HEADER,
                ContentBlockType.FOOTER,
                ContentBlockType.PAGE_NUMBER,
            }:
                continue
            element = by_id.get(entry.element_id or "")
            if element is None:
                continue
            if isinstance(element, Paragraph):
                parts.append(element.text)
            elif isinstance(element, Table):
                parts.append(element.to_markdown())
            elif isinstance(element, DocumentList):
                parts.append(element.to_text())
        return "\n\n".join(part for part in parts if part.strip())

    def text_for_pages(self, start: int, end: int) -> str:
        return "\n\n".join(
            paragraph.text
            for paragraph in self.paragraphs
            if start <= paragraph.page_number <= end and paragraph.text.strip()
        )


__all__ = [
    "CanonicalDocument",
    "ContentBlock",
    "Coordinates",
    "CrossReference",
    "DocumentList",
    "DocumentMetadata",
    "DocumentStatistics",
    "Footnote",
    "HeaderFooter",
    "Image",
    "ListItem",
    "NormalizedDocument",
    "Page",
    "Paragraph",
    "QualityMetrics",
    "ReadingOrderEntry",
    "Section",
    "Signature",
    "Table",
    "TableCell",
]

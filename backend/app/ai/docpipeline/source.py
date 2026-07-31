"""Reading the iDoc page JSON off disk, paragraph by paragraph, page by page.

The upstream service returns one JSON file per page, each an Azure
``prebuilt-layout`` analyse result for that page alone. This module turns a
directory of them into an ordered list of pages, and nothing else - no network,
no database, no settings. That is what makes it testable with a `tmp_path` and
three small dicts.

Three properties of the format that are easy to get wrong, and are handled here:

* **Paragraphs are flat, not nested under pages.** ``payload["paragraphs"]`` is a
  top-level array already in reading order. ``payload["pages"][0]`` holds
  geometry and the page number, not the text.
* **Files must be sorted numerically.** Lexical order puts ``page_10.json``
  between ``page_1`` and ``page_2``, which silently reorders a document into
  nonsense that still looks plausible.
* **Polygons are in inches, as four corners.** Eight floats, clockwise from the
  top-left. They are carried through unconverted; the consumer decides on units.

Table cells are emitted by Azure as paragraphs too, so table text appears here.
That is intentional for this pipeline: a clause set out in a table is still a
clause.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.ai.parsers.idoc_adapter import _SIGNATURE_HINTS
from app.core.logging import get_logger

logger = get_logger(__name__)

#: ``page_9.json``, ``page-9.json``, ``page9.json`` - captures the number so the
#: sort is numeric. Same shape as the parser adapter's, for the same reason.
_PAGE_FILE = re.compile(r"page[_-]?(\d+)\.json$", re.IGNORECASE)

#: Roles that mark running furniture rather than body text. Kept as content -
#: dropping them here would lose a clause set in a header - but exposed so
#: downstream passes can weigh them differently.
FURNITURE_ROLES = frozenset({"pageHeader", "pageFooter", "pageNumber"})

#: The role Azure gives a heading. The clause detector's first pass keys on this.
HEADING_ROLE = "sectionHeading"

#: Openings of an execution block. Imported from the parser adapter rather than
#: restated, so a phrase added there takes effect here too.
#:
#: A contract's closing pages carry no heading between the last clause and the
#: signatures, so "run to the next heading" runs straight through them. That put
#: `IN WITNESS WHEREOF ... Digitally signed by ... Date: 2025.12.26` inside a
#: stored clause and then embedded it.
SIGNATURE_HINTS = _SIGNATURE_HINTS


@dataclass(frozen=True, slots=True)
class Paragraph:
    """One paragraph of one page, with the geometry needed to point back at it."""

    page_number: int
    index: int
    role: str | None
    content: str
    polygon: tuple[float, ...]

    @property
    def ref(self) -> str:
        """Stable label used in prompts and logs, e.g. ``9.3``."""
        return f"{self.page_number}.{self.index}"

    @property
    def is_heading(self) -> bool:
        return self.role == HEADING_ROLE

    @property
    def is_furniture(self) -> bool:
        return self.role in FURNITURE_ROLES

    @property
    def is_signature_block(self) -> bool:
        """Does this paragraph open the execution block?

        Matched on a prefix rather than anywhere in the text: a clause may well
        discuss who has authority to sign, and that is clause text. It is the
        paragraph that *begins* with the formula which ends the operative part
        of the document.
        """
        head = self.content.strip().lower()[:80]
        return any(head.startswith(hint) or f" {hint}" in head for hint in SIGNATURE_HINTS)

    @property
    def ends_clause_extent(self) -> bool:
        """Should a clause extent stop *before* this paragraph?"""
        return self.is_heading or self.is_furniture or self.is_signature_block


@dataclass(frozen=True, slots=True)
class PageContent:
    """One page's paragraphs, in reading order."""

    page_number: int
    paragraphs: tuple[Paragraph, ...]
    source_file: Path

    @property
    def text(self) -> str:
        return "\n".join(paragraph.content for paragraph in self.paragraphs)

    @property
    def char_count(self) -> int:
        return sum(len(paragraph.content) for paragraph in self.paragraphs)


def load_pages(directory: Path) -> list[PageContent]:
    """Load every ``page_*.json`` in ``directory``, ordered by page number.

    Raises:
        FileNotFoundError: the directory is missing, or holds no page files.
    """
    if not directory.is_dir():
        raise FileNotFoundError(f"Not a directory: {directory}")

    numbered: list[tuple[int, Path]] = []
    for path in directory.iterdir():
        match = _PAGE_FILE.search(path.name)
        if match is not None:
            numbered.append((int(match.group(1)), path))

    if not numbered:
        raise FileNotFoundError(
            f"No page_*.json files in {directory}. Expected the per-page output of "
            "the PDF-to-JSON service, one file per page."
        )

    numbered.sort(key=lambda entry: entry[0])

    pages = [_read_page(path, fallback_number=number) for number, path in numbered]
    logger.info(
        "docpipeline_pages_loaded",
        directory=str(directory),
        pages=len(pages),
        paragraphs=sum(len(page.paragraphs) for page in pages),
        headings=sum(1 for page in pages for para in page.paragraphs if para.is_heading),
    )
    return pages


def iter_paragraphs(pages: list[PageContent]) -> list[Paragraph]:
    """Every paragraph across every page, flattened, still in document order."""
    return [paragraph for page in pages for paragraph in page.paragraphs]


def _read_page(path: Path, *, fallback_number: int) -> PageContent:
    payload = json.loads(path.read_text(encoding="utf-8"))
    page_number = _page_number(payload, fallback_number)

    paragraphs: list[Paragraph] = []
    for raw in payload.get("paragraphs") or []:
        content = (raw.get("content") or "").strip()
        if not content:
            continue
        paragraphs.append(
            Paragraph(
                page_number=page_number,
                index=len(paragraphs) + 1,
                role=raw.get("role"),
                content=content,
                polygon=_polygon_of(raw),
            )
        )

    return PageContent(
        page_number=page_number,
        paragraphs=tuple(paragraphs),
        source_file=path,
    )


def _page_number(payload: dict, fallback: int) -> int:
    """The page's own number, falling back to the one in its filename.

    A page file whose ``pages`` array is empty still belongs somewhere in the
    document, and the filename is the only remaining evidence of where.
    """
    pages = payload.get("pages")
    if isinstance(pages, list) and pages:
        number = pages[0].get("pageNumber")
        if isinstance(number, int):
            return number
    return fallback


def _polygon_of(raw: dict) -> tuple[float, ...]:
    """The paragraph's bounding polygon: 8 floats, inches, or empty if absent.

    A paragraph with no geometry is still real text worth classifying, so this
    returns an empty tuple rather than raising.
    """
    regions = raw.get("boundingRegions")
    if isinstance(regions, list) and regions:
        polygon = regions[0].get("polygon")
        if isinstance(polygon, list) and polygon:
            return tuple(float(value) for value in polygon)
    polygon = raw.get("polygon")
    if isinstance(polygon, list) and polygon:
        return tuple(float(value) for value in polygon)
    return ()


def per_page_boxes(paragraphs: Sequence[Paragraph]) -> list[float]:
    """One bounding box per page the paragraphs touch, flattened.

    Eight floats per page, pages in ascending order, so::

        len(polygon) == 8 * len(pageNumber)

    A single box spanning two pages describes a rectangle that exists on
    neither of them - it was stored for ten of eighteen clauses and could not
    have drawn a highlight on either page. Consumers pair the two arrays::

        for page, start in zip(page_numbers, range(0, len(polygon), 8)):
            box = polygon[start : start + 8]
    """
    by_page: dict[int, list[tuple[float, ...]]] = {}
    for paragraph in paragraphs:
        if paragraph.polygon:
            by_page.setdefault(paragraph.page_number, []).append(paragraph.polygon)

    flattened: list[float] = []
    for page_number in sorted(by_page):
        flattened.extend(union_polygon(by_page[page_number]))
    return flattened


def pages_with_geometry(paragraphs: Sequence[Paragraph]) -> list[int]:
    """The pages :func:`per_page_boxes` emitted a box for, in the same order.

    Paired with ``per_page_boxes`` this keeps the two arrays aligned. It is not
    the same as every page the clause touches: a page whose paragraphs all lack
    geometry contributes no box, and silently letting the arrays disagree is
    exactly the bug this pairing exists to prevent.
    """
    return sorted({p.page_number for p in paragraphs if p.polygon})


def union_polygon(polygons: list[tuple[float, ...]]) -> list[float]:
    """One axis-aligned box covering every polygon given, as 8 floats.

    The primitive behind :func:`per_page_boxes`, which calls it once per page.
    Scanned pages are slightly skewed, so the corners come from min/max over
    every vertex rather than assuming the first four numbers are the top edge.
    """
    xs: list[float] = []
    ys: list[float] = []
    for polygon in polygons:
        if len(polygon) < 8:
            continue
        xs.extend(polygon[0::2])
        ys.extend(polygon[1::2])

    if not xs or not ys:
        return []

    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    return [left, top, right, top, right, bottom, left, bottom]

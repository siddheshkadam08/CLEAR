"""iDoc layout-service parser adapter.

Calls the in-house document layout service, which takes a PDF on a multipart
``file`` field and returns a **ZIP of per-page JSON**, one entry per page named
``page_<n>.json``. Each entry is a Document Intelligence ``prebuilt-layout``
analyse result for that page.

Preferred over calling Azure Document Intelligence directly: the service holds the
Azure credential, so this application never does, and swapping the layout backend
becomes an infrastructure change rather than a code change.

Three properties of the response shape drive this file, and getting any of them
wrong corrupts the document silently rather than loudly:

* **Spans are page-local.** Every page's JSON restarts ``span.offset`` at zero, so
  offsets cannot be compared across pages. They are only ever used *within* a page,
  and global ordering comes from (page number, position) instead.
* **Table cells are also listed as paragraphs.** A table's cell text appears both
  in ``tables[].cells[]`` and as standalone entries in ``paragraphs[]``. Emitting
  both would duplicate every figure in a fee schedule - once inside the table and
  again as loose prose - so cell-backed paragraphs are excluded by resolving each
  cell's ``elements`` references.
* **Coordinates are polygons in inches.** Four corners, not a rectangle, and the
  page is measured in inches. They are converted to a point-based bounding box so
  they share a coordinate space with the PyMuPDF adapter and the PDF viewer can
  draw a highlight without knowing which parser ran.

Roles (``title``, ``sectionHeading``, ``pageHeader``, ``pageFooter``, ``pageNumber``,
``footnote``) come from the service, so heading detection and page-furniture removal
are read from the response rather than inferred from font sizes.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

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
from app.ai.parsers.base import (
    IDocumentParser,
    ParserCapabilities,
    ParseRequest,
)
from app.core.config import get_settings
from app.core.enums import ContentBlockType, FileType
from app.core.errors import ParserError
from app.core.logging import get_logger
from app.core.versions import PARSER_FRAMEWORK_VERSION

logger = get_logger(__name__)

ADAPTER_VERSION = "1.0.0"

#: Points per inch. The service reports inches; the CDM and the viewer work in
#: points, matching PyMuPDF, so every parser's coordinates are comparable.
_POINTS_PER_INCH = 72.0

#: ``page_12.json`` -> 12. Sorted numerically, because a lexical sort puts page 10
#: before page 2 and would silently reorder a ten-page contract.
_PAGE_FILE = re.compile(r"page[_-]?(\d+)\.json$", re.IGNORECASE)

#: DI roles that are page furniture, not document content.
_FURNITURE_ROLES = frozenset({"pageHeader", "pageFooter", "pageNumber"})

#: DI roles that are headings.
_HEADING_ROLES = frozenset({"title", "sectionHeading"})

#: ``/paragraphs/3`` -> ("paragraphs", 3). Used to resolve the element references
#: that link sections and table cells to their content.
_ELEMENT_REF = re.compile(r"^/(?P<kind>[a-zA-Z]+)/(?P<index>\d+)$")

#: Leading clause number in a heading: "11.2 Termination" -> ("11.2", "Termination").
_HEADING_NUMBER = re.compile(
    r"^\s*(?P<number>(?:\d+(?:\.\d+)*|[IVXLCDM]+|[A-Z])[.)]?)\s+(?P<title>\S.*)$"
)

#: A list marker at the start of a paragraph.
_LIST_MARKER = re.compile(
    r"^\s*(?:[-•·*•●]|\(\s*[a-z0-9ivx]{1,4}\s*\)|[a-z]{1,2}[.)]|\d{1,2}[.)])\s+",
    re.IGNORECASE,
)

_SIGNATURE_HINTS = (
    "in witness whereof",
    "signed by",
    "signature",
    "for and on behalf of",
    "authorised signatory",
    "authorized signatory",
    "duly authorised",
    "/s/",
)


@dataclass(slots=True)
class _Accumulator:
    """Document-wide element lists, filled page by page.

    The CDM models are frozen, so elements cannot be created and then adjusted -
    everything about an element has to be known when it is constructed. This carries
    the running state (the reading-order counter and the lists built so far) across
    pages without the per-page mapper closing over loop variables.
    """

    order: int = 0
    pages: list[Page] = field(default_factory=list)
    paragraphs: list[Paragraph] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    lists: list[DocumentList] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    signatures: list[Signature] = field(default_factory=list)
    headers: list[HeaderFooter] = field(default_factory=list)
    footers: list[HeaderFooter] = field(default_factory=list)
    empty_pages: list[int] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    api_versions: set[str] = field(default_factory=set)
    model_ids: set[str] = field(default_factory=set)


class IDocParser(IDocumentParser):
    """Remote layout parser backed by the iDoc service."""

    @property
    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities(
            name="idoc",
            version=PARSER_FRAMEWORK_VERSION,
            supported_types=frozenset({FileType.PDF}),
            supports_coordinates=True,
            supports_tables=True,
            supports_sections=True,
            supports_lists=True,
            supports_images=False,
            # The service runs its own OCR on scanned pages, so no local OCR pass is
            # needed - but it does not report whether it applied any.
            supports_ocr=True,
            supports_signatures=True,
            is_remote=True,
        )

    # =========================================================================
    # Parse
    # =========================================================================
    async def parse(self, request: ParseRequest) -> NormalizedDocument:
        if request.file_type is not FileType.PDF:
            raise ParserError(
                f"The iDoc service accepts PDF only; received {request.file_type.value}.",
                retryable=False,
                details={"file_type": request.file_type.value},
            )

        payloads = await self._payloads(request)
        return self._build(request, payloads)

    async def _payloads(self, request: ParseRequest) -> list[dict[str, Any]]:
        """The vendor response, from the service or from a recorded fixture.

        The split lives here rather than behind a separate adapter so that fixture
        mode exercises the *same* mapping code as live mode. A fixture that replayed
        a finished NormalizedDocument would leave `_build` - the part with the
        coordinate maths and the section resolution - untested.
        """
        from app.ai.parsers import fixtures

        settings = get_settings().parser
        store = fixtures.FixtureStore(parser=self.capabilities.name)
        cache = fixtures.ObjectCache(parser=self.capabilities.name)

        # Object storage is consulted first, in *both* modes and before anything
        # else. The layout service is the slowest and only metered step in the
        # pipeline, and re-uploading a document - a replacement version, the same
        # agreement into a second project, a reprocess after a prompt change - is
        # routine rather than exceptional. Keyed by content hash, so this is a hit
        # whenever the bytes have been seen before, whichever worker saw them.
        cached = await cache.load(request.file_hash)
        if cached is not None:
            return cached

        if settings.is_fixture_mode:
            # No cache entry and no live calls permitted: fall back to the on-disk
            # fixtures, which is what makes offline development possible.
            return fixtures.resolve(store, request.file_hash, file_name=request.file_name)

        payloads = await self._analyse(request)

        # Written before the local fixture: the shared copy is the one that saves
        # a future call, and a failure here is logged rather than raised.
        await cache.save(
            request.file_hash,
            payloads,
            file_name=request.file_name,
            source=settings.idoc_endpoint,
        )

        if settings.record_fixtures:
            try:
                store.save(
                    request.file_hash,
                    payloads,
                    file_name=request.file_name,
                    source=settings.idoc_endpoint,
                )
            except OSError as exc:
                # A read-only or full disk must not fail a parse that already
                # succeeded - the document is the deliverable, the fixture is a
                # convenience.
                logger.warning("parser_fixture_record_failed", error=str(exc))

        return payloads

    async def health(self) -> bool:
        """Is the service reachable?

        A HEAD/GET on the upload path is expected to be rejected - the endpoint only
        accepts POST - so anything that answers at all counts as reachable. What is
        being tested is the network path and TLS, not the contract.
        """
        settings = get_settings().parser
        if not settings.idoc_endpoint:
            return False
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0, verify=settings.idoc_verify_tls) as client:
                response = await client.get(settings.idoc_endpoint)
            return response.status_code < 500
        except httpx.HTTPError as exc:
            logger.warning("idoc_health_failed", error=str(exc))
            return False

    async def page_count(self, request: ParseRequest) -> int | None:
        """Unknown without analysing. Returning None avoids a second upload."""
        return None

    # =========================================================================
    # Transport
    # =========================================================================
    async def _analyse(self, request: ParseRequest) -> list[dict[str, Any]]:
        """Upload the PDF and return the per-page payloads, ordered by page number."""
        import httpx

        settings = get_settings().parser
        endpoint = settings.idoc_endpoint
        if not endpoint:
            raise ParserError(
                "IDOC_ENDPOINT is not configured, so the iDoc parser cannot run.",
                retryable=False,
            )

        headers: dict[str, str] = {}
        if settings.idoc_api_key:
            headers[settings.idoc_api_key_header] = settings.idoc_api_key

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(float(settings.idoc_timeout_seconds), connect=15.0),
                verify=settings.idoc_verify_tls,
                headers=headers,
                follow_redirects=True,
            ) as client:
                response = await client.post(
                    endpoint,
                    files={
                        "file": (
                            request.file_name,
                            request.content,
                            "application/pdf",
                        )
                    },
                )
        except httpx.TimeoutException as exc:
            # Retryable: a timeout on a long document says nothing about the document.
            raise ParserError(
                f"The iDoc service did not respond within {settings.idoc_timeout_seconds}s.",
                retryable=True,
                details={"endpoint": endpoint},
            ) from exc
        except httpx.HTTPError as exc:
            raise ParserError(
                f"Could not reach the iDoc service: {exc}",
                retryable=True,
                details={"endpoint": endpoint},
            ) from exc

        if response.status_code >= 400:
            # 4xx is the document's fault and will fail identically on retry; 5xx is
            # the service's and may not.
            raise ParserError(
                f"The iDoc service rejected the document ({response.status_code}).",
                retryable=response.status_code >= 500,
                details={
                    "status": response.status_code,
                    "body": response.text[:500],
                },
            )

        return self._unpack(response.content, response.headers.get("content-type", ""))

    def _unpack(self, body: bytes, content_type: str) -> list[dict[str, Any]]:
        """Extract per-page payloads from the response.

        The service returns a ZIP of ``page_<n>.json``. A single JSON object is also
        accepted, so a future single-document response shape does not break the
        adapter.
        """
        import json

        if not body:
            raise ParserError("The iDoc service returned an empty response.", retryable=True)

        is_zip = body[:2] == b"PK" or "zip" in content_type.lower()
        if not is_zip:
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ParserError(
                    "The iDoc response was neither a ZIP archive nor JSON.",
                    retryable=False,
                    details={"content_type": content_type, "head": body[:200].hex()},
                ) from exc
            return [parsed] if isinstance(parsed, dict) else list(parsed)

        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                entries: list[tuple[int, str]] = []
                for name in archive.namelist():
                    if name.endswith("/"):
                        continue
                    match = _PAGE_FILE.search(name)
                    # Numeric sort: a lexical one puts page_10 before page_2 and would
                    # silently reorder the document.
                    entries.append((int(match.group(1)) if match else 1 << 30, name))
                if not entries:
                    raise ParserError(
                        "The iDoc archive contained no page files.",
                        retryable=False,
                        details={"entries": archive.namelist()[:20]},
                    )

                payloads: list[dict[str, Any]] = []
                for _, name in sorted(entries):
                    raw = archive.read(name)
                    try:
                        parsed = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise ParserError(
                            f"Page file '{name}' in the iDoc archive is not valid JSON.",
                            retryable=False,
                        ) from exc
                    if isinstance(parsed, dict):
                        payloads.append(parsed)
                return payloads
        except zipfile.BadZipFile as exc:
            raise ParserError(
                "The iDoc response was not a readable ZIP archive.",
                retryable=True,
            ) from exc

    # =========================================================================
    # Mapping
    # =========================================================================
    def _build_page(
        self,
        payload: dict[str, Any],
        page_data: dict[str, Any],
        acc: _Accumulator,
        *,
        page_number: int,
        width: float,
        height: float,
        unit: str,
    ) -> tuple[int, list[tuple[Any, str, Coordinates | None, int | None, str | None]]]:
        """Map one page's layout result into CDM elements.

        A method rather than an inline loop body so the two closures below do not
        capture loop variables - a closure over a loop variable reads the *last*
        iteration's value if it ever escapes the iteration, which is a bug waiting
        for someone to defer a call.

        Returns the reading-order counter and the page's content blocks; elements are
        appended to ``acc``.
        """
        order = acc.order
        raw_paragraphs: list[dict[str, Any]] = payload.get("paragraphs") or []
        raw_tables: list[dict[str, Any]] = payload.get("tables") or []

        # Paragraph index -> the table that consumed it. A table's cell text is
        # also listed as a standalone paragraph, so those paragraphs are skipped
        # (otherwise a fee schedule's figures appear twice) and the mapping tells
        # us where in the flow the table itself belongs.
        consumed = self._table_paragraph_indices(raw_tables)

        # Section the document is in at each paragraph index, resolved *before*
        # anything is constructed. The CDM models are frozen, so ``section_id``
        # cannot be patched on afterwards - it has to be known up front.
        section_at, heading_ids = self._plan_sections(
            raw_paragraphs, page_number=page_number, section_offset=len(acc.sections)
        )

        # Where each table is emitted: at the first paragraph index its cells
        # cover, so it lands in the reading flow where its content actually is
        # rather than bunched at the top of the page.
        table_at: dict[int, list[int]] = {}
        for table_index, raw_table in enumerate(raw_tables):
            anchor = self._table_anchor(raw_table)
            table_at.setdefault(anchor, []).append(table_index)

        blocks: list[tuple[Any, str, Coordinates | None, int | None, str | None]] = []
        pending_list: list[tuple[ListItem, Coordinates | None]] = []

        def flush_list(section_id: str | None) -> None:
            """Close an accumulating run of list items into one list element."""
            nonlocal order, pending_list
            if not pending_list:
                return
            if len(pending_list) < 2:
                # A single marker line is a paragraph, not a list. Emitting a
                # one-item list would fragment prose that merely starts with a
                # dash.
                for item, coords in pending_list:
                    acc.paragraphs.append(
                        Paragraph(
                            paragraph_id=f"p{page_number}-para{len(acc.paragraphs)}",
                            text=f"{item.marker} {item.text}".strip(),
                            page_number=page_number,
                            section_id=section_id,
                            coordinates=coords,
                            reading_order=order,
                        )
                    )
                    order += 1
                pending_list = []
                return

            items = [item for item, _ in pending_list]
            boxes = [coords for _, coords in pending_list if coords is not None]
            first_marker = items[0].marker or ""
            acc.lists.append(
                DocumentList(
                    list_id=f"p{page_number}-list{len(acc.lists)}",
                    page_number=page_number,
                    ordered=bool(first_marker and first_marker[0].isalnum()),
                    items=items,
                    section_id=section_id,
                    coordinates=_union(boxes),
                    reading_order=order,
                )
            )
            order += 1
            pending_list = []

        def emit_tables(at_index: int, section_id: str | None) -> None:
            """Emit any tables anchored at this paragraph index."""
            nonlocal order
            for table_index in table_at.pop(at_index, []):
                table = self._table(
                    raw_tables[table_index],
                    page_number=page_number,
                    table_index=table_index,
                    width=width,
                    height=height,
                    unit=unit,
                    order=order,
                    section_id=section_id,
                )
                if table is None:
                    continue
                acc.tables.append(table)
                blocks.append((ContentBlockType.TABLE, "", table.coordinates, None, section_id))
                order += 1

        for para_index, raw in enumerate(raw_paragraphs):
            section_id = section_at.get(para_index)

            if para_index in consumed:
                # The paragraph belongs to a table; the table is emitted at its
                # anchor index instead.
                emit_tables(para_index, section_id)
                continue

            emit_tables(para_index, section_id)

            text = str(raw.get("content") or "").strip()
            if not text:
                continue
            role = raw.get("role")
            coords = self._coordinates(self._polygon_of(raw), page_number, width, height, unit)

            if role in _FURNITURE_ROLES:
                flush_list(section_id)
                # Header vs footer by vertical position, not by role name: a
                # `pageNumber` legitimately sits at either end of the page, and
                # `pageFooter` at the top of a landscape page is not unheard of.
                is_footer = coords is not None and coords.y > height * 0.5
                footers_or_headers = acc.footers if is_footer else acc.headers
                footers_or_headers.append(
                    HeaderFooter(
                        text=text,
                        page_number=page_number,
                        coordinates=coords,
                        kind="footer" if is_footer else "header",
                    )
                )
                continue

            if role in _HEADING_ROLES:
                flush_list(section_at.get(para_index - 1))
                number, title = _split_heading(text)
                level = 1 if role == "title" else _heading_level(number)
                acc.sections.append(
                    Section(
                        section_id=heading_ids[para_index],
                        title=title,
                        level=level,
                        start_page=page_number,
                        end_page=page_number,
                        number=number,
                        order=order,
                        coordinates=coords,
                    )
                )
                blocks.append(
                    (ContentBlockType.HEADING, text, coords, level, heading_ids[para_index])
                )
                order += 1
                continue

            marker_match = _LIST_MARKER.match(text)
            if marker_match:
                pending_list.append(
                    (
                        ListItem(
                            text=text[marker_match.end() :].strip(),
                            level=1,
                            marker=marker_match.group(0).strip(),
                            coordinates=coords,
                        ),
                        coords,
                    )
                )
                continue

            flush_list(section_id)
            if _looks_like_signature(text):
                acc.signatures.append(
                    Signature(
                        signature_id=f"p{page_number}-sig{len(acc.signatures)}",
                        page_number=page_number,
                        coordinates=coords,
                        raw_text=text,
                        party=_signature_party(text),
                    )
                )
                blocks.append((ContentBlockType.SIGNATURE, text, coords, None, section_id))
                order += 1
                continue

            acc.paragraphs.append(
                Paragraph(
                    paragraph_id=f"p{page_number}-para{len(acc.paragraphs)}",
                    text=text,
                    page_number=page_number,
                    section_id=section_id,
                    coordinates=coords,
                    reading_order=order,
                )
            )
            blocks.append((ContentBlockType.PARAGRAPH, text, coords, None, section_id))
            order += 1

        flush_list(section_at.get(len(raw_paragraphs) - 1))
        # Any table whose anchor fell outside the paragraph range - a table with no
        # resolvable cell references - still has to be emitted.
        for leftover in sorted(table_at):
            emit_tables(leftover, section_at.get(len(raw_paragraphs) - 1))

        return order, blocks

    def _build(self, request: ParseRequest, payloads: list[dict[str, Any]]) -> NormalizedDocument:
        acc = _Accumulator()

        for index, payload in enumerate(payloads):
            page_data = self._page_of(payload, fallback=index + 1)
            page_number = int(page_data.get("pageNumber") or index + 1)

            acc.api_versions.add(str(payload.get("apiVersion") or ""))
            acc.model_ids.add(str(payload.get("modelId") or ""))
            acc.confidences.extend(
                float(word["confidence"])
                for word in page_data.get("words", [])
                if isinstance(word.get("confidence"), (int, float))
            )

            width, height, unit = self._page_size(page_data)
            page_order_start = acc.order

            order, blocks = self._build_page(
                payload,
                page_data,
                acc,
                page_number=page_number,
                width=width,
                height=height,
                unit=unit,
            )
            acc.order = order

            content = str(payload.get("content") or "")
            if not content.strip():
                acc.empty_pages.append(page_number)
            acc.pages.append(
                Page(
                    page_number=page_number,
                    width=width,
                    height=height,
                    rotation=_rotation(page_data.get("angle")),
                    reading_order=page_order_start,
                    content_blocks=[
                        self._block(block, page_number, position)
                        for position, block in enumerate(blocks)
                    ],
                    text_char_count=len(content),
                    # The service OCRs scanned pages itself, so a page with text is a
                    # page with text regardless of how it was produced.
                    is_scanned=False,
                )
            )

        if not acc.pages:
            raise ParserError(
                "The iDoc service returned no pages for this document.",
                retryable=False,
            )

        mean_confidence = (
            round(sum(acc.confidences) / len(acc.confidences), 4) if acc.confidences else None
        )
        warnings: list[str] = []
        if not acc.sections:
            warnings.append(
                "The layout service reported no headings, so section structure is flat."
            )
        if acc.empty_pages:
            warnings.append(
                f"{len(acc.empty_pages)} page(s) contained no extractable text: "
                + ", ".join(str(page) for page in acc.empty_pages[:10])
            )

        quality = QualityMetrics(
            ocr_confidence=mean_confidence,
            missing_text=len(acc.empty_pages) == len(acc.pages),
            empty_pages=acc.empty_pages,
            coordinate_coverage=_coverage(acc.paragraphs, acc.tables, acc.lists, acc.sections),
            warnings=warnings,
        )

        logger.info(
            "idoc_parse_completed",
            document_id=request.document_id,
            pages=len(acc.pages),
            paragraphs=len(acc.paragraphs),
            sections=len(acc.sections),
            tables=len(acc.tables),
            lists=len(acc.lists),
            signatures=len(acc.signatures),
            mean_word_confidence=mean_confidence,
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
                parser_name="idoc",
                parser_version=PARSER_FRAMEWORK_VERSION,
                adapter_version=ADAPTER_VERSION,
                created_at=datetime.now(UTC).isoformat(),
                # The service does its own OCR and does not report whether it ran, so
                # this is left false rather than guessed - a wrong `true` would temper
                # downstream confidence for no reason.
                ocr_applied=False,
                ocr_engine=None,
                source_metadata={
                    key: value
                    for key, value in (
                        (
                            "layout_api_version",
                            ",".join(sorted(v for v in acc.api_versions if v)),
                        ),
                        ("layout_model", ",".join(sorted(m for m in acc.model_ids if m))),
                    )
                    if value
                },
            ),
            pages=acc.pages,
            sections=acc.sections,
            paragraphs=acc.paragraphs,
            tables=acc.tables,
            lists=acc.lists,
            images=[],
            signatures=acc.signatures,
            headers=acc.headers,
            footers=acc.footers,
            quality=quality,
        )

    # =========================================================================
    # Helpers
    # =========================================================================
    @staticmethod
    def _page_of(payload: dict[str, Any], *, fallback: int) -> dict[str, Any]:
        """The page object from a single-page payload."""
        pages = payload.get("pages") or []
        if pages and isinstance(pages[0], dict):
            return pages[0]
        return {"pageNumber": fallback}

    @staticmethod
    def _page_size(page_data: dict[str, Any]) -> tuple[float, float, str]:
        """Page dimensions in points, plus the unit they arrived in.

        Converted here so every parser's coordinates share one space and the viewer
        does not have to know which parser produced a highlight.
        """
        unit = str(page_data.get("unit") or "inch").lower()
        width = float(page_data.get("width") or 8.5)
        height = float(page_data.get("height") or 11.0)
        scale = _POINTS_PER_INCH if unit in {"inch", "inches"} else 1.0
        return round(width * scale, 2), round(height * scale, 2), unit

    @staticmethod
    def _polygon_of(node: dict[str, Any]) -> list[float]:
        """Pull a polygon from an element, wherever the schema puts it."""
        polygon = node.get("polygon")
        if isinstance(polygon, list) and polygon:
            return [float(value) for value in polygon]
        regions = node.get("boundingRegions") or []
        if regions and isinstance(regions[0], dict):
            candidate = regions[0].get("polygon")
            if isinstance(candidate, list) and candidate:
                return [float(value) for value in candidate]
        return []

    @staticmethod
    def _coordinates(
        polygon: list[float],
        page_number: int,
        page_width: float,
        page_height: float,
        unit: str,
    ) -> Coordinates | None:
        """Convert a four-corner polygon into a bounding box in points.

        The polygon is not necessarily axis-aligned - a skewed scan produces a
        rotated quadrilateral - so the bounding box is the extent of all four
        corners. Highlighting a slightly larger rectangle is correct behaviour;
        using only two corners would clip text on a skewed page.
        """
        if len(polygon) < 8:
            return None
        scale = _POINTS_PER_INCH if unit in {"inch", "inches"} else 1.0
        xs = [polygon[i] * scale for i in range(0, 8, 2)]
        ys = [polygon[i] * scale for i in range(1, 8, 2)]
        x, y = min(xs), min(ys)
        return Coordinates(
            page_number=page_number,
            x=round(x, 2),
            y=round(y, 2),
            width=round(max(xs) - x, 2),
            height=round(max(ys) - y, 2),
            page_width=page_width,
            page_height=page_height,
        )

    @staticmethod
    def _table_paragraph_indices(raw_tables: list[dict[str, Any]]) -> set[int]:
        """Paragraph indices that a table cell already covers.

        Each cell carries ``elements: ["/paragraphs/4", ...]``. Without this the fee
        schedule's cells would be emitted twice - once inside the table and again as
        loose paragraphs - and chunking would see the figures in both places.
        """
        consumed: set[int] = set()
        for table in raw_tables:
            consumed |= _cell_paragraph_indices(table)
        return consumed

    @staticmethod
    def _table_anchor(raw_table: dict[str, Any]) -> int:
        """The paragraph index a table should be emitted at.

        The lowest index its cells reference, so the table appears in the reading flow
        where its content sits. Falls back to a large sentinel when no cell resolves,
        which places such a table at the end of the page rather than losing it.
        """
        indices = _cell_paragraph_indices(raw_table)
        return min(indices) if indices else 1 << 30

    @staticmethod
    def _plan_sections(
        raw_paragraphs: list[dict[str, Any]],
        *,
        page_number: int,
        section_offset: int,
    ) -> tuple[dict[int, str | None], dict[int, str]]:
        """Resolve which section each paragraph belongs to, before construction.

        Returns ``(section_at, heading_ids)``: the open section for every paragraph
        index, and the id assigned to each heading paragraph. Done as a separate pass
        because the CDM models are frozen - a table cannot have its ``section_id``
        patched on after the headings are known, so the answer has to exist first.
        """
        section_at: dict[int, str | None] = {}
        heading_ids: dict[int, str] = {}
        current: str | None = None
        counter = section_offset

        for index, raw in enumerate(raw_paragraphs):
            if raw.get("role") in _HEADING_ROLES and str(raw.get("content") or "").strip():
                section_id = f"p{page_number}-sec{counter}"
                counter += 1
                heading_ids[index] = section_id
                current = section_id
            # A heading maps to its own section, so content under it and the heading
            # itself agree.
            section_at[index] = current
        return section_at, heading_ids

    def _table(
        self,
        raw: dict[str, Any],
        *,
        page_number: int,
        table_index: int,
        width: float,
        height: float,
        unit: str,
        order: int,
        section_id: str | None,
    ) -> Table | None:
        cells: list[TableCell] = []
        for raw_cell in raw.get("cells") or []:
            cells.append(
                TableCell(
                    row=int(raw_cell.get("rowIndex") or 0),
                    col=int(raw_cell.get("columnIndex") or 0),
                    text=str(raw_cell.get("content") or "").strip(),
                    row_span=int(raw_cell.get("rowSpan") or 1),
                    col_span=int(raw_cell.get("columnSpan") or 1),
                    is_header=str(raw_cell.get("kind") or "") == "columnHeader",
                    coordinates=self._coordinates(
                        self._polygon_of(raw_cell), page_number, width, height, unit
                    ),
                )
            )
        if not cells:
            return None

        return Table(
            table_id=f"p{page_number}-table{table_index}",
            page_number=page_number,
            rows=int(raw.get("rowCount") or (max(c.row for c in cells) + 1)),
            columns=int(raw.get("columnCount") or (max(c.col for c in cells) + 1)),
            cells=cells,
            caption=_caption_of(raw),
            section_id=section_id,
            coordinates=self._coordinates(self._polygon_of(raw), page_number, width, height, unit),
            reading_order=order,
        )

    @staticmethod
    def _block(
        block: tuple[Any, str, Coordinates | None, int | None, str | None],
        page_number: int,
        position: int,
    ) -> ContentBlock:
        block_type, text, coords, level, section_id = block
        return ContentBlock(
            block_id=f"p{page_number}-b{position}",
            block_type=block_type,
            order=position,
            text=text,
            coordinates=coords,
            level=level,
            section_id=section_id,
        )


# =============================================================================
# Module-level helpers
# =============================================================================
def _cell_paragraph_indices(raw_table: dict[str, Any]) -> set[int]:
    """Paragraph indices referenced by a table's cells."""
    indices: set[int] = set()
    for cell in raw_table.get("cells") or []:
        for reference in cell.get("elements") or []:
            match = _ELEMENT_REF.match(str(reference))
            if match and match.group("kind") == "paragraphs":
                indices.add(int(match.group("index")))
    return indices


def _caption_of(raw_table: dict[str, Any]) -> str | None:
    """A table's caption, when the service supplies one."""
    caption = raw_table.get("caption")
    if isinstance(caption, dict):
        text = str(caption.get("content") or "").strip()
        return text or None
    if isinstance(caption, str):
        return caption.strip() or None
    return None


def _split_heading(text: str) -> tuple[str | None, str]:
    """Split "2. Limitation of Liability" into its number and its title."""
    match = _HEADING_NUMBER.match(text)
    if not match:
        return None, text
    number = match.group("number").rstrip(".)")
    return number, match.group("title").strip()


def _heading_level(number: str | None) -> int:
    """Depth from the numbering: "11.2.1" is level 3.

    Derived from the printed number rather than from font size, because the number
    is what the contract itself asserts about structure.
    """
    if not number:
        return 1
    return min(number.count(".") + 1, 6)


def _rotation(angle: Any) -> int:
    """Page rotation in whole degrees.

    The service reports a fractional skew angle; the CDM wants the page's rotation.
    A fraction of a degree is scan skew, not rotation, so it rounds to zero.
    """
    try:
        return round(float(angle or 0.0))
    except (TypeError, ValueError):
        return 0


def _looks_like_signature(text: str) -> bool:
    lowered = text.lower()
    if any(hint in lowered for hint in _SIGNATURE_HINTS):
        return True
    # A run of underscores or dots is a signature rule.
    return bool(re.match(r"^[_\s.]{8,}$", text))


def _signature_party(text: str) -> str | None:
    """The party named in a signature block, where the wording gives one.

    "For and on behalf of Acme Corporation" names the party; a bare signature rule
    does not, and inventing one would put a party on the contract that the document
    never states.
    """
    match = re.search(
        r"(?:for and on behalf of|on behalf of|signed (?:for|by))\s+(?P<party>.+)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    party = match.group("party").strip(" .,:;-_")
    return party or None


def _union(boxes: list[Coordinates]) -> Coordinates | None:
    """Smallest box containing all of ``boxes``, which must share a page."""
    if not boxes:
        return None
    first = boxes[0]
    x = min(box.x for box in boxes)
    y = min(box.y for box in boxes)
    return Coordinates(
        page_number=first.page_number,
        x=round(x, 2),
        y=round(y, 2),
        width=round(max(box.x + box.width for box in boxes) - x, 2),
        height=round(max(box.y + box.height for box in boxes) - y, 2),
        page_width=first.page_width,
        page_height=first.page_height,
    )


def _coverage(
    paragraphs: list[Paragraph],
    tables: list[Table],
    lists: list[DocumentList],
    sections: list[Section],
) -> float:
    """Fraction of positioned elements that actually carry coordinates.

    Reported honestly: coordinates are mandatory for the evidence overlay, so a
    parse that lost them must say so rather than let the viewer fail silently.
    """
    elements: list[Any] = [*paragraphs, *tables, *lists, *sections]
    if not elements:
        return 0.0
    with_coords = sum(1 for element in elements if element.coordinates is not None)
    return round(with_coords / len(elements), 4)


__all__ = ["ADAPTER_VERSION", "IDocParser"]

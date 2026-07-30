"""``IDocumentParser`` - the parser abstraction (§9).

The contract that isolates the platform from any document-parsing vendor. Every
parser implements this interface and returns a
:class:`~app.ai.cdm.models.NormalizedDocument`. **No parser-specific JSON leaves an
adapter.** Switching parsers is a configuration change plus one new adapter, and
must never require a change to chunking, extraction, embedding, search or RAG.

An adapter's obligations, in order of importance:

1. **Preserve coordinates.** Every positioned element gets a
   :class:`~app.ai.cdm.models.Coordinates`. Without them the product cannot
   highlight evidence, which is the feature the whole platform is built around.
2. **Preserve reading order.** Deterministic ordering makes chunking reproducible.
3. **Preserve hierarchy.** Sections, sub-sections, and the paragraph/table/list
   membership that chunking follows.
4. **Report quality honestly.** A degraded parse must say so rather than silently
   yielding thin text.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.ai.cdm.models import NormalizedDocument, QualityMetrics
from app.core.enums import FileType


@dataclass(slots=True)
class ParseRequest:
    """Input to a parser adapter.

    Carries bytes rather than a storage path so an adapter never needs storage
    credentials, and can be tested with a fixture file.
    """

    document_id: str
    project_id: str
    organization_id: str
    file_name: str
    storage_path: str
    file_type: FileType
    content: bytes
    file_hash: str
    #: Whether OCR may be applied to pages that appear scanned.
    allow_ocr: bool = True
    language_hint: str | None = None
    #: Per-request overrides (page range for a partial re-parse, DPI, timeouts).
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass(slots=True)
class ParserCapabilities:
    """What an adapter can actually do.

    Declared rather than assumed: the registry validates that the selected parser
    supports the file type before dispatching, and the enrichment stage uses these
    flags to decide whether a missing element is a parse gap or simply unsupported.
    """

    name: str
    version: str
    supported_types: frozenset[FileType]
    supports_coordinates: bool = True
    supports_tables: bool = True
    supports_sections: bool = True
    supports_lists: bool = True
    supports_images: bool = False
    supports_ocr: bool = False
    supports_signatures: bool = False
    #: True when the adapter calls an external service (affects retry and cost).
    is_remote: bool = False
    max_pages: int | None = None

    def supports(self, file_type: FileType) -> bool:
        return file_type in self.supported_types


class IDocumentParser(ABC):
    """A document parser adapter.

    Implementations must be stateless between :meth:`parse` calls so one instance
    can serve a whole worker pool concurrently.
    """

    @property
    @abstractmethod
    def capabilities(self) -> ParserCapabilities:
        """What this adapter supports. Read by the registry and the enrichment stage."""

    @abstractmethod
    async def parse(self, request: ParseRequest) -> NormalizedDocument:
        """Parse a document into the normalized model.

        Must raise a subclass of :class:`~app.core.errors.ParserError` on failure -
        :class:`~app.core.errors.CorruptedDocumentError` for an unreadable file (not
        retryable), :class:`~app.core.errors.OcrError` or
        :class:`~app.core.errors.ParserTimeoutError` for transient problems.
        """

    @abstractmethod
    async def health(self) -> bool:
        """Is this parser usable right now?

        For a remote parser this checks the endpoint; for a local one it verifies the
        library and any binary dependency (e.g. the tesseract executable) resolve.
        """

    # --- optional hooks -------------------------------------------------------
    async def metadata(self, request: ParseRequest) -> dict[str, Any]:
        """Document properties without a full parse - page count, title, author.

        Used for a cheap pre-check; the default returns nothing rather than forcing
        every adapter to implement it.
        """
        return {}

    async def page_count(self, request: ParseRequest) -> int | None:
        return None

    def quality_from(self, document: NormalizedDocument) -> QualityMetrics:
        """Derive quality metrics from a parse result.

        Coordinate coverage is computed here rather than trusted from the adapter, so
        an adapter cannot over-report its own fidelity.
        """
        positioned = 0
        with_coords = 0
        for collection in (
            document.paragraphs,
            document.tables,
            document.lists,
            document.signatures,
        ):
            for element in collection:
                positioned += 1
                if getattr(element, "coordinates", None) is not None:
                    with_coords += 1

        coverage = (with_coords / positioned) if positioned else 1.0
        empty_pages = [page.page_number for page in document.pages if page.block_count == 0]
        scanned = [page.page_number for page in document.pages if page.is_scanned]

        warnings = list(document.quality.warnings)
        if coverage < 1.0:
            warnings.append(
                f"{positioned - with_coords} of {positioned} elements have no coordinates; "
                "their evidence cannot be highlighted."
            )
        if empty_pages:
            warnings.append(f"{len(empty_pages)} page(s) yielded no content.")

        total_text = sum(len(paragraph.text) for paragraph in document.paragraphs)

        return QualityMetrics(
            ocr_confidence=document.quality.ocr_confidence,
            # A multi-page document with almost no text is a parse failure dressed up
            # as a success - flag it rather than letting extraction find nothing.
            missing_text=total_text < 200 and len(document.pages) > 1,
            empty_pages=empty_pages,
            corrupted_pages=document.quality.corrupted_pages,
            scanned_pages=scanned,
            coordinate_coverage=round(coverage, 4),
            warnings=warnings,
        )

    def __repr__(self) -> str:  # pragma: no cover
        caps = self.capabilities
        return f"<{type(self).__name__} {caps.name}@{caps.version}>"


__all__ = ["IDocumentParser", "ParseRequest", "ParserCapabilities"]

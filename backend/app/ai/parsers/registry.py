"""Parser registry (§9).

Selects the parser implementation from configuration (``ACTIVE_PARSER``), validates
that it supports the file at hand, and lets several parser pools coexist - a
deployment can run 20 Docling workers and 10 Azure Document Intelligence workers
against the same platform.

Two selection rules matter:

* **Config chooses, capability confirms.** If the configured parser cannot handle a
  file type, the registry falls back to one that can rather than failing the job.
  A DOCX arriving at a PDF-only parser is a routing problem, not a document problem.
* **Adapters are cached per process.** They are stateless, so one instance serves a
  whole worker pool; constructing one per document would reload models needlessly.
"""

from __future__ import annotations

from typing import Any

from app.ai.parsers.base import IDocumentParser, ParserCapabilities
from app.core.config import get_settings
from app.core.enums import FileType
from app.core.errors import ParserError, UnsupportedFileTypeError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Adapter constructors, imported lazily so a deployment only loads the SDK it uses.
_FACTORIES: dict[str, str] = {
    "pdfextract": "app.ai.parsers.pdfextract_adapter:PdfTextExtractorParser",
    "pymupdf": "app.ai.parsers.pymupdf_adapter:PyMuPdfParser",
    "docx": "app.ai.parsers.docx_adapter:DocxParser",
    "mock": "app.ai.parsers.mock_adapter:MockParser",
    "adi": "app.ai.parsers.adi_adapter:AzureDocumentIntelligenceParser",
}

#: Preference order when the configured parser cannot handle a file type.
#:
#: ``adi`` leads for PDFs and ``pdfextract`` catches it: Azure Document
#: Intelligence has the tuned model, and the extractor container runs locally, so
#: the chain degrades from "best available" to "no external dependency" without
#: changing the *shape* of what comes back - both emit the per-page layout JSON the
#: document pipeline reads, with real layout roles and coordinates, so section
#: structure comes from the parser rather than from font-size heuristics.
#:
#: Deliberately just those two. ``pymupdf`` stays registered and selectable, but is
#: out of the chain: it produces no layout JSON, so a document that fell through to
#: it would parse and then strand ``docpipeline`` with nothing to pin clauses to -
#: a failure that surfaces two stages later as missing evidence rather than as a
#: parser problem. A deployment that wants it says so with ``ACTIVE_PARSER=pymupdf``.
_FALLBACKS: dict[FileType, tuple[str, ...]] = {
    FileType.PDF: ("adi", "pdfextract"),
    FileType.DOCX: ("docx",),
}

_instances: dict[str, IDocumentParser] = {}


def _load(name: str) -> IDocumentParser:
    """Instantiate an adapter by registry name, caching the instance."""
    if name in _instances:
        return _instances[name]

    target = _FACTORIES.get(name)
    if target is None:
        raise ParserError(f"Unknown parser '{name}'. Known: {', '.join(sorted(_FACTORIES))}.")

    module_path, _, class_name = target.partition(":")
    try:
        module = __import__(module_path, fromlist=[class_name])
        parser: IDocumentParser = getattr(module, class_name)()
    except ImportError as exc:
        raise ParserError(
            f"Parser '{name}' is not installed in this deployment: {exc}",
            details={"parser": name},
        ) from exc

    _instances[name] = parser
    return parser


def get_parser(file_type: FileType, *, preferred: str | None = None) -> IDocumentParser:
    """Resolve the parser for a file type.

    ``preferred`` overrides configuration - used when a profile or a reprocess pins a
    specific parser to reproduce an earlier extraction.
    """
    settings = get_settings()
    requested = preferred or settings.parser.active_parser

    # Try the requested parser first.
    try:
        parser = _load(requested)
        if parser.capabilities.supports(file_type):
            return parser
        logger.info(
            "parser_cannot_handle_type",
            parser=requested,
            file_type=file_type.value,
        )
    except ParserError as exc:
        logger.warning("parser_unavailable", parser=requested, error=str(exc))

    # Fall back to any parser that can handle this type.
    for candidate in _FALLBACKS.get(file_type, ()):
        if candidate == requested:
            continue
        try:
            parser = _load(candidate)
        except ParserError:
            continue
        if parser.capabilities.supports(file_type):
            logger.info(
                "parser_fallback_selected",
                requested=requested,
                selected=candidate,
                file_type=file_type.value,
            )
            return parser

    raise UnsupportedFileTypeError(
        f"No parser in this deployment can handle {file_type.value.upper()} files.",
        details={"file_type": file_type.value, "requested_parser": requested},
    )


def get_parser_by_name(name: str) -> IDocumentParser:
    """Load a specific adapter, ignoring capability matching."""
    return _load(name)


def available_parsers() -> dict[str, ParserCapabilities]:
    """Capabilities of every adapter that loads in this deployment.

    Surfaced by the admin API so an operator can see which parsers are actually
    installed rather than which are theoretically supported.
    """
    result: dict[str, ParserCapabilities] = {}
    for name in _FACTORIES:
        try:
            result[name] = _load(name).capabilities
        except ParserError:
            continue
    return result


async def parser_health() -> dict[str, Any]:
    """Health of the configured parser plus every loadable adapter."""
    settings = get_settings()
    report: dict[str, Any] = {"active": settings.parser.active_parser, "parsers": {}}
    for name, capabilities in available_parsers().items():
        try:
            healthy = await _load(name).health()
        except Exception as exc:  # noqa: BLE001
            healthy = False
            logger.debug("parser_health_check_failed", parser=name, error=str(exc))
        report["parsers"][name] = {
            "healthy": healthy,
            "version": capabilities.version,
            "supported_types": sorted(t.value for t in capabilities.supported_types),
            "coordinates": capabilities.supports_coordinates,
            "ocr": capabilities.supports_ocr,
            "remote": capabilities.is_remote,
        }
    return report


def reset_registry() -> None:
    """Drop cached adapters. Used by tests that swap configuration."""
    _instances.clear()


__all__ = [
    "available_parsers",
    "get_parser",
    "get_parser_by_name",
    "parser_health",
    "reset_registry",
]

"""Local layout parser backed by a `pdf_text_extractor` checkout.

A drop-in alternative to the iDoc service, not a degraded fallback. The extractor's
``--adi`` export maps its elements onto Azure Document Intelligence's
``analyzeResult`` - flat polygons in inches, a top-level ``paragraphs[]`` carrying
``role``, ``pages[].pageNumber`` - which is byte-for-byte the shape iDoc returns.
So this subclasses :class:`~app.ai.parsers.idoc_adapter.IDocParser` and replaces
exactly one method: where the payloads come from.

That matters more than it looks. ``docpipeline`` does not read the parser's
normalised output; it reads the raw per-page layout JSON back out of the parser
cache and pins clauses to the paragraph geometry in it. A parser that produced a
good ``NormalizedDocument`` but no layout JSON - ``pymupdf``, for instance - leaves
that stage with nothing to work from. This one produces the JSON, so the entire
pipeline runs unchanged.

**Why a subprocess rather than an import.** The extractor's dependency set is large
and partly non-Python: pdfplumber, OpenCV, Pillow, and the Tesseract and poppler
binaries at the OS level. Importing it would put an OCR stack into every deployment
of this service, including those that never parse a scanned page. Running its own
virtualenv keeps that boundary where the iDoc service used to draw it.

**Why one whole-document call, split afterwards.** The extractor emits a single
``analyzeResult`` covering every page; iDoc returned a ZIP of per-page files. The
cache and ``docpipeline`` both expect the per-page list, so :func:`split_pages`
does that division here rather than teaching two consumers a second shape.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from app.ai.parsers.base import ParserCapabilities, ParseRequest
from app.ai.parsers.idoc_adapter import IDocParser
from app.core.config import get_settings
from app.core.enums import FileType
from app.core.errors import ParserError
from app.core.logging import get_logger
from app.core.versions import PARSER_FRAMEWORK_VERSION

logger = get_logger(__name__)

ADAPTER_VERSION = "1.0.0"


def split_pages(adi: dict[str, Any]) -> list[dict[str, Any]]:
    """One whole-document ``analyzeResult`` -> one self-contained payload per page.

    Each payload is what ``IDocParser._build_page`` and
    ``app.ai.docpipeline.source._build_page`` both expect: ``pages[0]`` carries the
    dimensions, unit and page number, and ``paragraphs`` carries that page's text.

    Paragraphs are placed by ``boundingRegions[0].pageNumber``. One with no region
    is dropped rather than guessed at: assigning it to page 1 would give a clause a
    citation pointing at the wrong page, and a confidently wrong citation is worse
    in this product than a missing paragraph.
    """
    result = adi.get("analyzeResult", adi)

    by_page: dict[int, list[dict[str, Any]]] = {}
    orphans = 0
    for paragraph in result.get("paragraphs") or []:
        regions = paragraph.get("boundingRegions") or []
        number = regions[0].get("pageNumber") if regions else None
        if isinstance(number, int):
            by_page.setdefault(number, []).append(paragraph)
        else:
            orphans += 1

    if orphans:
        logger.warning("pdfextract_paragraphs_without_page", count=orphans)

    return [
        {
            # A list of one: `_page_of` reads `pages[0]`, because the page a payload
            # describes is always its own first entry.
            "pages": [page],
            "paragraphs": by_page.get(page.get("pageNumber"), []),
            "modelId": result.get("modelId", "prebuilt-layout"),
            "apiVersion": result.get("apiVersion"),
        }
        for page in result.get("pages") or []
    ]


class PdfTextExtractorParser(IDocParser):
    """Layout parser that shells out to a local pdf_text_extractor checkout."""

    @property
    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities(
            name="pdfextract",
            version=PARSER_FRAMEWORK_VERSION,
            supported_types=frozenset({FileType.PDF}),
            supports_coordinates=True,
            supports_tables=True,
            supports_sections=True,
            supports_lists=True,
            supports_images=False,
            # Routes per page: a page with a real text layer is read natively, a
            # scanned one goes through Tesseract.
            supports_ocr=True,
            supports_signatures=True,
            # The distinction that matters here: no network call, no metered
            # service, and nothing outside this machine has to be up.
            is_remote=False,
        )

    async def health(self) -> bool:
        """Is a usable checkout configured?

        Deliberately does not run the CLI. Health is polled by the readiness probe,
        and starting a Python interpreter on every poll would cost more than the
        check is worth; the interesting failure - a missing or half-built checkout -
        is visible from the filesystem alone.
        """
        settings = get_settings().parser
        if not settings.pdfextract_python:
            return False
        return (Path(settings.pdfextract_path) / "run_cli.py").is_file()

    async def _analyse(self, request: ParseRequest) -> list[dict[str, Any]]:
        """Run the extractor over the PDF and return the per-page payloads."""
        settings = get_settings().parser
        python = settings.pdfextract_python
        root = Path(settings.pdfextract_path) if settings.pdfextract_path else None

        if not root or not python:
            raise ParserError(
                "PDFEXTRACT_PATH is not set to a checkout with a .venv, so the "
                "pdfextract parser cannot run.",
                retryable=False,
                details={"pdfextract_path": settings.pdfextract_path},
            )
        if not (root / "run_cli.py").is_file():
            raise ParserError(
                f"No run_cli.py in {root}. PDFEXTRACT_PATH must point at the "
                "pdf_text_extractor checkout itself.",
                retryable=False,
                details={"pdfextract_path": str(root)},
            )

        # The CLI takes a path, and the request carries bytes - so the document
        # touches the disk here and nowhere else, inside a directory that is removed
        # even when the parse raises.
        with tempfile.TemporaryDirectory(prefix="cip-pdfextract-") as tmp:
            work = Path(tmp)
            source = work / "input.pdf"
            source.write_bytes(request.content)
            adi_path = work / "layout.adi.json"

            command = [
                python,
                str(root / "run_cli.py"),
                str(source),
                "-o",
                str(work / "elements.json"),
                "--adi",
                str(adi_path),
                # Inches, matching ADI's own convention for PDFs. `_coordinates`
                # and the document pipeline both read the unit off the page, but
                # asking for points here would make every stored polygon disagree
                # with the ones iDoc produced for documents parsed earlier.
                "--adi-unit",
                "inch",
                "--backend",
                settings.pdfextract_backend,
                "--dpi",
                str(settings.pdfextract_dpi),
            ]

            await self._run(command, root, settings.pdfextract_timeout_seconds, request)

            if not adi_path.is_file():
                raise ParserError(
                    "The extractor finished without writing its ADI export.",
                    retryable=True,
                    details={"expected": str(adi_path)},
                )
            try:
                adi = json.loads(adi_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ParserError(
                    f"The extractor's ADI export could not be read: {exc}",
                    retryable=True,
                ) from exc

        payloads = split_pages(adi)
        if not payloads:
            raise ParserError(
                "The extractor returned no pages for this document.",
                retryable=False,
                details={"file_name": request.file_name},
            )

        logger.info(
            "pdfextract_parsed",
            file_name=request.file_name,
            pages=len(payloads),
            paragraphs=sum(len(page["paragraphs"]) for page in payloads),
            backend=settings.pdfextract_backend,
        )
        return payloads

    @staticmethod
    async def _run(
        command: list[str], cwd: Path, timeout_seconds: int, request: ParseRequest
    ) -> None:
        """Run the CLI without blocking the event loop.

        ``create_subprocess_exec`` rather than ``subprocess.run``: a worker runs
        several stages concurrently, and a synchronous call here would stall every
        other one for the length of an OCR pass.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ParserError(
                f"Could not start the extractor: {exc}",
                retryable=False,
                details={"command": command[0]},
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ParserError(
                f"The extractor did not finish within {timeout_seconds}s.",
                # Retryable: on a long scanned document this is a budget problem,
                # not a broken document.
                retryable=True,
                details={"file_name": request.file_name, "timeout_seconds": timeout_seconds},
            ) from None

        if process.returncode != 0:
            detail = (stderr or b"").decode("utf-8", "replace").strip()
            tail = (stdout or b"").decode("utf-8", "replace").strip()
            logger.error(
                "pdfextract_failed",
                returncode=process.returncode,
                stderr=detail[-800:],
                stdout=tail[-400:],
            )
            raise ParserError(
                f"The extractor exited with code {process.returncode}.",
                retryable=True,
                details={"stderr": detail[-400:]},
            )


__all__ = ["PdfTextExtractorParser", "split_pages"]

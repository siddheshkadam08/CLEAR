"""Record a parser fixture from the local PDF extractor.

    python -m app.tools.record_fixture contract.pdf
    python -m app.tools.record_fixture *.pdf --extractor D:/pdf_text_extractor/pdf_text_extractor

Pre-records a document's layout JSON as an ``adi`` fixture, so it can be replayed
later with ``PARSER_MODE=fixture`` and no Azure resource or extractor present at
all.

This is *not* the way to run the extractor in the pipeline - ``ACTIVE_PARSER=pdfextract``
does that natively, on upload, with no pre-step. Use this tool when you want the
result committed rather than computed:

* CI, which must parse the same document identically on every run and has neither
  an Azure credential nor Tesseract installed;
* a demo machine that should not depend on a checkout being present;
* pinning a known-good parse of a document whose extraction you are about to
  change, so a regression is visible as a diff.

Writes into the ``adi`` fixture namespace deliberately: fixtures record *what the
layout was*, and a replay should not care which implementation produced it. That
namespace is the default parser's, so a fixture recorded here is the one a default
deployment replays.

Shares :func:`~app.ai.parsers.pdfextract_adapter.split_pages` with the adapter, so
the whole-document-to-per-page division cannot drift between the two paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.ai.parsers.pdfextract_adapter import split_pages

DEFAULT_EXTRACTOR = Path("D:/pdf_text_extractor/pdf_text_extractor")


def extract_adi(pdf: Path, extractor: Path, backend: str) -> dict[str, Any]:
    """Run the extractor's CLI in its own virtualenv and read back the ADI JSON."""
    python = extractor / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        python = extractor / ".venv" / "bin" / "python"
    if not python.is_file():
        raise SystemExit(f"No virtualenv in {extractor}. Expected .venv/Scripts/python.exe")

    with tempfile.TemporaryDirectory() as tmp:
        adi_path = Path(tmp) / "out.adi.json"
        command = [
            str(python),
            str(extractor / "run_cli.py"),
            str(pdf),
            "-o",
            str(Path(tmp) / "out.json"),
            "--adi",
            str(adi_path),
            "--adi-unit",
            # Inches: what ADI uses for PDFs, and what `source._polygon_of` and
            # `LayoutParser._coordinates` assume when converting back to points.
            "inch",
            "--backend",
            backend,
        ]
        # S603 is suppressed below: every element is built here from an
        # operator-supplied path, never from request data - this is a developer
        # tool run by hand, and the list form means no shell is involved.
        completed = subprocess.run(  # noqa: S603
            command, cwd=str(extractor), capture_output=True, text=True
        )
        if completed.returncode != 0:
            sys.stderr.write(completed.stdout[-2000:])
            sys.stderr.write(completed.stderr[-2000:])
            raise SystemExit(f"Extraction failed for {pdf.name} ({completed.returncode}).")
        return json.loads(adi_path.read_text(encoding="utf-8"))


def record(pdf: Path, extractor: Path, backend: str) -> None:
    from app.ai.parsers.fixtures import FixtureStore

    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    print(f"{pdf.name}  sha256={digest[:16]}...")

    store = FixtureStore(parser="adi")
    if store.load(digest) is not None:
        print("  already recorded - skipping (fixtures are never overwritten)")
        return

    payloads = split_pages(extract_adi(pdf, extractor, backend))
    if not payloads:
        raise SystemExit(f"  the extractor returned no pages for {pdf.name}")

    path = store.save(
        digest,
        payloads,
        file_name=pdf.name,
        source=f"pdf_text_extractor:{backend}",
    )
    paragraphs = sum(len(p["paragraphs"]) for p in payloads)
    headings = sum(
        1 for p in payloads for q in p["paragraphs"] if q.get("role") == "sectionHeading"
    )
    print(f"  {len(payloads)} pages, {paragraphs} paragraphs, {headings} headings -> {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdfs", nargs="+", type=Path, help="PDF(s) to record.")
    parser.add_argument(
        "--extractor",
        type=Path,
        default=DEFAULT_EXTRACTOR,
        help=f"Checkout of the PDF extractor (default: {DEFAULT_EXTRACTOR}).",
    )
    parser.add_argument(
        "--backend",
        default="baseline",
        help="Extractor backend: baseline (pdfplumber+tesseract) or docling.",
    )
    args = parser.parse_args()

    if not args.extractor.is_dir():
        raise SystemExit(f"Extractor not found at {args.extractor}. Pass --extractor.")

    for pdf in args.pdfs:
        if not pdf.is_file():
            print(f"{pdf}: not a file - skipping")
            continue
        record(pdf, args.extractor, args.backend)

    print("\nSet PARSER_MODE=fixture so the parser replays these.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

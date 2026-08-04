"""Word documents to PDF, so the pipeline only ever sees PDFs.

Every stage downstream of upload assumes a PDF. Not incidentally - evidence
highlighting positions a bounding box against a rendered page, and page geometry
only exists once there is a PDF. Parsing a DOCX natively would produce clauses
that could be searched but never *shown*, which is the half of the product that
makes an extraction trustworthy.

So a Word upload is converted once, at upload time, and the converted PDF is what
the pipeline reads and the viewer displays. The original is kept and remains
downloadable; it is simply not what the system reasons about.

LibreOffice does the conversion, headless, as a subprocess:

* it is the only engine that handles both legacy binary ``.doc`` and ``.docx``;
* it behaves identically on a developer's Windows laptop and in the Linux
  container, which a Word COM automation route does not - that would work in
  development and fail in every deployment;
* it needs no per-conversion licence or network call.

The cost is a real dependency: LibreOffice has to be installed, and
``LIBREOFFICE_PATH`` must point at it when it is not on ``PATH``. A missing
binary is reported as configuration, not as a corrupt document.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.core.enums import FileType
from app.core.errors import ConversionError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Names LibreOffice ships under, in the order they are worth trying.
_BINARIES = ("soffice", "libreoffice", "soffice.exe")

#: Where a Windows install puts it. LibreOffice does not add itself to `PATH`, so
#: without these a perfectly good install looks missing.
_WINDOWS_CANDIDATES = (
    Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
    Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
)

#: Where a Linux package manager puts it, for images that install it without
#: putting it on the service account's `PATH`.
_POSIX_CANDIDATES = (
    Path("/usr/bin/soffice"),
    Path("/usr/bin/libreoffice"),
    Path("/opt/libreoffice/program/soffice"),
    Path("/usr/lib/libreoffice/program/soffice"),
)


@dataclass(slots=True)
class ConversionResult:
    """A converted document, in memory."""

    content: bytes
    #: Name the PDF should be stored under - the original with a `.pdf` suffix.
    file_name: str
    page_count: int | None = None


def find_libreoffice() -> Path | None:
    """Locate the binary, or return None.

    Checked at startup so a misconfigured deployment says so on boot rather than
    on the first Word upload, and re-checked per conversion so installing it does
    not require a restart.
    """
    configured = get_settings().upload.libreoffice_path.strip()
    if configured:
        candidate = Path(configured)
        # A directory is the likely mistake - people paste the install root.
        if candidate.is_dir():
            for name in _BINARIES:
                nested = candidate / name
                if nested.exists():
                    return nested
                program = candidate / "program" / name
                if program.exists():
                    return program
        return candidate if candidate.exists() else None

    for name in _BINARIES:
        found = shutil.which(name)
        if found:
            return Path(found)

    for candidate in (*_WINDOWS_CANDIDATES, *_POSIX_CANDIDATES):
        if candidate.exists():
            return candidate
    return None


class DocumentConverter:
    """Converts Word documents to PDF via headless LibreOffice."""

    def __init__(self) -> None:
        self._settings = get_settings().upload

    @property
    def available(self) -> bool:
        return find_libreoffice() is not None

    async def to_pdf(self, content: bytes, file_name: str, file_type: FileType) -> ConversionResult:
        """Convert ``content`` to PDF.

        Raises ``ConversionError`` - never returns a partial result. A document
        that cannot be converted has no PDF, and the caller's job is to record
        that against this one file and carry on with the rest of the batch.
        """
        if not file_type.needs_pdf_conversion:
            raise ConversionError(
                f"{file_type.value} does not need conversion.",
                details={"file_name": file_name},
            )

        binary = find_libreoffice()
        if binary is None:
            raise ConversionError(
                "Word documents cannot be converted because LibreOffice is not "
                "installed or LIBREOFFICE_PATH does not point at it. PDF uploads "
                "are unaffected.",
                details={"file_name": file_name, "configured_path": self._settings.libreoffice_path},
                retryable=False,
            )

        # A private directory per conversion. LibreOffice writes the output beside
        # the input using its own naming, and runs of the same document in
        # parallel would otherwise collide on that name.
        # `ignore_cleanup_errors` so a conversion that timed out cannot also fail
        # the request on the way out: a killed soffice may still hold a handle on
        # its profile for a moment, and on Windows that makes the rmtree raise.
        # The directory is under the OS temp root and gets swept regardless; a
        # leaked one is untidy, whereas raising here would turn a reported timeout
        # into an unhandled error.
        with tempfile.TemporaryDirectory(prefix="cip-conv-", ignore_cleanup_errors=True) as tmp:
            work = Path(tmp)
            # The suffix matters: LibreOffice picks its import filter from it, and
            # a `.docx` fed in as `.doc` is rejected as corrupt.
            source = work / f"input.{file_type.value}"
            source.write_bytes(content)
            outdir = work / "out"
            outdir.mkdir()

            # `-env:UserInstallation` gives this run its own profile directory.
            # Without it concurrent conversions contend on the shared profile and
            # the later ones exit successfully having produced nothing - which is
            # the single most confusing way this can fail.
            command = [
                str(binary),
                f"-env:UserInstallation=file:///{(work / 'profile').as_posix().lstrip('/')}",
                "--headless",
                "--norestore",
                "--invisible",
                "--nolockcheck",
                "--nodefault",
                "--nologo",
                "--convert-to",
                "pdf",
                "--outdir",
                str(outdir),
                str(source),
            ]

            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self._settings.conversion_timeout_seconds
                )
            except TimeoutError as exc:
                # Kill it: an abandoned soffice holds its profile lock and the
                # next conversion inherits the failure.
                with contextlib.suppress(Exception):
                    process.kill()
                raise ConversionError(
                    f"Converting '{file_name}' timed out after "
                    f"{self._settings.conversion_timeout_seconds}s.",
                    details={"file_name": file_name},
                    retryable=True,
                ) from exc
            except OSError as exc:
                raise ConversionError(
                    f"Could not run LibreOffice: {exc}",
                    details={"file_name": file_name, "binary": str(binary)},
                    retryable=False,
                ) from exc

            produced = sorted(outdir.glob("*.pdf"))
            if process.returncode != 0 or not produced:
                # LibreOffice reports a refused document with exit code 0 and an
                # empty output directory, so the returncode alone is not enough to
                # tell success from failure.
                detail = (stderr or stdout or b"").decode("utf-8", "replace").strip()
                raise ConversionError(
                    f"'{file_name}' could not be converted to PDF."
                    + (f" LibreOffice said: {detail[:300]}" if detail else ""),
                    details={
                        "file_name": file_name,
                        "exit_code": process.returncode,
                        "produced": len(produced),
                    },
                    retryable=False,
                )

            pdf_bytes = produced[0].read_bytes()

        if not pdf_bytes.startswith(b"%PDF-"):
            raise ConversionError(
                f"The file produced from '{file_name}' is not a valid PDF.",
                details={"file_name": file_name},
                retryable=False,
            )

        logger.info(
            "document_converted",
            file_name=file_name,
            source_type=file_type.value,
            source_bytes=len(content),
            pdf_bytes=len(pdf_bytes),
        )
        return ConversionResult(content=pdf_bytes, file_name=pdf_name_for(file_name))


def pdf_name_for(file_name: str) -> str:
    """`Agreement.docx` -> `Agreement.pdf`.

    The stem is kept so the converted file is recognisable in storage and in the
    viewer's title bar; only the extension changes.
    """
    stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    return f"{stem}.pdf"

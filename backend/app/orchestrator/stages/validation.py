"""Stage 1 - Validation.

Re-checks the file inside the pipeline rather than trusting the upload request.
Two reasons this is a stage and not just request-time validation:

* The upload endpoint must answer in under five seconds, so anything slow (virus
  scanning, structural integrity, page counting) belongs here.
* A file can be re-processed months later, or arrive through a path other than the
  HTTP upload. The pipeline's own gate has to hold on its own.

Rejection is a **halt, not a failure**: a password-protected PDF is a valid outcome
of validation, not a system error, so the stage completes successfully and stops the
pipeline with a reason the user can act on.

Not cacheable - the checks are cheap and must re-run on a replacement upload.
"""

from __future__ import annotations

import contextlib
from typing import Any

from app.core.config import get_settings
from app.core.enums import ArtifactKind, FileType, PipelineStage
from app.core.errors import StorageError, ValidationError
from app.core.logging import get_logger
from app.core.security import sha256_bytes
from app.orchestrator.stages.base import (
    StageArtifact,
    StageContext,
    StageHandler,
    StageResult,
    register_stage,
)

logger = get_logger(__name__)

#: Signatures re-checked here; the upload path checks the same bytes.
_SIGNATURES: dict[FileType, tuple[bytes, ...]] = {
    FileType.PDF: (b"%PDF-",),
    FileType.DOCX: (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
}


class ValidationStage(StageHandler):
    stage = PipelineStage.VALIDATION
    cacheable = False
    retryable = True  # a storage blip is worth retrying; a bad file halts instead

    async def run(self, ctx: StageContext) -> StageResult:
        settings = get_settings()
        contract = ctx.contract
        checks: dict[str, Any] = {}
        warnings: list[str] = []

        await ctx.report_progress(1, "validating document")

        # --- retrieve ---------------------------------------------------------
        try:
            content = await ctx.storage.get_bytes(contract.storage_path)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                f"The stored document could not be read: {exc}",
                details={"storage_path": contract.storage_path},
            ) from exc

        checks["size_bytes"] = len(content)

        # --- integrity --------------------------------------------------------
        # The hash is re-verified because everything downstream is keyed to it: the
        # duplicate check, the artifact lineage, and the promise that an extraction
        # refers to these exact bytes.
        actual_hash = sha256_bytes(content)
        checks["sha256"] = actual_hash
        checks["hash_matches"] = actual_hash == contract.sha256_hash

        if not checks["hash_matches"]:
            logger.error(
                "validation_hash_mismatch",
                contract_id=str(contract.id),
                expected=contract.sha256_hash,
                actual=actual_hash,
            )
            return self._halt(
                checks,
                "The stored file does not match the hash recorded at upload. It may "
                "have been corrupted or replaced in storage.",
            )

        # --- size -------------------------------------------------------------
        if len(content) == 0:
            return self._halt(checks, "The document is empty.")
        if len(content) > settings.upload.max_upload_size_bytes:
            return self._halt(
                checks,
                f"The document exceeds the {settings.upload.max_upload_size_mb} MB limit.",
            )

        # --- file type --------------------------------------------------------
        signatures = _SIGNATURES.get(contract.file_type, ())
        header = content[:8]
        checks["signature_ok"] = any(header.startswith(sig) for sig in signatures)
        if not checks["signature_ok"]:
            return self._halt(
                checks,
                f"The file is not a valid {contract.file_type.value.upper()} document - "
                "its contents do not match its type.",
            )

        # --- structural integrity ---------------------------------------------
        structure = await self._check_structure(ctx, content)
        checks.update(structure)

        if structure.get("encrypted"):
            return self._halt(checks, "The document is password protected and cannot be processed.")
        if structure.get("unreadable"):
            return self._halt(
                checks,
                "The document could not be opened - it appears to be corrupted.",
            )

        page_count = structure.get("page_count")
        if page_count == 0:
            return self._halt(checks, "The document contains no pages.")

        # --- virus scan --------------------------------------------------------
        if settings.upload.virus_scan_enabled:
            infected, signature = await self._scan(content)
            checks["virus_scanned"] = True
            checks["infected"] = infected
            if infected:
                logger.error(
                    "validation_malware_detected",
                    contract_id=str(contract.id),
                    signature=signature,
                )
                return self._halt(
                    checks,
                    "The document was rejected by the malware scanner.",
                )
        else:
            checks["virus_scanned"] = False
            warnings.append("Malware scanning is disabled in this deployment.")

        await ctx.report_progress(3, "validated")

        context_updates: dict[str, Any] = {}
        if page_count:
            # Recorded on the contract now so the repository shows a page count before
            # parsing finishes.
            context_updates["page_count"] = page_count

        logger.info(
            "validation_passed",
            contract_id=str(contract.id),
            page_count=page_count,
            size_bytes=len(content),
        )

        return StageResult(
            artifacts=[
                StageArtifact(
                    kind=ArtifactKind.VALIDATION,
                    payload={
                        "contract_id": str(contract.id),
                        "file_name": contract.original_file_name,
                        "file_type": contract.file_type.value,
                        "checks": checks,
                        "warnings": warnings,
                        "passed": True,
                    },
                    summary={
                        "page_count": page_count,
                        "size_bytes": len(content),
                        "virus_scanned": checks.get("virus_scanned", False),
                    },
                )
            ],
            stats={
                "validation_size_bytes": len(content),
                "validation_page_count": page_count or 0,
            },
            context_updates=context_updates,
            warnings=warnings,
        )

    # =========================================================================
    # Helpers
    # =========================================================================
    @staticmethod
    def _halt(checks: dict[str, Any], reason: str) -> StageResult:
        """Reject the document.

        The artifact records *why*, so the failure is explainable in the UI rather
        than an opaque FAILED badge.
        """
        return StageResult(
            artifacts=[
                StageArtifact(
                    kind=ArtifactKind.VALIDATION,
                    payload={"checks": checks, "passed": False, "reason": reason},
                    summary={"passed": False, "reason": reason},
                )
            ],
            stats={"validation_rejected": 1},
            halt=True,
            halt_reason=reason,
        )

    async def _check_structure(self, ctx: StageContext, content: bytes) -> dict[str, Any]:
        """Open the document enough to confirm it is readable and count pages."""
        import asyncio

        if ctx.contract.file_type is FileType.PDF:
            return await asyncio.to_thread(self._check_pdf, content)
        return await asyncio.to_thread(self._check_docx, content)

    @staticmethod
    def _check_pdf(content: bytes) -> dict[str, Any]:
        try:
            import fitz
        except ImportError:
            # Without PyMuPDF the structural check is skipped rather than failing the
            # document; the parser stage will surface a real problem.
            return {"page_count": None, "structure_checked": False}

        try:
            with fitz.open(stream=content, filetype="pdf") as document:
                if document.needs_pass:
                    return {"encrypted": True, "structure_checked": True}
                return {
                    "page_count": int(document.page_count),
                    "structure_checked": True,
                    "pdf_version": document.metadata.get("format") if document.metadata else None,
                    "has_toc": bool(document.get_toc()),
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("pdf_structure_check_failed", error=str(exc))
            return {"unreadable": True, "structure_checked": True, "error": str(exc)[:200]}

    @staticmethod
    def _check_docx(content: bytes) -> dict[str, Any]:
        import io
        import zipfile

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = set(archive.namelist())
                # A DOCX without the main document part is not a DOCX.
                if "word/document.xml" not in names:
                    return {"unreadable": True, "structure_checked": True}
                if bad := archive.testzip():
                    return {
                        "unreadable": True,
                        "structure_checked": True,
                        "error": f"corrupt entry: {bad}",
                    }
                return {
                    "page_count": None,  # DOCX has no page model
                    "structure_checked": True,
                    "part_count": len(names),
                }
        except zipfile.BadZipFile:
            return {"unreadable": True, "structure_checked": True}
        except Exception as exc:  # noqa: BLE001
            logger.warning("docx_structure_check_failed", error=str(exc))
            return {"unreadable": True, "structure_checked": True, "error": str(exc)[:200]}

    @staticmethod
    async def _scan(content: bytes) -> tuple[bool, str | None]:
        """Scan with ClamAV over its TCP protocol.

        A scanner that cannot be reached raises rather than passing the file: a
        malware gate that fails open is not a gate.
        """
        import asyncio
        import struct

        settings = get_settings()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(settings.upload.clamav_host, settings.upload.clamav_port),
                timeout=10,
            )
        except Exception as exc:
            raise ValidationError(
                "Malware scanning is enabled but the scanner is unreachable; the "
                "document was not accepted.",
                details={"scanner": settings.upload.clamav_host},
            ) from exc

        try:
            writer.write(b"zINSTREAM\0")
            for offset in range(0, len(content), 8192):
                chunk = content[offset : offset + 8192]
                writer.write(struct.pack("!L", len(chunk)) + chunk)
            writer.write(struct.pack("!L", 0))
            await writer.drain()

            response = (await asyncio.wait_for(reader.read(4096), timeout=120)).decode(
                errors="replace"
            )
        finally:
            writer.close()
            # Suppressed deliberately: the scan result is already read, and a socket
            # that errors while closing tells us nothing about the file.
            with contextlib.suppress(Exception):
                await writer.wait_closed()

        if "FOUND" in response:
            return True, response.strip()
        return False, None


register_stage(ValidationStage())

__all__ = ["ValidationStage"]

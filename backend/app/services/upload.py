"""Upload service (FR-1).

The contract this service honours: **respond in under five seconds regardless of
file count**. It therefore does exactly four things per file - validate, hash,
store, create a queued job - and nothing else. No parsing, no page counting, no
extraction happens inside the request.

Validation is two-pass over the uploaded stream:

1. Pass one hashes and sniffs the content without holding the whole file in
   memory, then rewinds.
2. Pass two streams the same handle into object storage.

Hashing first is what makes duplicate detection cheap: a re-upload is rejected
before a single byte reaches storage.

A batch partially succeeding is a normal outcome, not an error - three duplicates
in a hundred-file upload should not fail the other ninety-seven.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
import zipfile
from dataclasses import dataclass
from typing import Any

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import get_settings
from app.core.enums import (
    ArchiveType,
    AuditAction,
    ContractStatus,
    FileType,
    JobPriority,
    PipelineStage,
)
from app.core.errors import (
    ArchiveError,
    ConversionError,
    ErrorCode,
    FileTooLargeError,
    UnsupportedFileTypeError,
    ValidationError,
)
from app.core.logging import get_logger, log_context
from app.core.telemetry import inject_context, span
from app.models.contract import Contract
from app.models.identity import User
from app.models.project import Project
from app.orchestrator.queue import StageMessage, get_queue_client
from app.repositories.contract import ContractRepository, ContractVersionRepository
from app.repositories.processing import ProcessingJobRepository
from app.repositories.project import ProjectActivityRepository, ProjectRepository
from app.schemas.contract import UploadedFileResult, UploadOptions, UploadResponse
from app.services import archive as archive_service
from app.services.audit import AuditService
from app.services.conversion import DocumentConverter, pdf_name_for
from app.storage import StorageKey, get_storage

logger = get_logger(__name__)

#: Magic-byte signatures. Extension and client-supplied content type are both
#: attacker-controlled, so the file's own header is what decides its type.
_ZIP_HEADERS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_SIGNATURES: dict[FileType, tuple[bytes, ...]] = {
    FileType.PDF: (b"%PDF-",),
    # DOCX is a ZIP container; the local file header is the only reliable marker.
    FileType.DOCX: _ZIP_HEADERS,
    # Legacy .doc is an OLE2 compound file. Its magic is shared with .xls and
    # .ppt, so this proves "an Office binary", not "a Word document" - the
    # conversion step is what finally rejects a spreadsheet renamed to .doc.
    FileType.DOC: (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
}

_CONTENT_TYPES: dict[FileType, str] = {
    FileType.PDF: "application/pdf",
    FileType.DOC: "application/msword",
    FileType.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_HASH_CHUNK = 1024 * 1024


def _is_archive(file_name: str) -> bool:
    extension = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    return extension in {member.value for member in ArchiveType}


def _upload_from_bytes(content: bytes, file_name: str) -> UploadFile:
    """Wrap in-memory bytes as an `UploadFile`.

    Archive members and converted PDFs arrive as bytes, but everything after this
    point - validation, hashing, storage - is written against `UploadFile`. Making
    them look the same is what keeps a document extracted from a ZIP on exactly
    the same code path as one uploaded directly, rather than a parallel
    implementation that drifts.
    """
    return UploadFile(file=io.BytesIO(content), filename=file_name, size=len(content))


@dataclass(slots=True)
class _ArchiveRef:
    """The archive a document came out of, carried onto its contract row."""

    id: uuid.UUID
    name: str


@dataclass(slots=True)
class _PendingUpload:
    """One document to process, whether uploaded directly or unpacked."""

    file_name: str
    upload: UploadFile
    archive: _ArchiveRef | None = None


@dataclass(slots=True)
class ValidatedFile:
    """Result of pass one over an uploaded stream."""

    file_name: str
    file_type: FileType
    size: int
    sha256: str
    content_type: str


class UploadService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.settings = get_settings()
        self.contracts = ContractRepository(db)
        self.versions = ContractVersionRepository(db)
        self.jobs = ProcessingJobRepository(db)
        self.projects = ProjectRepository(db)
        self.activities = ProjectActivityRepository(db)
        self.audit = AuditService(db)
        self.storage = get_storage()

    # =========================================================================
    # Entry point
    # =========================================================================
    async def upload(
        self,
        *,
        project: Project,
        files: list[UploadFile],
        options: UploadOptions,
        actor: User,
        ip: str | None = None,
    ) -> UploadResponse:
        """Accept a batch of files and queue one job per accepted contract."""
        if not files:
            raise ValidationError("No files were supplied.")
        if len(files) > self.settings.upload.max_files_per_upload:
            raise ValidationError(
                f"At most {self.settings.upload.max_files_per_upload} files may be "
                "uploaded at once.",
                details={"received": len(files)},
            )

        results: list[UploadedFileResult] = []
        queued: list[StageMessage] = []

        # Trace context captured once, at the request, and attached to every job so
        # a stage running minutes later still joins this upload's trace.
        trace = inject_context({})

        # An archive is expanded before the per-file loop, so each document inside
        # it takes exactly the same route as a directly uploaded one - its own
        # hash, duplicate check, contract, job and failure mode. The archive
        # itself never becomes a contract.
        expanded, archive_results = await self._expand_archives(files)
        results.extend(archive_results)

        # Content hashes accepted so far *in this batch*.
        #
        # `_process_one` checks for a duplicate against the database, but nothing
        # in this batch is committed yet - so two byte-identical files arriving
        # together both pass that check, and the second then violates
        # `uq_contracts_project_id_sha256_hash` when the session flushes. That
        # surfaces as a 409 for the *whole* upload: every other document in it is
        # lost too.
        #
        # Rare when a person picks files by hand; routine in a ZIP, where the same
        # contract filed under two counterparties is an ordinary way to organise
        # an archive. Tracking them here makes the in-batch case behave exactly
        # like the cross-batch one - reported as a duplicate, everything else
        # unaffected.
        seen_hashes: dict[str, uuid.UUID] = {}

        for item in expanded:
            with span("upload.file", **{"cip.file_name": item.file_name}):
                result, message = await self._process_one(
                    project=project,
                    upload=item.upload,
                    options=options,
                    actor=actor,
                    ip=ip,
                    trace=trace,
                    archive=item.archive,
                    seen_hashes=seen_hashes,
                )
            results.append(result)
            if message is not None:
                queued.append(message)

        # Enqueue after every row is persisted. If a worker picked up a job before
        # its contract row was committed it would find nothing to process.
        #
        # `db=` hands the driver this transaction. A database-backed queue must
        # have it: the rows above are flushed, not committed, so a driver opening
        # its own session cannot see the job and its insert dies on the foreign
        # key - the file is stored and the upload is rejected. Passing it also
        # makes the queue row and the job row commit together, so a rollback here
        # cannot leave work queued for a contract that never existed. Broker
        # drivers ignore it.
        job_ids: list[uuid.UUID] = []
        if queued:
            await self.db.flush()
            client = get_queue_client()
            try:
                await client.enqueue_many(queued, db=self.db)
                job_ids = [message.job_id for message in queued]
            except Exception as exc:  # noqa: BLE001
                # Enqueue failure must be visible: mark the jobs FAILED rather than
                # leaving contracts stuck in QUEUED with no worker coming.
                logger.error("upload_enqueue_failed", error=str(exc), count=len(queued))
                for message in queued:
                    await self.jobs.mark_failed(
                        message.job_id,
                        stage=PipelineStage.VALIDATION,
                        error={
                            "code": ErrorCode.QUEUE_ERROR,
                            "message": "Could not queue the document for processing.",
                            "retryable": True,
                        },
                    )
                for result in results:
                    if result.status == "accepted":
                        result.status = "rejected"
                        result.error_code = ErrorCode.QUEUE_ERROR
                        result.message = (
                            "Stored, but could not be queued for processing. "
                            "Retry from the job list."
                        )

        accepted = sum(1 for r in results if r.status == "accepted")
        duplicates = sum(1 for r in results if r.status == "duplicate")
        rejected = sum(1 for r in results if r.status == "rejected")

        if accepted:
            await self.projects.touch_activity(project.id)
            await self.activities.record(
                project_id=project.id,
                activity_type="contracts_uploaded",
                summary=f"{accepted} contract(s) uploaded by {actor.full_name}",
                user_id=actor.id,
                entity_type="project",
                entity_id=project.id,
                payload={"accepted": accepted, "duplicates": duplicates, "rejected": rejected},
            )

        logger.info(
            "upload_completed",
            project_id=str(project.id),
            total=len(files),
            accepted=accepted,
            duplicates=duplicates,
            rejected=rejected,
        )

        return UploadResponse(
            project_id=project.id,
            total=len(files),
            accepted=accepted,
            duplicates=duplicates,
            rejected=rejected,
            files=results,
            job_ids=job_ids,
            message=self._summary_message(accepted, duplicates, rejected),
        )

    # =========================================================================
    # Archives
    # =========================================================================
    async def _expand_archives(
        self, files: list[UploadFile]
    ) -> tuple[list[_PendingUpload], list[UploadedFileResult]]:
        """Replace each archive in the batch with the documents inside it.

        Returns the flattened work list plus a result row for every archive that
        could not contribute anything - a corrupt ZIP, or one holding no supported
        documents. Those are the only two archive-level outcomes; once a member is
        extracted its fate is its own.
        """
        pending: list[_PendingUpload] = []
        failures: list[UploadedFileResult] = []

        for upload in files:
            name = upload.filename or "unnamed"
            if not _is_archive(name):
                pending.append(_PendingUpload(file_name=name, upload=upload))
                continue

            await upload.seek(0)
            raw = await upload.read()
            await upload.seek(0)

            # One id per archive, shared by everything inside it, so the UI can
            # group the batch. There is no archive table - the archive is not an
            # entity, just a provenance label the contracts carry.
            archive = _ArchiveRef(id=uuid.uuid4(), name=name)

            try:
                contents = await asyncio.to_thread(archive_service.extract, raw, archive_name=name)
            except ArchiveError as exc:
                logger.info("archive_rejected", file_name=name, reason=exc.code)
                failures.append(
                    UploadedFileResult(
                        file_name=name,
                        status="rejected",
                        error_code=exc.code,
                        message=exc.message,
                    )
                )
                continue

            for member in contents.members:
                pending.append(
                    _PendingUpload(
                        file_name=member.file_name,
                        upload=_upload_from_bytes(member.content, member.file_name),
                        archive=archive,
                    )
                )

            # The archive always gets a row of its own, even when everything in it
            # succeeded. The client matches results to the files it sent by name,
            # and the names it sent are archive names - omitting this leaves it
            # with an unmatched file it can only report as "no result returned".
            skipped = ", ".join(f"{n} ({why})" for n, why in contents.ignored[:5])
            more = "" if len(contents.ignored) <= 5 else f" and {len(contents.ignored) - 5} more"
            if contents.ignored:
                logger.info("archive_members_ignored", archive=name, count=len(contents.ignored))

            if not contents.members:
                # Nothing usable. The reason has to name the files, or the user is
                # told "no supported documents" about a ZIP they can see has files
                # in it.
                failures.append(
                    UploadedFileResult(
                        file_name=name,
                        status="rejected",
                        error_code=ErrorCode.UNSUPPORTED_FILE_TYPE,
                        message=(
                            f"No supported documents in this archive. Skipped: {skipped}{more}."
                            if contents.ignored
                            else "This archive is empty."
                        ),
                    )
                )
            else:
                count = len(contents.members)
                note = f"{count} document(s) extracted and queued."
                if contents.ignored:
                    note += f" {len(contents.ignored)} ignored: {skipped}{more}."
                failures.append(
                    UploadedFileResult(
                        file_name=name,
                        status="expanded",
                        message=note,
                        # Only when something was left out - the client shows a
                        # warning on this code, and a clean archive is not a
                        # warning.
                        error_code=(
                            ErrorCode.UNSUPPORTED_FILE_TYPE if contents.ignored else None
                        ),
                    )
                )

        return pending, failures

    @staticmethod
    def _summary_message(accepted: int, duplicates: int, rejected: int) -> str:
        parts = []
        if accepted:
            parts.append(f"{accepted} queued for processing")
        if duplicates:
            parts.append(f"{duplicates} already uploaded")
        if rejected:
            parts.append(f"{rejected} rejected")
        return "; ".join(parts) or "Nothing to do."

    # =========================================================================
    # Per-file
    # =========================================================================
    async def _process_one(
        self,
        *,
        project: Project,
        upload: UploadFile,
        options: UploadOptions,
        actor: User,
        ip: str | None,
        trace: dict[str, str],
        archive: _ArchiveRef | None = None,
        seen_hashes: dict[str, uuid.UUID] | None = None,
    ) -> tuple[UploadedFileResult, StageMessage | None]:
        file_name = upload.filename or "unnamed"

        # --- validate + hash (pass one) --------------------------------------
        try:
            validated = await self._validate(upload)
        except (UnsupportedFileTypeError, FileTooLargeError, ValidationError) as exc:
            metrics.uploads_total.labels(file_type="unknown", outcome="rejected").inc()
            logger.info("upload_rejected", file_name=file_name, reason=exc.code)
            return (
                UploadedFileResult(
                    file_name=file_name,
                    status="rejected",
                    error_code=exc.code,
                    message=exc.message,
                ),
                None,
            )

        # --- duplicate detection ---------------------------------------------
        # `duplicate_check` is a switch rather than a constant because re-uploading
        # the same file is the normal loop while the pipeline is being tuned. With
        # it off the hash is still computed and still recorded - only the
        # rejection is skipped - so turning it back on needs no backfill.
        existing = await self.contracts.get_by_hash(project.id, validated.sha256)
        duplicate_check = self.settings.upload.duplicate_check

        # A file identical to one earlier in this same batch. The row above is not
        # committed yet, so `get_by_hash` cannot see it, and letting this through
        # breaks the unique constraint at flush - taking the whole upload with it.
        #
        # Refused even when `duplicate_check` is off. That switch decides whether a
        # re-upload is *policy*-acceptable; this is a constraint that will be
        # violated either way, and "store it anyway" is not an available outcome.
        if seen_hashes is not None and validated.sha256 in seen_hashes:
            metrics.uploads_total.labels(
                file_type=validated.file_type.value, outcome="duplicate"
            ).inc()
            return (
                UploadedFileResult(
                    file_name=file_name,
                    status="duplicate",
                    size=validated.size,
                    sha256=validated.sha256,
                    existing_contract_id=seen_hashes[validated.sha256],
                    error_code=ErrorCode.DUPLICATE_DOCUMENT,
                    message=(
                        "An identical document was already included in this upload. "
                        "It was stored once."
                    ),
                ),
                None,
            )

        if existing is not None and duplicate_check and not options.replace_existing:
            metrics.uploads_total.labels(
                file_type=validated.file_type.value, outcome="duplicate"
            ).inc()
            return (
                UploadedFileResult(
                    file_name=file_name,
                    status="duplicate",
                    size=validated.size,
                    sha256=validated.sha256,
                    existing_contract_id=existing.id,
                    error_code=ErrorCode.DUPLICATE_DOCUMENT,
                    message="This document is already in the project.",
                ),
                None,
            )

        # --- convert Word to PDF ----------------------------------------------
        #
        # Before anything is stored, so a document that cannot be converted leaves
        # no half-built contract behind. The pipeline is PDF-only; this is the one
        # place that is true, and everything downstream is unchanged because of it.
        converted: UploadFile | None = None
        converted_validated: ValidatedFile | None = None
        if validated.file_type.needs_pdf_conversion:
            try:
                converted, converted_validated = await self._convert_to_pdf(upload, validated)
            except ConversionError as exc:
                metrics.uploads_total.labels(
                    file_type=validated.file_type.value, outcome="conversion_failed"
                ).inc()
                logger.warning(
                    "upload_conversion_failed",
                    file_name=file_name,
                    file_type=validated.file_type.value,
                    reason=exc.message,
                )
                return (
                    UploadedFileResult(
                        file_name=file_name,
                        status="rejected",
                        size=validated.size,
                        sha256=validated.sha256,
                        error_code=exc.code,
                        message=exc.message,
                    ),
                    None,
                )

        # --- persist ----------------------------------------------------------
        if existing is not None and options.replace_existing:
            contract, version = await self._add_version(existing, validated, upload, actor=actor)
        else:
            contract, version = await self._create_contract(
                project=project,
                validated=validated,
                upload=upload,
                options=options,
                actor=actor,
                converted=converted,
                converted_validated=converted_validated,
                archive=archive,
            )

        # --- create the job ---------------------------------------------------
        priority = self._resolve_priority(project, options)
        job = await self.jobs.create_job(
            contract_id=contract.id,
            project_id=project.id,
            priority=priority,
            triggered_by=actor.id,
            trace_context=trace,
            is_reprocess=existing is not None,
        )

        await self.audit.record(
            action=AuditAction.UPLOAD,
            entity_type="contract",
            entity_id=contract.id,
            entity_label=file_name,
            project_id=project.id,
            user_id=actor.id,
            user_email=actor.email,
            after={
                "file_name": file_name,
                "size": validated.size,
                "sha256": validated.sha256,
                "version": version,
                "job_id": str(job.id),
            },
            ip=ip,
        )

        # Recorded only once the contract exists, so a rejected file does not
        # shadow a later identical one that would have succeeded.
        if seen_hashes is not None:
            seen_hashes[validated.sha256] = contract.id

        metrics.uploads_total.labels(file_type=validated.file_type.value, outcome="accepted").inc()
        metrics.jobs_created_total.labels(priority=priority.value).inc()

        message = StageMessage(
            job_id=job.id,
            contract_id=contract.id,
            project_id=project.id,
            # Every job starts at validation: the pipeline's own file checks (virus
            # scan, structural integrity) are a stage, not part of the request.
            stage=PipelineStage.VALIDATION,
            priority=priority,
            trace=trace,
        )

        return (
            UploadedFileResult(
                file_name=file_name,
                contract_id=contract.id,
                job_id=job.id,
                status="accepted",
                size=validated.size,
                sha256=validated.sha256,
            ),
            message,
        )

    # =========================================================================
    # Validation
    # =========================================================================
    async def _validate(self, upload: UploadFile) -> ValidatedFile:
        """Pass one: type, size, signature and content hash. Rewinds when done."""
        file_name = upload.filename or "unnamed"
        extension = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""

        allowed = set(self.settings.upload.allowed_file_types)
        if extension not in allowed:
            raise UnsupportedFileTypeError(
                f"'{extension or 'unknown'}' files are not supported. "
                f"Allowed: {', '.join(sorted(allowed))}.",
                details={"file_name": file_name, "extension": extension},
            )

        try:
            file_type = FileType(extension)
        except ValueError as exc:
            # An archive reaching here means `_expand_archives` did not unpack it,
            # which would be a bug rather than bad input - a ZIP is never a
            # document and must never become a contract.
            raise UnsupportedFileTypeError(
                f"'{extension}' cannot be stored as a document.",
                details={"extension": extension, "file_name": file_name},
            ) from exc

        await upload.seek(0)

        digest_source = __import__("hashlib").sha256()
        size = 0
        header = b""
        limit = self.settings.upload.max_upload_size_bytes

        while True:
            chunk = await upload.read(_HASH_CHUNK)
            if not chunk:
                break
            if not header:
                header = chunk[:8]
            size += len(chunk)
            if size > limit:
                raise FileTooLargeError(
                    f"'{file_name}' exceeds the {self.settings.upload.max_upload_size_mb} MB limit.",
                    details={"file_name": file_name, "limit_bytes": limit},
                )
            digest_source.update(chunk)

        await upload.seek(0)

        if size == 0:
            raise ValidationError(f"'{file_name}' is empty.", details={"file_name": file_name})

        # Content sniffing: the declared extension is not evidence.
        signatures = _SIGNATURES[file_type]
        if not any(header.startswith(signature) for signature in signatures):
            raise UnsupportedFileTypeError(
                f"'{file_name}' is not a valid {file_type.value.upper()} file - its "
                "contents do not match its extension.",
                details={"file_name": file_name, "declared_type": file_type.value},
            )
        # A `.docx` is a ZIP, so the header check above cannot tell one from a
        # plain archive that was renamed. Catching it here rather than letting it
        # reach LibreOffice turns a confusing conversion failure into a precise
        # rejection at the point the user can act on it.
        if file_type is FileType.DOCX and not await self._looks_like_docx(upload):
            raise UnsupportedFileTypeError(
                f"'{file_name}' is a ZIP archive but not a Word document. "
                "Rename it to .zip to upload it as an archive.",
                details={"file_name": file_name, "declared_type": file_type.value},
            )

        return ValidatedFile(
            file_name=file_name,
            file_type=file_type,
            size=size,
            sha256=digest_source.hexdigest(),
            content_type=_CONTENT_TYPES[file_type],
        )

    @staticmethod
    async def _looks_like_docx(upload: UploadFile) -> bool:
        """Does this ZIP contain the part every Word document has?

        `word/document.xml` is present in every OOXML word-processing file and in
        no other archive by accident. Read from the central directory, so nothing
        is decompressed to answer the question.
        """
        await upload.seek(0)
        raw = await upload.read()
        await upload.seek(0)
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                return any(name.startswith("word/") for name in archive.namelist())
        except (zipfile.BadZipFile, OSError):
            return False

    # =========================================================================
    # Conversion
    # =========================================================================
    async def _convert_to_pdf(
        self, upload: UploadFile, validated: ValidatedFile
    ) -> tuple[UploadFile, ValidatedFile]:
        """Turn a Word upload into the PDF the pipeline will read.

        Returns the PDF as an `UploadFile` plus its own `ValidatedFile`, because
        the converted file needs its own size, hash and content type - the
        original's describe different bytes.

        The hash deliberately stays the *original's* on the contract row: that is
        what duplicate detection compares, and two uploads of the same DOCX must
        collide even though LibreOffice does not produce byte-identical PDFs from
        one run to the next.
        """
        await upload.seek(0)
        source = await upload.read()
        await upload.seek(0)

        result = await DocumentConverter().to_pdf(source, validated.file_name, validated.file_type)
        pdf = _upload_from_bytes(result.content, result.file_name)

        return pdf, ValidatedFile(
            file_name=result.file_name,
            file_type=FileType.PDF,
            size=len(result.content),
            sha256=hashlib.sha256(result.content).hexdigest(),
            content_type=_CONTENT_TYPES[FileType.PDF],
        )

    # =========================================================================
    # Persistence
    # =========================================================================
    async def _create_contract(
        self,
        *,
        project: Project,
        validated: ValidatedFile,
        upload: UploadFile,
        options: UploadOptions,
        actor: User,
        converted: UploadFile | None = None,
        converted_validated: ValidatedFile | None = None,
        archive: _ArchiveRef | None = None,
    ) -> tuple[Contract, int]:
        """Store the file and create the contract plus its first version row.

        For a Word upload both files are stored: the original, so it stays
        downloadable, and the converted PDF, which is what the pipeline and the
        evidence viewer use. `storage_path` points at whichever of the two is the
        PDF, so nothing downstream had to learn about the distinction.
        """
        contract_id = uuid.uuid4()
        original_key = StorageKey.contract_file(project.id, contract_id, 1, validated.file_name)

        with log_context(contract_id=str(contract_id), project_id=str(project.id)):
            await self._store(original_key, upload, validated)

            converted_key: str | None = None
            if converted is not None and converted_validated is not None:
                converted_key = StorageKey.contract_file(
                    project.id, contract_id, 1, pdf_name_for(validated.file_name)
                )
                await self._store(converted_key, converted, converted_validated)

            # The PDF, always. For a PDF upload that is the original.
            key = converted_key or original_key
            processed = converted_validated or validated

            contract = await self.contracts.create(
                id=contract_id,
                project_id=project.id,
                uploaded_by=actor.id,
                original_file_name=validated.file_name,
                storage_path=key,
                original_file_path=original_key,
                converted_file_path=converted_key,
                processing_file_path=key,
                original_file_type=validated.file_type,
                source_archive_id=archive.id if archive else None,
                source_archive_name=archive.name if archive else None,
                # `file_type` describes the file being *processed*, so it is PDF
                # for a converted document. Parser selection and every existing
                # type filter read this, and they must keep seeing a PDF.
                file_type=processed.file_type,
                file_size=processed.size,
                # The *original's* hash: this is what duplicate detection compares,
                # and LibreOffice does not produce identical bytes twice, so
                # hashing the PDF would let the same DOCX in repeatedly.
                sha256_hash=validated.sha256,
                # The processed file's hash, which validation re-computes to prove
                # storage has not corrupted it. Equal to the above for a PDF.
                processing_sha256=processed.sha256,
                mime_type=processed.content_type,
                # Title starts as the filename and is replaced by the extracted
                # title once processing completes.
                title=None,
                # `str(...)`, not `.value`: `use_enum_values=True` on BaseSchema
                # means this field is already a plain string, and `.value` would
                # raise. See `_resolve_priority` for the same trap.
                agreement_type=str(options.agreement_type) if options.agreement_type else None,
                status=ContractStatus.UPLOADED,
                current_version=1,
                tags=options.tags,
                notes=options.notes,
            )

            await self.versions.create(
                contract_id=contract.id,
                project_id=project.id,
                version=1,
                # Same file `storage_path` names, so the size describes the same
                # bytes. `sha256_hash` stays the original's, matching the contract
                # row - it is the identity of what the user uploaded, and the
                # version history is a record of uploads, not of conversions.
                storage_path=key,
                sha256_hash=validated.sha256,
                file_size=processed.size,
                original_file_name=validated.file_name,
                uploaded_by=actor.id,
                change_note="Initial upload.",
            )

        return contract, 1

    async def _add_version(
        self,
        contract: Contract,
        validated: ValidatedFile,
        upload: UploadFile,
        *,
        actor: User,
    ) -> tuple[Contract, int]:
        """Add a new version of an existing contract.

        The prior version's bytes are kept: earlier extractions reference them, and
        overwriting would break the provenance chain.
        """
        version = await self.contracts.next_version(contract.id)
        key = StorageKey.contract_file(
            contract.project_id, contract.id, version, validated.file_name
        )

        await self._store(key, upload, validated)

        await self.versions.create(
            contract_id=contract.id,
            project_id=contract.project_id,
            version=version,
            storage_path=key,
            sha256_hash=validated.sha256,
            file_size=validated.size,
            original_file_name=validated.file_name,
            uploaded_by=actor.id,
            change_note="Replacement upload.",
        )

        # Point the contract at the new version and reset it for reprocessing.
        await self.contracts.update(
            contract,
            storage_path=key,
            sha256_hash=validated.sha256,
            file_size=validated.size,
            original_file_name=validated.file_name,
            current_version=version,
            status=ContractStatus.UPLOADED,
            page_count=None,
            needs_review=False,
            processed_at=None,
        )
        logger.info(
            "contract_version_added",
            contract_id=str(contract.id),
            version=version,
        )
        return contract, version

    async def _store(self, key: str, upload: UploadFile, validated: ValidatedFile) -> None:
        """Pass two: stream the file into object storage and verify the hash."""
        await upload.seek(0)
        result = await self.storage.put_stream(
            key,
            upload,  # UploadFile satisfies the async read protocol
            content_type=validated.content_type,
            length=validated.size,
            metadata={"original_name": validated.file_name, "sha256": validated.sha256},
        )
        # Guard against a truncated or altered upload between the two passes.
        if result.checksum and result.checksum != validated.sha256:
            raise ValidationError(
                f"'{validated.file_name}' changed while uploading. Please try again.",
                details={"expected": validated.sha256, "stored": result.checksum},
            )

    def _resolve_priority(self, project: Project, options: UploadOptions) -> JobPriority:
        """Explicit option wins, then the project default, then normal.

        ``BaseSchema`` sets ``use_enum_values=True``, so ``options.priority`` arrives
        as a plain ``str`` rather than a ``JobPriority``. Two consequences, both of
        which this coercion exists to prevent:

        * ``"normal" is not JobPriority.NORMAL`` is always true, so an identity check
          here returned early on every upload and the project's configured
          ``processing_priority`` was never read;
        * the caller records a metric label from ``priority.value``, which raises
          ``AttributeError`` on a str and turned every upload into a 500.
        """
        requested = JobPriority(options.priority)
        if requested != JobPriority.NORMAL:
            return requested
        configured = project.setting("processing_priority")
        if configured:
            try:
                return JobPriority(configured)
            except ValueError:
                logger.warning(
                    "invalid_project_priority",
                    project_id=str(project.id),
                    value=configured,
                )
        return JobPriority.NORMAL

    # =========================================================================
    # Requeue
    # =========================================================================
    async def requeue(
        self,
        *,
        job_id: uuid.UUID,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        stage: PipelineStage,
        priority: JobPriority = JobPriority.NORMAL,
        attempt: int = 1,
        options: dict[str, Any] | None = None,
    ) -> str:
        """Queue a single stage - used by retry and reprocess."""
        message = StageMessage(
            job_id=job_id,
            contract_id=contract_id,
            project_id=project_id,
            stage=stage,
            attempt=attempt,
            priority=priority,
            trace=inject_context({}),
            options=options or {},
        )
        # Same reasoning as `upload` above: a reprocess creates its job in this
        # transaction, so the driver has to enqueue inside it.
        return await get_queue_client().enqueue(message, db=self.db)


__all__ = ["UploadService", "ValidatedFile"]

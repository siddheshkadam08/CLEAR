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

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import get_settings
from app.core.enums import (
    AuditAction,
    ContractStatus,
    FileType,
    JobPriority,
    PipelineStage,
)
from app.core.errors import (
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
from app.services.audit import AuditService
from app.storage import StorageKey, get_storage

logger = get_logger(__name__)

#: Magic-byte signatures. Extension and client-supplied content type are both
#: attacker-controlled, so the file's own header is what decides its type.
_SIGNATURES: dict[FileType, tuple[bytes, ...]] = {
    FileType.PDF: (b"%PDF-",),
    # DOCX is a ZIP container; the local file header is the only reliable marker.
    FileType.DOCX: (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
}

_CONTENT_TYPES: dict[FileType, str] = {
    FileType.PDF: "application/pdf",
    FileType.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_HASH_CHUNK = 1024 * 1024


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

        for upload in files:
            with span("upload.file", **{"cip.file_name": upload.filename or "unknown"}):
                result, message = await self._process_one(
                    project=project,
                    upload=upload,
                    options=options,
                    actor=actor,
                    ip=ip,
                    trace=trace,
                )
            results.append(result)
            if message is not None:
                queued.append(message)

        # Enqueue after every row is persisted. If a worker picked up a job before
        # its contract row was committed it would find nothing to process.
        job_ids: list[uuid.UUID] = []
        if queued:
            await self.db.flush()
            client = get_queue_client()
            try:
                await client.enqueue_many(queued)
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
        existing = await self.contracts.get_by_hash(project.id, validated.sha256)
        if existing is not None and not options.replace_existing:
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

        # --- persist ----------------------------------------------------------
        if existing is not None and options.replace_existing:
            contract, version = await self._add_version(existing, validated, upload, actor=actor)
        else:
            contract, version = await self._create_contract(
                project=project, validated=validated, upload=upload, options=options, actor=actor
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
            raise UnsupportedFileTypeError(details={"extension": extension}) from exc

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

        return ValidatedFile(
            file_name=file_name,
            file_type=file_type,
            size=size,
            sha256=digest_source.hexdigest(),
            content_type=_CONTENT_TYPES[file_type],
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
    ) -> tuple[Contract, int]:
        """Store the file and create the contract plus its first version row."""
        contract_id = uuid.uuid4()
        key = StorageKey.contract_file(project.id, contract_id, 1, validated.file_name)

        with log_context(contract_id=str(contract_id), project_id=str(project.id)):
            await self._store(key, upload, validated)

            contract = await self.contracts.create(
                id=contract_id,
                project_id=project.id,
                uploaded_by=actor.id,
                original_file_name=validated.file_name,
                storage_path=key,
                file_type=validated.file_type,
                file_size=validated.size,
                sha256_hash=validated.sha256,
                mime_type=validated.content_type,
                # Title starts as the filename and is replaced by the extracted
                # title once processing completes.
                title=None,
                agreement_type=options.agreement_type.value if options.agreement_type else None,
                status=ContractStatus.UPLOADED,
                current_version=1,
                tags=options.tags,
                notes=options.notes,
            )

            await self.versions.create(
                contract_id=contract.id,
                project_id=project.id,
                version=1,
                storage_path=key,
                sha256_hash=validated.sha256,
                file_size=validated.size,
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
        """Explicit option wins, then the project default, then normal."""
        if options.priority is not JobPriority.NORMAL:
            return options.priority
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
        return await get_queue_client().enqueue(message)


__all__ = ["UploadService", "ValidatedFile"]

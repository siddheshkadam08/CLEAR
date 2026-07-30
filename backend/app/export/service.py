"""Export orchestration (§23, FR-6).

Exports run on the queue rather than in the request. A project-wide export across
thousands of contracts is not a 30-second HTTP response, and holding a database
connection and a request worker open for it starves everything else. The endpoint
creates a row and returns an id; this service does the work; the client polls.

The three phases are separated because they fail differently:

``create``   validates the request and records intent. Cheap, synchronous, and the
             only phase the user waits on.
``run``      loads, renders and uploads. Slow, retryable, and the only phase that
             can fail for reasons the user cannot fix.
``download`` mints a short-lived URL. Audited every time, because an export file
             is a copy of contract data leaving the platform.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AuditAction, ExportFormat, ExportStatus, SearchScope
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.export.base import ExportArtifact, ExportDataset, get_exporter
from app.export.dataset import ExportDatasetBuilder
from app.models.export import ExportJob
from app.schemas.contract import ContractFilterParams
from app.storage import get_storage
from app.storage.base import StorageKey

logger = get_logger(__name__)

#: How long a finished export stays downloadable. Exports are copies of contract
#: data sitting in object storage; keeping them indefinitely turns every export
#: into a permanent second copy outside the contract's own lifecycle.
DEFAULT_TTL_HOURS = 48

#: Signed download URLs are short-lived even within that window - the file is the
#: durable artifact, the URL is not.
DOWNLOAD_URL_TTL_SECONDS = 300


class ExportService:
    """Creates, runs and serves export jobs."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ----------------------------------------------------------------- create
    async def create(
        self,
        *,
        user_id: uuid.UUID,
        user_email: str,
        project_id: uuid.UUID | None,
        scope: SearchScope,
        scope_ref: uuid.UUID | None,
        export_format: ExportFormat,
        entities: Sequence[str],
        filters: dict[str, Any] | None,
        fields: dict[str, Any] | None,
    ) -> ExportJob:
        """Record the request. Raises before queueing if the format has no renderer."""
        # Resolving the exporter now rather than in the worker: "xlsx is spelled
        # wrong" should be a 400 the user sees immediately, not a job that sits in
        # the queue and fails four minutes later.
        get_exporter(export_format)

        if scope is SearchScope.PROJECT and project_id is None:
            raise ValidationError(
                "A project-scoped export needs a project_id.",
                details={"field": "project_id"},
            )
        if scope is SearchScope.CONTRACT and scope_ref is None:
            raise ValidationError(
                "A contract-scoped export needs the contract id in scope_ref.",
                details={"field": "scope_ref"},
            )

        job = ExportJob(
            project_id=project_id,
            requested_by=user_id,
            scope=scope,
            scope_ref=scope_ref,
            export_format=export_format,
            status=ExportStatus.QUEUED,
            entities=list(entities),
            fields=dict(fields or {}),
            filters=dict(filters or {}),
            progress=0,
        )
        self.db.add(job)
        await self.db.flush()

        from app.services.audit import AuditService

        await AuditService(self.db).record(
            action=AuditAction.EXPORT,
            entity_type="export_job",
            entity_id=job.id,
            entity_label=f"{export_format.value} export",
            project_id=project_id,
            user_id=user_id,
            user_email=user_email,
            after={
                "scope": scope.value,
                "format": export_format.value,
                "entities": list(entities),
                "filters": dict(filters or {}),
            },
        )

        logger.info(
            "export_created",
            export_id=str(job.id),
            format=export_format.value,
            scope=scope.value,
            project_id=str(project_id) if project_id else None,
        )
        return job

    # -------------------------------------------------------------------- run
    async def run(
        self,
        export_id: uuid.UUID,
        *,
        project_ids: Sequence[uuid.UUID],
        permissions: frozenset[str],
        is_system_admin: bool = False,
    ) -> ExportJob:
        """Build, render and store the file.

        ``project_ids`` is passed in rather than read from the job row: the job
        records *what was asked for*, and the caller is responsible for supplying
        the scope the requesting user is actually entitled to. Re-deriving it here
        from the row would mean a job that outlives a membership change still
        exports what the user can no longer see.
        """
        job = await self._get(export_id)

        if job.status is ExportStatus.COMPLETED:
            # Idempotent: a re-delivered queue message must not overwrite a file a
            # user may already be downloading.
            logger.info("export_already_complete", export_id=str(export_id))
            return job
        if job.status is ExportStatus.RUNNING:
            raise ConflictError("This export is already running.")

        job.status = ExportStatus.RUNNING
        job.started_at = datetime.now(UTC)
        job.progress = 5
        job.error = None
        await self.db.flush()

        try:
            dataset = await self._build_dataset(job, project_ids, permissions, is_system_admin)
            job.progress = 60
            await self.db.flush()

            artifact = await self._render(job.export_format, dataset)
            job.progress = 85
            await self.db.flush()

            await self._store(job, artifact)

            job.status = ExportStatus.COMPLETED
            job.progress = 100
            job.finished_at = datetime.now(UTC)
            job.expires_at = job.finished_at + timedelta(hours=DEFAULT_TTL_HOURS)
            await self.db.flush()

            logger.info(
                "export_completed",
                export_id=str(export_id),
                rows=artifact.row_count,
                bytes=artifact.size,
            )
            return job

        except Exception as exc:
            job.status = ExportStatus.FAILED
            job.finished_at = datetime.now(UTC)
            job.error = {
                "type": type(exc).__name__,
                "message": str(exc)[:1000],
            }
            await self.db.flush()
            logger.error("export_failed", export_id=str(export_id), error=str(exc))
            raise

    async def _build_dataset(
        self,
        job: ExportJob,
        project_ids: Sequence[uuid.UUID],
        permissions: frozenset[str],
        is_system_admin: bool,
    ) -> ExportDataset:
        # A project-scoped job narrows to that one project; it can never widen the
        # caller's scope, because the intersection is taken rather than a union.
        scoped = list(project_ids)
        if job.project_id is not None:
            scoped = [pid for pid in scoped if pid == job.project_id]

        contract_ids: list[uuid.UUID] | None = None
        if job.scope is SearchScope.CONTRACT and job.scope_ref is not None:
            contract_ids = [job.scope_ref]

        filters = ContractFilterParams.model_validate(job.filters or {})

        requester_email = getattr(getattr(job, "requester", None), "email", None) or str(
            job.requested_by
        )

        builder = ExportDatasetBuilder(
            self.db,
            project_ids=scoped,
            permissions=permissions,
            is_system_admin=is_system_admin,
        )
        return await builder.build(
            export_id=job.id,
            entities=list(job.entities or []),
            filters=filters,
            contract_ids=contract_ids,
            generated_by=requester_email,
            scope_label=self._scope_label(job),
            field_selection={
                key: list(value)
                for key, value in (job.fields or {}).items()
                if isinstance(value, list)
            },
        )

    @staticmethod
    async def _render(export_format: ExportFormat, dataset: ExportDataset) -> ExportArtifact:
        """Render off the event loop.

        Building a workbook is CPU-bound. Running it inline would block every other
        request this process is serving for the duration, which on a large export is
        seconds, not milliseconds.
        """
        exporter = get_exporter(export_format)
        return await asyncio.to_thread(exporter.render, dataset)

    async def _store(self, job: ExportJob, artifact: ExportArtifact) -> None:
        storage = get_storage()
        key = StorageKey.export(job.project_id, job.id, artifact.file_name)
        await storage.put_bytes(
            key,
            artifact.content,
            content_type=artifact.content_type,
            metadata={
                "export_id": str(job.id),
                "requested_by": str(job.requested_by),
            },
        )
        job.storage_path = key
        job.file_name = artifact.file_name
        job.file_size = artifact.size
        job.row_count = artifact.row_count
        # Lets a download be verified against what was written, and makes an
        # accidental overwrite detectable rather than invisible.
        job.checksum = hashlib.sha256(artifact.content).hexdigest()

    # --------------------------------------------------------------- download
    async def download_url(
        self,
        export_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        user_email: str,
    ) -> tuple[str, ExportJob]:
        """Mint a short-lived URL and record the download.

        Every call is audited. An export file is contract data leaving the
        platform, so who took a copy and when is exactly the question an audit is
        asked afterwards.
        """
        job = await self._get(export_id)

        if job.requested_by != user_id:
            # Same shape as a genuine miss: an export id is guessable-adjacent, and
            # distinguishing "not yours" from "does not exist" would confirm that
            # someone else's export exists.
            raise NotFoundError("Export", export_id)
        if job.status is ExportStatus.EXPIRED or (
            job.expires_at is not None and job.expires_at <= datetime.now(UTC)
        ):
            raise NotFoundError("Export", export_id)
        if job.status is not ExportStatus.COMPLETED or not job.storage_path:
            raise ConflictError("This export is not ready to download yet.")

        storage = get_storage()
        url = await storage.signed_url(
            job.storage_path,
            expires_in=DOWNLOAD_URL_TTL_SECONDS,
            download_filename=job.file_name,
        )

        job.downloaded_at = datetime.now(UTC)
        job.download_count += 1

        from app.services.audit import AuditService

        await AuditService(self.db).record(
            action=AuditAction.DOWNLOAD,
            entity_type="export_job",
            entity_id=job.id,
            entity_label=job.file_name,
            project_id=job.project_id,
            user_id=user_id,
            user_email=user_email,
            after={"download_count": job.download_count},
        )
        await self.db.flush()
        return url, job

    # ---------------------------------------------------------------- recover
    async def recover_stalled(self, *, older_than_minutes: int = 30, limit: int = 50) -> int:
        """Return exports abandoned by a restarted process to ``queued``.

        A background task dies with its process. The ``export_jobs`` row is the
        durable record, so a job left in ``running`` with no progress for longer
        than any real export takes is one whose worker is gone. Resetting it to
        ``queued`` makes it eligible to be picked up again instead of sitting in a
        state the user polls forever.

        Deliberately conservative: only ``running`` rows past the threshold, and
        the attempt is recorded on the row so a job that stalls repeatedly is
        visible rather than looping silently.
        """
        cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
        rows = (
            (
                await self.db.execute(
                    select(ExportJob)
                    .where(
                        ExportJob.status == ExportStatus.RUNNING,
                        ExportJob.started_at.isnot(None),
                        ExportJob.started_at <= cutoff,
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

        for job in rows:
            job.status = ExportStatus.QUEUED
            job.progress = 0
            job.started_at = None
            job.error = {
                "type": "Stalled",
                "message": (
                    f"No progress for {older_than_minutes} minutes; the worker "
                    "was presumed lost and the export was requeued."
                ),
            }

        if rows:
            await self.db.flush()
            logger.warning("export_recovered_stalled", count=len(rows))
        return len(rows)

    # ------------------------------------------------------------------ purge
    async def purge_expired(self, *, limit: int = 200) -> int:
        """Delete expired export files and mark their rows.

        Run by the scheduler. The row is kept - it is the audit record of an export
        having been taken - while the file it points at is removed.
        """
        now = datetime.now(UTC)
        rows = (
            (
                await self.db.execute(
                    select(ExportJob)
                    .where(
                        ExportJob.status == ExportStatus.COMPLETED,
                        ExportJob.expires_at.isnot(None),
                        ExportJob.expires_at <= now,
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

        storage = get_storage()
        purged = 0
        for job in rows:
            if job.storage_path:
                try:
                    await storage.delete(job.storage_path)
                except Exception as exc:  # noqa: BLE001 - one bad key must not stop the sweep
                    logger.warning("export_purge_failed", export_id=str(job.id), error=str(exc))
                    continue
            job.status = ExportStatus.EXPIRED
            job.storage_path = None
            purged += 1

        if purged:
            await self.db.flush()
            logger.info("export_purge_completed", purged=purged)
        return purged

    # ---------------------------------------------------------------- helpers
    async def _get(self, export_id: uuid.UUID) -> ExportJob:
        job = (
            await self.db.execute(select(ExportJob).where(ExportJob.id == export_id))
        ).scalar_one_or_none()
        if job is None:
            raise NotFoundError("Export", export_id)
        return job

    @staticmethod
    def _scope_label(job: ExportJob) -> str:
        if job.scope is SearchScope.CONTRACT:
            return f"Single contract ({job.scope_ref})"
        if job.scope is SearchScope.PROJECT:
            return "One project"
        return "All projects the requester belongs to"


__all__ = ["DEFAULT_TTL_HOURS", "DOWNLOAD_URL_TTL_SECONDS", "ExportService"]

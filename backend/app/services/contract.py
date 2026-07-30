"""Contract read/update service and document access for the viewer."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.enums import AuditAction, ContractStatus, JobState
from app.core.errors import ConflictError, ErrorCode, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.contract import Contract, ContractMetadata
from app.models.identity import User
from app.models.processing import ProcessingJob
from app.repositories.contract import (
    ContractMetadataRepository,
    ContractRepository,
    ContractVersionRepository,
)
from app.repositories.processing import ProcessingJobRepository
from app.repositories.project import ProjectActivityRepository, ProjectRepository
from app.schemas.common import Paginated, ProjectRef, UserRef
from app.schemas.contract import (
    ContractFilterParams,
    ContractListItem,
    ContractMetadataResponse,
    ContractMetadataUpdate,
    ContractResponse,
    ContractUpdateRequest,
    ContractVersionResponse,
    FileAccessResponse,
    ProcessingSummary,
)
from app.services.audit import AuditService, snapshot
from app.storage import get_storage

logger = get_logger(__name__)


class ContractService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.settings = get_settings()
        self.contracts = ContractRepository(db)
        self.metadata = ContractMetadataRepository(db)
        self.versions = ContractVersionRepository(db)
        self.jobs = ProcessingJobRepository(db)
        self.projects = ProjectRepository(db)
        self.activities = ProjectActivityRepository(db)
        self.audit = AuditService(db)
        self.storage = get_storage()

    # =========================================================================
    # List
    # =========================================================================
    async def list_contracts(
        self,
        *,
        project_ids: Sequence[uuid.UUID],
        filters: ContractFilterParams | None = None,
        page: int = 1,
        size: int = 25,
        sort_by: str | None = None,
        sort_dir: str = "desc",
    ) -> Paginated[ContractListItem]:
        """The Contract Repository listing.

        ``project_ids`` is the caller's accessible set, so a listing can never
        include a project the caller is not a member of.
        """
        if not project_ids:
            return Paginated.build([], page=page, size=size, total=0)

        rows, total = await self.contracts.paginate_filtered(
            project_ids, filters, page=page, size=size, sort_by=sort_by, sort_dir=sort_dir
        )

        # One query for the whole page's job state rather than one per row.
        jobs = await self.contracts.latest_jobs([row.id for row in rows])

        items = [self._to_list_item(row, jobs.get(row.id)) for row in rows]
        return Paginated.build(items, page=page, size=size, total=total)

    def _to_list_item(self, contract: Contract, job: ProcessingJob | None) -> ContractListItem:
        meta = contract.contract_metadata
        return ContractListItem(
            id=contract.id,
            project_id=contract.project_id,
            title=contract.title,
            original_file_name=contract.original_file_name,
            contract_number=contract.contract_number,
            agreement_type=contract.agreement_type,
            file_type=str(contract.file_type),
            file_size=contract.file_size,
            page_count=contract.page_count,
            status=str(contract.status),
            needs_review=contract.needs_review,
            party_a=meta.party_a if meta else None,
            party_b=meta.party_b if meta else None,
            vendor=meta.vendor if meta else None,
            effective_date=meta.effective_date if meta else None,
            expiration_date=meta.expiration_date if meta else None,
            contract_value=meta.contract_value if meta else None,
            currency=meta.currency if meta else None,
            risk_score=meta.risk_score if meta else None,
            risk_band=str(meta.risk_band) if meta and meta.risk_band else None,
            clause_count=meta.clause_count if meta else 0,
            missing_clause_count=len(meta.missing_mandatory_clauses) if meta else 0,
            processing=self._processing_summary(job),
            uploaded_by=UserRef(
                id=contract.uploader.id,
                email=contract.uploader.email,
                full_name=contract.uploader.full_name,
                avatar_url=contract.uploader.avatar_url,
            )
            if contract.uploader is not None
            else None,
            created_at=contract.created_at,
            processed_at=contract.processed_at,
        )

    @staticmethod
    def _processing_summary(job: ProcessingJob | None) -> ProcessingSummary | None:
        if job is None:
            return None
        error = job.error or {}
        return ProcessingSummary(
            job_id=job.id,
            state=str(job.state),
            current_stage=str(job.current_stage) if job.current_stage else None,
            progress=job.progress,
            retry_count=job.retry_count,
            error_message=error.get("message"),
            error_stage=error.get("stage"),
            # Only a failed job with a retryable error offers a retry button.
            is_retryable=bool(job.state is JobState.FAILED and error.get("retryable", True)),
            started_at=job.started_at,
            finished_at=job.finished_at,
            duration_ms=job.duration_ms,
        )

    # =========================================================================
    # Detail
    # =========================================================================
    async def get_contract(self, contract_id: uuid.UUID, project_id: uuid.UUID) -> ContractResponse:
        contract = await self.contracts.get_detail(contract_id, project_id)
        if contract is None:
            raise NotFoundError("Contract", contract_id)

        job = await self.contracts.latest_job(contract.id)
        counts = await self._knowledge_counts(contract.id)
        profile_name = await self._profile_name(contract.profile_id)

        return ContractResponse(
            id=contract.id,
            project=ProjectRef(
                id=contract.project.id,
                name=contract.project.name,
                slug=contract.project.slug,
            ),
            title=contract.title,
            original_file_name=contract.original_file_name,
            contract_number=contract.contract_number,
            agreement_type=contract.agreement_type,
            agreement_subtype=contract.agreement_subtype,
            file_type=str(contract.file_type),
            file_size=contract.file_size,
            page_count=contract.page_count,
            sha256_hash=contract.sha256_hash,
            mime_type=contract.mime_type,
            language=contract.language,
            status=str(contract.status),
            needs_review=contract.needs_review,
            current_version=contract.current_version,
            profile_id=contract.profile_id,
            profile_version=contract.profile_version,
            profile_name=profile_name,
            classification_confidence=contract.classification_confidence,
            contract_metadata=self._metadata_response(contract.contract_metadata),
            processing=self._processing_summary(job),
            tags=list(contract.tags or []),
            notes=contract.notes,
            uploaded_by=UserRef(
                id=contract.uploader.id,
                email=contract.uploader.email,
                full_name=contract.uploader.full_name,
                avatar_url=contract.uploader.avatar_url,
            )
            if contract.uploader is not None
            else None,
            created_at=contract.created_at,
            updated_at=contract.updated_at,
            processed_at=contract.processed_at,
            counts=counts,
        )

    @staticmethod
    def _metadata_response(
        meta: ContractMetadata | None,
    ) -> ContractMetadataResponse | None:
        if meta is None:
            return None
        return ContractMetadataResponse.model_validate(meta)

    async def _profile_name(self, profile_id: uuid.UUID | None) -> str | None:
        if profile_id is None:
            return None
        from app.models.profile import DocumentProfile

        return (
            await self.db.execute(
                select(DocumentProfile.name).where(DocumentProfile.id == profile_id)
            )
        ).scalar_one_or_none()

    async def _knowledge_counts(self, contract_id: uuid.UUID) -> dict[str, int]:
        """Counts for the tab badges on Contract Details."""
        from app.models.chunk import Chunk
        from app.models.knowledge import Clause, Entity, KeyDate, Obligation, Risk

        counts: dict[str, int] = {}
        for label, model in (
            ("clauses", Clause),
            ("entities", Entity),
            ("obligations", Obligation),
            ("risks", Risk),
            ("key_dates", KeyDate),
            ("chunks", Chunk),
        ):
            counts[label] = int(
                (
                    await self.db.execute(
                        select(func.count())
                        .select_from(model)
                        .where(model.contract_id == contract_id)
                    )
                ).scalar()
                or 0
            )
        return counts

    async def list_versions(
        self, contract_id: uuid.UUID, project_id: uuid.UUID
    ) -> list[ContractVersionResponse]:
        await self.contracts.get_scoped_or_404(contract_id, project_id, resource="Contract")
        return [
            ContractVersionResponse.model_validate(version)
            for version in await self.versions.list_for_contract(contract_id)
        ]

    # =========================================================================
    # Update
    # =========================================================================
    async def update_contract(
        self,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        payload: ContractUpdateRequest,
        *,
        actor: User,
        ip: str | None = None,
    ) -> ContractResponse:
        contract = await self.contracts.get_scoped_or_404(
            contract_id, project_id, resource="Contract"
        )
        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        if not changes:
            return await self.get_contract(contract_id, project_id)

        before = snapshot(contract)
        await self.contracts.update(contract, **changes)
        await self._record_field_history(contract, before, changes, actor=actor, source="user")

        await self.audit.record(
            action=AuditAction.UPDATE,
            entity_type="contract",
            entity_id=contract.id,
            entity_label=contract.display_title,
            project_id=project_id,
            user_id=actor.id,
            user_email=actor.email,
            before=before,
            after=snapshot(contract),
            ip=ip,
        )
        return await self.get_contract(contract_id, project_id)

    async def update_metadata(
        self,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        payload: ContractMetadataUpdate,
        *,
        actor: User,
        ip: str | None = None,
    ) -> ContractMetadataResponse:
        """Correct extracted metadata by hand.

        Recorded with ``source='user'`` so a human override is always
        distinguishable from an AI extraction in the history (§7.9).
        """
        contract = await self.contracts.get_scoped_or_404(
            contract_id, project_id, resource="Contract"
        )
        changes = payload.model_dump(exclude_unset=True)
        changes = {k: v for k, v in changes.items() if v is not None}
        if not changes:
            meta = await self.metadata.get_for_contract(contract_id)
            return self._metadata_response(meta) or ContractMetadataResponse()

        existing = await self.metadata.get_for_contract(contract_id)
        before = snapshot(existing) if existing else {}

        # Validate the corrected values as strictly as extraction is validated.
        effective = changes.get("effective_date") or (existing.effective_date if existing else None)
        expiration = changes.get("expiration_date") or (
            existing.expiration_date if existing else None
        )
        if effective and expiration and effective > expiration:
            raise ValidationError(
                "The effective date must not be after the expiration date.",
                details={"effective_date": str(effective), "expiration_date": str(expiration)},
            )

        meta = await self.metadata.upsert(
            contract_id=contract_id, project_id=project_id, values=changes
        )

        # Re-derive the band if the score moved with a manual risk edit.
        if "risk_score" in changes and changes["risk_score"] is not None:
            from app.core.enums import RiskBand

            meta.risk_band = RiskBand.from_score(int(changes["risk_score"]))
            await self.db.flush()

        await self._record_field_history(contract, before, changes, actor=actor, source="user")
        await self.audit.record(
            action=AuditAction.UPDATE,
            entity_type="contract_metadata",
            entity_id=contract.id,
            entity_label=contract.display_title,
            project_id=project_id,
            user_id=actor.id,
            user_email=actor.email,
            before=before,
            after=snapshot(meta),
            ip=ip,
        )
        return self._metadata_response(meta) or ContractMetadataResponse()

    async def _record_field_history(
        self,
        contract: Contract,
        before: dict[str, Any],
        changes: dict[str, Any],
        *,
        actor: User,
        source: str,
    ) -> None:
        """One history row per changed field, for legal traceability."""
        from app.models.audit import ContractHistory

        for field, new_value in changes.items():
            old_value = before.get(field)
            if old_value == new_value:
                continue
            self.db.add(
                ContractHistory(
                    contract_id=contract.id,
                    project_id=contract.project_id,
                    user_id=actor.id,
                    change_type="field_update",
                    field_name=field,
                    old_value=None if old_value is None else str(old_value)[:2000],
                    new_value=None if new_value is None else str(new_value)[:2000],
                    source=source,
                )
            )
        await self.db.flush()

    # =========================================================================
    # Delete
    # =========================================================================
    async def delete_contract(
        self,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        actor: User,
        ip: str | None = None,
        purge_files: bool = False,
    ) -> None:
        """Soft-delete a contract.

        Derived rows (clauses, chunks, embeddings) are left in place so the delete is
        reversible; they are excluded from every read by the contract's
        ``deleted_at``. ``purge_files`` additionally removes the stored bytes, which
        is irreversible and therefore opt-in.
        """
        contract = await self.contracts.get_scoped_or_404(
            contract_id, project_id, resource="Contract"
        )

        active_job = await self.jobs.active_for_contract(contract_id)
        if active_job is not None:
            raise ConflictError(
                "This contract is still being processed. Cancel the job before deleting.",
                details={"job_id": str(active_job.id), "state": str(active_job.state)},
                code=ErrorCode.INVALID_STATE_TRANSITION,
            )

        before = snapshot(contract)
        await self.contracts.soft_delete(contract)
        await self.contracts.update(contract, status=ContractStatus.ARCHIVED)

        if purge_files:
            prefix = __import__("app.storage", fromlist=["StorageKey"]).StorageKey.contract_prefix(
                project_id, contract_id
            )
            removed = await self.storage.delete_prefix(prefix)
            logger.info("contract_files_purged", contract_id=str(contract_id), objects=removed)

        await self.projects.recount_contracts(project_id)
        await self.activities.record(
            project_id=project_id,
            activity_type="contract_deleted",
            summary=f"'{contract.display_title}' deleted by {actor.full_name}",
            user_id=actor.id,
            entity_type="contract",
            entity_id=contract_id,
        )
        await self.audit.record(
            action=AuditAction.DELETE,
            entity_type="contract",
            entity_id=contract_id,
            entity_label=contract.display_title,
            project_id=project_id,
            user_id=actor.id,
            user_email=actor.email,
            before=before,
            after={"deleted": True, "files_purged": purge_files},
            ip=ip,
        )
        logger.info("contract_deleted", contract_id=str(contract_id), by=str(actor.id))

    # =========================================================================
    # File access (viewer)
    # =========================================================================
    async def file_access(
        self,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        version: int | None = None,
        download: bool = False,
        actor: User | None = None,
        ip: str | None = None,
    ) -> FileAccessResponse:
        """Signed URL the PDF viewer uses to fetch the source document.

        Access is authorised here, once, and the browser then reads bytes directly
        from storage - the API does not proxy every page render, which is what keeps
        the viewer responsive on a 150-page document.
        """
        contract = await self.contracts.get_scoped_or_404(
            contract_id, project_id, resource="Contract"
        )

        storage_path = contract.storage_path
        file_name = contract.original_file_name
        file_size = contract.file_size

        if version is not None and version != contract.current_version:
            match = next(
                (
                    candidate
                    for candidate in await self.versions.list_for_contract(contract_id)
                    if candidate.version == version
                ),
                None,
            )
            if match is None:
                raise NotFoundError("Contract version", version)
            storage_path = match.storage_path
            file_name = match.original_file_name
            file_size = match.file_size

        ttl = self.settings.storage.signed_url_ttl_seconds
        url = await self.storage.signed_url(
            storage_path,
            expires_in=ttl,
            download_filename=file_name if download else None,
        )

        if download and actor is not None:
            await self.audit.record(
                action=AuditAction.DOWNLOAD,
                entity_type="contract",
                entity_id=contract_id,
                entity_label=file_name,
                project_id=project_id,
                user_id=actor.id,
                user_email=actor.email,
                after={"version": version or contract.current_version},
                ip=ip,
            )

        return FileAccessResponse(
            contract_id=contract_id,
            file_name=file_name,
            file_type=str(contract.file_type),
            file_size=file_size,
            page_count=contract.page_count,
            url=url,
            expires_in=ttl,
            # The local adapter cannot sign, so it returns an API route instead.
            is_proxied=url.startswith("/api/"),
        )

    async def stream_file(
        self, contract_id: uuid.UUID, project_id: uuid.UUID
    ) -> tuple[Contract, Any]:
        """Byte stream for the document.

        Used where a signed URL is unavailable (local storage) or where the client
        cannot follow a redirect. Returns the contract plus an async iterator.
        """
        contract = await self.contracts.get_scoped_or_404(
            contract_id, project_id, resource="Contract"
        )
        return contract, self.storage.get_stream(contract.storage_path)


__all__ = ["ContractService"]

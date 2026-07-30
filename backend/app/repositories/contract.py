"""Contract repository, including the metadata-first repository query.

:meth:`ContractRepository.filtered_query` is the workhorse behind the Contract
Repository screen and the pre-filter stage of hybrid retrieval. It joins
``contract_metadata`` once and applies every business filter against indexed
columns, so narrowing to "high-risk vendor agreements expiring in 90 days" happens
in the relational planner **before** any vector work (§14).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import joinedload

from app.core.enums import ContractStatus
from app.models.contract import Contract, ContractMetadata, ContractVersion
from app.models.processing import ProcessingJob
from app.repositories.base import ProjectScopedRepository
from app.schemas.contract import ContractFilterParams


class ContractRepository(ProjectScopedRepository[Contract]):
    model = Contract
    sortable_fields = frozenset(
        {
            "created_at",
            "updated_at",
            "title",
            "original_file_name",
            "contract_number",
            "agreement_type",
            "status",
            "file_size",
            "page_count",
            "processed_at",
        }
    )
    default_order_by = "created_at"

    #: Sort fields that live on the joined metadata table rather than on contracts.
    _METADATA_SORT_FIELDS = frozenset(
        {
            "effective_date",
            "expiration_date",
            "contract_value",
            "risk_score",
            "vendor",
            "customer",
            "party_a",
        }
    )

    # ------------------------------------------------------------------ lookup
    async def get_by_hash(self, project_id: uuid.UUID, sha256: str) -> Contract | None:
        """Duplicate detection - unique per project, not globally (§7.2).

        The same document may legitimately exist in two projects; re-uploading it to
        the *same* project is what gets rejected.
        """
        stmt = self.scoped(project_id).where(Contract.sha256_hash == sha256)
        return (await self.db.execute(stmt.limit(1))).scalar_one_or_none()

    async def get_detail(self, contract_id: uuid.UUID, project_id: uuid.UUID) -> Contract | None:
        """Load a contract with metadata, uploader and project in one round trip."""
        stmt = (
            self.scoped(project_id)
            .where(Contract.id == contract_id)
            .options(
                joinedload(Contract.contract_metadata),
                joinedload(Contract.uploader),
                joinedload(Contract.project),
            )
        )
        return (await self.db.execute(stmt)).unique().scalar_one_or_none()

    async def latest_job(self, contract_id: uuid.UUID) -> ProcessingJob | None:
        """Most recent processing job - the one whose state the UI shows."""
        stmt = (
            select(ProcessingJob)
            .where(ProcessingJob.contract_id == contract_id)
            .order_by(ProcessingJob.created_at.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def latest_jobs(
        self, contract_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, ProcessingJob]:
        """Latest job per contract, in one query.

        A correlated subquery per row would make a 200-row repository page issue 200
        extra queries; this keeps it at one.
        """
        if not contract_ids:
            return {}
        ranked = (
            select(
                ProcessingJob,
                func.row_number()
                .over(
                    partition_by=ProcessingJob.contract_id,
                    order_by=ProcessingJob.created_at.desc(),
                )
                .label("rank"),
            )
            .where(ProcessingJob.contract_id.in_(list(contract_ids)))
            .subquery()
        )
        stmt = select(ProcessingJob).join(
            ranked,
            and_(ranked.c.id == ProcessingJob.id, ranked.c.rank == 1),
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return {row.contract_id: row for row in rows}

    # ------------------------------------------------------------------ filter
    def filtered_query(
        self,
        project_ids: Sequence[uuid.UUID],
        filters: ContractFilterParams | None = None,
        *,
        with_metadata: bool = True,
    ) -> Select[tuple[Contract]]:
        """Build the repository query for one or more projects.

        Always scoped to ``project_ids`` - the caller's accessible set - so there is
        no code path that reads contracts outside the project boundary.
        """
        stmt = self.live().where(Contract.project_id.in_(list(project_ids)))

        if with_metadata:
            # Outer join: a contract that has not finished processing has no metadata
            # row yet but must still appear in the repository.
            stmt = stmt.outerjoin(
                ContractMetadata, ContractMetadata.contract_id == Contract.id
            ).options(joinedload(Contract.contract_metadata))

        if filters is None:
            return stmt

        conditions: list[Any] = []

        # --- text search ----------------------------------------------------
        if filters.search:
            pattern = f"%{filters.search.strip()}%"
            # Trigram indexes cover title and file name; metadata columns are
            # included so a vendor or party name matches too.
            conditions.append(
                or_(
                    Contract.title.ilike(pattern),
                    Contract.original_file_name.ilike(pattern),
                    func.coalesce(Contract.contract_number, "").ilike(pattern),
                    func.coalesce(ContractMetadata.vendor, "").ilike(pattern),
                    func.coalesce(ContractMetadata.customer, "").ilike(pattern),
                    func.coalesce(ContractMetadata.party_a, "").ilike(pattern),
                    func.coalesce(ContractMetadata.party_b, "").ilike(pattern),
                    func.coalesce(ContractMetadata.summary, "").ilike(pattern),
                )
            )

        # --- contract columns ------------------------------------------------
        if filters.status:
            conditions.append(Contract.status.in_(filters.status))
        if filters.agreement_type:
            conditions.append(Contract.agreement_type.in_(filters.agreement_type))
        if filters.file_type:
            conditions.append(Contract.file_type == filters.file_type)
        if filters.contract_number:
            conditions.append(
                func.lower(func.coalesce(Contract.contract_number, ""))
                == filters.contract_number.strip().lower()
            )
        if filters.needs_review is not None:
            conditions.append(Contract.needs_review.is_(filters.needs_review))
        if filters.uploaded_by:
            conditions.append(Contract.uploaded_by == filters.uploaded_by)
        if filters.uploaded_after:
            conditions.append(Contract.created_at >= filters.uploaded_after)
        if filters.language:
            conditions.append(
                or_(
                    Contract.language == filters.language,
                    ContractMetadata.language == filters.language,
                )
            )
        if filters.tags:
            # JSONB containment against the GIN-indexed tags column.
            conditions.append(Contract.tags.contains(filters.tags))

        # --- party (matches any party field) ---------------------------------
        if filters.party:
            pattern = f"%{filters.party.strip()}%"
            conditions.append(
                or_(
                    func.coalesce(ContractMetadata.party_a, "").ilike(pattern),
                    func.coalesce(ContractMetadata.party_b, "").ilike(pattern),
                    func.coalesce(ContractMetadata.vendor, "").ilike(pattern),
                    func.coalesce(ContractMetadata.customer, "").ilike(pattern),
                )
            )
        if filters.vendor:
            conditions.append(ContractMetadata.vendor.ilike(f"%{filters.vendor.strip()}%"))
        if filters.customer:
            conditions.append(ContractMetadata.customer.ilike(f"%{filters.customer.strip()}%"))

        # --- dates ------------------------------------------------------------
        if filters.effective_date and filters.effective_date.is_set:
            if filters.effective_date.from_date:
                conditions.append(
                    ContractMetadata.effective_date >= filters.effective_date.from_date
                )
            if filters.effective_date.to_date:
                conditions.append(ContractMetadata.effective_date <= filters.effective_date.to_date)
        if filters.expiration_date and filters.expiration_date.is_set:
            if filters.expiration_date.from_date:
                conditions.append(
                    ContractMetadata.expiration_date >= filters.expiration_date.from_date
                )
            if filters.expiration_date.to_date:
                conditions.append(
                    ContractMetadata.expiration_date <= filters.expiration_date.to_date
                )
        if filters.expiring_within_days is not None:
            horizon = date.today() + timedelta(days=filters.expiring_within_days)
            conditions.append(
                and_(
                    ContractMetadata.expiration_date.isnot(None),
                    ContractMetadata.expiration_date >= date.today(),
                    ContractMetadata.expiration_date <= horizon,
                )
            )

        # --- risk / value -----------------------------------------------------
        if filters.risk_band:
            conditions.append(ContractMetadata.risk_band.in_(filters.risk_band))
        if filters.risk_score:
            if filters.risk_score.min is not None:
                conditions.append(ContractMetadata.risk_score >= int(filters.risk_score.min))
            if filters.risk_score.max is not None:
                conditions.append(ContractMetadata.risk_score <= int(filters.risk_score.max))
        if filters.contract_value:
            if filters.contract_value.min is not None:
                conditions.append(ContractMetadata.contract_value >= filters.contract_value.min)
            if filters.contract_value.max is not None:
                conditions.append(ContractMetadata.contract_value <= filters.contract_value.max)
        if filters.currency:
            conditions.append(ContractMetadata.currency == filters.currency.upper())

        # --- classification / ownership --------------------------------------
        for column, value in (
            (ContractMetadata.governing_law, filters.governing_law),
            (ContractMetadata.country, filters.country),
            (ContractMetadata.category, filters.category),
            (ContractMetadata.department, filters.department),
            (ContractMetadata.owner, filters.owner),
        ):
            if value:
                conditions.append(column == value)

        # --- AI insight flags --------------------------------------------------
        if filters.auto_renewal is not None:
            conditions.append(ContractMetadata.auto_renewal.is_(filters.auto_renewal))
        if filters.has_unlimited_liability is not None:
            conditions.append(
                ContractMetadata.has_unlimited_liability.is_(filters.has_unlimited_liability)
            )
        if filters.missing_clause_types:
            # "Missing any of these" - JSONB overlap via OR of containments, which the
            # GIN index on missing_mandatory_clauses serves.
            conditions.append(
                or_(
                    *[
                        ContractMetadata.missing_mandatory_clauses.contains([clause])
                        for clause in filters.missing_clause_types
                    ]
                )
            )

        if conditions:
            stmt = stmt.where(and_(*conditions))
        return stmt

    def apply_sort(
        self,
        stmt: Select[tuple[Contract]],
        sort_by: str | None,
        sort_dir: str = "desc",
    ) -> Select[tuple[Contract]]:
        """Order by a contract or metadata column.

        Metadata sorts are handled here rather than in the base repository because
        the sortable column lives on the joined table.
        """
        if sort_by in self._METADATA_SORT_FIELDS:
            column = getattr(ContractMetadata, sort_by)
            # NULLS LAST both ways: an unprocessed contract with no expiry date
            # should not head the list when sorting by expiry.
            ordering = column.desc().nullslast() if sort_dir == "desc" else column.asc().nullslast()
            return stmt.order_by(ordering)
        return self._apply_order(stmt, sort_by, sort_dir)

    async def paginate_filtered(
        self,
        project_ids: Sequence[uuid.UUID],
        filters: ContractFilterParams | None,
        *,
        page: int = 1,
        size: int = 25,
        sort_by: str | None = None,
        sort_dir: str = "desc",
    ) -> tuple[Sequence[Contract], int]:
        stmt = self.filtered_query(project_ids, filters)
        total = await self._count(stmt)
        ordered = self.apply_sort(stmt, sort_by, sort_dir)
        rows = (
            (await self.db.execute(ordered.offset((page - 1) * size).limit(size)))
            .unique()
            .scalars()
            .all()
        )
        return rows, total

    # ---------------------------------------------------------------- mutation
    async def next_version(self, contract_id: uuid.UUID) -> int:
        current = (
            await self.db.execute(
                select(func.coalesce(func.max(ContractVersion.version), 0)).where(
                    ContractVersion.contract_id == contract_id
                )
            )
        ).scalar() or 0
        return int(current) + 1

    async def mark_processed(
        self,
        contract: Contract,
        *,
        status: ContractStatus,
        page_count: int | None = None,
        needs_review: bool | None = None,
    ) -> None:
        contract.status = status
        if page_count is not None:
            contract.page_count = page_count
        if needs_review is not None:
            contract.needs_review = needs_review
        if status is ContractStatus.READY:
            contract.processed_at = datetime.now(UTC)
        await self.db.flush()

    async def status_counts(self, project_ids: Sequence[uuid.UUID]) -> dict[str, int]:
        if not project_ids:
            return {}
        stmt = (
            select(Contract.status, func.count(Contract.id))
            .where(
                Contract.project_id.in_(list(project_ids)),
                Contract.deleted_at.is_(None),
            )
            .group_by(Contract.status)
        )
        return {str(status): int(count) for status, count in await self.db.execute(stmt)}


class ContractMetadataRepository(ProjectScopedRepository[ContractMetadata]):
    model = ContractMetadata

    async def get_for_contract(self, contract_id: uuid.UUID) -> ContractMetadata | None:
        return (
            await self.db.execute(
                select(ContractMetadata).where(ContractMetadata.contract_id == contract_id)
            )
        ).scalar_one_or_none()

    async def upsert(
        self,
        *,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        values: dict[str, Any],
    ) -> ContractMetadata:
        """Create or update the metadata row.

        Upsert rather than insert: the AI Extraction stage is idempotent, so a
        re-run must overwrite the projection instead of failing on the primary key.
        """
        existing = await self.get_for_contract(contract_id)
        if existing is None:
            return await self.create(contract_id=contract_id, project_id=project_id, **values)
        for key, value in values.items():
            if hasattr(existing, key):
                setattr(existing, key, value)
        await self.db.flush()
        return existing


class ContractVersionRepository(ProjectScopedRepository[ContractVersion]):
    model = ContractVersion
    default_order_by = "version"

    async def list_for_contract(self, contract_id: uuid.UUID) -> Sequence[ContractVersion]:
        stmt = (
            select(ContractVersion)
            .where(ContractVersion.contract_id == contract_id)
            .order_by(ContractVersion.version.desc())
        )
        return (await self.db.execute(stmt)).scalars().all()


__all__ = [
    "ContractMetadataRepository",
    "ContractRepository",
    "ContractVersionRepository",
]

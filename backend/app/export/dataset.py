"""Assembles the rows an export renders.

This module is the security boundary for exports (§1.1). Every query here is
constrained to a caller-supplied set of ``project_id`` values that was derived
from the caller's memberships, never from the request body - a client that names
a project it does not belong to gets an empty scope, not that project's data.

It is also where clause masking happens. Both concerns live here, in one file,
because the alternative - each exporter filtering for itself - multiplies the
number of places a mistake becomes a disclosure.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Permission
from app.core.logging import get_logger
from app.export.base import (
    DEFAULT_ENTITIES,
    ExportColumn,
    ExportDataset,
    ExportEntity,
    ExportSheet,
)
from app.models.contract import Contract, ContractMetadata
from app.models.knowledge import Clause, Entity, KeyDate, Obligation, Risk
from app.models.project import Project
from app.repositories.contract import ContractRepository
from app.schemas.contract import ContractFilterParams

logger = get_logger(__name__)

#: Clause categories whose body text is withheld from anyone without review
#: rights. The clause is still listed - its existence, type and page are not the
#: sensitive part - but the wording is replaced. Hiding the row entirely would be
#: worse: a reader would conclude the contract has no confidentiality clause.
_MASKED_CLAUSE_TYPES = frozenset({"confidentiality", "non_solicit", "exclusivity"})

_MASK_PLACEHOLDER = "[Withheld - requires clause review permission]"

#: A worksheet cell tops out at 32,767 characters in the XLSX format, and a long
#: clause can exceed it. Truncated with a marker rather than silently clipped.
MAX_CELL_CHARS = 32_000


# =============================================================================
# Column definitions
# =============================================================================
CONTRACT_COLUMNS: tuple[ExportColumn, ...] = (
    ExportColumn("contract_id", "Contract ID", width=38),
    ExportColumn("title", "Title", width=42),
    ExportColumn("file_name", "File", width=30),
    ExportColumn("project", "Project", width=22),
    ExportColumn("agreement_type", "Agreement type", width=20),
    ExportColumn("status", "Status", width=14),
    ExportColumn("party_a", "Party A", width=26),
    ExportColumn("party_b", "Party B", width=26),
    ExportColumn("vendor", "Vendor", width=22),
    ExportColumn("effective_date", "Effective", kind="date", width=13),
    ExportColumn("expiration_date", "Expires", kind="date", width=13),
    ExportColumn("term_months", "Term (months)", kind="integer", width=13),
    ExportColumn("auto_renewal", "Auto-renews", kind="bool", width=12),
    ExportColumn("auto_renewal_notice_days", "Renewal notice (days)", kind="integer", width=18),
    ExportColumn("contract_value", "Value", kind="number", width=16),
    ExportColumn("currency", "Currency", width=10),
    ExportColumn("payment_terms_days", "Payment terms (days)", kind="integer", width=18),
    ExportColumn("governing_law", "Governing law", width=20),
    ExportColumn("jurisdiction", "Jurisdiction", width=18),
    ExportColumn("risk_score", "Risk score", kind="integer", width=11),
    ExportColumn("risk_band", "Risk band", width=11),
    ExportColumn("has_unlimited_liability", "Unlimited liability", kind="bool", width=16),
    ExportColumn("missing_mandatory_clauses", "Missing mandatory clauses", width=34, wrap=True),
    ExportColumn("clause_count", "Clauses", kind="integer", width=9),
    ExportColumn("needs_review", "Needs review", kind="bool", width=12),
    ExportColumn("page_count", "Pages", kind="integer", width=8),
    ExportColumn("uploaded_at", "Uploaded", kind="datetime", width=18),
)

CLAUSE_COLUMNS: tuple[ExportColumn, ...] = (
    ExportColumn("contract_title", "Contract", width=34),
    ExportColumn("clause_type", "Clause type", width=24),
    ExportColumn("clause_number", "No.", width=9),
    ExportColumn("title", "Heading", width=30),
    ExportColumn("page_start", "Page", kind="integer", width=7),
    ExportColumn("is_mandatory", "Mandatory", kind="bool", width=11),
    ExportColumn("is_risk_flagged", "Risk flagged", kind="bool", width=12),
    ExportColumn("confidence", "Confidence", kind="number", width=11),
    ExportColumn("review_status", "Review", width=12),
    ExportColumn("attributes", "Extracted attributes", width=46, wrap=True),
    ExportColumn("summary", "Summary", width=46, wrap=True),
    ExportColumn("text", "Clause text", width=70, wrap=True),
)

OBLIGATION_COLUMNS: tuple[ExportColumn, ...] = (
    ExportColumn("contract_title", "Contract", width=34),
    ExportColumn("action", "Obligation", width=56, wrap=True),
    ExportColumn("responsible_party", "Responsible", width=26),
    ExportColumn("due_date", "Due", kind="date", width=13),
    ExportColumn("due_description", "Due (as worded)", width=32, wrap=True),
    ExportColumn("trigger_event", "Trigger", width=28, wrap=True),
    ExportColumn("frequency", "Frequency", width=14),
    ExportColumn("is_recurring", "Recurring", kind="bool", width=11),
    ExportColumn("status", "Status", width=13),
    ExportColumn("penalty", "Penalty", width=32, wrap=True),
    ExportColumn("page_start", "Page", kind="integer", width=7),
)

RISK_COLUMNS: tuple[ExportColumn, ...] = (
    ExportColumn("contract_title", "Contract", width=34),
    ExportColumn("risk_type", "Risk type", width=24),
    ExportColumn("severity", "Severity", width=11),
    ExportColumn("category", "Category", width=16),
    ExportColumn("is_omission", "Absence", kind="bool", width=10),
    ExportColumn("score_contribution", "Score impact", kind="integer", width=12),
    ExportColumn("description", "Description", width=56, wrap=True),
    ExportColumn("recommendation", "Recommendation", width=46, wrap=True),
    ExportColumn("page_start", "Page", kind="integer", width=7),
)

KEY_DATE_COLUMNS: tuple[ExportColumn, ...] = (
    ExportColumn("contract_title", "Contract", width=34),
    ExportColumn("date_type", "Type", width=20),
    ExportColumn("date_value", "Date", kind="date", width=13),
    ExportColumn("date_expression", "As worded", width=32, wrap=True),
    ExportColumn("description", "Description", width=42, wrap=True),
    ExportColumn("page_start", "Page", kind="integer", width=7),
)

ENTITY_COLUMNS: tuple[ExportColumn, ...] = (
    ExportColumn("contract_title", "Contract", width=34),
    ExportColumn("name", "Name", width=32),
    ExportColumn("legal_name", "Legal name", width=32),
    ExportColumn("entity_type", "Type", width=16),
    ExportColumn("role", "Role", width=16),
    ExportColumn("jurisdiction", "Jurisdiction", width=18),
    ExportColumn("registration_number", "Registration no.", width=20),
    ExportColumn("is_primary", "Primary", kind="bool", width=9),
    ExportColumn("page_start", "Page", kind="integer", width=7),
)

_COLUMNS: dict[ExportEntity, tuple[ExportColumn, ...]] = {
    ExportEntity.CONTRACTS: CONTRACT_COLUMNS,
    ExportEntity.CLAUSES: CLAUSE_COLUMNS,
    ExportEntity.OBLIGATIONS: OBLIGATION_COLUMNS,
    ExportEntity.RISKS: RISK_COLUMNS,
    ExportEntity.KEY_DATES: KEY_DATE_COLUMNS,
    ExportEntity.ENTITIES: ENTITY_COLUMNS,
}

_TITLES: dict[ExportEntity, str] = {
    ExportEntity.CONTRACTS: "Contracts",
    ExportEntity.CLAUSES: "Clauses",
    ExportEntity.OBLIGATIONS: "Obligations",
    ExportEntity.RISKS: "Risks",
    ExportEntity.KEY_DATES: "Key dates",
    ExportEntity.ENTITIES: "Parties",
}


# =============================================================================
# Builder
# =============================================================================
class ExportDatasetBuilder:
    """Loads and flattens the rows for one export request."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        project_ids: Sequence[uuid.UUID],
        permissions: frozenset[str],
        is_system_admin: bool = False,
    ) -> None:
        self.db = db
        #: Already resolved from membership by the caller. Never from the request.
        self.project_ids = list(project_ids)
        self.permissions = permissions
        self.is_system_admin = is_system_admin

    @property
    def may_see_masked(self) -> bool:
        return self.is_system_admin or Permission.KNOWLEDGE_REVIEW.value in self.permissions

    async def build(
        self,
        *,
        export_id: uuid.UUID,
        entities: Sequence[str],
        filters: ContractFilterParams | None,
        contract_ids: Sequence[uuid.UUID] | None,
        generated_by: str,
        scope_label: str,
        field_selection: dict[str, list[str]] | None = None,
    ) -> ExportDataset:
        """Assemble the dataset.

        The contract set is resolved first and every other sheet is restricted to
        those ids. That is what makes "export what I am looking at" exact: the
        clauses in the workbook are the clauses of the contracts in the workbook,
        not every clause the filters happen to touch.
        """
        requested = self._resolve_entities(entities)

        if not self.project_ids:
            # No memberships resolved: an empty workbook, not an error. The caller
            # asked a legitimate question and the honest answer is "nothing".
            logger.info("export_empty_scope", export_id=str(export_id))
            return ExportDataset(
                export_id=export_id,
                generated_at=datetime.now(UTC),
                generated_by=generated_by,
                scope_label=scope_label,
                project_names=[],
                filters=self._describe_filters(filters, contract_ids),
                sheets=[
                    ExportSheet(
                        entity=entity,
                        title=_TITLES[entity],
                        columns=self._columns_for(entity, field_selection),
                        rows=[],
                    )
                    for entity in requested
                ],
            )

        contracts = await self._load_contracts(filters, contract_ids)
        selected_ids = [contract.id for contract in contracts]
        titles = {
            contract.id: (contract.title or contract.original_file_name) for contract in contracts
        }
        project_names = await self._project_names()

        sheets: list[ExportSheet] = []
        masked_total = 0

        for entity in requested:
            columns = self._columns_for(entity, field_selection)
            if entity is ExportEntity.CONTRACTS:
                rows = [self._contract_row(contract, project_names) for contract in contracts]
                notes: list[str] = []
            elif not selected_ids:
                rows, notes = [], []
            elif entity is ExportEntity.CLAUSES:
                rows, masked = await self._clause_rows(selected_ids, titles)
                masked_total += masked
                notes = (
                    [f"{masked} clause body/bodies withheld by clause masking."] if masked else []
                )
            elif entity is ExportEntity.OBLIGATIONS:
                rows, notes = await self._obligation_rows(selected_ids, titles), []
            elif entity is ExportEntity.RISKS:
                rows, notes = await self._risk_rows(selected_ids, titles), []
            elif entity is ExportEntity.KEY_DATES:
                rows, notes = await self._key_date_rows(selected_ids, titles), []
            else:
                rows, notes = await self._entity_rows(selected_ids, titles), []

            sheets.append(
                ExportSheet(
                    entity=entity,
                    title=_TITLES[entity],
                    columns=columns,
                    rows=rows,
                    notes=tuple(notes),
                )
            )

        return ExportDataset(
            export_id=export_id,
            generated_at=datetime.now(UTC),
            generated_by=generated_by,
            scope_label=scope_label,
            project_names=list(project_names.values()),
            filters=self._describe_filters(filters, contract_ids),
            sheets=sheets,
            masked_note=(
                f"{masked_total} clause body/bodies were withheld because this account "
                "does not hold clause review permission."
                if masked_total
                else None
            ),
        )

    # ---------------------------------------------------------------- loading
    async def _load_contracts(
        self,
        filters: ContractFilterParams | None,
        contract_ids: Sequence[uuid.UUID] | None,
    ) -> Sequence[Contract]:
        """The contract set, using the same filter builder the list endpoint uses.

        Sharing `filtered_query` is deliberate: an export whose filter semantics
        drift from the screen it was launched from produces a file the user cannot
        reconcile with what they saw.
        """
        repository = ContractRepository(self.db)
        stmt: Select[tuple[Contract]] = repository.filtered_query(self.project_ids, filters)
        if contract_ids:
            stmt = stmt.where(Contract.id.in_(list(contract_ids)))
        stmt = repository.apply_sort(stmt, "created_at", "desc")
        return (await self.db.execute(stmt)).unique().scalars().all()

    async def _project_names(self) -> dict[uuid.UUID, str]:
        rows = (
            await self.db.execute(
                select(Project.id, Project.name).where(Project.id.in_(self.project_ids))
            )
        ).all()
        return {row[0]: row[1] for row in rows}

    # ------------------------------------------------------------------- rows
    def _contract_row(
        self, contract: Contract, project_names: dict[uuid.UUID, str]
    ) -> dict[str, Any]:
        meta: ContractMetadata | None = getattr(contract, "contract_metadata", None)
        return {
            "contract_id": str(contract.id),
            "title": contract.title,
            "file_name": contract.original_file_name,
            "project": project_names.get(contract.project_id, ""),
            "agreement_type": contract.agreement_type,
            "status": _value(contract.status),
            "party_a": getattr(meta, "party_a", None),
            "party_b": getattr(meta, "party_b", None),
            "vendor": getattr(meta, "vendor", None),
            "effective_date": getattr(meta, "effective_date", None),
            "expiration_date": getattr(meta, "expiration_date", None),
            "term_months": getattr(meta, "term_months", None),
            "auto_renewal": getattr(meta, "auto_renewal", None),
            "auto_renewal_notice_days": getattr(meta, "auto_renewal_notice_days", None),
            "contract_value": _number(getattr(meta, "contract_value", None)),
            "currency": getattr(meta, "currency", None),
            "payment_terms_days": getattr(meta, "payment_terms_days", None),
            "governing_law": getattr(meta, "governing_law", None),
            "jurisdiction": getattr(meta, "jurisdiction", None),
            "risk_score": getattr(meta, "risk_score", None),
            "risk_band": _value(getattr(meta, "risk_band", None)),
            "has_unlimited_liability": getattr(meta, "has_unlimited_liability", None),
            "missing_mandatory_clauses": ", ".join(
                getattr(meta, "missing_mandatory_clauses", None) or []
            ),
            "clause_count": getattr(meta, "clause_count", None),
            "needs_review": contract.needs_review,
            "page_count": contract.page_count,
            "uploaded_at": contract.created_at,
        }

    async def _clause_rows(
        self, contract_ids: Sequence[uuid.UUID], titles: dict[uuid.UUID, str]
    ) -> tuple[list[dict[str, Any]], int]:
        stmt = (
            select(Clause)
            .where(
                Clause.contract_id.in_(contract_ids),
                # Belt and braces: the contract ids are already project-scoped, but
                # the row filter states the invariant where a reader will see it.
                Clause.project_id.in_(self.project_ids),
            )
            .order_by(Clause.contract_id, Clause.clause_type, Clause.clause_number)
        )
        clauses = (await self.db.execute(stmt)).scalars().all()

        rows: list[dict[str, Any]] = []
        masked = 0
        for clause in clauses:
            hide = not self.may_see_masked and clause.clause_type in _MASKED_CLAUSE_TYPES
            if hide:
                masked += 1
            provenance = getattr(clause, "confidence", None)
            rows.append(
                {
                    "contract_title": titles.get(clause.contract_id, ""),
                    "clause_type": clause.clause_type,
                    "clause_number": clause.clause_number,
                    "title": clause.title or clause.section_title,
                    "page_start": getattr(clause, "page_start", None),
                    "is_mandatory": clause.is_mandatory,
                    "is_risk_flagged": clause.is_risk_flagged,
                    "confidence": _number(provenance),
                    "review_status": _value(getattr(clause, "review_status", None)),
                    "attributes": _flatten_attributes(clause.attributes),
                    "summary": _MASK_PLACEHOLDER if hide else clause.summary,
                    "text": _MASK_PLACEHOLDER if hide else _truncate(clause.text_content),
                }
            )
        return rows, masked

    async def _obligation_rows(
        self, contract_ids: Sequence[uuid.UUID], titles: dict[uuid.UUID, str]
    ) -> list[dict[str, Any]]:
        stmt = (
            select(Obligation)
            .where(
                Obligation.contract_id.in_(contract_ids),
                Obligation.project_id.in_(self.project_ids),
            )
            .order_by(Obligation.contract_id, Obligation.due_date.nullslast())
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return [
            {
                "contract_title": titles.get(row.contract_id, ""),
                "action": _truncate(row.action),
                "responsible_party": row.responsible_party,
                "due_date": row.due_date,
                # Kept verbatim: "within 30 days of termination" *is* the
                # obligation, and resolving it to a date the contract never states
                # would be an invention in a file people act on.
                "due_description": row.due_description,
                "trigger_event": row.trigger_event,
                "frequency": row.frequency,
                "is_recurring": row.is_recurring,
                "status": _value(row.status),
                "penalty": _truncate(row.penalty),
                "page_start": getattr(row, "page_start", None),
            }
            for row in rows
        ]

    async def _risk_rows(
        self, contract_ids: Sequence[uuid.UUID], titles: dict[uuid.UUID, str]
    ) -> list[dict[str, Any]]:
        stmt = (
            select(Risk)
            .where(Risk.contract_id.in_(contract_ids), Risk.project_id.in_(self.project_ids))
            .order_by(Risk.contract_id, Risk.severity)
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return [
            {
                "contract_title": titles.get(row.contract_id, ""),
                "risk_type": row.risk_type,
                "severity": _value(row.severity),
                "category": row.category,
                "is_omission": row.is_omission,
                "score_contribution": row.score_contribution,
                "description": _truncate(row.description),
                "recommendation": _truncate(row.recommendation),
                "page_start": getattr(row, "page_start", None),
            }
            for row in rows
        ]

    async def _key_date_rows(
        self, contract_ids: Sequence[uuid.UUID], titles: dict[uuid.UUID, str]
    ) -> list[dict[str, Any]]:
        stmt = (
            select(KeyDate)
            .where(KeyDate.contract_id.in_(contract_ids), KeyDate.project_id.in_(self.project_ids))
            .order_by(KeyDate.contract_id, KeyDate.date_value.nullslast())
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return [
            {
                "contract_title": titles.get(row.contract_id, ""),
                "date_type": _value(row.date_type),
                "date_value": row.date_value,
                "date_expression": row.date_expression,
                "description": _truncate(getattr(row, "description", None)),
                "page_start": getattr(row, "page_start", None),
            }
            for row in rows
        ]

    async def _entity_rows(
        self, contract_ids: Sequence[uuid.UUID], titles: dict[uuid.UUID, str]
    ) -> list[dict[str, Any]]:
        stmt = (
            select(Entity)
            .where(Entity.contract_id.in_(contract_ids), Entity.project_id.in_(self.project_ids))
            .order_by(Entity.contract_id, Entity.is_primary.desc(), Entity.name)
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return [
            {
                "contract_title": titles.get(row.contract_id, ""),
                "name": row.name,
                "legal_name": row.legal_name,
                "entity_type": _value(row.entity_type),
                "role": row.role,
                "jurisdiction": row.jurisdiction,
                "registration_number": row.registration_number,
                "is_primary": row.is_primary,
                "page_start": getattr(row, "page_start", None),
            }
            for row in rows
        ]

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _resolve_entities(entities: Sequence[str]) -> list[ExportEntity]:
        if not entities:
            return list(DEFAULT_ENTITIES)
        resolved: list[ExportEntity] = []
        for name in entities:
            try:
                entity = ExportEntity(name)
            except ValueError:
                logger.warning("export_unknown_entity", entity=name)
                continue
            if entity not in resolved:
                resolved.append(entity)
        return resolved or list(DEFAULT_ENTITIES)

    def _columns_for(
        self, entity: ExportEntity, field_selection: dict[str, list[str]] | None
    ) -> tuple[ExportColumn, ...]:
        """Honour a per-entity column selection, preserving the canonical order."""
        available = _COLUMNS[entity]
        wanted = (field_selection or {}).get(entity.value)
        if not wanted:
            return available
        keep = set(wanted)
        narrowed = tuple(column for column in available if column.key in keep)
        # An unrecognised selection would otherwise yield a sheet with headers and
        # no columns, which reads as "no data" rather than "bad request".
        return narrowed or available

    @staticmethod
    def _describe_filters(
        filters: ContractFilterParams | None, contract_ids: Sequence[uuid.UUID] | None
    ) -> dict[str, Any]:
        described: dict[str, Any] = {}
        if filters is not None:
            described = {
                key: value
                for key, value in filters.model_dump(exclude_none=True, mode="json").items()
                if value not in ([], {}, "")
            }
        if contract_ids:
            described["contract_ids"] = [str(value) for value in contract_ids]
        return described


# =============================================================================
# Value helpers
# =============================================================================
def _value(value: Any) -> Any:
    """Unwrap an enum to the string that is actually stored."""
    return getattr(value, "value", value)


def _number(value: Any) -> float | None:
    """Decimal to float for the spreadsheet's numeric cell type."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    if len(text) <= MAX_CELL_CHARS:
        return text
    return text[:MAX_CELL_CHARS] + "\n[truncated - see the contract for the full text]"


def _flatten_attributes(attributes: dict[str, Any] | None) -> str:
    """Render extracted attributes as readable `key: value` lines.

    One column rather than a column per attribute, because attributes differ by
    clause type: a column-per-attribute sheet covering 23 clause types would be
    mostly empty cells, and the reader would have to scroll past irrelevant
    columns to find the one that matters for the clause in front of them.

    `null` is rendered explicitly - the schema requires every attribute key, so a
    null means the extractor looked and the contract is silent. That is a finding,
    and a blank cell would lose it.
    """
    if not attributes:
        return ""
    parts: list[str] = []
    for key, value in attributes.items():
        if key == "extracted" and isinstance(value, dict):
            value = value  # nested block from the graph builder; rendered as-is
        if value is None:
            rendered = "not specified"
        elif isinstance(value, bool):
            rendered = "yes" if value else "no"
        elif isinstance(value, (list, tuple)):
            rendered = ", ".join(str(item) for item in value) if value else "none"
        elif isinstance(value, dict):
            rendered = "; ".join(f"{k}={v}" for k, v in value.items())
        else:
            rendered = str(value)
        parts.append(f"{key.replace('_', ' ')}: {rendered}")
    return _truncate("\n".join(parts)) or ""


__all__ = ["MAX_CELL_CHARS", "ExportDatasetBuilder"]

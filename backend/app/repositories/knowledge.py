"""Extracted-knowledge persistence (§13).

One repository per knowledge table. They share three properties that the
extraction stage depends on:

* **Project-scoped reads.** Every query takes ``project_id``; the knowledge tables
  are the retrieval and reporting surface, and an unscoped read here leaks another
  project's contract terms (§1.1).
* **Replace, never accumulate.** ``delete_for_contract`` (inherited) is what makes
  re-extraction idempotent: the stage deletes its prior output and re-inserts, so a
  retried job cannot leave two generations of clauses side by side (§10.1).
* **Bulk insert with the clause link resolved.** Obligations and risks reference the
  clause they arise from, so clauses are inserted first and their ids threaded into
  the dependent rows rather than looked up again.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, case, delete, func, select

from app.core.enums import RiskSeverity
from app.models.knowledge import (
    Clause,
    ContractSummary,
    Entity,
    KeyDate,
    KnowledgeRelationship,
    Obligation,
    Risk,
)
from app.repositories.base import ProjectScopedRepository, affected_rows, table_of

_INSERT_BATCH = 500


class _BulkRepository(ProjectScopedRepository[Any]):
    """Shared bulk-insert helper."""

    async def insert_many(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        table = table_of(self.model)
        for start in range(0, len(rows), _INSERT_BATCH):
            await self.db.execute(table.insert(), list(rows[start : start + _INSERT_BATCH]))
        await self.db.flush()
        return len(rows)

    def for_contract(self, contract_id: uuid.UUID, project_id: uuid.UUID) -> Select[tuple[Any]]:
        return self.scoped(project_id).where(self.model.contract_id == contract_id)


class ClauseRepository(_BulkRepository):
    model = Clause

    async def list_for_contract(
        self,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        clause_types: Sequence[str] | None = None,
        flagged_only: bool = False,
    ) -> Sequence[Clause]:
        stmt = self.for_contract(contract_id, project_id)
        if clause_types:
            stmt = stmt.where(Clause.clause_type.in_(list(clause_types)))
        if flagged_only:
            stmt = stmt.where(Clause.is_risk_flagged.is_(True))
        return (await self.db.execute(stmt.order_by(Clause.page_start, Clause.id))).scalars().all()

    async def get_by_type(
        self, contract_id: uuid.UUID, project_id: uuid.UUID, clause_type: str
    ) -> Sequence[Clause]:
        """Every clause of one type, most confident first.

        Plural because a contract can state the same term twice - in the body and
        again in an addendum - and the dedicated clause tabs must show both rather
        than silently pick one.
        """
        stmt = (
            self.for_contract(contract_id, project_id)
            .where(Clause.clause_type == clause_type)
            .order_by(Clause.confidence.desc().nullslast())
        )
        return (await self.db.execute(stmt)).scalars().all()

    async def find_by_attribute(
        self,
        project_ids: Sequence[uuid.UUID],
        *,
        clause_type: str,
        attribute_filter: dict[str, Any],
        limit: int = 200,
    ) -> Sequence[Clause]:
        """Clauses whose attributes contain a filter - the repository-wide query.

        This is what makes "every contract with an uncapped liability cap" an indexed
        JSONB containment lookup rather than a text search. ``project_ids`` is the
        caller's accessible set, never the whole table.
        """
        if not project_ids:
            return []
        stmt = (
            self.scoped_many(project_ids)
            .where(
                Clause.clause_type == clause_type,
                Clause.attributes.contains(attribute_filter),
            )
            .limit(limit)
        )
        return (await self.db.execute(stmt)).scalars().all()

    async def type_counts(self, project_ids: Sequence[uuid.UUID]) -> dict[str, int]:
        if not project_ids:
            return {}
        stmt = (
            select(Clause.clause_type, func.count())
            .where(Clause.project_id.in_(list(project_ids)))
            .group_by(Clause.clause_type)
        )
        return {row[0]: int(row[1]) for row in (await self.db.execute(stmt)).all()}


class EntityRepository(_BulkRepository):
    model = Entity

    async def list_for_contract(
        self, contract_id: uuid.UUID, project_id: uuid.UUID
    ) -> Sequence[Entity]:
        stmt = self.for_contract(contract_id, project_id).order_by(
            Entity.is_primary.desc(), Entity.name
        )
        return (await self.db.execute(stmt)).scalars().all()

    async def counterparties(
        self, project_ids: Sequence[uuid.UUID], *, limit: int = 100
    ) -> Sequence[tuple[str, int]]:
        """Distinct counterparty names with contract counts, for the vendor view."""
        if not project_ids:
            return []
        stmt = (
            select(Entity.name, func.count(func.distinct(Entity.contract_id)))
            .where(
                Entity.project_id.in_(list(project_ids)),
                Entity.is_primary.is_(True),
            )
            .group_by(Entity.name)
            .order_by(func.count(func.distinct(Entity.contract_id)).desc())
            .limit(limit)
        )
        return [(row[0], int(row[1])) for row in (await self.db.execute(stmt)).all()]


class ObligationRepository(_BulkRepository):
    model = Obligation

    async def list_for_contract(
        self, contract_id: uuid.UUID, project_id: uuid.UUID
    ) -> Sequence[Obligation]:
        stmt = self.for_contract(contract_id, project_id).order_by(
            Obligation.due_date.nullslast(), Obligation.id
        )
        return (await self.db.execute(stmt)).scalars().all()

    async def upcoming(
        self,
        project_ids: Sequence[uuid.UUID],
        *,
        before: Any,
        limit: int = 200,
    ) -> Sequence[Obligation]:
        if not project_ids:
            return []
        stmt = (
            self.scoped_many(project_ids)
            .where(Obligation.due_date.is_not(None), Obligation.due_date <= before)
            .order_by(Obligation.due_date)
            .limit(limit)
        )
        return (await self.db.execute(stmt)).scalars().all()


class RiskRepository(_BulkRepository):
    model = Risk

    async def list_for_contract(
        self, contract_id: uuid.UUID, project_id: uuid.UUID
    ) -> Sequence[Risk]:
        """Risks for one contract, most severe first.

        Ordered by the enum's own severity ranking rather than alphabetically, so
        "critical" precedes "high" precedes "medium" as a reader expects.
        """
        stmt = self.for_contract(contract_id, project_id).order_by(_severity_rank(), Risk.risk_type)
        return (await self.db.execute(stmt)).scalars().all()

    async def severity_counts(self, project_ids: Sequence[uuid.UUID]) -> dict[str, int]:
        if not project_ids:
            return {}
        stmt = (
            select(Risk.severity, func.count())
            .where(Risk.project_id.in_(list(project_ids)))
            .group_by(Risk.severity)
        )
        return {
            (row[0].value if hasattr(row[0], "value") else str(row[0])): int(row[1])
            for row in (await self.db.execute(stmt)).all()
        }


def _severity_rank() -> Any:
    """CASE expression ranking severity from critical to low."""
    return case(
        {
            RiskSeverity.CRITICAL.value: 0,
            RiskSeverity.HIGH.value: 1,
            RiskSeverity.MEDIUM.value: 2,
            RiskSeverity.LOW.value: 3,
        },
        value=Risk.severity,
        else_=4,
    )


class KeyDateRepository(_BulkRepository):
    model = KeyDate

    async def list_for_contract(
        self, contract_id: uuid.UUID, project_id: uuid.UUID
    ) -> Sequence[KeyDate]:
        stmt = self.for_contract(contract_id, project_id).order_by(
            KeyDate.date_value.nullslast(), KeyDate.date_type
        )
        return (await self.db.execute(stmt)).scalars().all()

    async def between(
        self,
        project_ids: Sequence[uuid.UUID],
        *,
        start: Any,
        end: Any,
        date_types: Sequence[str] | None = None,
    ) -> Sequence[KeyDate]:
        """Dated entries in a window - the timeline and alert sweep query."""
        if not project_ids:
            return []
        stmt = self.scoped_many(project_ids).where(
            KeyDate.date_value.is_not(None),
            KeyDate.date_value >= start,
            KeyDate.date_value <= end,
        )
        if date_types:
            stmt = stmt.where(KeyDate.date_type.in_(list(date_types)))
        return (await self.db.execute(stmt.order_by(KeyDate.date_value))).scalars().all()


class KnowledgeRelationshipRepository(_BulkRepository):
    model = KnowledgeRelationship

    async def delete_derived_for_contract(
        self, contract_id: uuid.UUID, project_id: uuid.UUID
    ) -> int:
        """Delete only the edges the indexing stage derived.

        Two stages write to this table and they own different rows: extraction stores
        what the *document says* (``origin: extracted``), indexing stores what the
        platform *derived* from resolving those statements against real rows. Indexing
        must be re-runnable without destroying the extraction output it depends on, so
        the delete is scoped by origin rather than by contract.
        """
        result = await self.db.execute(
            delete(KnowledgeRelationship).where(
                KnowledgeRelationship.contract_id == contract_id,
                KnowledgeRelationship.project_id == project_id,
                KnowledgeRelationship.attributes["origin"].astext == "derived",
            )
        )
        await self.db.flush()
        return affected_rows(result)

    async def list_for_contract(
        self, contract_id: uuid.UUID, project_id: uuid.UUID
    ) -> Sequence[KnowledgeRelationship]:
        stmt = self.for_contract(contract_id, project_id).order_by(
            KnowledgeRelationship.relation, KnowledgeRelationship.source_ref
        )
        return (await self.db.execute(stmt)).scalars().all()

    async def neighbours(
        self,
        project_id: uuid.UUID,
        *,
        node_ref: str,
        limit: int = 100,
    ) -> Sequence[KnowledgeRelationship]:
        """Edges touching one node, in either direction.

        Scoped to a single project deliberately: graph traversal across the isolation
        boundary is prohibited (§1.1), and a traversal that silently widened scope
        would be the easiest way to breach it.
        """
        stmt = (
            self.scoped(project_id)
            .where(
                (KnowledgeRelationship.source_ref == node_ref)
                | (KnowledgeRelationship.target_ref == node_ref)
            )
            .limit(limit)
        )
        return (await self.db.execute(stmt)).scalars().all()


class ContractSummaryRepository(_BulkRepository):
    model = ContractSummary

    async def get_for_contract(
        self, contract_id: uuid.UUID, project_id: uuid.UUID, *, summary_type: str
    ) -> ContractSummary | None:
        stmt = self.for_contract(contract_id, project_id).where(
            ContractSummary.summary_type == summary_type
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def upsert(
        self,
        *,
        contract_id: uuid.UUID,
        project_id: uuid.UUID,
        summary_type: str,
        values: dict[str, Any],
    ) -> ContractSummary:
        existing = await self.get_for_contract(contract_id, project_id, summary_type=summary_type)
        if existing is None:
            return await self.create(
                contract_id=contract_id,
                project_id=project_id,
                summary_type=summary_type,
                **values,
            )
        for key, value in values.items():
            if hasattr(existing, key):
                setattr(existing, key, value)
        await self.db.flush()
        return existing


__all__ = [
    "ClauseRepository",
    "ContractSummaryRepository",
    "EntityRepository",
    "KeyDateRepository",
    "KnowledgeRelationshipRepository",
    "ObligationRepository",
    "RiskRepository",
]

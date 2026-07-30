"""Generic repository base.

Two things are enforced here rather than left to each call site, because both are
the kind of rule that fails silently when forgotten:

* **Soft-delete filtering** - :meth:`BaseRepository.live` applies
  ``deleted_at IS NULL`` so a deleted row cannot reappear in a listing.
* **Project scoping** - :class:`ProjectScopedRepository` requires a ``project_id``
  on every read and write. A query that forgets it does not silently return
  another project's data; it fails to compile because the method signature
  demands it.

Repositories own queries. They never commit: the request-scoped transaction in
:func:`~app.db.session.get_db` decides that, so a handler that fails halfway
leaves nothing behind.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.db.base import Base

logger = get_logger(__name__)

ModelT = TypeVar("ModelT", bound=Base)


def affected_rows(result: Any) -> int:
    """How many rows a DML statement touched.

    ``AsyncSession.execute`` is typed as returning ``Result``, which does not declare
    ``rowcount`` - only ``CursorResult`` does, and that is what a DELETE or UPDATE
    actually returns. The narrowing is stated once here rather than repeated at every
    call site.
    """
    return int(getattr(result, "rowcount", 0) or 0)


def table_of(model: Any) -> Any:
    """A model's ``Table``, for a Core bulk insert.

    ``__table__`` is declared as ``FromClause``, which has no ``insert()``; the real
    object is always a ``Table``. Narrowed here so bulk-insert paths read cleanly.
    """
    return model.__table__


class BaseRepository(Generic[ModelT]):
    """CRUD and query helpers for one model."""

    #: Subclasses set this.
    model: type[ModelT]

    #: Columns that may be sorted on. Allow-listed because ``sort_by`` arrives from
    #: the client, and an unchecked column name is a query-injection surface.
    sortable_fields: frozenset[str] = frozenset()

    #: Default ordering when the caller does not specify one.
    default_order_by: str = "created_at"

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------ query
    def query(self) -> Select[tuple[ModelT]]:
        """Bare select over the model."""
        return select(self.model)

    def live(self) -> Select[tuple[ModelT]]:
        """Select excluding soft-deleted rows."""
        stmt = self.query()
        deleted_at = getattr(self.model, "deleted_at", None)
        if deleted_at is not None:
            stmt = stmt.where(deleted_at.is_(None))
        return stmt

    def _apply_order(
        self,
        stmt: Select[tuple[ModelT]],
        sort_by: str | None,
        sort_dir: str = "desc",
    ) -> Select[tuple[ModelT]]:
        """Order a statement, ignoring any field outside the allow-list."""
        field = sort_by if sort_by in self.sortable_fields else self.default_order_by
        column = getattr(self.model, field, None)
        if column is None:
            return stmt
        return stmt.order_by(column.desc() if sort_dir == "desc" else column.asc())

    async def _count(self, stmt: Select[Any]) -> int:
        """Count the rows a statement would return.

        Counts over the statement's own subquery so every filter, join and
        distinct clause is honoured - a hand-built ``count()`` that re-applies
        filters is where pagination totals start disagreeing with the page.
        """
        subquery = stmt.order_by(None).options().subquery()
        result = await self.db.execute(select(func.count()).select_from(subquery))
        return int(result.scalar() or 0)

    # ------------------------------------------------------------------- read
    async def get(self, entity_id: uuid.UUID, *, include_deleted: bool = False) -> ModelT | None:
        stmt = (self.query() if include_deleted else self.live()).where(
            self.model.id == entity_id  # type: ignore[attr-defined]
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_or_404(self, entity_id: uuid.UUID, *, resource: str | None = None) -> ModelT:
        entity = await self.get(entity_id)
        if entity is None:
            raise NotFoundError(resource or self.model.__name__, entity_id)
        return entity

    async def get_by(self, **filters: Any) -> ModelT | None:
        stmt = self.live()
        for key, value in filters.items():
            stmt = stmt.where(getattr(self.model, key) == value)
        return (await self.db.execute(stmt.limit(1))).scalar_one_or_none()

    async def list_all(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        sort_by: str | None = None,
        sort_dir: str = "desc",
        **filters: Any,
    ) -> Sequence[ModelT]:
        stmt = self.live()
        for key, value in filters.items():
            if value is not None:
                stmt = stmt.where(getattr(self.model, key) == value)
        stmt = self._apply_order(stmt, sort_by, sort_dir)
        if offset:
            stmt = stmt.offset(offset)
        if limit:
            stmt = stmt.limit(limit)
        return (await self.db.execute(stmt)).scalars().all()

    async def paginate(
        self,
        stmt: Select[tuple[ModelT]],
        *,
        page: int = 1,
        size: int = 25,
        sort_by: str | None = None,
        sort_dir: str = "desc",
    ) -> tuple[Sequence[ModelT], int]:
        """Return ``(items, total)`` for one page.

        The count runs before ordering/limiting is applied to the page query, so
        the total reflects the filter set rather than the page.
        """
        total = await self._count(stmt)
        ordered = self._apply_order(stmt, sort_by, sort_dir)
        ordered = ordered.offset((page - 1) * size).limit(size)
        items = (await self.db.execute(ordered)).scalars().all()
        return items, total

    async def exists(self, **filters: Any) -> bool:
        stmt = select(func.count()).select_from(self.model)
        for key, value in filters.items():
            stmt = stmt.where(getattr(self.model, key) == value)
        deleted_at = getattr(self.model, "deleted_at", None)
        if deleted_at is not None:
            stmt = stmt.where(deleted_at.is_(None))
        return bool((await self.db.execute(stmt)).scalar())

    async def count(self, **filters: Any) -> int:
        stmt = select(func.count()).select_from(self.model)
        for key, value in filters.items():
            if value is not None:
                stmt = stmt.where(getattr(self.model, key) == value)
        deleted_at = getattr(self.model, "deleted_at", None)
        if deleted_at is not None:
            stmt = stmt.where(deleted_at.is_(None))
        return int((await self.db.execute(stmt)).scalar() or 0)

    # ------------------------------------------------------------------ write
    async def create(self, **values: Any) -> ModelT:
        entity = self.model(**values)
        self.db.add(entity)
        # Flush (not commit) so the caller gets the generated id and DB defaults
        # while staying inside the request transaction.
        await self.db.flush()
        return entity

    async def create_many(self, rows: Sequence[dict[str, Any]]) -> list[ModelT]:
        entities = [self.model(**row) for row in rows]
        self.db.add_all(entities)
        await self.db.flush()
        return entities

    async def update(self, entity: ModelT, **values: Any) -> ModelT:
        for key, value in values.items():
            if hasattr(entity, key):
                setattr(entity, key, value)
        await self.db.flush()
        return entity

    async def update_by_id(self, entity_id: uuid.UUID, **values: Any) -> ModelT:
        entity = await self.get_or_404(entity_id)
        return await self.update(entity, **values)

    async def bulk_update(self, where: ColumnElement[bool], **values: Any) -> int:
        """Set-based update. Bypasses the ORM, so no Python-side events fire."""
        result = await self.db.execute(update(self.model).where(where).values(**values))
        await self.db.flush()
        return affected_rows(result)

    async def soft_delete(self, entity: ModelT) -> ModelT:
        """Mark deleted. Nothing legally traceable is hard-deleted."""
        if not hasattr(entity, "deleted_at"):
            raise TypeError(f"{self.model.__name__} does not support soft delete")
        from datetime import UTC, datetime

        entity.deleted_at = datetime.now(UTC)  # type: ignore[attr-defined]
        await self.db.flush()
        return entity

    async def hard_delete(self, entity: ModelT) -> None:
        """Physically remove a row.

        Reserved for genuinely transient data (expired refresh tokens, superseded
        artifacts). Contract-derived records are soft-deleted.
        """
        await self.db.delete(entity)
        await self.db.flush()

    async def delete_where(self, where: ColumnElement[bool]) -> int:
        result = await self.db.execute(delete(self.model).where(where))
        await self.db.flush()
        return affected_rows(result)


class ProjectScopedRepository(BaseRepository[ModelT]):
    """Repository for a model carrying ``project_id``.

    Every method takes ``project_id`` as a required positional argument. That is
    the point: project isolation is the platform's security boundary (§1.1), and
    making the scope impossible to omit is stronger than remembering to add a
    filter. Cross-project reads live in explicitly named methods that a System
    Admin path must call deliberately.
    """

    def scoped(self, project_id: uuid.UUID) -> Select[tuple[ModelT]]:
        """Live rows for one project."""
        return self.live().where(self.model.project_id == project_id)  # type: ignore[attr-defined]

    def scoped_many(self, project_ids: Sequence[uuid.UUID]) -> Select[tuple[ModelT]]:
        """Live rows across several projects - the user's accessible set.

        Used for the application-wide dashboard, where "all projects" means
        exactly the projects this user is a member of, never the whole table.
        """
        return self.live().where(self.model.project_id.in_(list(project_ids)))  # type: ignore[attr-defined]

    async def get_scoped(self, entity_id: uuid.UUID, project_id: uuid.UUID) -> ModelT | None:
        stmt = self.scoped(project_id).where(self.model.id == entity_id)  # type: ignore[attr-defined]
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_scoped_or_404(
        self,
        entity_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        resource: str | None = None,
    ) -> ModelT:
        """Fetch within a project, or 404.

        Deliberately returns 404 rather than 403 when the row exists in another
        project: revealing that an id exists elsewhere is itself a leak across the
        isolation boundary.
        """
        entity = await self.get_scoped(entity_id, project_id)
        if entity is None:
            raise NotFoundError(resource or self.model.__name__, entity_id)
        return entity

    async def count_scoped(self, project_id: uuid.UUID, **filters: Any) -> int:
        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(
                self.model.project_id == project_id  # type: ignore[attr-defined]
            )
        )
        for key, value in filters.items():
            if value is not None:
                stmt = stmt.where(getattr(self.model, key) == value)
        deleted_at = getattr(self.model, "deleted_at", None)
        if deleted_at is not None:
            stmt = stmt.where(deleted_at.is_(None))
        return int((await self.db.execute(stmt)).scalar() or 0)

    async def delete_for_contract(self, contract_id: uuid.UUID, project_id: uuid.UUID) -> int:
        """Remove every row this repository owns for one contract.

        The idempotency primitive for pipeline stages: a stage deletes its prior
        output and re-inserts, so re-running it can never duplicate rows (§10.1).
        Scoped by project as well as contract so a mis-passed id cannot reach
        across the boundary.
        """
        contract_col: InstrumentedAttribute[Any] | None = getattr(self.model, "contract_id", None)
        if contract_col is None:
            raise TypeError(f"{self.model.__name__} has no contract_id")
        result = await self.db.execute(
            delete(self.model).where(
                contract_col == contract_id,
                self.model.project_id == project_id,  # type: ignore[attr-defined]
            )
        )
        await self.db.flush()
        return affected_rows(result)


__all__ = ["BaseRepository", "ProjectScopedRepository"]

"""Repositories for projects, memberships and the activity feed."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import joinedload, selectinload

from app.core.enums import ProjectStatus, RoleName
from app.models.identity import Role, User
from app.models.project import Project, ProjectActivity, ProjectMember
from app.repositories.base import BaseRepository


class ProjectRepository(BaseRepository[Project]):
    model = Project
    sortable_fields = frozenset(
        {"name", "created_at", "updated_at", "last_activity_at", "status", "contract_count"}
    )
    default_order_by = "created_at"

    # ------------------------------------------------------------------ lookup
    async def get_by_slug(self, slug: str) -> Project | None:
        return (
            await self.db.execute(self.live().where(Project.slug == slug).limit(1))
        ).scalar_one_or_none()

    async def slug_exists(self, slug: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        stmt = select(func.count()).select_from(Project).where(Project.slug == slug)
        if exclude_id:
            stmt = stmt.where(Project.id != exclude_id)
        return bool((await self.db.execute(stmt)).scalar())

    async def unique_slug(self, base: str) -> str:
        """Return ``base``, or ``base-2``, ``base-3``... until unused.

        Slugs appear in URLs, so a collision must not surface as a 500; the
        numeric suffix keeps creation succeeding without asking the user to retry.
        """
        if not await self.slug_exists(base):
            return base
        for suffix in range(2, 1000):
            candidate = f"{base[:135]}-{suffix}"
            if not await self.slug_exists(candidate):
                return candidate
        return f"{base[:120]}-{uuid.uuid4().hex[:8]}"

    # -------------------------------------------------------------------- list
    def accessible_query(
        self,
        *,
        user_id: uuid.UUID,
        is_system_admin: bool,
        search: str | None = None,
        status: ProjectStatus | None = None,
        department: str | None = None,
        client_name: str | None = None,
        favourites_only: bool = False,
    ) -> Select[tuple[Project]]:
        """Projects the caller may see.

        A System Admin sees every project; everyone else sees exactly the projects
        they hold a ``project_members`` row for. This join *is* the access control -
        there is no post-filter to forget.
        """
        stmt = self.live()

        if not is_system_admin:
            stmt = stmt.join(
                ProjectMember,
                (ProjectMember.project_id == Project.id) & (ProjectMember.user_id == user_id),
            )
            if favourites_only:
                stmt = stmt.where(ProjectMember.is_favourite.is_(True))
        elif favourites_only:
            # An admin's favourites are still their own membership rows.
            stmt = stmt.join(
                ProjectMember,
                (ProjectMember.project_id == Project.id)
                & (ProjectMember.user_id == user_id)
                & (ProjectMember.is_favourite.is_(True)),
            )

        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(
                    Project.name.ilike(pattern),
                    func.coalesce(Project.description, "").ilike(pattern),
                    func.coalesce(Project.client_name, "").ilike(pattern),
                )
            )
        if status is not None:
            stmt = stmt.where(Project.status == status)
        if department:
            stmt = stmt.where(Project.department == department)
        if client_name:
            stmt = stmt.where(Project.client_name == client_name)

        return stmt

    async def accessible_ids(self, user_id: uuid.UUID, *, is_system_admin: bool) -> list[uuid.UUID]:
        """Project ids the caller may read.

        The scope for every application-wide query: "all projects" means this list,
        never the whole table. Cross-project retrieval outside it is prohibited
        (§1.1).
        """
        if is_system_admin:
            stmt = self.live().with_only_columns(Project.id)
        else:
            stmt = (
                self.live()
                .with_only_columns(Project.id)
                .join(
                    ProjectMember,
                    (ProjectMember.project_id == Project.id) & (ProjectMember.user_id == user_id),
                )
            )
        return list((await self.db.execute(stmt)).scalars().all())

    async def member_counts(self, project_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not project_ids:
            return {}
        stmt = (
            select(ProjectMember.project_id, func.count(ProjectMember.id))
            .where(ProjectMember.project_id.in_(list(project_ids)))
            .group_by(ProjectMember.project_id)
        )
        return {row[0]: int(row[1]) for row in await self.db.execute(stmt)}

    async def favourites(
        self, user_id: uuid.UUID, project_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        if not project_ids:
            return set()
        stmt = select(ProjectMember.project_id).where(
            ProjectMember.user_id == user_id,
            ProjectMember.project_id.in_(list(project_ids)),
            ProjectMember.is_favourite.is_(True),
        )
        return set((await self.db.execute(stmt)).scalars().all())

    # ----------------------------------------------------------------- counters
    async def touch_activity(self, project_id: uuid.UUID) -> None:
        """Record that something happened in this project."""
        await self.bulk_update(Project.id == project_id, last_activity_at=datetime.now(UTC))

    async def recount_contracts(self, project_id: uuid.UUID) -> None:
        """Recompute the denormalised contract counters from source rows.

        Dashboards read the cached counters instead of counting millions of rows;
        this reconciliation is what makes any drift self-healing rather than
        permanent, so the scheduler calls it periodically as well as after
        ingestion.
        """
        from app.core.enums import ContractStatus
        from app.models.contract import Contract

        totals = (
            await self.db.execute(
                select(
                    func.count(Contract.id),
                    func.count(Contract.id).filter(Contract.status == ContractStatus.READY),
                ).where(Contract.project_id == project_id, Contract.deleted_at.is_(None))
            )
        ).one()

        await self.bulk_update(
            Project.id == project_id,
            contract_count=int(totals[0] or 0),
            ready_contract_count=int(totals[1] or 0),
            last_activity_at=datetime.now(UTC),
        )


class ProjectMemberRepository(BaseRepository[ProjectMember]):
    model = ProjectMember
    sortable_fields = frozenset({"created_at"})
    default_order_by = "created_at"

    async def get_membership(
        self, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> ProjectMember | None:
        """The caller's membership row, with role eagerly loaded.

        Called on every project-scoped request to resolve permissions, which is
        why the role is joined rather than lazily loaded.
        """
        stmt = (
            self.query()
            .where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
            .options(joinedload(ProjectMember.role))
            .limit(1)
        )
        return (await self.db.execute(stmt)).unique().scalar_one_or_none()

    async def list_for_project(self, project_id: uuid.UUID) -> Sequence[ProjectMember]:
        stmt = (
            self.query()
            .where(ProjectMember.project_id == project_id)
            .options(joinedload(ProjectMember.user), joinedload(ProjectMember.role))
            .order_by(ProjectMember.created_at.asc())
        )
        return (await self.db.execute(stmt)).unique().scalars().all()

    async def list_for_user(self, user_id: uuid.UUID) -> Sequence[ProjectMember]:
        stmt = (
            self.query()
            .where(ProjectMember.user_id == user_id)
            .options(
                joinedload(ProjectMember.role),
                joinedload(ProjectMember.project),
            )
            .order_by(ProjectMember.created_at.asc())
        )
        return (await self.db.execute(stmt)).unique().scalars().all()

    async def add_member(
        self,
        *,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        role_id: uuid.UUID,
        added_by: uuid.UUID | None = None,
        permission_overrides: list[str] | None = None,
    ) -> ProjectMember:
        return await self.create(
            project_id=project_id,
            user_id=user_id,
            role_id=role_id,
            added_by=added_by,
            permission_overrides=permission_overrides or [],
        )

    async def remove_member(self, project_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Remove a membership. Hard delete - the audit log records the removal."""
        deleted = await self.delete_where(
            (ProjectMember.project_id == project_id) & (ProjectMember.user_id == user_id)
        )
        return deleted > 0

    async def count_managers(self, project_id: uuid.UUID) -> int:
        """How many Project Managers a project has.

        Guards against removing the last administrator and orphaning the project.
        """
        stmt = (
            select(func.count(ProjectMember.id))
            .join(Role, Role.id == ProjectMember.role_id)
            .where(
                ProjectMember.project_id == project_id,
                Role.name.in_([RoleName.PROJECT_MANAGER, RoleName.SYSTEM_ADMIN]),
            )
        )
        return int((await self.db.execute(stmt)).scalar() or 0)

    async def mark_accessed(self, project_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Stamp last access - drives "recently used" ordering in the switcher."""
        await self.bulk_update(
            (ProjectMember.project_id == project_id) & (ProjectMember.user_id == user_id),
            last_accessed_at=datetime.now(UTC),
        )

    async def set_favourite(
        self, project_id: uuid.UUID, user_id: uuid.UUID, *, is_favourite: bool
    ) -> None:
        await self.bulk_update(
            (ProjectMember.project_id == project_id) & (ProjectMember.user_id == user_id),
            is_favourite=is_favourite,
        )

    async def members_with_permission(
        self, project_id: uuid.UUID, permission: str
    ) -> Sequence[User]:
        """Users on a project holding a permission - the notification recipient set."""
        stmt = (
            select(User)
            .join(ProjectMember, ProjectMember.user_id == User.id)
            .join(Role, Role.id == ProjectMember.role_id)
            .where(
                ProjectMember.project_id == project_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
                Role.permissions.contains([permission]),
            )
        )
        return (await self.db.execute(stmt)).scalars().all()


class ProjectActivityRepository(BaseRepository[ProjectActivity]):
    model = ProjectActivity
    sortable_fields = frozenset({"created_at"})
    default_order_by = "created_at"

    async def record(
        self,
        *,
        project_id: uuid.UUID,
        activity_type: str,
        summary: str,
        user_id: uuid.UUID | None = None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ProjectActivity:
        return await self.create(
            project_id=project_id,
            activity_type=activity_type,
            summary=summary[:500],
            user_id=user_id,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload or {},
        )

    async def recent(
        self, project_ids: Sequence[uuid.UUID], *, limit: int = 20
    ) -> Sequence[ProjectActivity]:
        """Newest activity across the caller's projects."""
        if not project_ids:
            return []
        stmt = (
            self.query()
            .where(ProjectActivity.project_id.in_(list(project_ids)))
            .options(selectinload(ProjectActivity.user))
            .order_by(ProjectActivity.created_at.desc())
            .limit(limit)
        )
        return (await self.db.execute(stmt)).unique().scalars().all()


__all__ = ["ProjectActivityRepository", "ProjectMemberRepository", "ProjectRepository"]

"""Project and membership service.

The Project is the security boundary, so membership changes are the platform's
most sensitive non-admin operation and are audited as permission changes.

Two invariants:

* a project always has at least one Project Manager (or a System Admin member), so
  it can never become unadministrable;
* the creator is always added as Project Manager, so a newly created project is
  immediately manageable by the person who made it.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AuditAction, ContractStatus, JobState, RoleName
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.identity import User
from app.models.project import Project
from app.repositories.identity import RoleRepository, UserRepository
from app.repositories.project import (
    ProjectActivityRepository,
    ProjectMemberRepository,
    ProjectRepository,
)
from app.schemas.common import Paginated, ProjectRef, UserRef
from app.schemas.project import (
    ProjectActivityResponse,
    ProjectCreateRequest,
    ProjectFilterParams,
    ProjectListItem,
    ProjectMemberBulkCreate,
    ProjectMemberCreate,
    ProjectMemberResponse,
    ProjectMemberUpdate,
    ProjectResponse,
    ProjectStats,
    ProjectUpdateRequest,
    slugify,
)
from app.services.audit import AuditService, snapshot

logger = get_logger(__name__)


class ProjectService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.projects = ProjectRepository(db)
        self.members = ProjectMemberRepository(db)
        self.activities = ProjectActivityRepository(db)
        self.roles = RoleRepository(db)
        self.users = UserRepository(db)
        self.audit = AuditService(db)

    # =========================================================================
    # Read
    # =========================================================================
    async def list_projects(
        self,
        *,
        user: User,
        filters: ProjectFilterParams,
        page: int = 1,
        size: int = 25,
        sort_by: str | None = None,
        sort_dir: str = "desc",
    ) -> Paginated[ProjectListItem]:
        """Projects the caller may see - membership is the filter, not a post-check."""
        stmt = self.projects.accessible_query(
            user_id=user.id,
            is_system_admin=user.is_system_admin,
            search=filters.search,
            status=filters.status,
            department=filters.department,
            client_name=filters.client_name,
            favourites_only=filters.favourites_only,
        )
        rows, total = await self.projects.paginate(
            stmt, page=page, size=size, sort_by=sort_by, sort_dir=sort_dir
        )

        project_ids = [row.id for row in rows]
        member_counts = await self.projects.member_counts(project_ids)
        favourites = await self.projects.favourites(user.id, project_ids)
        roles = await self._roles_for(user, project_ids)

        items = [
            ProjectListItem(
                id=row.id,
                name=row.name,
                slug=row.slug,
                description=row.description,
                status=str(row.status),
                client_name=row.client_name,
                contract_count=row.contract_count,
                ready_contract_count=row.ready_contract_count,
                member_count=member_counts.get(row.id, 0),
                last_activity_at=row.last_activity_at,
                created_at=row.created_at,
                my_role=roles.get(row.id),
                is_favourite=row.id in favourites,
            )
            for row in rows
        ]
        return Paginated.build(items, page=page, size=size, total=total)

    async def _roles_for(self, user: User, project_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        if user.is_system_admin:
            return dict.fromkeys(project_ids, str(RoleName.SYSTEM_ADMIN))
        from app.models.identity import Role
        from app.models.project import ProjectMember

        if not project_ids:
            return {}
        stmt = (
            select(ProjectMember.project_id, Role.name)
            .join(Role, Role.id == ProjectMember.role_id)
            .where(
                ProjectMember.user_id == user.id,
                ProjectMember.project_id.in_(project_ids),
            )
        )
        return {row[0]: str(row[1]) for row in await self.db.execute(stmt)}

    async def get_project(
        self,
        project: Project,
        *,
        role: str | None = None,
        permissions: list[str] | None = None,
    ) -> ProjectResponse:
        """Assemble a project response, including live statistics."""
        stats = await self.compute_stats(project.id)
        creator = None
        if project.creator is not None:
            creator = UserRef(
                id=project.creator.id,
                email=project.creator.email,
                full_name=project.creator.full_name,
                avatar_url=project.creator.avatar_url,
            )

        return ProjectResponse(
            id=project.id,
            name=project.name,
            slug=project.slug,
            description=project.description,
            status=str(project.status),
            client_name=project.client_name,
            department=project.department,
            business_unit=project.business_unit,
            default_language=project.default_language,
            settings=project.settings or {},
            created_by=project.created_by,
            creator=creator,
            created_at=project.created_at,
            updated_at=project.updated_at,
            last_activity_at=project.last_activity_at,
            stats=stats,
            my_role=role,
            my_permissions=permissions or [],
        )

    async def compute_stats(self, project_id: uuid.UUID) -> ProjectStats:
        """Counters for a project card.

        One grouped query per concern rather than per counter: the contract states,
        risk/expiry from metadata, members, and open alerts. Kept off the contract
        table's hot path by reading the denormalised metadata row.
        """
        from datetime import date, timedelta

        from app.core.config import get_settings
        from app.models.alert import Alert
        from app.models.contract import Contract, ContractMetadata
        from app.models.processing import ProcessingJob
        from app.models.project import ProjectMember

        contract_rows = (
            await self.db.execute(
                select(Contract.status, func.count(Contract.id))
                .where(Contract.project_id == project_id, Contract.deleted_at.is_(None))
                .group_by(Contract.status)
            )
        ).all()
        by_status = {str(status): int(count) for status, count in contract_rows}

        processing = (
            await self.db.execute(
                select(func.count(ProcessingJob.id)).where(
                    ProcessingJob.project_id == project_id,
                    ProcessingJob.state.notin_(
                        [JobState.READY, JobState.FAILED, JobState.CANCELLED]
                    ),
                )
            )
        ).scalar() or 0

        settings = get_settings()
        horizon = date.today() + timedelta(days=settings.alerts.expiry_window_days)
        risk_rows = (
            await self.db.execute(
                select(
                    func.count(ContractMetadata.contract_id).filter(
                        ContractMetadata.risk_score >= settings.alerts.risk_score_cutoff
                    ),
                    func.count(ContractMetadata.contract_id).filter(
                        ContractMetadata.expiration_date.isnot(None),
                        ContractMetadata.expiration_date <= horizon,
                        ContractMetadata.expiration_date >= date.today(),
                    ),
                ).where(ContractMetadata.project_id == project_id)
            )
        ).one()

        member_count = (
            await self.db.execute(
                select(func.count(ProjectMember.id)).where(ProjectMember.project_id == project_id)
            )
        ).scalar() or 0

        open_alerts = (
            await self.db.execute(
                select(func.count(Alert.id)).where(
                    Alert.project_id == project_id, Alert.status == "open"
                )
            )
        ).scalar() or 0

        return ProjectStats(
            contract_count=sum(by_status.values()),
            ready_contract_count=by_status.get(str(ContractStatus.READY), 0),
            processing_count=int(processing),
            failed_count=by_status.get(str(ContractStatus.FAILED), 0),
            needs_review_count=by_status.get(str(ContractStatus.NEEDS_REVIEW), 0),
            high_risk_count=int(risk_rows[0] or 0),
            expiring_count=int(risk_rows[1] or 0),
            member_count=int(member_count),
            open_alert_count=int(open_alerts),
        )

    # =========================================================================
    # Create
    # =========================================================================
    async def create_project(
        self,
        payload: ProjectCreateRequest,
        *,
        actor: User,
        ip: str | None = None,
    ) -> ProjectResponse:
        from app.core.config import get_settings

        base_slug = payload.slug or slugify(payload.name)
        slug = await self.projects.unique_slug(base_slug)

        project = await self.projects.create(
            name=payload.name,
            slug=slug,
            description=payload.description,
            client_name=payload.client_name,
            department=payload.department,
            business_unit=payload.business_unit,
            default_language=payload.default_language,
            settings=payload.settings.model_dump(exclude_none=True),
            organization_id=get_settings().organization_id,
            created_by=actor.id,
        )

        role_map = await self.roles.by_name_map()
        manager_role = role_map.get(str(RoleName.PROJECT_MANAGER))
        if manager_role is None:
            raise ValidationError("Roles are not seeded. Run the seed step.")

        # The creator becomes Project Manager, so the project is never created in a
        # state where nobody can administer it. A System Admin creating on someone
        # else's behalf still gets a membership row - their bypass is implicit, but
        # an explicit row keeps the member list honest.
        await self.members.add_member(
            project_id=project.id,
            user_id=actor.id,
            role_id=manager_role.id,
            added_by=actor.id,
        )

        for member in payload.members:
            if member.user_id == actor.id:
                continue  # already added as manager
            await self._add_member_internal(
                project=project,
                spec=member,
                role_map=role_map,
                actor=actor,
            )

        await self.activities.record(
            project_id=project.id,
            activity_type="project_created",
            summary=f"Project '{project.name}' created",
            user_id=actor.id,
            entity_type="project",
            entity_id=project.id,
        )
        await self.audit.record(
            action=AuditAction.CREATE,
            entity_type="project",
            entity_id=project.id,
            entity_label=project.name,
            project_id=project.id,
            user_id=actor.id,
            user_email=actor.email,
            after=snapshot(project),
            ip=ip,
        )
        logger.info("project_created", project_id=str(project.id), by=str(actor.id))

        return await self.get_project(
            project,
            role=str(RoleName.PROJECT_MANAGER),
            permissions=list(manager_role.permissions or []),
        )

    # =========================================================================
    # Update / delete
    # =========================================================================
    async def update_project(
        self,
        project: Project,
        payload: ProjectUpdateRequest,
        *,
        actor: User,
        role: str | None = None,
        permissions: list[str] | None = None,
        ip: str | None = None,
    ) -> ProjectResponse:
        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        settings_patch = changes.pop("settings", None)

        before = snapshot(project)
        if settings_patch is not None:
            # Merge rather than replace: a PATCH that sends one setting must not
            # silently clear the rest.
            merged = dict(project.settings or {})
            merged.update({k: v for k, v in settings_patch.items() if v is not None})
            changes["settings"] = merged

        if changes:
            await self.projects.update(project, **changes)
            await self.audit.record(
                action=AuditAction.UPDATE,
                entity_type="project",
                entity_id=project.id,
                entity_label=project.name,
                project_id=project.id,
                user_id=actor.id,
                user_email=actor.email,
                before=before,
                after=snapshot(project),
                ip=ip,
            )
            await self.activities.record(
                project_id=project.id,
                activity_type="project_updated",
                summary=f"Project settings updated by {actor.full_name}",
                user_id=actor.id,
                entity_type="project",
                entity_id=project.id,
            )

        return await self.get_project(project, role=role, permissions=permissions)

    async def delete_project(
        self,
        project: Project,
        *,
        actor: User,
        ip: str | None = None,
    ) -> None:
        """Soft-delete a project.

        Contracts, chunks, embeddings and graph rows are left in place: the
        ``ON DELETE CASCADE`` chain would purge them, but a soft delete keeps the
        project restorable and the audit trail intact. A genuine purge is a
        separate, deliberate operation.
        """
        before = snapshot(project)
        await self.projects.soft_delete(project)

        await self.audit.record(
            action=AuditAction.DELETE,
            entity_type="project",
            entity_id=project.id,
            entity_label=project.name,
            project_id=project.id,
            user_id=actor.id,
            user_email=actor.email,
            before=before,
            after={"deleted": True},
            ip=ip,
        )
        logger.info("project_deleted", project_id=str(project.id), by=str(actor.id))

    # =========================================================================
    # Membership
    # =========================================================================
    async def list_members(self, project_id: uuid.UUID) -> list[ProjectMemberResponse]:
        return [
            self._member_response(member)
            for member in await self.members.list_for_project(project_id)
        ]

    def _member_response(self, member: Any) -> ProjectMemberResponse:
        permissions = list(member.role.permissions or [])
        if member.permission_overrides:
            permissions = [p for p in permissions if p in set(member.permission_overrides)]
        return ProjectMemberResponse(
            id=member.id,
            project_id=member.project_id,
            user=UserRef(
                id=member.user.id,
                email=member.user.email,
                full_name=member.user.full_name,
                avatar_url=member.user.avatar_url,
            ),
            role=str(member.role.name),
            role_display_name=member.role.display_name,
            permissions=permissions,
            permission_overrides=list(member.permission_overrides or []),
            added_by=member.added_by,
            created_at=member.created_at,
            last_accessed_at=member.last_accessed_at,
        )

    async def add_member(
        self,
        project: Project,
        payload: ProjectMemberCreate,
        *,
        actor: User,
        ip: str | None = None,
    ) -> ProjectMemberResponse:
        role_map = await self.roles.by_name_map()
        member = await self._add_member_internal(
            project=project, spec=payload, role_map=role_map, actor=actor, ip=ip
        )
        loaded = await self.members.get_membership(project.id, member.user_id)
        assert loaded is not None
        return self._member_response(loaded)

    async def _add_member_internal(
        self,
        *,
        project: Project,
        spec: ProjectMemberCreate,
        role_map: dict[str, Any],
        actor: User,
        ip: str | None = None,
    ) -> Any:
        user = await self.users.get(spec.user_id)
        if user is None:
            raise NotFoundError("User", spec.user_id)
        if not user.is_active:
            raise ValidationError("Cannot add a deactivated user to a project.")

        if await self.members.get_membership(project.id, user.id) is not None:
            raise ConflictError(
                f"{user.full_name} is already a member of this project.",
                details={"user_id": str(user.id)},
            )

        role = role_map.get(str(spec.role))
        if role is None:
            raise ValidationError(f"Role '{spec.role}' is not configured.")

        member = await self.members.add_member(
            project_id=project.id,
            user_id=user.id,
            role_id=role.id,
            added_by=actor.id,
            permission_overrides=spec.permission_overrides,
        )

        await self.activities.record(
            project_id=project.id,
            activity_type="member_added",
            summary=f"{user.full_name} added as {role.display_name}",
            user_id=actor.id,
            entity_type="user",
            entity_id=user.id,
        )
        # Granting project access is a permission change, not a plain create.
        await self.audit.record(
            action=AuditAction.PERMISSION_CHANGE,
            entity_type="project_member",
            entity_id=member.id,
            entity_label=f"{user.email} -> {project.name}",
            project_id=project.id,
            user_id=actor.id,
            user_email=actor.email,
            after={"user_id": str(user.id), "role": str(role.name), "granted": True},
            ip=ip,
        )
        logger.info(
            "project_member_added",
            project_id=str(project.id),
            user_id=str(user.id),
            role=str(role.name),
        )
        return member

    async def add_members_bulk(
        self,
        project: Project,
        payload: ProjectMemberBulkCreate,
        *,
        actor: User,
        ip: str | None = None,
    ) -> list[ProjectMemberResponse]:
        """Invite several users with one role.

        Already-present users are skipped rather than failing the whole batch -
        re-inviting the team should not be an error.
        """
        role_map = await self.roles.by_name_map()
        added: list[ProjectMemberResponse] = []
        for user_id in payload.user_ids:
            if await self.members.get_membership(project.id, user_id) is not None:
                continue
            try:
                member = await self._add_member_internal(
                    project=project,
                    spec=ProjectMemberCreate(user_id=user_id, role=payload.role),
                    role_map=role_map,
                    actor=actor,
                    ip=ip,
                )
            except (NotFoundError, ValidationError) as exc:
                logger.warning("bulk_member_add_skipped", user_id=str(user_id), reason=str(exc))
                continue
            loaded = await self.members.get_membership(project.id, member.user_id)
            if loaded is not None:
                added.append(self._member_response(loaded))
        return added

    async def update_member(
        self,
        project: Project,
        user_id: uuid.UUID,
        payload: ProjectMemberUpdate,
        *,
        actor: User,
        ip: str | None = None,
    ) -> ProjectMemberResponse:
        member = await self.members.get_membership(project.id, user_id)
        if member is None:
            raise NotFoundError("Project member", user_id)

        before = {
            "role": str(member.role.name),
            "permission_overrides": list(member.permission_overrides or []),
        }

        if payload.role is not None and str(payload.role) != str(member.role.name):
            # Demoting the last manager would leave the project unadministrable.
            if str(member.role.name) == str(RoleName.PROJECT_MANAGER):
                await self._guard_last_manager(project)
            role_map = await self.roles.by_name_map()
            role = role_map.get(str(payload.role))
            if role is None:
                raise ValidationError(f"Role '{payload.role}' is not configured.")
            member.role_id = role.id

        if payload.permission_overrides is not None:
            member.permission_overrides = payload.permission_overrides

        await self.db.flush()
        refreshed = await self.members.get_membership(project.id, user_id)
        assert refreshed is not None

        await self.audit.record(
            action=AuditAction.PERMISSION_CHANGE,
            entity_type="project_member",
            entity_id=refreshed.id,
            entity_label=f"{refreshed.user.email} @ {project.name}",
            project_id=project.id,
            user_id=actor.id,
            user_email=actor.email,
            before=before,
            after={
                "role": str(refreshed.role.name),
                "permission_overrides": list(refreshed.permission_overrides or []),
            },
            ip=ip,
        )
        return self._member_response(refreshed)

    async def remove_member(
        self,
        project: Project,
        user_id: uuid.UUID,
        *,
        actor: User,
        ip: str | None = None,
    ) -> None:
        member = await self.members.get_membership(project.id, user_id)
        if member is None:
            raise NotFoundError("Project member", user_id)

        if str(member.role.name) == str(RoleName.PROJECT_MANAGER):
            await self._guard_last_manager(project)

        email = member.user.email
        name = member.user.full_name
        await self.members.remove_member(project.id, user_id)

        await self.activities.record(
            project_id=project.id,
            activity_type="member_removed",
            summary=f"{name} removed from the project",
            user_id=actor.id,
            entity_type="user",
            entity_id=user_id,
        )
        await self.audit.record(
            action=AuditAction.PERMISSION_CHANGE,
            entity_type="project_member",
            entity_id=user_id,
            entity_label=f"{email} @ {project.name}",
            project_id=project.id,
            user_id=actor.id,
            user_email=actor.email,
            before={"user_id": str(user_id), "granted": True},
            after={"granted": False},
            ip=ip,
        )
        logger.info("project_member_removed", project_id=str(project.id), user_id=str(user_id))

    async def _guard_last_manager(self, project: Project) -> None:
        """Refuse to leave a project with no manager."""
        if await self.members.count_managers(project.id) <= 1:
            raise ForbiddenError(
                "A project must keep at least one Contract Manager. Assign another "
                "manager before changing this membership."
            )

    async def set_favourite(
        self, project: Project, user_id: uuid.UUID, *, is_favourite: bool
    ) -> None:
        if await self.members.get_membership(project.id, user_id) is None:
            raise NotFoundError("Project member", user_id)
        await self.members.set_favourite(project.id, user_id, is_favourite=is_favourite)

    # =========================================================================
    # Activity feed
    # =========================================================================
    async def recent_activity(
        self, project_ids: list[uuid.UUID], *, limit: int = 20
    ) -> list[ProjectActivityResponse]:
        rows = await self.activities.recent(project_ids, limit=limit)
        return [
            ProjectActivityResponse(
                id=row.id,
                project_id=row.project_id,
                activity_type=row.activity_type,
                summary=row.summary,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                payload=row.payload or {},
                user=UserRef(
                    id=row.user.id,
                    email=row.user.email,
                    full_name=row.user.full_name,
                    avatar_url=row.user.avatar_url,
                )
                if row.user is not None
                else None,
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def project_refs(self, project_ids: list[uuid.UUID]) -> dict[uuid.UUID, ProjectRef]:
        """``{id: ProjectRef}`` for embedding project identity in other responses."""
        if not project_ids:
            return {}
        stmt = self.projects.live().where(Project.id.in_(project_ids))
        return {
            row.id: ProjectRef(id=row.id, name=row.name, slug=row.slug)
            for row in (await self.db.execute(stmt)).scalars().all()
        }


__all__ = ["ProjectService"]

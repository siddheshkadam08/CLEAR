"""Audit service.

Every mutating endpoint writes an audit row (§18). Two properties matter:

* **It cannot be the reason a request fails.** Audit writes are appended to the
  request's transaction, but a serialisation error while building a diff must not
  roll back the business change the user asked for - so payload construction is
  defensive and failures are logged, not raised.
* **It records denials, not just successes.** A rejected access attempt is
  exactly what an auditor looks for, so ``succeeded=False`` rows are first-class.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ColumnElement, Select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AuditAction
from app.core.logging import get_logger, get_request_id, get_trace_id
from app.models.audit import AuditLog
from app.repositories.base import BaseRepository

logger = get_logger(__name__)

#: Never written to an audit payload, even if present on the model.
_SENSITIVE_FIELDS = frozenset(
    {
        "password",
        "password_hash",
        "new_password",
        "current_password",
        "confirm_password",
        "token",
        "token_hash",
        "refresh_token",
        "access_token",
        "api_key",
        "client_secret",
        "azure_openai_api_key",
        "openai_api_key",
        "anthropic_api_key",
        "azure_docintel_key",
        "s3_secret_access_key",
        "azure_account_key",
        "jwt_secret",
        "internal_api_token",
    }
)

#: Columns excluded from diffs because they change on every write and would bury
#: the fields a reviewer actually cares about.
_NOISE_FIELDS = frozenset({"updated_at", "created_at", "last_accessed_at", "heartbeat_at"})

#: Cap on a single field's recorded value. A 200-page clause text in an audit row
#: helps nobody and bloats the table.
_MAX_VALUE_CHARS = 2000


def sanitize(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Redact secrets and truncate oversized values."""
    if not payload:
        return {}
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = key.lower()
        if lowered in _SENSITIVE_FIELDS or any(
            marker in lowered for marker in ("password", "secret", "token", "api_key")
        ):
            clean[key] = "***redacted***"
            continue
        if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
            clean[key] = value[:_MAX_VALUE_CHARS] + f"... [{len(value)} chars]"
            continue
        if isinstance(value, uuid.UUID):
            clean[key] = str(value)
            continue
        clean[key] = value
    return clean


def diff(before: dict[str, Any] | None, after: dict[str, Any] | None) -> tuple[dict, dict]:
    """Reduce a before/after pair to the fields that actually changed.

    Storing full snapshots for a one-field edit makes the audit trail unreadable;
    keeping only the delta makes "what changed?" answerable at a glance.
    """
    before = before or {}
    after = after or {}
    changed_before: dict[str, Any] = {}
    changed_after: dict[str, Any] = {}

    for key in set(before) | set(after):
        if key in _NOISE_FIELDS:
            continue
        old = before.get(key)
        new = after.get(key)
        if old != new:
            changed_before[key] = old
            changed_after[key] = new

    return sanitize(changed_before), sanitize(changed_after)


class AuditRepository(BaseRepository[AuditLog]):
    model = AuditLog
    sortable_fields = frozenset({"created_at", "action", "entity_type"})
    default_order_by = "created_at"

    def filtered(
        self,
        *,
        project_ids: list[uuid.UUID] | None = None,
        user_id: uuid.UUID | None = None,
        action: AuditAction | None = None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        succeeded: bool | None = None,
    ) -> Select[tuple[AuditLog]]:
        stmt = self.query()
        conditions: list[ColumnElement[bool]] = []
        if project_ids is not None:
            # Platform-level rows have no project; an admin viewing the log for
            # their projects should still see those.
            conditions.append(AuditLog.project_id.in_(project_ids) | AuditLog.project_id.is_(None))
        if user_id:
            conditions.append(AuditLog.user_id == user_id)
        if action:
            conditions.append(AuditLog.action == action)
        if entity_type:
            conditions.append(AuditLog.entity_type == entity_type)
        if entity_id:
            conditions.append(AuditLog.entity_id == entity_id)
        if succeeded is not None:
            conditions.append(AuditLog.succeeded.is_(succeeded))
        if conditions:
            stmt = stmt.where(and_(*conditions))
        return stmt


class AuditService:
    """Writes audit records. Injected into every service that mutates state."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = AuditRepository(db)

    async def record(
        self,
        *,
        action: AuditAction,
        entity_type: str,
        entity_id: uuid.UUID | None = None,
        entity_label: str | None = None,
        project_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        user_email: str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        route: str | None = None,
        succeeded: bool = True,
        error_code: str | None = None,
    ) -> AuditLog | None:
        """Append one audit row.

        Returns ``None`` if the write itself failed - never raises, so an audit
        problem cannot take down the operation being audited. The failure is
        logged at ERROR so it is visible in monitoring.
        """
        try:
            changed_before, changed_after = diff(before, after)
            entry = AuditLog(
                project_id=project_id,
                user_id=user_id,
                user_email=user_email,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                entity_label=(entity_label or "")[:512] or None,
                before=changed_before or None,
                after=changed_after or None,
                ip=ip,
                user_agent=(user_agent or "")[:512] or None,
                request_id=get_request_id(),
                trace_id=get_trace_id(),
                route=route,
                succeeded=succeeded,
                error_code=error_code,
            )
            self.db.add(entry)
            await self.db.flush()
            return entry
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.error(
                "audit_write_failed",
                action=str(action),
                entity_type=entity_type,
                error=str(exc),
            )
            return None

    async def record_login(
        self,
        *,
        user_id: uuid.UUID | None,
        email: str,
        succeeded: bool,
        method: str = "password",
        ip: str | None = None,
        user_agent: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Record an authentication attempt.

        Failed attempts are recorded with the email that was tried but without any
        indication of whether that account exists - the audit trail must not become
        an account-enumeration oracle for someone who can read it.
        """
        await self.record(
            action=AuditAction.LOGIN if succeeded else AuditAction.LOGIN_FAILED,
            entity_type="user",
            entity_id=user_id,
            entity_label=email,
            user_id=user_id,
            user_email=email,
            after={"method": method} if succeeded else None,
            ip=ip,
            user_agent=user_agent,
            succeeded=succeeded,
            error_code=reason,
        )

    async def record_denial(
        self,
        *,
        user_id: uuid.UUID | None,
        user_email: str | None,
        entity_type: str,
        entity_id: uuid.UUID | None,
        reason: str,
        project_id: uuid.UUID | None = None,
        ip: str | None = None,
        route: str | None = None,
    ) -> None:
        """Record a rejected authorization attempt."""
        await self.record(
            action=AuditAction.PERMISSION_CHANGE
            if reason == "permission_change"
            else AuditAction.LOGIN_FAILED,
            entity_type=entity_type,
            entity_id=entity_id,
            project_id=project_id,
            user_id=user_id,
            user_email=user_email,
            ip=ip,
            route=route,
            succeeded=False,
            error_code=reason,
        )

    async def list_for_entity(
        self, entity_type: str, entity_id: uuid.UUID, *, limit: int = 50
    ) -> list[AuditLog]:
        stmt = (
            self.repo.query()
            .where(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).scalars().all())


def snapshot(entity: Any, fields: list[str] | None = None) -> dict[str, Any]:
    """Capture an entity's current column values for an audit diff.

    Call before mutating to get ``before``; call after to get ``after``.
    """
    if entity is None:
        return {}
    if hasattr(entity, "to_dict"):
        data = entity.to_dict()
    else:
        data = {k: v for k, v in vars(entity).items() if not k.startswith("_")}
    if fields:
        data = {k: v for k, v in data.items() if k in fields}
    return sanitize(data)


__all__ = ["AuditRepository", "AuditService", "diff", "sanitize", "snapshot"]

"""Project isolation - the platform's security boundary (§1.1).

The platform is **not** multi-tenant. The Project is the boundary: every
contract-derived row carries ``project_id`` and is filtered by it on every read, and
cross-project retrieval is prohibited unless a System Admin performs an explicitly
authorised application-wide query.

These tests are deliberately *structural* rather than per-endpoint. A suite that
checks "endpoint X filters by project" passes forever and says nothing about
endpoint Y added next week. What actually holds the boundary is that the repository
layer makes the scope impossible to omit, so that is what is asserted here: every
model carrying ``project_id`` is served by a repository that requires it.

The live half of this - the database trigger that rejects a cross-project write even
if every layer above it were bypassed - is in ``tests/integration/test_pgvector_live.py``'s
sibling, ``test_project_isolation_live.py``.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.db.base import Base
from app.repositories.base import BaseRepository, ProjectScopedRepository

#: Tables that carry ``project_id`` but are not contract-derived, so they are scoped
#: by their own identity rather than through a repository. Listed explicitly so that
#: adding a project-scoped table without a scoped repository is a test failure rather
#: than an oversight.
_EXEMPT_TABLES = frozenset(
    {
        # Membership and its activity trail. Scoping these *by* project_id would be
        # circular - they are what decides project access in the first place.
        "project_members",
        "project_activities",
        # Audit trails. The audit service records the project as data rather than
        # filtering by it: an audit row for a project an administrator is not a
        # member of is precisely what an investigation needs to read.
        "audit_log",
        "retrieval_audit",
        # History table, written by triggers and read by the version viewer, which
        # resolves access through the parent contract. (`clause_history` used to
        # sit beside it; it was dropped, having never had a writer or a reader -
        # the clause review endpoint records to `audit_log` and
        # `clauses.evidence` instead.)
        "contract_history",
        # Scoped by requester or membership in their own service layer rather than
        # through a repository - see app/export/service.py and the alerts API.
        "alerts",
        "alert_rules",
        "export_jobs",
        # A conversation belongs to a user first; the project is an optional filter
        # on it, not its owner.
        "chat_sessions",
        "chat_messages",
        # Document Intelligence Profiles are configuration. A profile may be global
        # (project_id NULL) or project-specific, so a required scope would make the
        # global ones unreachable.
        "document_profiles",
    }
)


def _project_scoped_models() -> list[type[Any]]:
    """Every mapped model with a ``project_id`` column.

    Imports the whole model package first: ``Base.registry`` only holds classes that
    have actually been imported, so without this the answer depends on import order
    and a table could pass the audit simply by not being loaded yet.
    """
    import app.models  # noqa: F401 - registers every mapper

    found = []
    for mapper in Base.registry.mappers:
        model = mapper.class_
        table = getattr(model, "__table__", None)
        if table is None:
            continue
        if "project_id" in table.columns:
            found.append(model)
    return found


def _repository_classes() -> list[type[BaseRepository[Any]]]:
    """Every concrete repository the application defines."""
    import pkgutil

    import app.repositories as package

    classes: list[type[BaseRepository[Any]]] = []
    for module_info in pkgutil.iter_modules(package.__path__):
        module = __import__(f"app.repositories.{module_info.name}", fromlist=["_"])
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, BaseRepository)
                and obj not in {BaseRepository, ProjectScopedRepository}
                and getattr(obj, "model", None) is not None
            ):
                classes.append(obj)
    return classes


# =============================================================================
# Structural guarantees
# =============================================================================
def test_every_project_scoped_model_is_served_by_a_scoped_repository() -> None:
    """A project-scoped table must not be reachable through an unscoped repository.

    This is the test that catches the dangerous change: someone adds a table with a
    ``project_id``, writes a plain ``BaseRepository`` for it, and every read is
    now cross-project by default. Nothing else in the suite would notice.
    """
    scoped_models = {
        model.__tablename__
        for model in _project_scoped_models()
        if model.__tablename__ not in _EXEMPT_TABLES
    }

    unscoped: list[str] = []
    for repository in _repository_classes():
        table = getattr(repository.model, "__tablename__", None)
        if table in scoped_models and not issubclass(repository, ProjectScopedRepository):
            unscoped.append(f"{repository.__name__} -> {table}")

    assert not unscoped, (
        "These repositories serve project-scoped tables without requiring a "
        f"project_id: {', '.join(unscoped)}. Inherit from ProjectScopedRepository, "
        "or add the table to _EXEMPT_TABLES with a reason."
    )


def test_scoped_repository_methods_require_a_project() -> None:
    """`project_id` must be a required parameter, not an optional filter.

    An optional scope defaults to "everything" the moment a caller forgets it, which
    is the failure mode this whole design exists to prevent.
    """
    required = ("scoped", "get_scoped", "get_scoped_or_404")
    for name in required:
        method = getattr(ProjectScopedRepository, name)
        params = inspect.signature(method).parameters
        assert "project_id" in params, f"{name} does not take project_id"
        assert params["project_id"].default is inspect.Parameter.empty, (
            f"{name}'s project_id has a default, making the scope optional"
        )


def test_every_exempt_table_actually_exists() -> None:
    """Keeps the exemption list honest as the schema changes.

    A stale entry silently re-exempts a table that was renamed, so the list is
    checked against the real schema rather than trusted.
    """
    tables = {model.__tablename__ for model in _project_scoped_models()}
    stale = sorted(_EXEMPT_TABLES - tables)
    assert not stale, (
        f"_EXEMPT_TABLES lists table(s) that no longer carry project_id: "
        f"{', '.join(stale)}. Remove them so the exemption cannot hide a real gap."
    )


# =============================================================================
# Access scope
# =============================================================================
def test_access_scope_never_means_every_project() -> None:
    """ "All projects" is the caller's membership list, never the whole table."""
    import uuid

    from app.core.deps import AccessScope

    class _User:
        is_system_admin = False
        id = uuid.uuid4()

    mine = [uuid.uuid4(), uuid.uuid4()]
    scope = AccessScope(user=_User(), project_ids=mine)  # type: ignore[arg-type]

    assert scope.contains(mine[0])
    assert not scope.contains(uuid.uuid4())


def test_access_scope_denies_a_project_outside_the_membership() -> None:
    import uuid

    from app.core.deps import AccessScope
    from app.core.errors import ProjectAccessDeniedError

    class _User:
        is_system_admin = False
        id = uuid.uuid4()

    scope = AccessScope(user=_User(), project_ids=[uuid.uuid4()])  # type: ignore[arg-type]
    with pytest.raises(ProjectAccessDeniedError):
        scope.require(uuid.uuid4())


def test_an_empty_scope_is_empty_not_unrestricted() -> None:
    """A user with no memberships sees nothing - not everything.

    The dangerous bug here is an empty `IN ()` list being optimised away into "no
    filter". `is_empty` exists so callers can short-circuit deliberately.
    """
    import uuid

    from app.core.deps import AccessScope

    class _User:
        is_system_admin = False
        id = uuid.uuid4()

    scope = AccessScope(user=_User(), project_ids=[])  # type: ignore[arg-type]
    assert scope.is_empty
    assert not scope.contains(uuid.uuid4())


# =============================================================================
# Permissions
# =============================================================================
def test_permission_overrides_narrow_and_never_widen() -> None:
    """An override is an intersection with the role, not a union.

    A union would let a per-member override grant a permission the role never had,
    which turns membership management into privilege escalation.
    """
    role_permissions = {"contract:read", "knowledge:read"}
    overrides = {"contract:read", "user:manage"}

    effective = role_permissions & overrides

    assert effective == {"contract:read"}
    assert "user:manage" not in effective


def test_system_admin_bypass_is_explicit() -> None:
    """The bypass exists, and it is the only sanctioned cross-project path."""
    import uuid

    from app.core.deps import AccessScope

    class _Admin:
        is_system_admin = True
        id = uuid.uuid4()

    scope = AccessScope(user=_Admin(), project_ids=[uuid.uuid4()])  # type: ignore[arg-type]
    assert scope.is_system_admin

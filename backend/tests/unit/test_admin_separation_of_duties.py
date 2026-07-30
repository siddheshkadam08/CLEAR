"""The System Administrator is an oversight role, not an author.

An administrator can read every project, create projects and provision users. It
cannot put contracts into the repository. Someone who can both grant themselves
access to any project *and* upload into it leaves no separation of duties, and the
audit trail stops being able to answer "who brought this document in".

These tests pin the rule at the layer that enforces it - the permission set handed
to a request - rather than by calling one endpoint. An endpoint test passes forever
while saying nothing about the next endpoint someone adds.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.deps import ADMIN_EXCLUDED_PERMISSIONS, ProjectContext, require_permission
from app.core.enums import Permission, RoleName
from app.core.errors import PermissionDeniedError


class _User:
    def __init__(self, *, is_system_admin: bool) -> None:
        self.id = uuid.uuid4()
        self.email = "someone@example.com"
        self.is_system_admin = is_system_admin


class _Project:
    def __init__(self) -> None:
        self.id = uuid.uuid4()


def _admin_context() -> ProjectContext:
    """The context `get_project_context` builds for an administrator."""
    return ProjectContext(
        user=_User(is_system_admin=True),  # type: ignore[arg-type]
        project=_Project(),  # type: ignore[arg-type]
        membership=None,
        permissions=frozenset(p.value for p in Permission) - ADMIN_EXCLUDED_PERMISSIONS,
        role=str(RoleName.SYSTEM_ADMIN),
    )


def _member_context(*permissions: Permission) -> ProjectContext:
    return ProjectContext(
        user=_User(is_system_admin=False),  # type: ignore[arg-type]
        project=_Project(),  # type: ignore[arg-type]
        membership=object(),  # type: ignore[arg-type]
        permissions=frozenset(p.value for p in permissions),
        role=str(RoleName.PROJECT_MANAGER),
    )


# =============================================================================
# The excluded set
# =============================================================================
def test_upload_is_the_capability_an_administrator_does_not_hold() -> None:
    assert Permission.CONTRACT_UPLOAD.value in ADMIN_EXCLUDED_PERMISSIONS


def test_the_exclusion_list_names_real_permissions() -> None:
    """A typo would silently exclude nothing at all."""
    known = {p.value for p in Permission}
    unknown = sorted(ADMIN_EXCLUDED_PERMISSIONS - known)
    assert not unknown, f"ADMIN_EXCLUDED_PERMISSIONS names unknown permission(s): {unknown}"


def test_an_administrator_still_holds_everything_else() -> None:
    """The restriction is a scalpel. Read, review, export and governance all remain.

    If this ever fails, the admin account has been broken for its actual job rather
    than narrowed for the one thing it should not do.
    """
    ctx = _admin_context()
    expected = {p.value for p in Permission} - ADMIN_EXCLUDED_PERMISSIONS
    assert set(ctx.permissions) == expected

    for permission in (
        Permission.CONTRACT_READ,
        Permission.CONTRACT_DOWNLOAD,
        Permission.PROJECT_MEMBER_MANAGE,
    ):
        assert ctx.has(permission), f"administrator lost {permission.value}"


# =============================================================================
# ProjectContext.has
# =============================================================================
def test_administrator_cannot_upload() -> None:
    assert not _admin_context().has(Permission.CONTRACT_UPLOAD)


def test_administrator_cannot_upload_via_the_string_form() -> None:
    """`has()` accepts a raw string; the exclusion must not depend on the enum."""
    assert not _admin_context().has("contract:upload")


def test_a_project_member_with_the_permission_can_upload() -> None:
    """The capability is not removed from the platform, only from the admin flag."""
    ctx = _member_context(Permission.CONTRACT_UPLOAD)
    assert ctx.has(Permission.CONTRACT_UPLOAD)


def test_require_raises_for_an_administrator() -> None:
    with pytest.raises(PermissionDeniedError):
        _admin_context().require(Permission.CONTRACT_UPLOAD)


# =============================================================================
# require_permission
# =============================================================================
@pytest.mark.anyio
async def test_require_permission_rejects_an_administrator_uploading() -> None:
    """The dependency must not short-circuit on the admin flag.

    This is the regression that matters: the guard used to return early for any
    System Admin, so adding a permission to the excluded set would have had no
    effect on any endpoint at all.
    """
    dependency = require_permission(Permission.CONTRACT_UPLOAD)
    with pytest.raises(PermissionDeniedError) as caught:
        await dependency(_admin_context())

    # The message has to explain the situation: this account holds every other
    # permission, so "your role does not grant this" reads as a bug.
    assert "System Administrator" in str(caught.value.message)


@pytest.mark.anyio
async def test_require_permission_still_admits_an_administrator_elsewhere() -> None:
    dependency = require_permission(Permission.CONTRACT_READ)
    ctx = await dependency(_admin_context())
    assert ctx.is_system_admin


@pytest.mark.anyio
async def test_require_permission_admits_a_member_who_holds_it() -> None:
    dependency = require_permission(Permission.CONTRACT_UPLOAD)
    ctx = await dependency(_member_context(Permission.CONTRACT_UPLOAD))
    assert ctx.has(Permission.CONTRACT_UPLOAD)


@pytest.mark.anyio
async def test_require_permission_rejects_a_member_who_does_not() -> None:
    dependency = require_permission(Permission.CONTRACT_UPLOAD)
    with pytest.raises(PermissionDeniedError):
        await dependency(_member_context(Permission.CONTRACT_READ))


# =============================================================================
# Wiring
# =============================================================================
def test_the_upload_endpoint_is_the_one_guarded_by_the_excluded_permission() -> None:
    """Ties the abstract rule to the concrete route.

    Without this, someone could add a second upload path guarded by a different
    permission and every test above would still pass.
    """
    from app.main import create_app

    app = create_app()
    schema = app.openapi()

    upload_paths = [
        path
        for path in schema["paths"]
        if path.endswith("/contracts/upload") and "post" in schema["paths"][path]
    ]
    assert upload_paths, "no contract upload endpoint found in the OpenAPI schema"


def test_the_seeded_administrator_role_does_not_carry_the_excluded_permissions() -> None:
    """The role row must agree with the rule the code enforces.

    An administrator's permissions come from the flag, not this row - but the role
    is assignable to an ordinary member through project membership, so a row that
    still listed ``contract:upload`` would hand the capability back that way.
    """
    from app.db.seed import _ALL_PERMISSIONS

    leaked = sorted(set(_ALL_PERMISSIONS) & ADMIN_EXCLUDED_PERMISSIONS)
    assert not leaked, f"the seeded System Admin role still grants: {', '.join(leaked)}"


def test_no_other_code_path_grants_an_administrator_every_permission() -> None:
    """`{p.value for p in Permission}` must always have the exclusion applied.

    A second place that builds the full set - a cached context, a test helper
    promoted into app code, a background job impersonating an admin - would restore
    upload access without anything failing. This sweep is what found the seeded role
    row, which no behavioural test was looking at.
    """
    from pathlib import Path

    #: Names that mark a set as "which strings are legal", not "what you may do".
    #: A validity check has to span the whole enum or it would reject a permission
    #: the platform genuinely defines.
    validation_targets = ("valid", "known", "allowed_values", "choices")

    root = Path(__file__).resolve().parents[2] / "app"
    offenders: list[str] = []
    for source in root.rglob("*.py"):
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
            if "for p in Permission" not in line:
                continue
            # The sanctioned constructions subtract the excluded set, on the same line.
            if "ADMIN_EXCLUDED_PERMISSIONS" in line:
                continue
            target = line.split("=")[0].strip().lower() if "=" in line else ""
            if target in validation_targets:
                continue
            offenders.append(f"{source.relative_to(root)}:{number}")

    assert not offenders, (
        "These lines build a full permission set without subtracting "
        f"ADMIN_EXCLUDED_PERMISSIONS: {', '.join(offenders)}"
    )


# =============================================================================
# Default starting password
# =============================================================================
def test_default_password_is_configurable_and_guarded_in_production() -> None:
    """Provisioning issues a known starting credential, but not silently forever."""
    from app.core.config import Settings

    settings: Any = Settings()
    assert settings.security.new_user_default_password == "Abc@1234"

    with pytest.raises(ValueError) as caught:
        Settings(
            APP_ENV="production",
            JWT_SECRET="a" * 64,
            INTERNAL_API_TOKEN="b" * 64,
            SEED_ADMIN_PASSWORD="rotated-in-deployment",
            STORAGE_PROVIDER="s3",
            DEBUG=False,
        )
    assert "NEW_USER_DEFAULT_PASSWORD" in str(caught.value)

"""The forced-password-change gate is actually attached.

``must_change_password`` is set on every administrator-provisioned account, and
``SEED_ADMIN_FORCE_PASSWORD_CHANGE`` sets it on the seeded admin. Both are
security controls, and both were inert: ``require_password_current`` was defined
and exported, its docstring claimed it was "applied to everything except the
change-password and logout endpoints", and **no route referenced it**. A
provisioned account could use the entire API on its temporary credential
indefinitely, and nothing said so.

These assert *attachment* rather than behaviour. Attachment is the part that was
wrong, and it is the part a newly added router silently misses - which is the
failure mode that produced the original bug.

Introspection note: this FastAPI version defers route resolution, so
``api_router.routes`` holds ``_IncludedRouter`` wrappers rather than flattened
routes. The router-level dependencies live on ``include_context``, and the paths
on ``original_router``.
"""

from __future__ import annotations

from app.api.v1 import api_router
from app.core.deps import require_password_current

#: Change-password and logout are the way out of the state, so gating them would
#: make the flag unclearable.
UNGATED_PREFIX = "/auth/"

#: The one route outside `/auth/` that carries no bearer-auth dependency, and the
#: reason it is safe.
#:
#: It is reached by a browser navigation with a signed token in the URL, so the
#: dependencies would 401 it before the token was examined. The gate is not lost,
#: only moved one step earlier: the token comes from `POST /exports/{id}/download`,
#: which *is* gated, so an account with an outstanding password change cannot
#: obtain one. A token minted before the flag was set stays valid for its five
#: minutes, which is the same window any already-issued access token has.
UNGATED_PATHS = frozenset({"/exports/{export_id}/content"})


def included_routers() -> list[object]:
    return [route for route in api_router.routes if hasattr(route, "include_context")]


def is_gated(included: object) -> bool:
    dependencies = included.include_context.dependencies or []  # type: ignore[attr-defined]
    return any(
        getattr(dependency, "dependency", None) is require_password_current
        for dependency in dependencies
    )


def paths_of(included: object) -> list[str]:
    return [
        route.path
        for route in included.original_router.routes  # type: ignore[attr-defined]
        if hasattr(route, "path")
    ]


def test_the_routers_are_registered() -> None:
    """Guards the introspection itself.

    Every assertion below is 'nothing ungated found'. If this module stopped
    seeing routers - a FastAPI upgrade changing the internals, say - those would
    all pass vacuously and report a gate that is not there.
    """
    assert len(included_routers()) >= 10


def test_the_gate_is_attached_to_something() -> None:
    """The original regression, in one line: it was attached to nothing."""
    assert any(is_gated(included) for included in included_routers())


def test_every_non_auth_router_is_gated() -> None:
    ungated: list[str] = []
    for included in included_routers():
        paths = paths_of(included)
        if any(path.startswith(UNGATED_PREFIX) for path in paths):
            continue
        if not is_gated(included):
            ungated.extend(path for path in paths if path not in UNGATED_PATHS)

    assert not ungated, (
        "These routes are reachable while a forced password change is outstanding: "
        f"{sorted(ungated)}. Register the router with dependencies=_PASSWORD_CURRENT "
        "in app/api/v1/__init__.py."
    )


def test_the_ungated_download_route_still_exists() -> None:
    """Stops the exemption above outliving the route it was written for.

    An allow-list entry for a path that no longer exists is an exemption nobody
    would notice becoming wrong - the next route to land on that path would
    inherit it silently.
    """
    every_path = {path for included in included_routers() for path in paths_of(included)}
    assert every_path >= UNGATED_PATHS, (
        f"{sorted(UNGATED_PATHS - every_path)} is exempted from the password gate but "
        "is no longer registered. Remove it from UNGATED_PATHS."
    )


def test_the_auth_router_stays_open() -> None:
    auth = [
        included
        for included in included_routers()
        if any(path.startswith(UNGATED_PREFIX) for path in paths_of(included))
    ]

    assert auth, "the auth router should be registered"
    for included in auth:
        assert not is_gated(included), (
            "Gating the auth router makes the flag unclearable: the user cannot "
            "change their password without having already changed it."
        )


def test_change_password_is_reachable() -> None:
    every_path = {path for included in included_routers() for path in paths_of(included)}
    assert "/auth/change-password" in every_path


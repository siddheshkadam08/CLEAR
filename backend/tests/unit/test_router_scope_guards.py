"""Every project-scoped route bounds its query to the caller's memberships.

``tests/unit/test_project_isolation.py`` holds the boundary at the *model* layer:
every table with a ``project_id`` must be served by a repository that requires a
scope. ``tests/unit/test_portfolio_scope.py`` covers the portfolio router, which
builds its own statements and so buys out of that guarantee.

Four more routers do the same thing - ``docpipeline`` aggregates across contracts,
clauses and embeddings; ``dashboard`` counts; ``jobs`` and ``alerts`` read rows
that carry their own ``project_id`` - and none of them had a structural check. So
each was correct only for as long as whoever wrote the next handler remembered.

This is that check. It is deliberately about *shape*, not behaviour: it cannot
prove a guard is right, only that one is present. The value is entirely in the
route somebody adds next month, which is where the leak would come from - not
from the routes audited today, all of which pass.

Adding a route to a router below and no guard to the route fails this test. If the
route genuinely has no project scope, name it in ``UNSCOPED`` with the reason,
which makes the exemption a decision somebody wrote down.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from fastapi import APIRouter

from app.api.v1 import admin, docpipeline, jobs

#: Ways a handler may bound its rows. More than one is fine; none is the bug.
#:
#: * ``resolve_scope_for_project`` - the list endpoints. Narrows to one project or
#:   keeps the caller's whole set, and refuses a project they cannot see.
#: * ``scope.contains`` / ``scope.require`` - detail-by-id, where the row is loaded
#:   first and its ``project_id`` checked against the scope.
#: * ``_load_job`` - the jobs router's private helper, which does exactly that.
#: * ``ContractContextDep`` - resolves the project *from the contract* and 404s a
#:   non-member, so the id cannot be probed.
#: * ``require_system_admin`` - not a scope, but a stricter gate: nobody but a
#:   system administrator reaches the handler at all.
SCOPE_GUARDS = (
    "resolve_scope_for_project",
    "scope.contains",
    "scope.require",
    "_load_job",
    "ContractContextDep",
    "require_system_admin",
)

ROUTERS: dict[str, APIRouter] = {
    "docpipeline": docpipeline.router,
    "jobs": jobs.router,
    "dashboard": admin.dashboard_router,
    "alerts": admin.alert_router,
    "audit": admin.audit_router,
}

#: Handler name -> why it needs no scope. Empty, and meant to stay that way.
UNSCOPED: dict[str, str] = {}

#: Handlers that resolve a scope but must *not* return early on an empty one.
#:
#: Only for rows that are legitimately visible without a membership. Each entry
#: has to explain what those rows are, because "it returns something to a caller
#: who belongs to nothing" is exactly the shape of the bug this file looks for.
NO_SHORT_CIRCUIT: dict[str, str] = {
    "list_alert_rules": (
        "a rule with project_id IS NULL is a platform default and applies "
        "everywhere, so it is visible to a caller with no membership. The empty "
        "scope is handled by `if project_ids:` widening the condition instead of "
        "by returning early - configuration, not contract data."
    ),
}


def _handlers() -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    for label, router in ROUTERS.items():
        for route in router.routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is not None:
                found.append((label, endpoint))
    return found


HANDLERS = _handlers()


def test_the_routers_still_exist() -> None:
    """Without this, a renamed router would empty the suite below silently."""
    assert len(HANDLERS) >= 11, (
        f"expected at least 11 scoped handlers, found {len(HANDLERS)} - "
        "has a router been renamed or split?"
    )
    for label, router in ROUTERS.items():
        assert router.routes, f"the {label} router has no routes"


@pytest.mark.parametrize(
    ("label", "handler"),
    HANDLERS,
    ids=[f"{label}:{handler.__name__}" for label, handler in HANDLERS],
)
def test_every_route_bounds_its_rows(label: str, handler: Any) -> None:
    name = handler.__name__
    if name in UNSCOPED:
        pytest.skip(f"exempt: {UNSCOPED[name]}")

    source = inspect.getsource(handler)
    used = [guard for guard in SCOPE_GUARDS if guard in source]
    assert used, (
        f"{label}.{name} uses none of {SCOPE_GUARDS}, so its query is not bounded "
        f"to the caller's projects. Add a guard, or name it in UNSCOPED with why."
    )


@pytest.mark.parametrize(
    ("label", "handler"),
    HANDLERS,
    ids=[f"{label}:{handler.__name__}" for label, handler in HANDLERS],
)
def test_a_list_endpoint_short_circuits_on_an_empty_scope(label: str, handler: Any) -> None:
    """A caller who belongs to nothing must get nothing, without a query.

    Only applies to handlers that resolve a scope into ``project_ids``: an empty
    list passed to ``IN ()`` is a correctness question in its own right, and the
    cheapest answer is not to issue the statement.
    """
    name = handler.__name__
    if name in NO_SHORT_CIRCUIT:
        pytest.skip(f"exempt: {NO_SHORT_CIRCUIT[name]}")

    source = inspect.getsource(handler)
    if "resolve_scope_for_project" not in source:
        pytest.skip("not a scope-resolving list endpoint")

    assert "if not project_ids" in source, (
        f"{label}.{name} resolves a scope but does not short-circuit on an empty "
        "one. If that is deliberate, name it in NO_SHORT_CIRCUIT with why."
    )


def test_every_exemption_names_a_real_handler() -> None:
    """Keeps the exemption lists honest: a stale entry exempts nothing, silently."""
    names = {handler.__name__ for _, handler in HANDLERS}
    for exempt in UNSCOPED:
        assert exempt in names, f"UNSCOPED names {exempt!r}, which is not a route"
    for exempt in NO_SHORT_CIRCUIT:
        assert exempt in names, f"NO_SHORT_CIRCUIT names {exempt!r}, which is not a route"

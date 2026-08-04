"""Project scoping for the cross-contract registers.

``tests/unit/test_project_isolation.py`` holds the boundary structurally, by
asserting that every project-scoped model is served by a repository that *requires*
a scope. The portfolio endpoints deliberately sit outside that: they join across
four tables and aggregate, which the per-model repositories are not shaped for, so
they build their own statements. That buys them out of the guarantee, and this file
is what replaces it.

Two properties, tested two ways:

* **Behaviourally** - a caller with no project memberships gets an empty page and
  the database is never touched. The stub session raises on any query, so a handler
  that dropped the guard fails loudly instead of quietly running an unbounded
  ``SELECT`` and returning the whole estate.
* **Structurally** - every route on the router resolves its scope through
  :func:`resolve_scope_for_project`. A behavioural test only covers the routes it
  names; this one covers the route somebody adds next month.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any

import pytest

from app.api.v1 import portfolio
from app.core.deps import AccessScope
from app.schemas.common import PaginationParams


class ExplodingSession:
    """Any query at all is a failure: an unscoped read must never be issued."""

    async def execute(self, _statement: Any) -> Any:
        raise AssertionError(
            "queried the database for a caller with no accessible projects - "
            "the scope guard is missing"
        )


class FakeUser:
    id = uuid.uuid4()
    email = "nobody@example.com"
    is_system_admin = False


def _empty_scope() -> AccessScope:
    """A real user who belongs to nothing. Not an anonymous or malformed request."""
    return AccessScope(user=FakeUser(), project_ids=[])  # type: ignore[arg-type]


HANDLERS = [
    portfolio.list_obligations,
    portfolio.list_key_dates,
    portfolio.list_risks,
    portfolio.list_parties,
]


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", HANDLERS, ids=lambda fn: fn.__name__)
async def test_no_memberships_means_an_empty_page_and_no_query(handler: Any) -> None:
    page = await handler(
        db=ExplodingSession(),
        scope=_empty_scope(),
        pagination=PaginationParams(page=1, size=25),
    )
    assert page.items == []
    assert page.meta.total == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", HANDLERS, ids=lambda fn: fn.__name__)
async def test_a_project_the_caller_cannot_see_is_refused(handler: Any) -> None:
    """`project_id` narrows the scope. It must never widen it."""
    from app.core.errors import ProjectAccessDeniedError

    scope = AccessScope(user=FakeUser(), project_ids=[uuid.uuid4()])  # type: ignore[arg-type]
    with pytest.raises(ProjectAccessDeniedError):
        await handler(
            db=ExplodingSession(),
            scope=scope,
            pagination=PaginationParams(page=1, size=25),
            project_id=uuid.uuid4(),  # somebody else's project
        )


def test_every_route_resolves_its_scope() -> None:
    """The guard against a future endpoint being added without one."""
    routes = [route for route in portfolio.router.routes if getattr(route, "endpoint", None)]
    assert routes, "the portfolio router has no routes - has it been renamed?"

    for route in routes:
        source = inspect.getsource(route.endpoint)  # type: ignore[attr-defined]
        name = route.endpoint.__name__  # type: ignore[attr-defined]
        assert "resolve_scope_for_project" in source, (
            f"{name} does not resolve an access scope, so its query is unbounded"
        )
        assert "if not project_ids" in source, (
            f"{name} does not short-circuit on an empty scope"
        )

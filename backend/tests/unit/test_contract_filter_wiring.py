"""Every declared contract-list filter reaches the model that applies it.

``_filters_from_query`` declares ~30 query parameters and then hand-writes a
``ContractFilterParams(...)`` call. Adding a parameter to the signature and
forgetting it in that call typechecks, starts, serves, and silently ignores the
filter: FastAPI parses the value, the model defaults the field to ``None``, and
the repository's ``if filters.x is not None`` never fires. The request returns
200 with the *unfiltered* list.

That is what happened to ``missing_mandatory`` the day it was added, and it was
only caught because the filtered count happened not to match its complement. Had
every contract in the fixture been incomplete, the wrong answer and the right
answer would have been the same number.

So this asserts the wiring structurally rather than per-parameter - the point is
the filter somebody adds next, not the ones audited today.
"""

from __future__ import annotations

import inspect
import re

from app.api.v1 import admin, contracts
from app.schemas.contract import ContractFilterParams

#: Query params that legitimately do not map 1:1 onto a model field.
#:
#: The range fields are assembled from two flat parameters each (``DateRange`` /
#: ``NumberRange``), because a URL cannot nest; ``status_filter`` is the Python
#: name for a parameter aliased to ``status``.
COMPOSITE_PARAMS = {
    "status_filter",
    "effective_from",
    "effective_to",
    "expiry_from",
    "expiry_to",
    "risk_score_min",
    "risk_score_max",
    "value_min",
    "value_max",
}


#: Keywords belonging to the range objects constructed inline inside the call -
#: `NumberRange(min=..., max=...)`. They are nested arguments, not fields of
#: ContractFilterParams, and the flat regex below cannot tell the difference.
NESTED_KWARGS = {"min", "max"}


def _constructor_kwargs() -> set[str]:
    source = inspect.getsource(contracts._filters_from_query)
    _, _, call = source.partition("return ContractFilterParams(")
    assert call, "the query builder no longer constructs ContractFilterParams directly"
    return set(re.findall(r"(\w+)\s*=", call)) - NESTED_KWARGS


def test_every_query_parameter_reaches_the_filter_model() -> None:
    declared = set(inspect.signature(contracts._filters_from_query).parameters)
    passed = _constructor_kwargs()

    dropped = sorted(declared - passed - COMPOSITE_PARAMS)
    assert not dropped, (
        f"{dropped} are accepted as query parameters but never passed to "
        "ContractFilterParams, so the API takes them and ignores them"
    )


def test_the_composite_exemptions_are_all_real_parameters() -> None:
    """A stale exemption would hide a genuinely dropped parameter."""
    declared = set(inspect.signature(contracts._filters_from_query).parameters)
    unknown = sorted(COMPOSITE_PARAMS - declared)
    assert not unknown, f"COMPOSITE_PARAMS names {unknown}, which are not parameters"


def test_every_passed_keyword_is_a_real_model_field() -> None:
    """The other direction: a typo'd keyword would raise only at request time."""
    unknown = sorted(_constructor_kwargs() - set(ContractFilterParams.model_fields))
    assert not unknown, f"passed to ContractFilterParams but not a field: {unknown}"


def test_the_dashboard_drilldowns_name_real_query_parameters() -> None:
    """Every "needs attention" tile must link to a filter the API declares.

    Two tiles shipped pointing at ``expiring_before`` / ``expiring_after`` and
    ``missing_mandatory``, none of which the endpoint declared. FastAPI drops an
    undeclared parameter without complaint, so each tile opened the complete,
    unfiltered repository while the page reported a filter as active.
    """
    declared = set(inspect.signature(contracts._filters_from_query).parameters)
    # `status` is the alias of `status_filter`; the drilldowns may use either.
    declared.add("status")

    keys = set(re.findall(r"drilldown=\{([^}]*)\}", inspect.getsource(admin), flags=re.S))
    named = {k for block in keys for k in re.findall(r'"(\w+)"\s*:', block)}
    assert named, "no drilldowns found - has the dashboard stopped linking its tiles?"

    unknown = sorted(named - declared)
    assert not unknown, (
        f"dashboard tiles link to {unknown}, which the contracts endpoint does not "
        "declare - those tiles would open an unfiltered list"
    )

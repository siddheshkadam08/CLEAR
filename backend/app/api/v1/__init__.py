"""Versioned API router aggregation.

One place lists every domain router, so the surface of ``/api/v1`` is readable at
a glance. Route ordering matters where a literal path could be shadowed by a path
parameter, so static segments are registered before parameterised ones.

**The forced-password-change gate is applied here**, once, rather than on each
route. ``require_password_current`` existed and was exported but was attached to
nothing, so ``must_change_password`` - set on every administrator-provisioned
account, and on the seeded admin when
``SEED_ADMIN_FORCE_PASSWORD_CHANGE`` is on - had no effect whatsoever: the account
could use the entire API on its temporary credential indefinitely. Applying it
per-route would mean remembering it on every future endpoint, which is how it came
to be missed in the first place.

``auth`` is excluded deliberately, and is the only exclusion: the change-password
and logout endpoints have to remain reachable, or clearing the flag would require
the flag to already be clear.
"""

from fastapi import APIRouter, Depends

from app.api.v1 import (
    admin,
    auth,
    clause_master,
    contracts,
    docpipeline,
    evaluation,
    exports,
    jobs,
    knowledge,
    portfolio,
    projects,
    search,
    users,
)
from app.core.deps import require_password_current

#: Applied to every router except `auth`.
_PASSWORD_CURRENT = [Depends(require_password_current)]

api_router = APIRouter()

# No gate: this is the way *out* of the forced-change state.
api_router.include_router(auth.router)

api_router.include_router(users.router, dependencies=_PASSWORD_CURRENT)
api_router.include_router(projects.router, dependencies=_PASSWORD_CURRENT)
api_router.include_router(projects.activity_router, dependencies=_PASSWORD_CURRENT)

# Project-scoped upload/listing, then the contract-scoped routes. Registered in
# this order so `/contracts` (cross-project listing) is matched before
# `/contracts/{contract_id}` can shadow it.
api_router.include_router(contracts.router, dependencies=_PASSWORD_CURRENT)
api_router.include_router(contracts.contract_router, dependencies=_PASSWORD_CURRENT)

# `/contracts/{contract_id}/jobs` and `/contracts/{contract_id}/knowledge` must be
# registered after the contract router so their literal segments win over
# `/contracts/{contract_id}`.
api_router.include_router(jobs.contract_jobs_router, dependencies=_PASSWORD_CURRENT)
api_router.include_router(knowledge.router, dependencies=_PASSWORD_CURRENT)
api_router.include_router(knowledge.clause_router, dependencies=_PASSWORD_CURRENT)

# Cross-contract registers. Every route is a bare top-level noun (`/obligations`,
# `/risks`) with no path parameter, so ordering against the contract routers above
# does not matter - nothing here can be shadowed by `/contracts/{id}/...`.
api_router.include_router(portfolio.router, dependencies=_PASSWORD_CURRENT)

api_router.include_router(jobs.router, dependencies=_PASSWORD_CURRENT)
api_router.include_router(search.router, dependencies=_PASSWORD_CURRENT)
api_router.include_router(search.copilot_router, dependencies=_PASSWORD_CURRENT)

# `/exports/capabilities` is a literal that `/exports/{export_id}` would otherwise
# swallow, so the export router is registered with its static route first - see the
# ordering note at the top of this module.
api_router.include_router(exports.router, dependencies=_PASSWORD_CURRENT)
api_router.include_router(exports.project_export_router, dependencies=_PASSWORD_CURRENT)

# The redesigned Clause Master, grouped by agreement type. Registered *before*
# the legacy router: both live under `/clause-master`, and this one's literal
# segments (`/by-agreement-type`, `/clauses`, `/export`, `/import`) would
# otherwise be swallowed by the legacy `/{category_id}` and fail a UUID parse.
api_router.include_router(clause_master.router, dependencies=_PASSWORD_CURRENT)
api_router.include_router(admin.clause_master_router, dependencies=_PASSWORD_CURRENT)
api_router.include_router(admin.dashboard_router, dependencies=_PASSWORD_CURRENT)
# Insights over the document pipeline's own tables, kept separate from the
# overview dashboard because the two count different things.
api_router.include_router(docpipeline.router, dependencies=_PASSWORD_CURRENT)
api_router.include_router(admin.alert_router, dependencies=_PASSWORD_CURRENT)
# The compliance trail. Not administrator-only - AUDIT_READ belongs to Project
# Manager - so the endpoint guards on the permission and scopes rows to the
# caller's projects rather than gating the whole router on admin.
api_router.include_router(admin.audit_router, dependencies=_PASSWORD_CURRENT)
# Retrieval-quality artefacts, read from disk. Administrator-only: the failing
# case lists name the questions users asked.
api_router.include_router(evaluation.router, dependencies=_PASSWORD_CURRENT)

__all__ = ["api_router"]

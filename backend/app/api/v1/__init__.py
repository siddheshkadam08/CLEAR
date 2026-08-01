"""Versioned API router aggregation.

One place lists every domain router, so the surface of ``/api/v1`` is readable at
a glance. Route ordering matters where a literal path could be shadowed by a path
parameter, so static segments are registered before parameterised ones.
"""

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    auth,
    contracts,
    docpipeline,
    exports,
    jobs,
    knowledge,
    projects,
    search,
    users,
)

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(projects.router)
api_router.include_router(projects.activity_router)

# Project-scoped upload/listing, then the contract-scoped routes. Registered in
# this order so `/contracts` (cross-project listing) is matched before
# `/contracts/{contract_id}` can shadow it.
api_router.include_router(contracts.router)
api_router.include_router(contracts.contract_router)

# `/contracts/{contract_id}/jobs` and `/contracts/{contract_id}/knowledge` must be
# registered after the contract router so their literal segments win over
# `/contracts/{contract_id}`.
api_router.include_router(jobs.contract_jobs_router)
api_router.include_router(knowledge.router)
api_router.include_router(knowledge.clause_router)

api_router.include_router(jobs.router)
api_router.include_router(search.router)
api_router.include_router(search.copilot_router)

# `/exports/capabilities` is a literal that `/exports/{export_id}` would otherwise
# swallow, so the export router is registered with its static route first - see the
# ordering note at the top of this module.
api_router.include_router(exports.router)
api_router.include_router(exports.project_export_router)

api_router.include_router(admin.clause_master_router)
api_router.include_router(admin.dashboard_router)
# Insights over the document pipeline's own tables, kept separate from the
# overview dashboard because the two count different things.
api_router.include_router(docpipeline.router)
api_router.include_router(admin.alert_router)

__all__ = ["api_router"]

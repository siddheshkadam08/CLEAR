"""Extracted-knowledge endpoints: clauses, parties, obligations, risks, dates.

The contract detail screen reads from here. Two things it needs that are worth
stating explicitly:

* **Dedicated clause tabs are data-driven.** Which clause categories get their own
  tab, which fields they show, and which values are highlighted all come from
  ``clause_master.ui_config``. Promoting a clause to its own tab is an administrator
  edit, not a frontend release - which is what the Limitation of Liability tab and
  its cap dropdown are built on.
* **Absence is a first-class response.** A mandatory clause the profile expects and
  extraction did not find is returned as a tab marked ``is_missing``, not omitted.
  A missing liability cap is a bigger finding than a bad one, and a UI that simply
  shows nothing would bury it.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import ValidationError

from app.core.deps import ContractContextDep, DbSession, RequestInfoDep
from app.core.enums import AuditAction, Permission, ReviewStatus, RiskBand
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.schemas.common import BoundingBox, ProvenanceInfo
from app.schemas.knowledge import (
    ClauseResponse,
    ClauseReviewRequest,
    ClauseTabGroup,
    ContractKnowledgeResponse,
    EntityResponse,
    EvidenceResponse,
    KeyDateResponse,
    ObligationResponse,
    RiskAssessmentResponse,
    RiskResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/contracts/{contract_id}", tags=["Knowledge"])

# Contract-scoped, not `/clauses/{clause_id}`: these routes depend on
# ``ContractContextDep``, which resolves the owning project from ``contract_id`` as
# a *path* parameter. Mounted without that segment the parameter is unsatisfiable
# and every call fails validation before the handler is reached - so the scoping
# that makes the route safe is the same thing that has to be in its path.
clause_router = APIRouter(prefix="/contracts/{contract_id}/clauses", tags=["Knowledge"])


# =============================================================================
# Aggregate
# =============================================================================
@router.get(
    "/knowledge",
    response_model=ContractKnowledgeResponse,
    summary="Everything extracted from a contract",
)
async def get_knowledge(ref: ContractContextDep, db: DbSession) -> ContractKnowledgeResponse:
    """The contract detail screen in one call.

    One round trip rather than six: the detail view needs all of it to render, and
    six sequential requests is six chances to show a half-populated screen.
    """
    ref.require(Permission.KNOWLEDGE_READ)
    contract_id, project_id = ref.contract_id, ref.project_id

    from app.repositories.contract import ContractMetadataRepository
    from app.repositories.knowledge import (
        ClauseRepository,
        EntityRepository,
        KeyDateRepository,
        ObligationRepository,
        RiskRepository,
    )

    clauses = list(await ClauseRepository(db).list_for_contract(contract_id, project_id))
    parties = list(await EntityRepository(db).list_for_contract(contract_id, project_id))
    obligations = list(await ObligationRepository(db).list_for_contract(contract_id, project_id))
    risks = list(await RiskRepository(db).list_for_contract(contract_id, project_id))
    dates = list(await KeyDateRepository(db).list_for_contract(contract_id, project_id))
    metadata = await ContractMetadataRepository(db).get_for_contract(contract_id)

    tabs, listed = await _build_tabs(db, clauses, metadata)

    return ContractKnowledgeResponse(
        contract_id=contract_id,
        clause_count=len(clauses),
        tabs=tabs,
        clauses=[_clause(row) for row in listed],
        parties=[_entity(row) for row in parties],
        obligations=[_obligation(row) for row in obligations],
        key_dates=[_key_date(row) for row in dates],
        assessment=_assessment(risks, metadata),
        summary=getattr(metadata, "summary", None),
        key_topics=list(getattr(metadata, "key_topics", None) or []),
        needs_review=bool(getattr(ref.project.project, "needs_review", False))
        or _needs_review(metadata),
        review_reasons=list((getattr(metadata, "extra", None) or {}).get("review_reasons", [])),
    )


# =============================================================================
# Individual collections
# =============================================================================
@router.get("/clauses", response_model=list[ClauseResponse], summary="Extracted clauses")
async def list_clauses(
    ref: ContractContextDep,
    db: DbSession,
    clause_type: Annotated[list[str] | None, Query(description="Filter by category")] = None,
    flagged_only: Annotated[bool, Query(description="Only risk-flagged clauses")] = False,
) -> list[ClauseResponse]:
    ref.require(Permission.KNOWLEDGE_READ)

    from app.repositories.knowledge import ClauseRepository

    rows = await ClauseRepository(db).list_for_contract(
        ref.contract_id, ref.project_id, clause_types=clause_type, flagged_only=flagged_only
    )
    return [_clause(row) for row in rows]


@router.get("/parties", response_model=list[EntityResponse], summary="Contracting parties")
async def list_parties(ref: ContractContextDep, db: DbSession) -> list[EntityResponse]:
    ref.require(Permission.KNOWLEDGE_READ)

    from app.repositories.knowledge import EntityRepository

    rows = await EntityRepository(db).list_for_contract(ref.contract_id, ref.project_id)
    return [_entity(row) for row in rows]


@router.get("/obligations", response_model=list[ObligationResponse], summary="Obligations")
async def list_obligations(ref: ContractContextDep, db: DbSession) -> list[ObligationResponse]:
    ref.require(Permission.KNOWLEDGE_READ)

    from app.repositories.knowledge import ObligationRepository

    rows = await ObligationRepository(db).list_for_contract(ref.contract_id, ref.project_id)
    return [_obligation(row) for row in rows]


@router.get("/risks", response_model=RiskAssessmentResponse, summary="Risk assessment")
async def get_risks(ref: ContractContextDep, db: DbSession) -> RiskAssessmentResponse:
    """The score, its banding, and the findings that produced it."""
    ref.require(Permission.KNOWLEDGE_READ)

    from app.repositories.contract import ContractMetadataRepository
    from app.repositories.knowledge import RiskRepository

    risks = list(await RiskRepository(db).list_for_contract(ref.contract_id, ref.project_id))
    metadata = await ContractMetadataRepository(db).get_for_contract(ref.contract_id)
    return _assessment(risks, metadata)


@router.get("/dates", response_model=list[KeyDateResponse], summary="Key dates")
async def list_key_dates(ref: ContractContextDep, db: DbSession) -> list[KeyDateResponse]:
    ref.require(Permission.KNOWLEDGE_READ)

    from app.repositories.knowledge import KeyDateRepository

    rows = await KeyDateRepository(db).list_for_contract(ref.contract_id, ref.project_id)
    return [_key_date(row) for row in rows]


# =============================================================================
# Evidence
# =============================================================================
@router.get(
    "/evidence/{chunk_id}",
    response_model=EvidenceResponse,
    summary="Resolve a citation to its page and coordinates",
)
async def get_evidence(
    chunk_id: uuid.UUID,
    ref: ContractContextDep,
    db: DbSession,
) -> EvidenceResponse:
    """Everything the viewer needs to highlight one citation.

    Returns the coordinates *and* a signed document URL, so the overlay can be drawn
    without a second round trip to work out which file to open.
    """
    ref.require(Permission.KNOWLEDGE_READ)

    from app.repositories.chunk import ChunkRepository
    from app.services.contract import ContractService

    chunk = await ChunkRepository(db).get_scoped(chunk_id, ref.project_id)
    if chunk is None or chunk.contract_id != ref.contract_id:
        raise NotFoundError("Evidence", chunk_id)

    document_url: str | None = None
    expires_at = None
    if ref.has(Permission.CONTRACT_DOWNLOAD):
        access = await ContractService(db).file_access(ref.contract_id, ref.project_id)
        document_url = getattr(access, "url", None)
        expires_at = getattr(access, "expires_at", None)

    return EvidenceResponse(
        contract_id=chunk.contract_id,
        chunk_id=chunk.id,
        text=chunk.text_content,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        bounding_boxes=[BoundingBox(**box) for box in (chunk.bounding_boxes or [])],
        clause_number=chunk.clause_number,
        section_title=chunk.section_title,
        document_url=document_url,
        expires_at=expires_at,
    )


# =============================================================================
# Review
# =============================================================================
@clause_router.post(
    "/{clause_id}/review",
    response_model=ClauseResponse,
    status_code=status.HTTP_200_OK,
    summary="Record a review decision on a clause",
)
async def review_clause(
    clause_id: uuid.UUID,
    payload: ClauseReviewRequest,
    ref: ContractContextDep,
    db: DbSession,
    info: RequestInfoDep,
) -> ClauseResponse:
    """Approve, reject or correct an extracted clause (§13).

    A correction records the model's original output alongside the corrected value
    rather than overwriting it: the two must stay distinguishable, both to audit the
    extraction and to measure whether the model is improving.
    """
    ref.require(Permission.KNOWLEDGE_REVIEW)

    from app.repositories.knowledge import ClauseRepository
    from app.services.audit import AuditService

    repository = ClauseRepository(db)
    clause = await repository.get_scoped(clause_id, ref.project_id)
    if clause is None or clause.contract_id != ref.contract_id:
        raise NotFoundError("Clause", clause_id)

    before = {
        "review_status": clause.review_status,
        "attributes": dict(clause.attributes or {}),
        "text": clause.text_content,
    }

    # A plain string on the wire (`use_enum_values`); coerced so an invalid value
    # fails here rather than reaching the column.
    decision = ReviewStatus(payload.review_status)
    values: dict[str, Any] = {"review_status": decision.value}
    evidence = dict(clause.evidence or {})

    if payload.attributes is not None or payload.text is not None:
        # The model's output is preserved under `original`, so a human correction
        # never destroys the record of what was extracted.
        evidence.setdefault(
            "original",
            {
                "attributes": dict(clause.attributes or {}),
                "text": clause.text_content,
            },
        )
        evidence["reviewed_by"] = str(ref.user.id)
        if payload.note:
            evidence["review_note"] = payload.note
        values["evidence"] = evidence

    if payload.attributes is not None:
        values["attributes"] = {**(clause.attributes or {}), **payload.attributes}
    if payload.text is not None:
        values["text_content"] = payload.text

    updated = await repository.update(clause, **values)

    await AuditService(db).record(
        action=AuditAction.REVIEW_DECISION,
        entity_type="clause",
        entity_id=clause_id,
        entity_label=clause.clause_type,
        project_id=ref.project_id,
        user_id=ref.user.id,
        user_email=ref.user.email,
        ip=info.ip,
        user_agent=info.user_agent,
        route=info.route,
        before=before,
        after={"review_status": decision.value},
    )
    logger.info(
        "clause_reviewed",
        clause_id=str(clause_id),
        clause_type=clause.clause_type,
        decision=decision.value,
        corrected=payload.attributes is not None or payload.text is not None,
        user_id=str(ref.user.id),
    )
    return _clause(updated)


@clause_router.get(
    "/{clause_id}",
    response_model=ClauseResponse,
    summary="Get one clause",
)
async def get_clause(
    clause_id: uuid.UUID, ref: ContractContextDep, db: DbSession
) -> ClauseResponse:
    ref.require(Permission.KNOWLEDGE_READ)

    from app.repositories.knowledge import ClauseRepository

    clause = await ClauseRepository(db).get_scoped(clause_id, ref.project_id)
    if clause is None or clause.contract_id != ref.contract_id:
        raise NotFoundError("Clause", clause_id)
    return _clause(clause)


# =============================================================================
# Tab construction
# =============================================================================
async def _build_tabs(
    db: Any, clauses: list[Any], metadata: Any
) -> tuple[list[ClauseTabGroup], list[Any]]:
    """Split clauses into dedicated tabs and the general list.

    Driven entirely by ``clause_master.ui_config``: which categories get a tab, in
    what order, which fields lead, and what a dropdown offers. That is what lets an
    administrator promote a clause to its own tab without a frontend change.
    """
    from sqlalchemy import select

    from app.models.clause_master import ClauseMasterCategory

    categories = (
        (
            await db.execute(
                select(ClauseMasterCategory)
                .where(
                    ClauseMasterCategory.is_active.is_(True),
                    ClauseMasterCategory.deleted_at.is_(None),
                )
                .order_by(ClauseMasterCategory.priority)
            )
        )
        .scalars()
        .all()
    )

    tabbed = {
        str(category.key): category
        for category in categories
        if (category.ui_config or {}).get("placement") == "dedicated_tab"
    }
    if not tabbed:
        return [], clauses

    missing = set(getattr(metadata, "missing_mandatory_clauses", None) or [])
    by_type: dict[str, list[Any]] = {}
    for clause in clauses:
        by_type.setdefault(clause.clause_type, []).append(clause)

    tabs: list[ClauseTabGroup] = []
    for key, category in tabbed.items():
        config = category.ui_config or {}
        rows = by_type.get(key, [])
        tabs.append(
            ClauseTabGroup(
                key=key,
                label=config.get("tab_label") or category.name,
                priority=category.priority,
                primary_fields=list(config.get("primary_fields") or []),
                dropdown=config.get("cap_dropdown") or config.get("dropdown"),
                highlight_when=dict(config.get("highlight_when") or {}),
                clauses=[_clause(row) for row in rows],
                # An expected clause that was not found is shown as an empty tab
                # flagged missing, never omitted: absence is the finding.
                is_missing=not rows and (key in missing or category.mandatory),
            )
        )

    tabs.sort(key=lambda tab: tab.priority)
    listed = [clause for clause in clauses if clause.clause_type not in tabbed]
    return tabs, listed


# =============================================================================
# Serialisation
# =============================================================================
def _boxes(raw: Any) -> list[BoundingBox]:
    """Stored geometry as highlight rectangles, skipping anything unreadable.

    A row written in some other shape is dropped rather than raised on. These
    are decoration - the clause text and its page numbers are the answer, the
    rectangle only says where to draw a box - so a bad one costs a highlight,
    whereas letting it raise costs the caller every clause in the contract.

    That is not hypothetical: the extraction stage briefly wrote the document
    pipeline's `{page, polygon}` inches here, and since this ran inside the
    knowledge payload, one malformed box turned the whole of Contract Detail
    into a 500 with no clue as to which field was at fault.
    """
    boxes: list[BoundingBox] = []
    for box in raw or []:
        if not isinstance(box, dict):
            continue
        try:
            boxes.append(BoundingBox(**box))
        except ValidationError:
            logger.warning("bounding_box_unreadable", keys=sorted(box))
    return boxes


def _provenance(row: Any) -> ProvenanceInfo | None:
    confidence = getattr(row, "confidence", None)
    if confidence is None and not getattr(row, "model_version", None):
        return None
    return ProvenanceInfo(
        confidence=float(confidence) if confidence is not None else None,
        validation_score=float(row.validation_score)
        if getattr(row, "validation_score", None) is not None
        else None,
        review_status=getattr(row, "review_status", None),
        model_version=getattr(row, "model_version", None),
        prompt_version=getattr(row, "prompt_version", None),
        profile_version=getattr(row, "profile_version", None),
    )


def _clause(row: Any) -> ClauseResponse:
    evidence = row.evidence if isinstance(row.evidence, dict) else {}
    return ClauseResponse(
        id=row.id,
        contract_id=row.contract_id,
        clause_type=row.clause_type,
        title=row.title,
        text=row.text_content,
        summary=row.summary,
        clause_number=row.clause_number,
        section_title=row.section_title,
        attributes=dict(row.attributes or {}),
        is_mandatory=bool(row.is_mandatory),
        is_risk_flagged=bool(row.is_risk_flagged),
        deviation_score=float(row.deviation_score) if row.deviation_score is not None else None,
        page_start=row.page_start,
        page_end=row.page_end,
        bounding_boxes=_boxes(row.bounding_boxes),
        chunk_id=row.chunk_id,
        provenance=_provenance(row),
        review_status=ReviewStatus(row.review_status)
        if row.review_status
        else ReviewStatus.NOT_REQUIRED,
        issues=list(evidence.get("issues") or []),
    )


def _entity(row: Any) -> EntityResponse:
    return EntityResponse(
        id=row.id,
        contract_id=row.contract_id,
        entity_type=row.entity_type,
        name=row.name,
        legal_name=row.legal_name,
        aliases=list(row.aliases or []),
        role=row.role,
        jurisdiction=row.jurisdiction,
        registration_number=row.registration_number,
        address=row.address,
        contact=dict(row.contact or {}),
        is_primary=bool(row.is_primary),
        page_start=row.page_start,
        bounding_boxes=_boxes(row.bounding_boxes),
        provenance=_provenance(row),
    )


def _obligation(row: Any) -> ObligationResponse:
    return ObligationResponse(
        id=row.id,
        contract_id=row.contract_id,
        clause_id=row.clause_id,
        action=row.action,
        responsible_party=row.responsible_party,
        due_date=row.due_date,
        due_description=row.due_description,
        trigger_event=row.trigger_event,
        frequency=row.frequency,
        is_recurring=bool(row.is_recurring),
        status=row.status,
        penalty=row.penalty,
        page_start=row.page_start,
        bounding_boxes=_boxes(row.bounding_boxes),
        provenance=_provenance(row),
    )


def _risk(row: Any) -> RiskResponse:
    return RiskResponse(
        id=row.id,
        contract_id=row.contract_id,
        clause_id=row.clause_id,
        risk_type=row.risk_type,
        severity=row.severity,
        description=row.description,
        recommendation=row.recommendation,
        category=row.category,
        score_contribution=row.score_contribution,
        is_omission=bool(row.is_omission),
        page_start=row.page_start,
        bounding_boxes=_boxes(row.bounding_boxes),
        provenance=_provenance(row),
    )


def _key_date(row: Any) -> KeyDateResponse:
    return KeyDateResponse(
        id=row.id,
        contract_id=row.contract_id,
        date_type=row.date_type,
        date_value=row.date_value,
        date_expression=row.date_expression,
        description=row.description,
        is_recurring=bool(row.is_recurring),
        page_start=row.page_start,
        bounding_boxes=_boxes(row.bounding_boxes),
    )


def _assessment(risks: list[Any], metadata: Any) -> RiskAssessmentResponse:
    by_severity: dict[str, int] = {}
    for risk in risks:
        key = risk.severity.value if hasattr(risk.severity, "value") else str(risk.severity)
        by_severity[key] = by_severity.get(key, 0) + 1

    score = int(getattr(metadata, "risk_score", 0) or 0)
    band = getattr(metadata, "risk_band", None)
    return RiskAssessmentResponse(
        score=score,
        band=RiskBand(band) if band else RiskBand.from_score(score),
        by_severity=by_severity,
        missing_mandatory_clauses=list(getattr(metadata, "missing_mandatory_clauses", None) or []),
        has_unlimited_liability=bool(getattr(metadata, "has_unlimited_liability", False)),
        # The per-finding contributions, so the score decomposes into named findings
        # rather than being an opaque number.
        breakdown=list(getattr(metadata, "risk_factors", None) or []),
        risks=[_risk(row) for row in risks],
    )


def _needs_review(metadata: Any) -> bool:
    extra = getattr(metadata, "extra", None) or {}
    return bool(extra.get("review_reasons"))


__all__ = ["clause_router", "router"]

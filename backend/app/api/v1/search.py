"""Search and Copilot endpoints.

Where the retrieval stack becomes reachable. The layering is preserved end to end:
the planner decides, the engine executes, the assembler packs, the RAG engine
answers - and this module only wires them to a request.

Two properties every route here enforces:

* **Scope is resolved once, from membership.** ``project_ids`` comes from the
  caller's accessible set, never from the request body. A client cannot widen its
  own scope by asking (§1.1).
* **The plan is returned with the results.** A user who searched "expiring next
  quarter" and got nothing needs to see that it resolved to a date range rather
  than guessing why similarity failed them.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from sse_starlette.sse import EventSourceResponse

from app.core.deps import (
    AccessScope,
    AccessScopeDep,
    CurrentUserDep,
    DbSession,
    RequestInfoDep,
    resolve_scope_for_project,
)
from app.core.enums import AuditAction, ChatRole
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.schemas.common import BoundingBox, MessageResponse
from app.schemas.search import (
    AnswerResponse,
    AskRequest,
    ChatMessageResponse,
    ChatSessionCreate,
    ChatSessionResponse,
    CitationResponse,
    ContractMatch,
    PlanExplanation,
    SearchHit,
    SearchRequest,
    SearchResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/search", tags=["Search"])
copilot_router = APIRouter(prefix="/copilot", tags=["Copilot"])

#: Prior turns fed back into the prompt. Enough for pronouns to resolve without
#: spending the evidence budget on conversation history.
_HISTORY_TURNS = 6


# =============================================================================
# Search
# =============================================================================
@router.post("", response_model=SearchResponse, summary="Search contracts")
async def search(
    payload: SearchRequest,
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
    info: RequestInfoDep,
) -> SearchResponse:
    """Hybrid search across the caller's accessible contracts.

    The planner decides whether this is a metadata lookup, a clause search or a
    full hybrid retrieval - so "which contracts expire next quarter" is answered
    from the projection rather than by an ANN scan that has no opinion about dates.
    """
    started = time.perf_counter()
    project_ids = await _scope_for(payload, scope)

    from app.ai.retrieval import RetrievalEngine, RetrievalPlanner
    from app.services.audit import AuditService

    planner = RetrievalPlanner()
    plan = planner.plan(
        payload.query,
        project_ids=project_ids,
        scope=payload.scope,
        contract_ids=payload.contract_ids or None,
        mode=payload.mode,
        agreement_types=payload.agreement_types or None,
    )
    result = await RetrievalEngine(db).retrieve(plan)

    await AuditService(db).record(
        action=AuditAction.SEARCH,
        entity_type="search",
        project_id=payload.project_id,
        user_id=user.id,
        user_email=user.email,
        ip=info.ip,
        user_agent=info.user_agent,
        route=info.route,
        # The query text is recorded: search history is a legitimate audit trail,
        # and it is what makes "who looked at what" answerable.
        after={"query": payload.query, "intent": plan.intent.value, "hits": len(result.evidence)},
    )

    hits = [_hit(item) for item in result.evidence[: payload.limit]]
    return SearchResponse(
        query=payload.query,
        hits=hits,
        contracts=[ContractMatch(**row) for row in result.metadata_rows],
        total_hits=len(result.evidence),
        plan=_plan_explanation(plan),
        duration_ms=int((time.perf_counter() - started) * 1000),
        warnings=result.warnings,
    )


@router.get("", response_model=SearchResponse, summary="Search by query string")
async def search_get(
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
    info: RequestInfoDep,
    q: Annotated[str, Query(min_length=1, max_length=2000, description="Query")],
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SearchResponse:
    """GET form, so a search result is a linkable URL."""
    return await search(
        SearchRequest(query=q, project_id=project_id, limit=limit), user, db, scope, info
    )


# =============================================================================
# Copilot
# =============================================================================
@copilot_router.post("/ask", response_model=AnswerResponse, summary="Ask a question")
async def ask(
    payload: AskRequest,
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
    info: RequestInfoDep,
) -> AnswerResponse:
    """Answer a question from the contracts, with citations.

    Every claim in the answer is traceable to a page. Citations the model invents
    are stripped before the answer is returned, and an answer that cites nothing at
    all is flagged for review rather than presented as fact.
    """
    started = time.perf_counter()
    project_ids = await _scope_for(payload, scope)

    package, plan, answer = await _answer(db, payload, project_ids=project_ids, user=user)

    session_id, message_id = await _persist_turn(
        db, payload=payload, user=user, answer=answer, package=package
    )

    from app.services.audit import AuditService

    await AuditService(db).record(
        action=AuditAction.COPILOT_QUERY,
        entity_type="chat_session",
        entity_id=session_id,
        project_id=payload.project_id,
        user_id=user.id,
        user_email=user.email,
        ip=info.ip,
        user_agent=info.user_agent,
        route=info.route,
        after={
            "query": payload.query,
            "intent": plan.intent.value,
            "citations": len(answer.citations),
            "confidence": answer.confidence,
            "needs_review": answer.needs_review,
        },
    )

    return AnswerResponse(
        answer=answer.text,
        citations=[_citation(c) for c in answer.citations],
        confidence=answer.confidence,
        confidence_band=answer.confidence_band,
        response_format=answer.response_format,
        refused=answer.refused,
        needs_review=answer.needs_review,
        warnings=answer.warnings,
        plan=_plan_explanation(plan),
        session_id=session_id,
        message_id=message_id,
        model=answer.model,
        duration_ms=int((time.perf_counter() - started) * 1000),
        tokens=answer.usage.total,
        cost_usd=answer.cost_usd,
    )


@copilot_router.post("/stream", summary="Ask a question, streamed")
async def ask_stream(
    payload: AskRequest,
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
) -> EventSourceResponse:
    """Stream the answer token by token over SSE.

    Citations are emitted **after** the text completes, not during. A citation can
    only be checked once the text containing it exists, and streaming an unverified
    label would put a reference on screen that might then be withdrawn.
    """
    project_ids = await _scope_for(payload, scope)

    from app.ai.rag.engine import RAGEngine
    from app.ai.retrieval import (
        ContextAssembler,
        RetrievalEngine,
        RetrievalPlanner,
    )

    plan = RetrievalPlanner().plan(
        payload.query,
        project_ids=project_ids,
        scope=payload.scope,
        contract_ids=payload.contract_ids or None,
        mode=payload.mode,
    )
    retrieval = await RetrievalEngine(db).retrieve(plan)
    history = await _history(db, payload.session_id, user)
    package = ContextAssembler().assemble(
        query=payload.query,
        intent=plan.intent,
        retrieval=retrieval,
        history=history,
    )

    engine = RAGEngine()
    stream, prompt = await engine.stream(package, response_format=payload.response_format)

    async def events() -> Any:
        import json

        # The plan first, so the UI can show "searching 12 contracts" before any
        # token arrives.
        yield {"event": "plan", "data": json.dumps(_plan_explanation(plan).model_dump(mode="json"))}

        if stream is None:
            from app.ai.rag.engine import RAGEngine as _Engine

            empty = await _Engine().answer(package)
            yield {"event": "token", "data": json.dumps({"text": empty.text})}
            yield {
                "event": "done",
                "data": json.dumps(
                    {
                        "citations": [],
                        "confidence": 0.0,
                        "needs_review": False,
                    }
                ),
            }
            return

        collected: list[str] = []
        try:
            async for token in stream:
                collected.append(token)
                yield {"event": "token", "data": json.dumps({"text": token})}
        except Exception as exc:  # noqa: BLE001 - a stream error must close cleanly
            logger.warning("copilot_stream_failed", error=str(exc))
            yield {
                "event": "error",
                "data": json.dumps(
                    {"message": "The answer stream was interrupted. Please try again."}
                ),
            }
            return

        # Validate only now: a citation is checkable once its text exists.
        answer = engine.validate_text("".join(collected), package, prompt)
        yield {
            "event": "done",
            "data": json.dumps(
                {
                    "citations": [_citation(c).model_dump(mode="json") for c in answer.citations],
                    "confidence": answer.confidence,
                    "confidence_band": answer.confidence_band.value,
                    "needs_review": answer.needs_review,
                    "warnings": answer.warnings,
                    # If any label was fabricated the client must re-render the
                    # cleaned text rather than keep what it streamed.
                    "text": answer.text if answer.invalid_citations else None,
                }
            ),
        }

    return EventSourceResponse(events())


# =============================================================================
# Sessions
# =============================================================================
@copilot_router.post(
    "/sessions",
    response_model=ChatSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a conversation",
)
async def create_session(
    payload: ChatSessionCreate,
    user: CurrentUserDep,
    db: DbSession,
    scope: AccessScopeDep,
) -> ChatSessionResponse:
    if payload.project_id is not None:
        scope.require(payload.project_id)

    from app.core.enums import SearchScope
    from app.models.chat import ChatSession

    # `scope` + `scope_ref` rather than separate columns: the reference is one field
    # discriminated by the scope, so a contract-scoped and a project-scoped session
    # share one shape.
    scope_value = SearchScope.CONTRACT if payload.contract_id else SearchScope.PROJECT
    session = ChatSession(
        user_id=user.id,
        project_id=payload.project_id,
        scope=scope_value,
        scope_ref=payload.contract_id or payload.project_id,
        title=payload.title or "New conversation",
    )
    db.add(session)
    await db.flush()
    return _session_response(session)


@copilot_router.get(
    "/sessions",
    response_model=list[ChatSessionResponse],
    summary="List your conversations",
)
async def list_sessions(
    user: CurrentUserDep,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[ChatSessionResponse]:
    """Your own conversations only.

    Scoped to the caller rather than the project: a chat history is personal, and
    another project member has no business reading the questions you asked.
    """
    from sqlalchemy import select

    from app.models.chat import ChatSession

    rows = (
        (
            await db.execute(
                select(ChatSession)
                .where(ChatSession.user_id == user.id, ChatSession.deleted_at.is_(None))
                .order_by(ChatSession.updated_at.desc().nullslast())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [_session_response(row) for row in rows]


@copilot_router.get(
    "/sessions/{session_id}",
    response_model=ChatSessionResponse,
    summary="Conversation with its messages",
)
async def get_session(
    session_id: uuid.UUID, user: CurrentUserDep, db: DbSession
) -> ChatSessionResponse:
    session, messages = await _load_session(db, session_id, user)
    return _session_response(
        session,
        messages=[
            ChatMessageResponse(
                id=message.id,
                role=str(message.role),
                content=message.content,
                created_at=message.created_at,
                citations=[
                    CitationResponse(**_rehydrate_citation(c)) for c in (message.citations or [])
                ],
                confidence=float(message.confidence) if message.confidence else None,
                needs_review=bool(getattr(message, "needs_review", False)),
            )
            for message in messages
        ],
    )


@copilot_router.delete(
    "/sessions/{session_id}",
    response_model=MessageResponse,
    summary="Delete a conversation",
)
async def delete_session(
    session_id: uuid.UUID, user: CurrentUserDep, db: DbSession
) -> MessageResponse:
    session, _ = await _load_session(db, session_id, user)

    from app.models.chat import ChatSession
    from app.repositories.base import BaseRepository

    class _Sessions(BaseRepository[ChatSession]):
        model = ChatSession

    await _Sessions(db).soft_delete(session)
    return MessageResponse(message="The conversation was deleted.")


# =============================================================================
# Helpers
# =============================================================================
async def _scope_for(payload: SearchRequest, scope: AccessScope) -> list[uuid.UUID]:
    """Resolve the project scope from membership, never from the request."""
    return await resolve_scope_for_project(payload.project_id, scope)


async def _answer(
    db: Any, payload: AskRequest, *, project_ids: list[uuid.UUID], user: Any
) -> tuple[Any, Any, Any]:
    """Plan, retrieve, assemble and answer.

    Returns ``(package, plan, answer)``. The raw retrieval result is deliberately not
    returned: the context package is the authoritative record of what the answer was
    allowed to see, and exposing both invites a caller to report evidence the model
    never actually received.
    """
    from app.ai.rag.engine import RAGEngine
    from app.ai.retrieval import ContextAssembler, RetrievalEngine, RetrievalPlanner

    plan = RetrievalPlanner().plan(
        payload.query,
        project_ids=project_ids,
        scope=payload.scope,
        contract_ids=payload.contract_ids or None,
        mode=payload.mode,
        agreement_types=payload.agreement_types or None,
    )
    retrieval = await RetrievalEngine(db).retrieve(plan)
    history = await _history(db, payload.session_id, user)
    package = ContextAssembler().assemble(
        query=payload.query,
        intent=plan.intent,
        retrieval=retrieval,
        history=history,
    )
    answer = await RAGEngine().answer(package, response_format=payload.response_format)
    return package, plan, answer


async def _history(db: Any, session_id: uuid.UUID | None, user: Any) -> list[dict[str, str]]:
    """Recent turns, so pronouns in a follow-up resolve."""
    if session_id is None:
        return []
    try:
        _, messages = await _load_session(db, session_id, user)
    except NotFoundError:
        return []
    return [
        {"role": str(message.role), "content": message.content}
        for message in messages[-_HISTORY_TURNS:]
    ]


async def _load_session(db: Any, session_id: uuid.UUID, user: Any) -> tuple[Any, list[Any]]:
    """Load a session the caller owns, or 404."""
    from sqlalchemy import select

    from app.models.chat import ChatMessage, ChatSession

    session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                # Ownership, not project membership: a conversation is personal.
                ChatSession.user_id == user.id,
                ChatSession.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise NotFoundError("Conversation", session_id)

    messages = (
        (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at)
            )
        )
        .scalars()
        .all()
    )
    return session, list(messages)


async def _persist_turn(
    db: Any, *, payload: AskRequest, user: Any, answer: Any, package: Any
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Store the question and the answer, when the ask belongs to a conversation.

    A one-off question is not persisted: an unsolicited chat history is a privacy
    cost with no user benefit.
    """
    if payload.session_id is None:
        return None, None

    from app.models.chat import ChatMessage

    session, _ = await _load_session(db, payload.session_id, user)

    db.add(
        ChatMessage(
            session_id=session.id,
            role=ChatRole.USER,
            content=payload.query,
        )
    )
    assistant = ChatMessage(
        session_id=session.id,
        role=ChatRole.ASSISTANT,
        content=answer.text,
        citations=[_citation(c).model_dump(mode="json") for c in answer.citations],
        confidence=round(answer.confidence, 4),
    )
    db.add(assistant)

    if not session.title or session.title == "New conversation":
        # First question becomes the title, so the list is browsable.
        session.title = payload.query[:120]

    # The model tracks these, so the session list can be ordered and summarised
    # without counting messages on every read.
    session.message_count = (session.message_count or 0) + 2
    session.last_message_at = datetime.now(UTC)

    await db.flush()
    return session.id, assistant.id


def _session_response(
    session: Any, *, messages: list[ChatMessageResponse] | None = None
) -> ChatSessionResponse:
    """Serialise a session, resolving the discriminated scope reference."""
    from app.core.enums import SearchScope

    contract_id = session.scope_ref if session.scope is SearchScope.CONTRACT else None
    return ChatSessionResponse(
        id=session.id,
        project_id=session.project_id,
        contract_id=contract_id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=len(messages) if messages is not None else session.message_count,
        messages=messages or [],
    )


def _plan_explanation(plan: Any) -> PlanExplanation:
    return PlanExplanation(
        intent=plan.intent,
        strategy=plan.strategy,
        scope=plan.scope,
        mode=plan.mode,
        filters=plan.filters.as_dict(),
        reasoning=plan.reasoning,
        levels=[
            {"level": b.level.value, "limit": b.limit, "min_similarity": b.min_similarity}
            for b in plan.levels
        ],
    )


def _hit(item: Any) -> SearchHit:
    return SearchHit(
        level=item.level.value,
        ref_id=item.ref_id,
        contract_id=item.contract_id,
        contract_title=item.contract_title,
        text=item.text,
        score=round(item.score, 6),
        source=item.source,
        rank=item.rank,
        clause_type=item.clause_type,
        clause_number=item.clause_number,
        section_title=item.section_title,
        page_start=item.page_start,
        page_end=item.page_end,
        bounding_boxes=[BoundingBox(**box) for box in item.bounding_boxes],
        chunk_id=item.chunk_id,
    )


def _citation(citation: Any) -> CitationResponse:
    return CitationResponse(
        label=citation.label,
        contract_id=citation.contract_id,
        contract_title=citation.contract_title,
        level=citation.level,
        ref_id=citation.ref_id,
        text=citation.text,
        clause_type=citation.clause_type,
        clause_number=citation.clause_number,
        section_title=citation.section_title,
        page_start=citation.page_start,
        page_end=citation.page_end,
        page_range=citation.page_range,
        bounding_boxes=[BoundingBox(**box) for box in citation.bounding_boxes],
        chunk_id=citation.chunk_id,
        score=round(citation.score, 6),
    )


def _rehydrate_citation(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce a stored citation back into the response shape."""
    payload = dict(raw)
    payload["bounding_boxes"] = [
        BoundingBox(**box) if isinstance(box, dict) else box
        for box in (payload.get("bounding_boxes") or [])
    ]
    return payload


__all__ = ["copilot_router", "router"]

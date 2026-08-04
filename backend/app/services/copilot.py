"""Copilot service - the one place the query pipeline is assembled.

The stages themselves live where they belong: classification in
:mod:`app.ai.retrieval.analysis`, planning in the planner, execution in the
retrieval engine, packing in the context assembler, generation in the RAG engine.
This module owns only the *sequence*, plus the two decisions that belong to
neither the retrieval layer nor the generation layer:

* **Scope.** Which projects and which contract this question may see. Resolved
  from membership before anything else runs, and re-asserted on the contract id -
  a caller cannot widen its own scope by naming a contract in another project.
* **The guardrail.** Whether what came back is good enough to answer from at all.
  Retrieval's per-level floors decide what is *returned*; this decides whether the
  best of it clears the bar. Below it the answer is a fixed sentence and no
  inference call is made - not a cost optimisation but a correctness one, because
  a model handed weak evidence and a contract question will produce something
  plausible from its own knowledge of contracts, which is the exact failure this
  platform exists to prevent.

Everything the pipeline needs is injected with a default, so a test substitutes a
stage without patching module globals.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.docpipeline.mapping import ResolvedDocumentType, load_doc_types, resolve_document_type
from app.ai.rag.engine import Answer, RAGEngine
from app.ai.retrieval.analysis import QueryAnalysis, QueryAnalysisService
from app.ai.retrieval.context import ContextAssembler, ContextPackage
from app.ai.retrieval.engine import RetrievalEngine, RetrievalResult
from app.ai.retrieval.planner import RetrievalPlan, RetrievalPlanner
from app.core import metrics
from app.core.config import get_settings
from app.core.enums import ConfidenceBand, SearchScope
from app.core.errors import NotFoundError, ProviderError
from app.core.logging import get_logger
from app.core.telemetry import set_span_attributes, span
from app.models.contract import Contract

logger = get_logger(__name__)

#: The exact wording required when nothing retrieved clears the similarity bar.
#: A constant rather than an inline string because it is a product decision that
#: two call sites and a test all have to agree on.
INSUFFICIENT_CONTEXT = (
    "I couldn't find wording in this contract that answers that confidently, so "
    "I'd rather say so than guess.\n\n"
    "Two things usually help:\n"
    "- Name the clause directly - \"What is the liability cap?\" rather than "
    "\"Key risks?\". Broad questions match many passages weakly instead of one "
    "strongly, which is what this check measures.\n"
    "- Check the contract has finished processing. Clauses only become "
    "searchable once extraction and indexing complete."
)

#: Shown when retrieval succeeded but the model could not be reached. Deliberately
#: distinct from the guardrail message: "we found nothing" and "we found this and
#: could not summarise it" are different facts, and the sources below the message
#: only make sense against the second.
GENERATION_UNAVAILABLE = (
    "The answer could not be generated just now - the language model is "
    "unavailable. The passages the search found are listed below; they are the "
    "evidence an answer would have been built from."
)

#: How retrieval was scoped, reported back so a thin answer is explainable.
MODE_DOCUMENT_TYPE = "DocumentTypeFiltered"
MODE_CONTRACT = "ContractScoped"
MODE_UNFILTERED = "Unfiltered"

#: Retrieval's internal provenance labels, in the vocabulary the API reports.
#: ``neighbour`` and ``graph`` become ``context``: neither was retrieved on its own
#: merit, and presenting them as matches would overstate what the search found.
_MATCH_TYPES = {
    "vector": "semantic",
    "keyword": "keyword",
    "fused": "hybrid",
    "neighbour": "context",
    "graph": "context",
}


@dataclass(slots=True)
class CopilotSourceRef:
    """One supporting passage, in the shape the API reports it."""

    contract_id: uuid.UUID
    contract_name: str | None
    clause_heading: str | None
    section_number: str | None
    page_number: int | None
    #: ``None`` for a keyword-only hit, which has no cosine similarity to report.
    similarity_score: float | None
    text: str
    label: int
    #: The re-ranker's relevance judgement, when one ran. A stronger signal than
    #: similarity and reported separately rather than blended into it.
    rerank_score: float | None = None
    #: ``semantic`` · ``keyword`` · ``hybrid`` · ``context``. How the passage was
    #: found, so a similarity of ``None`` is explainable rather than a gap.
    match_type: str = "semantic"


@dataclass(slots=True)
class CopilotPreparation:
    """Everything decided before a token is generated.

    Split out so the streaming endpoint runs the identical classification,
    retrieval and guardrail path as the non-streaming one. The alternative - each
    endpoint assembling the stages itself - is how the two drift until a question
    is refused over SSE and answered over POST.
    """

    query: str
    plan: RetrievalPlan
    retrieval: RetrievalResult
    analysis: QueryAnalysis
    document_type: ResolvedDocumentType | None
    retrieval_mode: str
    timings: dict[str, int]
    started: float
    #: ``None`` when the guardrail fired: nothing was assembled, because nothing
    #: is going to be generated.
    package: ContextPackage | None = None
    #: True when a document-type filter matched nothing and was dropped for a
    #: second attempt. The user has to be told - a thin answer from a widened
    #: search reads exactly like a thin answer from a narrow one.
    relaxed_filters: bool = False

    @property
    def insufficient_context(self) -> bool:
        return self.package is None


@dataclass(slots=True)
class CopilotResult:
    """A finished answer plus everything needed to explain and audit it."""

    answer: str
    sources: list[CopilotSourceRef] = field(default_factory=list)
    #: True when the guardrail fired. The answer is the fixed sentence and no
    #: provider call was made.
    insufficient_context: bool = False
    #: True when retrieval succeeded but generation failed. Sources are still
    #: populated; the answer text is not a synthesis.
    generation_failed: bool = False
    document_type: str | None = None
    document_type_detected: bool = False
    document_type_confidence: float = 0.0
    retrieval_mode: str = MODE_UNFILTERED
    retrieved_chunks: int = 0
    top_similarity: float = 0.0
    #: True when a document-type filter matched nothing and the search was widened.
    relaxed_filters: bool = False
    #: True when more contracts matched than the pre-filter can carry, so results
    #: are ranked across the project rather than a pre-selected subset.
    scope_truncated: bool = False
    confidence: float = 0.0
    confidence_band: str = ConfidenceBand.LOW.value
    needs_review: bool = False
    refused: bool = False
    warnings: list[str] = field(default_factory=list)
    model: str | None = None
    tokens: int = 0
    cost_usd: float = 0.0
    timings: dict[str, int] = field(default_factory=dict)
    #: Retained for the caller that has to persist the turn and write the audit
    #: row; not serialised into the response.
    plan: RetrievalPlan | None = None
    retrieval: RetrievalResult | None = None
    package: ContextPackage | None = None
    generated: Answer | None = None
    analysis: QueryAnalysis | None = None


class CopilotService:
    """Runs a question through the retrieval-augmented answer pipeline."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        analysis_service: QueryAnalysisService | None = None,
        planner: RetrievalPlanner | None = None,
        retrieval_engine: RetrievalEngine | None = None,
        assembler: ContextAssembler | None = None,
        rag_engine: RAGEngine | None = None,
    ) -> None:
        self.db = db
        self._settings = get_settings().retrieval
        self._analysis = analysis_service or QueryAnalysisService()
        self._planner = planner or RetrievalPlanner()
        self._retrieval = retrieval_engine or RetrievalEngine(db)
        self._assembler = assembler or ContextAssembler()
        # Constructed lazily: building it resolves the inference provider, and the
        # guardrail path must be reachable without one.
        self._rag = rag_engine

    async def prepare(
        self,
        query: str,
        *,
        project_ids: list[uuid.UUID],
        contract_id: uuid.UUID | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> CopilotPreparation:
        """Classify, plan, retrieve and apply the guardrail.

        ``project_ids`` must already be the caller's accessible set - this service
        does not resolve membership, and passing a wider list would defeat the
        isolation boundary rather than being caught here.
        """
        started = time.perf_counter()
        timings: dict[str, int] = {}

        # One span per stage, so "the Copilot is slow" resolves to a flame graph
        # rather than to a guess. The structured log line carries the same
        # figures, but logs cannot be correlated across the stages of one
        # question the way a trace can.
        set_span_attributes(
            **{
                "cip.copilot.projects": len(project_ids),
                "cip.copilot.contract_scoped": contract_id is not None,
            }
        )

        with span("copilot.load_contract", **{"cip.contract_id": str(contract_id or "")}):
            contract = await self._load_contract(contract_id, project_ids) if contract_id else None

        # ---- 1. analyse -----------------------------------------------------
        #
        # The classification and the query embedding are independent - the vector
        # depends only on the question text - and both are network round trips of
        # comparable length. Running them in sequence put the whole of one on the
        # critical path of every question for no reason.
        with span("copilot.analysis") as current:
            analysis_task = self._document_type(query, contract)
            embedding_task = self._warm_query_embedding(query)
            (analysis, document_type), _ = await asyncio.gather(analysis_task, embedding_task)
            current.set_attribute("cip.analysis.method", analysis.method)
            current.set_attribute("cip.analysis.document_type", analysis.document_type or "")
            current.set_attribute("cip.analysis.confidence", analysis.confidence)
        timings["analysis_ms"] = analysis.duration_ms

        # ---- 2. plan and retrieve -------------------------------------------
        plan = self._planner.plan(
            query,
            project_ids=project_ids,
            scope=SearchScope.CONTRACT if contract_id else SearchScope.PROJECT,
            contract_ids=[contract_id] if contract_id else None,
            analysis=analysis,
            document_type=document_type,
            level_limit=self._settings.copilot_top_k,
            # Answers come from passages, never from the metadata projection alone.
            # It is also what makes the similarity guardrail below meaningful: a
            # plan with no vector levels has no similarity to judge.
            prefer_content=True,
            # This is an answering caller, so the document-summary level may be
            # skipped when a contract is already named - it selects documents, and
            # the selection is made. `/search` deliberately does not set this: its
            # results are browsed rather than answered from, and the summary is a
            # row the reader came to see.
            for_answer=True,
        )
        with span(
            "copilot.retrieval",
            **{
                "cip.plan.intent": plan.intent.value,
                "cip.plan.strategy": plan.strategy.value,
                "cip.plan.agreement_types": ",".join(plan.filters.agreement_types),
            },
        ) as current:
            retrieval = await self._retrieval.retrieve(plan)
            current.set_attribute("cip.retrieval.evidence", len(retrieval.evidence))
            current.set_attribute("cip.retrieval.top_similarity", retrieval.answerable_similarity)
            current.set_attribute("cip.retrieval.truncated", retrieval.truncated)
            current.set_attribute("cip.retrieval.rerank_ms", retrieval.rerank_ms)
        timings["retrieval_ms"] = retrieval.duration_ms
        timings["rerank_ms"] = retrieval.rerank_ms

        preparation = CopilotPreparation(
            query=query,
            plan=plan,
            retrieval=retrieval,
            analysis=analysis,
            document_type=document_type,
            retrieval_mode=self._mode(plan, document_type, contract_id),
            timings=timings,
            started=started,
        )

        # ---- 3. relaxed retry -------------------------------------------------
        if not retrieval.evidence and plan.filters.agreement_types:
            # The type filter removed everything. One bounded retry without it,
            # because "the classifier picked the wrong type" and "the answer is not
            # in the corpus" are indistinguishable from here, and only one of them
            # is worth telling the user about.
            dropped = list(plan.filters.agreement_types)
            logger.info("copilot_retry_unfiltered", dropped=dropped)
            plan.filters.agreement_types = []
            plan.reasoning.append(
                f"Nothing matched within '{', '.join(dropped)}', so the search was "
                "repeated across every document type."
            )
            retrieval = await self._retrieval.retrieve(plan)
            preparation.retrieval = retrieval
            preparation.retrieval_mode = MODE_CONTRACT if contract_id else MODE_UNFILTERED
            preparation.relaxed_filters = True
            timings["retrieval_ms"] = timings.get("retrieval_ms", 0) + retrieval.duration_ms
            timings["rerank_ms"] = timings.get("rerank_ms", 0) + retrieval.rerank_ms

        # ---- 4. guardrail ----------------------------------------------------
        threshold = self._settings.answer_similarity_threshold
        # `answerable_similarity`, not `top_similarity`: an L1 document summary is
        # long and topical and scores 0.55-0.70 against almost any question about
        # that contract. Comparing the overall maximum against the threshold let a
        # document that is merely *about* the subject vouch for clause evidence that
        # scored 0.31, which is the exact case this guardrail exists to catch.
        similarity = retrieval.answerable_similarity
        if not retrieval.evidence or similarity < threshold:
            logger.info(
                "copilot_insufficient_context",
                answerable_similarity=round(similarity, 4),
                top_similarity=round(retrieval.top_similarity, 4),
                by_level={k: round(v, 4) for k, v in retrieval.top_similarity_by_level.items()},
                threshold=threshold,
                retrieved=len(retrieval.evidence),
            )
            return preparation

        # ---- 5. assemble ------------------------------------------------------
        with span("copilot.assembly") as current:
            preparation.package = self._assembler.assemble(
                query=query,
                intent=plan.intent,
                retrieval=retrieval,
                history=history,
                max_citations=self._settings.top_context_chunks,
            )
            current.set_attribute("cip.context.citations", len(preparation.package.citations))
            current.set_attribute("cip.context.tokens", preparation.package.token_estimate)
            current.set_attribute("cip.context.dropped", preparation.package.dropped)

        metrics.copilot_context_chunks.observe(len(preparation.package.citations))
        return preparation

    async def answer(
        self,
        query: str,
        *,
        project_ids: list[uuid.UUID],
        contract_id: uuid.UUID | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> CopilotResult:
        """Answer ``query`` from the contracts in ``project_ids``."""
        preparation = await self.prepare(
            query, project_ids=project_ids, contract_id=contract_id, history=history
        )

        if preparation.package is None:
            return self.guardrail_result(preparation)

        inference_started = time.perf_counter()
        try:
            with span("copilot.generation"):
                generated = await self._engine().answer(preparation.package)
        except ProviderError as exc:
            preparation.timings["inference_ms"] = int(
                (time.perf_counter() - inference_started) * 1000
            )
            return self.degraded_result(preparation, exc)
        preparation.timings["inference_ms"] = int((time.perf_counter() - inference_started) * 1000)

        return self.finish(preparation, generated)

    def degraded_result(self, preparation: CopilotPreparation, error: Exception) -> CopilotResult:
        """The answer when the model could not be reached.

        The sources are returned anyway. Retrieval already ran, already cost money,
        and already found the passages - handing back the evidence with an honest
        "no synthesis" is far more use to a reviewer than a 500, and it is exactly
        the passages they would have gone to read next.
        """
        package = preparation.package
        preparation.timings["total_ms"] = self._elapsed(preparation)
        logger.warning(
            "copilot_generation_unavailable",
            error=str(error),
            retrieved=len(preparation.retrieval.evidence),
        )
        result = CopilotResult(
            answer=GENERATION_UNAVAILABLE,
            sources=self._sources(package, None) if package else [],
            generation_failed=True,
            retrieval_mode=preparation.retrieval_mode,
            retrieved_chunks=len(package.citations) if package else 0,
            top_similarity=round(preparation.retrieval.answerable_similarity, 4),
            relaxed_filters=preparation.relaxed_filters,
            scope_truncated=preparation.retrieval.truncated,
            needs_review=True,
            warnings=[
                "The answer could not be generated. The passages below are what the "
                "search found, unsummarised."
            ],
            timings=preparation.timings,
            plan=preparation.plan,
            retrieval=preparation.retrieval,
            package=package,
            analysis=preparation.analysis,
        )
        self._describe(result, preparation.document_type, preparation.analysis)
        self._log(preparation.query, result, guardrail=False)
        return result

    def guardrail_result(self, preparation: CopilotPreparation) -> CopilotResult:
        """The answer when nothing retrieved was good enough to answer from."""
        preparation.timings["total_ms"] = self._elapsed(preparation)
        result = CopilotResult(
            answer=INSUFFICIENT_CONTEXT,
            insufficient_context=True,
            retrieval_mode=preparation.retrieval_mode,
            # The answerable figure, matching what the guardrail compared. Reporting
            # the overall maximum here would show a similarity above the threshold
            # next to a message saying the threshold was not met.
            top_similarity=round(preparation.retrieval.answerable_similarity, 4),
            relaxed_filters=preparation.relaxed_filters,
            scope_truncated=preparation.retrieval.truncated,
            warnings=list(preparation.retrieval.warnings),
            timings=preparation.timings,
            plan=preparation.plan,
            retrieval=preparation.retrieval,
            analysis=preparation.analysis,
        )
        self._describe(result, preparation.document_type, preparation.analysis)
        self._log(preparation.query, result, guardrail=True)
        return result

    def finish(self, preparation: CopilotPreparation, generated: Answer) -> CopilotResult:
        """Assemble the result once an answer exists.

        Separate from :meth:`answer` so the streaming endpoint, which cannot
        validate citations until its stream completes, reports exactly the same
        fields as the non-streaming one.
        """
        package = preparation.package
        if package is None:  # pragma: no cover - guarded by the caller
            return self.guardrail_result(preparation)

        preparation.timings["total_ms"] = self._elapsed(preparation)
        result = CopilotResult(
            answer=generated.text,
            sources=self._sources(package, generated),
            retrieval_mode=preparation.retrieval_mode,
            retrieved_chunks=len(package.citations),
            top_similarity=round(preparation.retrieval.answerable_similarity, 4),
            relaxed_filters=preparation.relaxed_filters,
            scope_truncated=preparation.retrieval.truncated,
            confidence=generated.confidence,
            confidence_band=generated.confidence_band.value,
            needs_review=generated.needs_review,
            refused=generated.refused,
            warnings=list(generated.warnings),
            model=generated.model or None,
            tokens=generated.usage.total,
            cost_usd=generated.cost_usd,
            timings=preparation.timings,
            plan=preparation.plan,
            retrieval=preparation.retrieval,
            package=package,
            generated=generated,
            analysis=preparation.analysis,
        )
        self._describe(result, preparation.document_type, preparation.analysis)
        self._log(preparation.query, result, guardrail=False)
        return result

    @staticmethod
    def _elapsed(preparation: CopilotPreparation) -> int:
        return int((time.perf_counter() - preparation.started) * 1000)

    # =========================================================================
    # Scope
    # =========================================================================
    async def _load_contract(
        self, contract_id: uuid.UUID, project_ids: list[uuid.UUID]
    ) -> Contract:
        """Load a contract the caller may read, or 404.

        Bounded by ``project_ids`` in the same statement rather than fetched and
        then checked: a contract in another project must be indistinguishable from
        one that does not exist, or the endpoint becomes a way to probe for ids.
        """
        contract = (
            await self.db.execute(
                select(Contract).where(
                    Contract.id == contract_id,
                    Contract.project_id.in_(project_ids),
                    Contract.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if contract is None:
            raise NotFoundError("Contract", contract_id)
        return contract

    @staticmethod
    async def _warm_query_embedding(query: str) -> None:
        """Embed the query concurrently with classification, into the provider cache.

        The retrieval engine calls ``embed_query`` itself and is the authority on
        when and how - this only ensures the round trip has already happened by the
        time it asks. A failure here is ignored entirely: the engine has its own
        degradation path to keyword-only search, and pre-warming must not be able
        to introduce a failure mode that the real call does not already have.
        """
        try:
            from app.ai.embedding import get_embedding_provider

            await get_embedding_provider().embed_query(query)
        except Exception as exc:  # noqa: BLE001 - a warm-up cannot fail a question
            logger.debug("query_embedding_prewarm_failed", error=str(exc))

    # =========================================================================
    # Document type
    # =========================================================================
    async def _document_type(
        self, query: str, contract: Contract | None
    ) -> tuple[QueryAnalysis, ResolvedDocumentType | None]:
        """Decide which document type the question is about.

        When a contract is named there is nothing to infer: its ``agreement_type``
        is recorded fact, and asking a model to guess it from the question could
        only disagree with the database. The classifier runs for project-wide
        questions, where the type is genuinely unknown.

        No filter is returned in the contract case even though the type is known.
        The contract id already narrows retrieval to that one document, so an
        agreement-type predicate on top could only remove rows - and would remove
        them for a document whose vectors predate that field being populated.
        """
        if contract is not None:
            return (
                QueryAnalysis(
                    document_type=contract.agreement_type,
                    confidence=1.0 if contract.agreement_type else 0.0,
                    method="skipped",
                    reasoning="The question names a contract, so its recorded type is used.",
                ),
                None,
            )

        # `LookupError` only: that is the one failure this can absorb honestly - no
        # document profiles are configured, so there is no vocabulary to classify
        # into, and the question is still answerable without a type filter.
        #
        # A bare `except Exception` here was worse than it looked. It also caught a
        # missing table, a dead connection and a bug in the query, and because the
        # early return skips `analyse()` entirely it discarded *the whole query
        # analysis* - intent, entities and all - not merely the type filter. Every
        # question then fell back to the deterministic planner with
        # `intent=GENERAL_QA`, silently and with no user-visible error.
        try:
            doc_types = await load_doc_types(self.db)
        except LookupError as exc:
            logger.warning("doc_type_vocabulary_unavailable", error=str(exc))
            return QueryAnalysis(method="unavailable"), None

        analysis = await self._analysis.analyse(query, doc_types=doc_types)
        if not analysis.has_document_type:
            return analysis, None

        resolved = await resolve_document_type(self.db, analysis.document_type or "")
        return analysis, resolved

    @staticmethod
    def _mode(
        plan: RetrievalPlan,
        document_type: ResolvedDocumentType | None,
        contract_id: uuid.UUID | None,
    ) -> str:
        """How retrieval was actually scoped.

        Read from the plan rather than from the detection result, because the
        threshold sits between the two: a type can be detected and still not
        filtered, and reporting the detection would misdescribe the search.
        """
        if plan.filters.agreement_types and document_type is not None:
            return MODE_DOCUMENT_TYPE
        if contract_id is not None:
            return MODE_CONTRACT
        return MODE_UNFILTERED

    @staticmethod
    def _describe(
        result: CopilotResult,
        document_type: ResolvedDocumentType | None,
        analysis: QueryAnalysis,
    ) -> None:
        """Attach the detection outcome to the result.

        ``document_type_detected`` reports whether a known type actually *shaped*
        the search, not whether one was proposed. A type detected at 0.6 and
        discarded by the threshold did not narrow anything, and reporting it as
        detected would describe a search that did not happen. A contract-scoped
        question counts: the contract id narrowed retrieval more tightly than the
        type could have.
        """
        result.document_type = document_type.label if document_type else analysis.document_type
        result.document_type_detected = bool(result.document_type) and result.retrieval_mode in {
            MODE_DOCUMENT_TYPE,
            MODE_CONTRACT,
        }
        result.document_type_confidence = round(analysis.confidence, 4)

    # =========================================================================
    # Sources
    # =========================================================================
    @staticmethod
    def _sources(package: ContextPackage, generated: Answer | None) -> list[CopilotSourceRef]:
        """The passages behind the answer.

        Cited passages only, when the answer cited any. Listing everything
        retrieved would present passages the answer did not use as if they
        supported it, which is the same misattribution a fabricated citation
        makes - just in the other direction.

        ``generated`` is ``None`` on the degraded path, where there is no answer to
        attribute to and every admitted passage is shown.
        """
        citations = (generated.citations if generated else None) or package.citations
        return [
            CopilotSourceRef(
                contract_id=citation.contract_id,
                contract_name=citation.contract_title,
                clause_heading=(
                    citation.section_title
                    or (
                        citation.clause_type.replace("_", " ").title()
                        if citation.clause_type
                        else None
                    )
                ),
                section_number=citation.clause_number,
                page_number=citation.page_start,
                # The cosine similarity, not the fusion score - see `Citation.similarity`.
                # Falls back to 0.0 rather than to `score`, because reporting an RRF
                # value under a "similarity" label would be a wrong number, not a
                # missing one.
                # None, not 0.0, when the passage came from the keyword leg. A
                # keyword hit has no cosine similarity - that is unmeasured, not
                # irrelevant - and "Similarity 0.00" beside what may be the best
                # exact-phrase match in the corpus reads as the opposite of the truth.
                similarity_score=(
                    round(citation.similarity, 4) if citation.similarity is not None else None
                ),
                rerank_score=(
                    round(citation.rerank_score, 4) if citation.rerank_score is not None else None
                ),
                match_type=_MATCH_TYPES.get(citation.source, citation.source),
                text=citation.text,
                label=citation.label,
            )
            for citation in citations
        ]

    # =========================================================================
    # Helpers
    # =========================================================================
    def _engine(self) -> RAGEngine:
        if self._rag is None:
            self._rag = RAGEngine()
        return self._rag

    def _log(self, query: str, result: CopilotResult, *, guardrail: bool) -> None:
        """One line carrying every figure needed to explain a slow or thin answer."""
        outcome = (
            "insufficient_context"
            if result.insufficient_context
            else "generation_failed"
            if result.generation_failed
            else "refused"
            if result.refused
            else "answered"
        )
        metrics.copilot_queries_total.labels(
            outcome=outcome, retrieval_mode=result.retrieval_mode
        ).inc()
        metrics.copilot_similarity.observe(result.top_similarity)

        logger.info(
            "copilot_query",
            query=query[:200],
            intent=result.plan.intent.value if result.plan else None,
            strategy=result.plan.strategy.value if result.plan else None,
            document_type=result.document_type,
            document_type_detected=result.document_type_detected,
            document_type_confidence=result.document_type_confidence,
            retrieval_mode=result.retrieval_mode,
            retrieved=len(result.retrieval.evidence) if result.retrieval else 0,
            context_chunks=result.retrieved_chunks,
            top_similarity=result.top_similarity,
            similarity_threshold=self._settings.answer_similarity_threshold,
            insufficient_context=guardrail,
            confidence=round(result.confidence, 4),
            needs_review=result.needs_review,
            model=result.model,
            tokens=result.tokens,
            cost_usd=round(result.cost_usd, 6),
            **result.timings,
        )


__all__ = [
    "INSUFFICIENT_CONTEXT",
    "MODE_CONTRACT",
    "MODE_DOCUMENT_TYPE",
    "MODE_UNFILTERED",
    "CopilotResult",
    "CopilotService",
    "CopilotSourceRef",
]

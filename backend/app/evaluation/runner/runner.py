"""Executes the real pipeline, once per golden case.

**Nothing here is mocked.** ``CopilotService`` is constructed exactly as the API
constructs it, against a real session, and it runs the whole chain: query
analysis against the live model, planning, the metadata pre-filter, vector and
keyword search against the real index, re-ranking, context assembly and
generation. What the benchmark scores is what a user would have received.

That has three consequences worth being explicit about, because each of them is
a constraint on how this can be used rather than a detail:

* **It costs money and takes time.** Every case is at least one embedding call
  and one generation, plus a classification and possibly a re-rank. A thousand
  cases is a real bill and tens of minutes. Concurrency is bounded rather than
  unlimited for exactly that reason - see ``concurrency``.
* **It needs a populated database.** A golden case names contracts and clauses
  by id; those rows have to exist, and their embeddings have to be current. A
  run against an empty database produces a beautifully formatted zero.
* **It is not perfectly deterministic.** Generation varies between runs even at
  temperature zero. Retrieval is deterministic; answer text is not, which is why
  the regression gate is built around retrieval and citation metrics and treats
  answer-text checks as a secondary signal.

Each case runs in its own session. A shared session would serialise the whole
run behind one connection and make a failure in one case poison the next.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.evaluation.dataset.models import GoldenCase, GoldenDataset
from app.evaluation.runner.result import (
    CaseResult,
    CitationRecord,
    RetrievedItem,
    RunResult,
)

logger = get_logger(__name__)

#: Concurrent cases. Deliberately modest: the bottleneck is the provider's rate
#: limit and the database pool, and driving either into throttling makes the
#: latency figures measure the benchmark rather than the platform.
DEFAULT_CONCURRENCY = 4


@dataclass(slots=True)
class RunnerOptions:
    """How a run is executed, as opposed to what it measures."""

    concurrency: int = DEFAULT_CONCURRENCY
    #: Per-case ceiling. A case that hangs must not hold the run open; it is
    #: recorded as a failure, which is the honest outcome.
    timeout_seconds: float = 120.0
    #: Skip generation and score retrieval only. Roughly ten times cheaper and
    #: fully deterministic, which makes it the right mode for a threshold sweep
    #: where only retrieval quality is in question.
    retrieval_only: bool = False
    label: str = ""
    #: Overrides applied to ``settings.retrieval`` for the duration of the run.
    #: This is how the sweep and the ablations vary one knob at a time.
    overrides: dict[str, Any] | None = None


class EvaluationRunner:
    """Runs golden cases through the production pipeline."""

    def __init__(self, options: RunnerOptions | None = None) -> None:
        self.options = options or RunnerOptions()

    # =========================================================================
    # Public surface
    # =========================================================================
    async def evaluate(self, case: GoldenCase) -> CaseResult:
        """Run one case end to end. Never raises - a failure becomes a result."""
        try:
            return await asyncio.wait_for(
                self._evaluate(case), timeout=self.options.timeout_seconds
            )
        except TimeoutError:
            logger.warning("evaluation_case_timeout", case=case.id)
            return CaseResult(
                case=case,
                ok=False,
                error=f"timed out after {self.options.timeout_seconds}s",
            )
        except Exception as exc:  # noqa: BLE001 - one bad case must not end the run
            logger.warning("evaluation_case_failed", case=case.id, error=str(exc))
            return CaseResult(case=case, ok=False, error=f"{type(exc).__name__}: {exc}")

    async def run(self, dataset: GoldenDataset) -> RunResult:
        """Run every case, bounded by ``concurrency``, preserving dataset order."""
        started = time.perf_counter()
        started_at = datetime.now(UTC).isoformat()

        with _configuration(self.options.overrides):
            configuration = _snapshot_configuration()
            semaphore = asyncio.Semaphore(max(1, self.options.concurrency))
            completed = 0

            async def _one(case: GoldenCase) -> CaseResult:
                nonlocal completed
                async with semaphore:
                    result = await self.evaluate(case)
                completed += 1
                if completed % 25 == 0 or completed == len(dataset):
                    logger.info(
                        "evaluation_progress",
                        completed=completed,
                        total=len(dataset),
                        dataset=dataset.identifier,
                    )
                return result

            results = await asyncio.gather(*(_one(case) for case in dataset))

        duration = time.perf_counter() - started
        run = RunResult(
            dataset=dataset.identifier,
            label=self.options.label,
            configuration=configuration,
            results=list(results),
            started_at=started_at,
            finished_at=datetime.now(UTC).isoformat(),
            duration_seconds=duration,
        )
        logger.info(
            "evaluation_run_complete",
            dataset=dataset.identifier,
            cases=len(run.results),
            failures=len(run.failures),
            duration_seconds=round(duration, 1),
        )
        return run

    # =========================================================================
    # One case
    # =========================================================================
    async def _evaluate(self, case: GoldenCase) -> CaseResult:
        from app.db.session import session_scope
        from app.services.copilot import CopilotService

        async with session_scope() as session:
            service = CopilotService(session)
            project_ids = [case.project_id] if case.project_id else []

            if self.options.retrieval_only:
                preparation = await service.prepare(
                    case.question,
                    project_ids=project_ids,
                    contract_id=case.contract_id,
                )
                return self._from_preparation(case, preparation)

            result = await service.answer(
                case.question,
                project_ids=project_ids,
                contract_id=case.contract_id,
            )
            return self._from_answer(case, result)

    def _from_preparation(self, case: GoldenCase, preparation: Any) -> CaseResult:
        """Score a retrieval-only run.

        The context package is present unless the guardrail fired, so
        ``in_context`` is still meaningful; ``cited`` never is, because nothing
        was generated. Citation metrics simply have no cases to average over,
        which is the correct behaviour - better an empty metric than one computed
        from a run that could not produce citations.
        """
        record = CaseResult(case=case)
        self._record_plan(
            record, preparation.plan, preparation.analysis, preparation.retrieval_mode
        )
        self._record_retrieval(record, preparation.retrieval, preparation.package, cited_refs=set())

        record.insufficient_context = preparation.package is None
        record.answered = preparation.package is not None
        record.relaxed_filters = preparation.relaxed_filters
        record.scope_truncated = preparation.retrieval.truncated
        record.timings = dict(preparation.timings)
        if preparation.package is not None:
            record.context_tokens = preparation.package.token_estimate
            record.context_dropped = preparation.package.dropped
        return record

    def _from_answer(self, case: GoldenCase, result: Any) -> CaseResult:
        record = CaseResult(case=case)
        self._record_plan(record, result.plan, result.analysis, result.retrieval_mode)

        generated = result.generated
        cited_refs = {citation.ref_id for citation in (generated.citations if generated else [])}
        self._record_retrieval(record, result.retrieval, result.package, cited_refs=cited_refs)

        record.answer = result.answer
        record.insufficient_context = result.insufficient_context
        record.generation_failed = result.generation_failed
        record.refused = result.refused
        record.needs_review = result.needs_review
        # "Answered" means the platform stood behind an answer. A guardrail
        # response, a refusal and a generation failure are all non-answers, and
        # conflating any of them with a real answer would make the guardrail
        # confusion matrix meaningless.
        record.answered = not (
            result.insufficient_context or result.generation_failed or result.refused
        )

        record.confidence = result.confidence
        record.confidence_band = result.confidence_band
        record.relaxed_filters = result.relaxed_filters
        record.scope_truncated = result.scope_truncated
        record.timings = dict(result.timings)
        record.tokens = result.tokens
        record.cost_usd = result.cost_usd

        if result.package is not None:
            record.context_tokens = result.package.token_estimate
            record.context_dropped = result.package.dropped

        record.citations = self._record_citations(result, generated)
        return record

    # =========================================================================
    # Flattening
    # =========================================================================
    @staticmethod
    def _record_plan(record: CaseResult, plan: Any, analysis: Any, mode: str) -> None:
        if plan is not None:
            record.intent = plan.intent.value
            record.strategy = plan.strategy.value
            record.applied_agreement_types = list(plan.filters.agreement_types)
        record.retrieval_mode = mode
        if analysis is not None:
            record.document_type = analysis.document_type
            record.document_type_confidence = analysis.confidence
            record.analysis_method = analysis.method
        record.document_type_detected = bool(
            record.applied_agreement_types and record.document_type
        )

    @staticmethod
    def _record_retrieval(
        record: CaseResult,
        retrieval: Any,
        package: Any,
        *,
        cited_refs: set[uuid.UUID],
    ) -> None:
        if retrieval is None:
            return

        record.top_similarity = retrieval.answerable_similarity
        record.similarity_by_level = {
            level: round(score, 6) for level, score in retrieval.top_similarity_by_level.items()
        }

        # Which passages reached the prompt, by ref id. `in_context` separates
        # "retrieval found it" from "the model was shown it", and the gap between
        # those two is exactly what the context budget controls.
        in_context = {citation.ref_id for citation in (package.citations if package else [])}

        record.retrieved = [
            RetrievedItem(
                rank=index,
                level=item.level.value,
                ref_id=item.ref_id,
                contract_id=item.contract_id,
                chunk_id=item.chunk_id,
                clause_number=item.clause_number,
                section_title=item.section_title,
                page_start=item.page_start,
                page_end=item.page_end,
                similarity=item.similarity,
                rerank_score=item.rerank_score,
                score=item.score,
                source=item.source,
                text=item.text,
                in_context=item.ref_id in in_context,
                cited=item.ref_id in cited_refs,
            )
            for index, item in enumerate(retrieval.evidence, start=1)
        ]

    @staticmethod
    def _record_citations(result: Any, generated: Any) -> list[CitationRecord]:
        if generated is None:
            return []

        offered = {
            citation.label: citation
            for citation in (result.package.citations if result.package else [])
        }
        records = [
            CitationRecord(
                label=citation.label,
                resolved=True,
                contract_id=citation.contract_id,
                ref_id=citation.ref_id,
                clause_number=citation.clause_number,
                section_title=citation.section_title,
                page_start=citation.page_start,
                similarity=citation.similarity,
                in_evidence=citation.label in offered,
            )
            for citation in generated.citations
        ]
        # Fabricated labels are stripped from the answer text before it is shown,
        # so they would otherwise vanish from the record entirely. The rate at
        # which a model invents them is a quality signal about the model, and it
        # is only visible here.
        records.extend(
            CitationRecord(label=label, resolved=False, in_evidence=False)
            for label in generated.invalid_citations
        )
        return records


# =============================================================================
# Configuration
# =============================================================================
class _configuration:  # noqa: N801 - a context manager, reads as one
    """Temporarily override retrieval settings for the duration of a run.

    Sweeps and ablations vary one knob at a time, and the knob lives on a cached
    settings object every layer reads at call time. Mutating the cached object
    and restoring it afterwards is the only way to change it without a process
    restart per sweep point - forty restarts for a six-point sweep across two
    arms is not a benchmark anyone will run.

    Restoration is in ``finally``, so a failed run does not leave the process
    configured for whatever the last sweep point happened to be.
    """

    def __init__(self, overrides: dict[str, Any] | None) -> None:
        self._overrides = overrides or {}
        self._previous: dict[str, Any] = {}

    def __enter__(self) -> None:
        if not self._overrides:
            return
        settings = get_settings()
        for key, value in self._overrides.items():
            target, attribute = self._resolve(settings, key)
            self._previous[key] = getattr(target, attribute)
            object.__setattr__(target, attribute, value)
        logger.info("evaluation_configuration_overridden", **self._overrides)

    def __exit__(self, *_: Any) -> None:
        if not self._previous:
            return
        settings = get_settings()
        for key, value in self._previous.items():
            target, attribute = self._resolve(settings, key)
            object.__setattr__(target, attribute, value)

    @staticmethod
    def _resolve(settings: Any, key: str) -> tuple[Any, str]:
        """``"retrieval.min_similarity_clause"`` -> (settings.retrieval, attr)."""
        if "." not in key:
            return settings.retrieval, key
        group, attribute = key.split(".", 1)
        return getattr(settings, group), attribute


def _snapshot_configuration() -> dict[str, Any]:
    """The settings that shaped a run, recorded alongside its results.

    Without this, two runs that disagree are just two numbers. With it, the
    comparison can say which knob differed - which is the difference between a
    benchmark and a diary.
    """
    settings = get_settings()
    retrieval = settings.retrieval
    embedding = settings.embedding
    return {
        "min_similarity": retrieval.min_similarity,
        "min_similarity_document": retrieval.min_similarity_document,
        "min_similarity_clause": retrieval.min_similarity_clause,
        "min_similarity_chunk": retrieval.min_similarity_chunk,
        "answer_similarity_threshold": retrieval.answer_similarity_threshold,
        "document_type_confidence_threshold": retrieval.document_type_confidence_threshold,
        "copilot_top_k": retrieval.copilot_top_k,
        "top_context_chunks": retrieval.top_context_chunks,
        "reranker_enabled": retrieval.reranker_enabled,
        "reranker_model": retrieval.reranker_model,
        "rerank_top_k": retrieval.rerank_top_k,
        "vector_weight": retrieval.vector_weight,
        "keyword_weight": retrieval.keyword_weight,
        "rrf_k": retrieval.rrf_k,
        "embedding_provider": embedding.provider,
        "embedding_model": embedding.model,
        "embedding_dim": embedding.dim,
        "hnsw_ef_search": embedding.hnsw_ef_search,
        "hnsw_iterative_scan": embedding.hnsw_iterative_scan,
        "llm_model": settings.llm.model,
    }


__all__ = ["DEFAULT_CONCURRENCY", "EvaluationRunner", "RunnerOptions"]

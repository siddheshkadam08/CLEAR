"""What one evaluated case produced.

This is the record every metric is computed from, so it has one job: capture
enough that no metric ever needs to re-run the pipeline, and capture it in a form
that survives being written to JSON and read back a month later by a regression
comparison.

That second requirement is why nothing here holds a live object. The runner
flattens ``Evidence``, ``Citation`` and the plan into plain values at the moment
of capture. Holding references instead would make a result unserialisable, and a
benchmark whose results cannot be stored cannot detect a regression - which is
the entire point of storing them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.evaluation.dataset.models import GoldenCase


@dataclass(slots=True)
class RetrievedItem:
    """One passage the pipeline retrieved, flattened for scoring."""

    rank: int
    level: str
    ref_id: uuid.UUID
    contract_id: uuid.UUID
    chunk_id: uuid.UUID | None = None
    clause_number: str | None = None
    section_title: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    similarity: float | None = None
    rerank_score: float | None = None
    #: Fusion or similarity score, whichever retrieval put on the item.
    score: float = 0.0
    source: str = "vector"
    text: str = ""
    #: True when this passage reached the prompt rather than only the result set.
    in_context: bool = False
    #: True when the answer actually cited it.
    cited: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "level": self.level,
            "ref_id": str(self.ref_id),
            "contract_id": str(self.contract_id),
            "chunk_id": str(self.chunk_id) if self.chunk_id else None,
            "clause_number": self.clause_number,
            "section_title": self.section_title,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "similarity": self.similarity,
            "rerank_score": self.rerank_score,
            "score": round(self.score, 6),
            "source": self.source,
            "in_context": self.in_context,
            "cited": self.cited,
            # The text is truncated: a result file holding full clause text for
            # ten thousand cases is hundreds of megabytes, and every metric that
            # needs the text needs only enough to identify the passage.
            "text": self.text[:400],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RetrievedItem:
        return cls(
            rank=int(payload["rank"]),
            level=str(payload["level"]),
            ref_id=uuid.UUID(payload["ref_id"]),
            contract_id=uuid.UUID(payload["contract_id"]),
            chunk_id=uuid.UUID(payload["chunk_id"]) if payload.get("chunk_id") else None,
            clause_number=payload.get("clause_number"),
            section_title=payload.get("section_title"),
            page_start=payload.get("page_start"),
            page_end=payload.get("page_end"),
            similarity=payload.get("similarity"),
            rerank_score=payload.get("rerank_score"),
            score=float(payload.get("score") or 0.0),
            source=str(payload.get("source") or "vector"),
            text=str(payload.get("text") or ""),
            in_context=bool(payload.get("in_context")),
            cited=bool(payload.get("cited")),
        )


@dataclass(slots=True)
class CitationRecord:
    """One citation the answer emitted, with what it resolved to."""

    label: int
    #: False when the model cited a label it was never offered. The pipeline
    #: strips these from the text; the benchmark still counts them, because the
    #: rate at which a model invents labels is a model-quality signal that
    #: stripping them hides.
    resolved: bool = True
    contract_id: uuid.UUID | None = None
    ref_id: uuid.UUID | None = None
    clause_number: str | None = None
    section_title: str | None = None
    page_start: int | None = None
    similarity: float | None = None
    #: False when the cited passage is not among what retrieval returned. Should
    #: be impossible - the validator allow-lists offered labels - so a non-zero
    #: count here is a defect in the validator, not in the model.
    in_evidence: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "resolved": self.resolved,
            "contract_id": str(self.contract_id) if self.contract_id else None,
            "ref_id": str(self.ref_id) if self.ref_id else None,
            "clause_number": self.clause_number,
            "section_title": self.section_title,
            "page_start": self.page_start,
            "similarity": self.similarity,
            "in_evidence": self.in_evidence,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CitationRecord:
        return cls(
            label=int(payload["label"]),
            resolved=bool(payload.get("resolved", True)),
            contract_id=uuid.UUID(payload["contract_id"]) if payload.get("contract_id") else None,
            ref_id=uuid.UUID(payload["ref_id"]) if payload.get("ref_id") else None,
            clause_number=payload.get("clause_number"),
            section_title=payload.get("section_title"),
            page_start=payload.get("page_start"),
            similarity=payload.get("similarity"),
            in_evidence=bool(payload.get("in_evidence", True)),
        )


@dataclass(slots=True)
class CaseResult:
    """Everything one evaluated question produced."""

    case: GoldenCase
    #: False when the pipeline raised. The case is excluded from quality metrics
    #: and counted separately - averaging a failure in as a zero would let an
    #: outage look like a quality regression.
    ok: bool = True
    error: str | None = None

    answer: str = ""
    answered: bool = False
    insufficient_context: bool = False
    generation_failed: bool = False
    refused: bool = False
    needs_review: bool = False

    retrieved: list[RetrievedItem] = field(default_factory=list)
    citations: list[CitationRecord] = field(default_factory=list)

    # --- planner ------------------------------------------------------------
    intent: str | None = None
    strategy: str | None = None
    retrieval_mode: str | None = None
    document_type: str | None = None
    document_type_detected: bool = False
    document_type_confidence: float = 0.0
    applied_agreement_types: list[str] = field(default_factory=list)
    relaxed_filters: bool = False
    scope_truncated: bool = False
    analysis_method: str = "unavailable"

    # --- scoring ------------------------------------------------------------
    confidence: float = 0.0
    confidence_band: str = "low"
    top_similarity: float = 0.0
    similarity_by_level: dict[str, float] = field(default_factory=dict)

    # --- cost and time ------------------------------------------------------
    timings: dict[str, int] = field(default_factory=dict)
    tokens: int = 0
    cost_usd: float = 0.0
    context_tokens: int = 0
    context_dropped: int = 0

    @property
    def latency_ms(self) -> int:
        return int(self.timings.get("total_ms", 0))

    @property
    def cited_labels(self) -> set[int]:
        return {citation.label for citation in self.citations}

    def as_dict(self) -> dict[str, Any]:
        return {
            "case": self.case.as_dict(),
            "ok": self.ok,
            "error": self.error,
            "answer": self.answer[:2000],
            "answered": self.answered,
            "insufficient_context": self.insufficient_context,
            "generation_failed": self.generation_failed,
            "refused": self.refused,
            "needs_review": self.needs_review,
            "retrieved": [item.as_dict() for item in self.retrieved],
            "citations": [citation.as_dict() for citation in self.citations],
            "intent": self.intent,
            "strategy": self.strategy,
            "retrieval_mode": self.retrieval_mode,
            "document_type": self.document_type,
            "document_type_detected": self.document_type_detected,
            "document_type_confidence": self.document_type_confidence,
            "applied_agreement_types": list(self.applied_agreement_types),
            "relaxed_filters": self.relaxed_filters,
            "scope_truncated": self.scope_truncated,
            "analysis_method": self.analysis_method,
            "confidence": self.confidence,
            "confidence_band": self.confidence_band,
            "top_similarity": self.top_similarity,
            "similarity_by_level": dict(self.similarity_by_level),
            "timings": dict(self.timings),
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "context_tokens": self.context_tokens,
            "context_dropped": self.context_dropped,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CaseResult:
        return cls(
            case=GoldenCase.from_dict(payload["case"]),
            ok=bool(payload.get("ok", True)),
            error=payload.get("error"),
            answer=str(payload.get("answer") or ""),
            answered=bool(payload.get("answered")),
            insufficient_context=bool(payload.get("insufficient_context")),
            generation_failed=bool(payload.get("generation_failed")),
            refused=bool(payload.get("refused")),
            needs_review=bool(payload.get("needs_review")),
            retrieved=[RetrievedItem.from_dict(item) for item in payload.get("retrieved") or []],
            citations=[CitationRecord.from_dict(item) for item in payload.get("citations") or []],
            intent=payload.get("intent"),
            strategy=payload.get("strategy"),
            retrieval_mode=payload.get("retrieval_mode"),
            document_type=payload.get("document_type"),
            document_type_detected=bool(payload.get("document_type_detected")),
            document_type_confidence=float(payload.get("document_type_confidence") or 0.0),
            applied_agreement_types=list(payload.get("applied_agreement_types") or []),
            relaxed_filters=bool(payload.get("relaxed_filters")),
            scope_truncated=bool(payload.get("scope_truncated")),
            analysis_method=str(payload.get("analysis_method") or "unavailable"),
            confidence=float(payload.get("confidence") or 0.0),
            confidence_band=str(payload.get("confidence_band") or "low"),
            top_similarity=float(payload.get("top_similarity") or 0.0),
            similarity_by_level=dict(payload.get("similarity_by_level") or {}),
            timings=dict(payload.get("timings") or {}),
            tokens=int(payload.get("tokens") or 0),
            cost_usd=float(payload.get("cost_usd") or 0.0),
            context_tokens=int(payload.get("context_tokens") or 0),
            context_dropped=int(payload.get("context_dropped") or 0),
        )


@dataclass(slots=True)
class RunResult:
    """A whole benchmark run: every case plus what produced it."""

    dataset: str
    #: The configuration in force. Recorded so a comparison between two runs can
    #: say *which setting* moved a metric, rather than only that it moved.
    configuration: dict[str, Any] = field(default_factory=dict)
    results: list[CaseResult] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    #: Free-form label for this run: a git sha, a sweep point, an ablation arm.
    label: str = ""

    @property
    def ok_results(self) -> list[CaseResult]:
        return [result for result in self.results if result.ok]

    @property
    def failures(self) -> list[CaseResult]:
        return [result for result in self.results if not result.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "label": self.label,
            "configuration": self.configuration,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "results": [result.as_dict() for result in self.results],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RunResult:
        return cls(
            dataset=str(payload.get("dataset") or ""),
            label=str(payload.get("label") or ""),
            configuration=dict(payload.get("configuration") or {}),
            results=[CaseResult.from_dict(item) for item in payload.get("results") or []],
            started_at=str(payload.get("started_at") or ""),
            finished_at=str(payload.get("finished_at") or ""),
            duration_seconds=float(payload.get("duration_seconds") or 0.0),
        )


__all__ = ["CaseResult", "CitationRecord", "RetrievedItem", "RunResult"]

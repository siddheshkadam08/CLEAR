"""Compare two extraction pipelines on the same input.

Built before the pipeline it exists to judge. Every optimisation proposed for
extraction so far - batching, prompt compression, schema trimming - has been an
*argument*, because there was no way to tell an improvement from a regression.
This is the missing instrument.

**It knows nothing about batching.** A pipeline here is any callable that turns
an ``ExtractionRequest`` into an ``ExtractionResult``:

    async def pipeline(request: ExtractionRequest) -> ExtractionResult: ...

    report = await evaluate(baseline, candidate, requests=[...])

That signature is the whole extension point. When batched extraction exists it is
passed as ``candidate`` and nothing in this module changes - which is the
property that makes the comparison trustworthy, because an evaluator that has to
be edited to understand a new pipeline can be edited to flatter it.

Nothing here is imported by production code. It runs from a script or a test.

**On what "accuracy" means.** The baseline is not ground truth - it is the
incumbent. A field where the candidate differs is a *difference*, not an error,
and this module never decides which side is right. That judgement needs a human
or a labelled set, and pretending otherwise would turn "the candidate matches the
thing we already had" into "the candidate is correct", which are very different
claims.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.ai.extraction.engine import ExtractionRequest
from app.ai.extraction.models import CategoryOutcome, ExtractedClause, ExtractionResult
from app.core.logging import get_logger

logger = get_logger(__name__)

#: A pipeline under test. Deliberately the narrowest possible contract.
Pipeline = Callable[[ExtractionRequest], Awaitable[ExtractionResult]]

#: Clause fields compared verbatim. `attributes` is compared key-by-key
#: separately; the bookkeeping fields (`prompt_id`, `model_version`) are excluded
#: because they are *expected* to differ between two pipelines and would drown
#: the signal in noise that means nothing.
_COMPARED_FIELDS = (
    "clause_type",
    "title",
    "text",
    "summary",
    "clause_number",
    "section_id",
    "section_title",
    "confidence",
    "validation_score",
    "review_status",
    "is_mandatory",
    "is_risk_flagged",
    "deviation_score",
)

#: Confidence and scores are floats from a model; identical inputs can differ in
#: the last place without meaning anything. Compared to 3 decimals.
_FLOAT_FIELDS = frozenset({"confidence", "validation_score", "deviation_score"})
_FLOAT_TOLERANCE = 0.001


# =============================================================================
# Findings
# =============================================================================
@dataclass(slots=True)
class FieldDiff:
    """One field that differs. The report's atom."""

    clause_type: str
    field: str
    baseline: Any
    candidate: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "clause_type": self.clause_type,
            "field": self.field,
            "current": _safe(self.baseline),
            "candidate": _safe(self.candidate),
            "match": False,
            "difference": _describe(self.baseline, self.candidate),
        }


@dataclass(slots=True)
class Accuracy:
    """Matched over compared. Kept as a pair so a rate is never reported
    without the denominator that makes it meaningful - "100%" over two fields
    is not the same claim as "100%" over two thousand."""

    matched: int = 0
    total: int = 0

    def record(self, ok: bool) -> None:
        self.total += 1
        self.matched += 1 if ok else 0

    @property
    def rate(self) -> float:
        return self.matched / self.total if self.total else 1.0

    def as_dict(self) -> dict[str, Any]:
        return {"matched": self.matched, "total": self.total, "rate": round(self.rate, 6)}


@dataclass(slots=True)
class Cost:
    """What one pipeline spent. Read from `CategoryOutcome`, which the engine
    already populates - this module measures nothing itself except wall time."""

    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    evidence_tokens: int = 0
    repeated_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    wall_ms: int = 0

    @classmethod
    def from_outcomes(cls, outcomes: list[CategoryOutcome], wall_ms: int) -> Cost:
        return cls(
            llm_calls=sum(o.llm_calls for o in outcomes),
            prompt_tokens=sum(o.input_tokens for o in outcomes),
            completion_tokens=sum(o.output_tokens for o in outcomes),
            cached_tokens=sum(o.cache_read_tokens for o in outcomes),
            evidence_tokens=sum(getattr(o, "evidence_tokens", 0) for o in outcomes),
            repeated_tokens=sum(getattr(o, "repeated_tokens", 0) for o in outcomes),
            cost_usd=round(sum(o.cost_usd for o in outcomes), 6),
            latency_ms=sum(o.latency_ms for o in outcomes),
            wall_ms=wall_ms,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ContractReport:
    """One contract, both pipelines."""

    label: str
    field_accuracy: Accuracy = field(default_factory=Accuracy)
    citation_accuracy: Accuracy = field(default_factory=Accuracy)
    evidence_accuracy: Accuracy = field(default_factory=Accuracy)
    null_accuracy: Accuracy = field(default_factory=Accuracy)
    json_validity: Accuracy = field(default_factory=Accuracy)
    #: Clauses present in one pipeline and absent from the other. Counted apart
    #: from field accuracy: a clause the candidate never produced has no fields
    #: to compare, so folding it in would score a total miss as 100% accurate.
    only_in_baseline: list[str] = field(default_factory=list)
    only_in_candidate: list[str] = field(default_factory=list)
    diffs: list[FieldDiff] = field(default_factory=list)
    baseline_cost: Cost = field(default_factory=Cost)
    candidate_cost: Cost = field(default_factory=Cost)

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract": self.label,
            "field_accuracy": self.field_accuracy.as_dict(),
            "citation_accuracy": self.citation_accuracy.as_dict(),
            "evidence_accuracy": self.evidence_accuracy.as_dict(),
            "null_accuracy": self.null_accuracy.as_dict(),
            "json_validity": self.json_validity.as_dict(),
            "clauses_only_in_current": self.only_in_baseline,
            "clauses_only_in_candidate": self.only_in_candidate,
            "diff_count": len(self.diffs),
            "current": self.baseline_cost.as_dict(),
            "candidate": self.candidate_cost.as_dict(),
        }


@dataclass(slots=True)
class EvaluationReport:
    contracts: list[ContractReport] = field(default_factory=list)

    # ------------------------------------------------------------------ totals
    def _totals(self, attr: str) -> Accuracy:
        total = Accuracy()
        for report in self.contracts:
            acc: Accuracy = getattr(report, attr)
            total.matched += acc.matched
            total.total += acc.total
        return total

    @property
    def regression_pct(self) -> float:
        """How far field accuracy falls short of a perfect match, as a percentage.

        The number the abort rule is written against. 0.0 means the candidate
        reproduced the incumbent exactly.
        """
        return round((1.0 - self._totals("field_accuracy").rate) * 100, 4)

    def summary(self) -> dict[str, Any]:
        n = len(self.contracts) or 1
        base = [c.baseline_cost for c in self.contracts]
        cand = [c.candidate_cost for c in self.contracts]

        def avg(rows: list[Cost], name: str) -> float:
            return round(sum(getattr(r, name) for r in rows) / n, 2)

        def total(rows: list[Cost], name: str) -> float:
            return round(sum(getattr(r, name) for r in rows), 6)

        prompt_base = total(base, "prompt_tokens")
        prompt_cand = total(cand, "prompt_tokens")
        reduction = (
            round((prompt_base - prompt_cand) / prompt_base * 100, 2) if prompt_base else 0.0
        )

        return {
            "contracts": len(self.contracts),
            "overall_accuracy": {
                "field": self._totals("field_accuracy").as_dict(),
                "citation": self._totals("citation_accuracy").as_dict(),
                "evidence": self._totals("evidence_accuracy").as_dict(),
                "null": self._totals("null_accuracy").as_dict(),
                "json_validity": self._totals("json_validity").as_dict(),
            },
            "regression_pct": self.regression_pct,
            "per_clause_accuracy": self._per_clause(),
            "per_field_accuracy": self._per_field(),
            "averages": {
                "current": {
                    "latency_ms": avg(base, "wall_ms"),
                    "prompt_tokens": avg(base, "prompt_tokens"),
                    "completion_tokens": avg(base, "completion_tokens"),
                    "cost_usd": avg(base, "cost_usd"),
                    "llm_calls": avg(base, "llm_calls"),
                },
                "candidate": {
                    "latency_ms": avg(cand, "wall_ms"),
                    "prompt_tokens": avg(cand, "prompt_tokens"),
                    "completion_tokens": avg(cand, "completion_tokens"),
                    "cost_usd": avg(cand, "cost_usd"),
                    "llm_calls": avg(cand, "llm_calls"),
                },
            },
            "prompt_token_reduction_pct": reduction,
        }

    def _per_clause(self) -> dict[str, Any]:
        """Which clause categories the differences land in.

        A candidate that is 99% accurate overall but wrong on every liability cap
        is not 99% good, and only this breakdown shows that.
        """
        counts: dict[str, int] = {}
        for report in self.contracts:
            for diff in report.diffs:
                counts[diff.clause_type] = counts.get(diff.clause_type, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def _per_field(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for report in self.contracts:
            for diff in report.diffs:
                counts[diff.field] = counts.get(diff.field, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def diff_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for report in self.contracts:
            for diff in report.diffs:
                row = diff.as_dict()
                row["contract"] = report.label
                rows.append(row)
        return rows

    def write(self, directory: Path) -> dict[str, Path]:
        """Emit the three machine-readable reports."""
        directory.mkdir(parents=True, exist_ok=True)
        paths = {
            "evaluation": directory / "evaluation.json",
            "summary": directory / "evaluation_summary.json",
            "diff": directory / "evaluation_diff.json",
        }
        _dump(paths["evaluation"], {"contracts": [c.as_dict() for c in self.contracts]})
        _dump(paths["summary"], self.summary())
        _dump(paths["diff"], {"differences": self.diff_rows()})
        return paths


# =============================================================================
# The evaluator
# =============================================================================
async def evaluate(
    baseline: Pipeline,
    candidate: Pipeline,
    *,
    requests: list[tuple[str, ExtractionRequest]],
) -> EvaluationReport:
    """Run both pipelines over the same requests and compare.

    Sequential, not concurrent. Running them together would have them contend for
    the same provider rate limit and make the latency figures meaningless - and
    latency is one of the things being measured.
    """
    report = EvaluationReport()

    for label, request in requests:
        logger.info("extraction_eval_started", contract=label)
        base_result, base_wall = await _timed(baseline, request)
        cand_result, cand_wall = await _timed(candidate, request)

        contract = ContractReport(label=label)
        contract.baseline_cost = Cost.from_outcomes(base_result.outcomes, base_wall)
        contract.candidate_cost = Cost.from_outcomes(cand_result.outcomes, cand_wall)
        _compare(contract, base_result, cand_result)
        report.contracts.append(contract)

        logger.info(
            "extraction_eval_contract_done",
            contract=label,
            field_accuracy=round(contract.field_accuracy.rate, 4),
            diffs=len(contract.diffs),
        )

    return report


async def _timed(pipeline: Pipeline, request: ExtractionRequest) -> tuple[ExtractionResult, int]:
    started = time.perf_counter()
    result = await pipeline(request)
    return result, int((time.perf_counter() - started) * 1000)


def _compare(
    contract: ContractReport, baseline: ExtractionResult, candidate: ExtractionResult
) -> None:
    base_by_type = _index(baseline.clauses)
    cand_by_type = _index(candidate.clauses)

    contract.only_in_baseline = sorted(set(base_by_type) - set(cand_by_type))
    contract.only_in_candidate = sorted(set(cand_by_type) - set(base_by_type))

    for clause_type in sorted(set(base_by_type) & set(cand_by_type)):
        for base, cand in zip(base_by_type[clause_type], cand_by_type[clause_type], strict=False):
            _compare_clause(contract, clause_type, base, cand)

    # JSON validity is judged from the outcomes, where a schema failure is
    # already recorded - re-parsing the payload here would test this module's
    # copy of the rules rather than the pipeline's.
    for outcome in candidate.outcomes:
        contract.json_validity.record(outcome.status not in {"invalid", "error"})


def _compare_clause(
    contract: ContractReport, clause_type: str, base: ExtractedClause, cand: ExtractedClause
) -> None:
    for name in _COMPARED_FIELDS:
        left, right = getattr(base, name, None), getattr(cand, name, None)
        ok = _equal(name, left, right)
        contract.field_accuracy.record(ok)
        # Null handling tracked separately: "both said nothing" and "both said
        # the same thing" are different successes, and conflating them hides a
        # candidate that has quietly stopped extracting anything.
        if left is None or right is None:
            contract.null_accuracy.record(left is None and right is None)
        if not ok:
            contract.diffs.append(FieldDiff(clause_type, name, left, right))

    for key in sorted(set(base.attributes or {}) | set(cand.attributes or {})):
        left = (base.attributes or {}).get(key)
        right = (cand.attributes or {}).get(key)
        ok = _equal(key, left, right)
        contract.field_accuracy.record(ok)
        if left is None or right is None:
            contract.null_accuracy.record(left is None and right is None)
        if not ok:
            contract.diffs.append(FieldDiff(clause_type, f"attributes.{key}", left, right))

    # Citations and evidence ids compared as sets: order carries no meaning and
    # would otherwise report a reordering as a difference.
    base_chunks = {str(ref.chunk_id) for ref in base.evidence}
    cand_chunks = {str(ref.chunk_id) for ref in cand.evidence}
    contract.citation_accuracy.record(base_chunks == cand_chunks)
    contract.evidence_accuracy.record(len(base.evidence) == len(cand.evidence))
    if base_chunks != cand_chunks:
        contract.diffs.append(
            FieldDiff(clause_type, "evidence.chunk_ids", sorted(base_chunks), sorted(cand_chunks))
        )


def _index(clauses: list[ExtractedClause]) -> dict[str, list[ExtractedClause]]:
    grouped: dict[str, list[ExtractedClause]] = {}
    for clause in clauses:
        grouped.setdefault(str(clause.clause_type), []).append(clause)
    return grouped


def _equal(name: str, left: Any, right: Any) -> bool:
    if name in _FLOAT_FIELDS and isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= _FLOAT_TOLERANCE
    return bool(left == right)


def _describe(left: Any, right: Any) -> str:
    if left is None:
        return "candidate produced a value where the current pipeline produced none"
    if right is None:
        return "candidate produced no value where the current pipeline produced one"
    return "values differ"


def _safe(value: Any) -> Any:
    """JSON-safe rendering. Long text is truncated - a diff report is for
    spotting *that* something changed, and a full clause body per row makes it
    unreadable."""
    if isinstance(value, (str,)) and len(value) > 300:
        return value[:300] + f"... [{len(value)} chars]"
    if isinstance(value, (int, float, bool, type(None), list, dict)):
        return value
    return str(value)


def _dump(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


__all__ = [
    "Accuracy",
    "ContractReport",
    "Cost",
    "EvaluationReport",
    "FieldDiff",
    "Pipeline",
    "evaluate",
]

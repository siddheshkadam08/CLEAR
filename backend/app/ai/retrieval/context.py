"""Context Assembly - packs retrieved evidence into a Context Package (§16).

Between retrieval and generation. Retrieval returns everything that scored well;
this decides what actually fits, in what order, and with what citation label.

Three decisions live here:

* **Budget.** A context window is finite and every token spent on a marginal passage
  is one not spent on a relevant one. Evidence is admitted in score order until the
  budget is gone, and what was dropped is *recorded* - a silently truncated context
  produces an answer that looks complete and is not.
* **Citation identity.** Each admitted item gets a stable label (``[1]``, ``[2]``)
  that the prompt instructs the model to cite and the answer parser maps back to a
  page and a bounding box. Without a stable label there is no verifiable citation.
* **Grouping.** Evidence is grouped by contract, because an answer that interleaves
  three agreements sentence by sentence is unreadable, and because a
  cross-contract question needs the model to see which term came from which
  document.

The package is the *only* thing the RAG engine sees. If a fact is not in here, the
engine cannot ground an answer on it - which is exactly the property that makes
"answer only from supplied evidence" enforceable rather than aspirational.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.ai.rag.providers import estimate_tokens
from app.ai.rag.sanitise import sanitise_evidence
from app.ai.retrieval.engine import Evidence, RetrievalResult
from app.core.config import get_settings
from app.core.enums import EmbeddingLevel, QueryIntent
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Fraction of the model's input budget the evidence may occupy. The remainder is
#: the system prompt, the question, the conversation history and the answer itself.
_EVIDENCE_BUDGET_RATIO = 0.55

#: Never admit a single item larger than this share of the budget. One enormous
#: chunk would otherwise crowd out every other citation and produce a
#: single-source answer to a multi-source question.
_MAX_SINGLE_ITEM_RATIO = 0.25

#: Minimum items to admit regardless of budget, so a tight budget still produces a
#: citable answer rather than an empty context.
_MIN_ITEMS = 3

#: Share of the budget conversation history may occupy. History is context for the
#: question; the evidence is what the answer must be grounded in, so history is
#: never allowed to crowd it out.
_HISTORY_BUDGET_RATIO = 0.15


@dataclass(slots=True)
class Citation:
    """One numbered citation the model may reference and the UI can resolve."""

    label: int
    contract_id: uuid.UUID
    contract_title: str | None
    level: str
    ref_id: uuid.UUID
    text: str
    clause_type: str | None = None
    clause_number: str | None = None
    section_title: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    bounding_boxes: list[dict[str, Any]] = field(default_factory=list)
    chunk_id: uuid.UUID | None = None
    score: float = 0.0
    source: str = "vector"
    #: Cosine similarity from the vector search. Distinct from ``score``, which
    #: after hybrid fusion is a reciprocal-rank value on an unrelated scale - this
    #: is the figure that means something to a reader.
    similarity: float | None = None
    #: Relevance as the re-ranker judged it, when one ran. A stronger signal than
    #: ``similarity``: it answers "does this passage answer the question" rather
    #: than "is it about the same subject".
    rerank_score: float | None = None

    @property
    def marker(self) -> str:
        return f"[{self.label}]"

    @property
    def page_range(self) -> str:
        if self.page_start is None:
            return ""
        if self.page_end is None or self.page_end == self.page_start:
            return f"p.{self.page_start}"
        return f"pp.{self.page_start}-{self.page_end}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "contract_id": str(self.contract_id),
            "contract_title": self.contract_title,
            "level": self.level,
            "ref_id": str(self.ref_id),
            "clause_type": self.clause_type,
            "clause_number": self.clause_number,
            "section_title": self.section_title,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "page_range": self.page_range,
            "bounding_boxes": self.bounding_boxes,
            "chunk_id": str(self.chunk_id) if self.chunk_id else None,
            "score": round(self.score, 6),
            "similarity": round(self.similarity, 6) if self.similarity is not None else None,
            "rerank_score": (
                round(self.rerank_score, 6) if self.rerank_score is not None else None
            ),
            "source": self.source,
            "text": self.text,
        }


@dataclass(slots=True)
class ContextPackage:
    """Validated context, ready for generation.

    The RAG engine sees this and nothing else.
    """

    query: str
    intent: QueryIntent
    citations: list[Citation] = field(default_factory=list)
    #: Document-level facts, for questions the projection answers directly.
    metadata_rows: list[dict[str, Any]] = field(default_factory=list)
    #: Prior turns, already trimmed to fit.
    history: list[dict[str, str]] = field(default_factory=list)
    token_estimate: int = 0
    budget: int = 0
    #: Evidence retrieval found but the budget could not fit. Surfaced so the answer
    #: can say it was working from a subset.
    dropped: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.citations and not self.metadata_rows

    @property
    def contract_ids(self) -> list[uuid.UUID]:
        seen: list[uuid.UUID] = []
        for citation in self.citations:
            if citation.contract_id not in seen:
                seen.append(citation.contract_id)
        return seen

    def citation(self, label: int) -> Citation | None:
        return next((c for c in self.citations if c.label == label), None)

    @property
    def valid_labels(self) -> set[int]:
        """Labels the model is permitted to cite. The answer validator's allow-list."""
        return {citation.label for citation in self.citations}

    def render_evidence(self) -> str:
        """Format the evidence for the prompt, grouped by contract.

        Grouped because a cross-contract answer has to attribute each term to its
        document, and a flat list makes the model's job of keeping them straight
        harder than it needs to be.
        """
        if not self.citations:
            return "(no supporting evidence was retrieved)"

        by_contract: dict[uuid.UUID, list[Citation]] = {}
        for citation in self.citations:
            by_contract.setdefault(citation.contract_id, []).append(citation)

        blocks: list[str] = []
        for contract_id, group in by_contract.items():
            title = group[0].contract_title or f"Contract {contract_id}"
            lines = [f"=== {title} ==="]
            # Document order within a contract, not relevance order. Contract
            # language is heavily order-dependent - a clause reading "subject to the
            # foregoing" is misleading when the foregoing appears after it - and the
            # labels stay in relevance order regardless, so nothing about the
            # citation numbering changes.
            for citation in sorted(group, key=_document_position):
                header = citation.marker
                if citation.clause_number:
                    header += f" Clause {citation.clause_number}"
                if citation.clause_type:
                    header += f" ({citation.clause_type.replace('_', ' ')})"
                elif citation.section_title:
                    header += f" - {citation.section_title}"
                if citation.page_range:
                    header += f" [{citation.page_range}]"
                lines.append(f"{header}\n{sanitise_evidence(citation.text)}")
            blocks.append("\n\n".join(lines))
        return "\n\n".join(blocks)

    def render_metadata(self) -> str:
        """Format the document-level facts as a table the model can read."""
        if not self.metadata_rows:
            return ""
        lines = ["=== CONTRACT FACTS ==="]
        for row in self.metadata_rows:
            parts = [f"- {row.get('title') or row.get('contract_id')}"]
            for key in (
                "agreement_type",
                "risk_band",
                "effective_date",
                "expiration_date",
                "contract_value",
                "currency",
                "party_a",
                "party_b",
            ):
                value = row.get(key)
                if value not in (None, "", []):
                    parts.append(f"{key.replace('_', ' ')}: {value}")
            missing = row.get("missing_mandatory_clauses") or []
            if missing:
                parts.append(f"missing clauses: {', '.join(missing)}")
            lines.append("; ".join(parts))
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "intent": self.intent.value,
            "citations": [c.as_dict() for c in self.citations],
            "metadata_rows": self.metadata_rows,
            "token_estimate": self.token_estimate,
            "budget": self.budget,
            "dropped": self.dropped,
            "contracts": [str(cid) for cid in self.contract_ids],
            "warnings": self.warnings,
        }


class ContextAssembler:
    """Builds a Context Package from retrieval output."""

    def __init__(self, *, budget_tokens: int | None = None) -> None:
        settings = get_settings()
        if budget_tokens is not None:
            self._budget = budget_tokens
        else:
            # Derived from the output ceiling rather than hardcoded, so raising
            # LLM_MAX_OUTPUT_TOKENS does not silently starve the evidence.
            self._budget = int(settings.llm.max_output_tokens / _EVIDENCE_BUDGET_RATIO)

    def assemble(
        self,
        *,
        query: str,
        intent: QueryIntent,
        retrieval: RetrievalResult,
        history: list[dict[str, str]] | None = None,
        max_citations: int = 20,
    ) -> ContextPackage:
        package = ContextPackage(
            query=query,
            intent=intent,
            budget=self._budget,
            warnings=list(retrieval.warnings),
        )

        package.metadata_rows = retrieval.metadata_rows[:25]
        package.history = self._trim_history(history or [], budget=self._budget)

        ordered = self._prioritise(retrieval.evidence, intent)
        # History counts against the budget like everything else. It used not to,
        # and six long prior turns could push the real prompt well past the ceiling
        # the budget exists to enforce - silently, because the accounting said the
        # context fitted.
        spent = estimate_tokens(package.render_metadata()) if package.metadata_rows else 0
        spent += sum(estimate_tokens(turn.get("content", "")) + 8 for turn in package.history)
        max_single = int(self._budget * _MAX_SINGLE_ITEM_RATIO)
        label = 1

        for item in ordered:
            if label > max_citations:
                package.dropped += 1
                continue

            text = item.text.strip()
            if not text:
                continue

            cost = estimate_tokens(text) + 30  # header and separator overhead
            if cost > max_single and label > _MIN_ITEMS:
                # One passage must not consume the room every other citation needs.
                package.dropped += 1
                continue
            if spent + cost > self._budget and label > _MIN_ITEMS:
                package.dropped += 1
                continue

            package.citations.append(
                Citation(
                    label=label,
                    contract_id=item.contract_id,
                    contract_title=item.contract_title,
                    level=item.level.value,
                    ref_id=item.ref_id,
                    text=text,
                    clause_type=item.clause_type,
                    clause_number=item.clause_number,
                    section_title=item.section_title,
                    page_start=item.page_start,
                    page_end=item.page_end,
                    bounding_boxes=item.bounding_boxes,
                    chunk_id=item.chunk_id,
                    score=item.score,
                    similarity=item.similarity,
                    rerank_score=item.rerank_score,
                    source=item.source,
                )
            )
            spent += cost
            label += 1

        package.token_estimate = spent

        if package.dropped:
            # Stated rather than hidden: an answer built from a subset should be able
            # to say so, and a persistently truncated context is a tuning signal.
            package.warnings.append(
                f"{package.dropped} retrieved passage(s) did not fit the context budget "
                "and were not used."
            )

        if package.is_empty:
            package.warnings.append("No evidence was available for this question.")

        logger.info(
            "context_assembled",
            intent=intent.value,
            citations=len(package.citations),
            metadata_rows=len(package.metadata_rows),
            tokens=package.token_estimate,
            budget=package.budget,
            dropped=package.dropped,
            contracts=len(package.contract_ids),
        )
        return package

    # =========================================================================
    # Ordering
    # =========================================================================
    @staticmethod
    def _prioritise(evidence: list[Evidence], intent: QueryIntent) -> list[Evidence]:
        """Order evidence for admission.

        Retrieval order decides most of it, but the level matters for some intents:
        a clause lookup wants whole clauses at the top even when a fragment ranked
        marginally higher, because a fragment cannot answer "what does the
        indemnity say".

        The tie-break is the item's **position** in the retrieved list, not its
        ``score``. That distinction is load-bearing once re-ranking is enabled: the
        re-ranker expresses its judgement by reordering, and leaves ``score`` as the
        similarity or fusion value that produced the candidate. Sorting on ``score``
        here would put the list straight back into the order the re-ranker was run
        to change.
        """
        prefers_clauses = intent in {
            QueryIntent.CLAUSE_LOOKUP,
            QueryIntent.COMPARISON,
            QueryIntent.RISK_ASSESSMENT,
            QueryIntent.COMPLIANCE,
        }

        def sort_key(entry: tuple[int, Evidence]) -> tuple[int, int]:
            position, item = entry
            level_rank = 0
            if prefers_clauses and item.level is EmbeddingLevel.CLAUSE:
                level_rank = -1
            # Neighbours are context, not answers: they go last regardless of rank.
            if item.source in {"neighbour", "graph"}:
                level_rank = 1
            return (level_rank, position)

        return [item for _, item in sorted(enumerate(evidence), key=sort_key)]

    @staticmethod
    def _trim_history(
        history: list[dict[str, str]], *, keep: int = 6, budget: int = 0
    ) -> list[dict[str, str]]:
        """Keep the most recent turns, bounded by tokens as well as by count.

        Recent turns carry the referents ("it", "that clause") the current question
        depends on; older ones mostly cost tokens. Trimmed here rather than in the
        prompt builder so the budget accounting sees the real size.

        The token bound matters more than the count: six turns is small if they are
        questions and enormous if one of them is a summary of a fifty-page
        agreement. Capped at a fraction of the budget so history can never crowd out
        the evidence - the conversation is context for the question, but the
        evidence is what the answer has to be grounded in.
        """
        recent = history[-keep:]
        if budget <= 0:
            return recent

        ceiling = int(budget * _HISTORY_BUDGET_RATIO)
        kept: list[dict[str, str]] = []
        spent = 0
        # Newest first, so the turns that resolve the current question's pronouns
        # are the ones that survive a tight budget.
        for turn in reversed(recent):
            cost = estimate_tokens(turn.get("content", "")) + 8
            if spent + cost > ceiling and kept:
                break
            kept.append(turn)
            spent += cost
        return list(reversed(kept))


def _document_position(citation: Citation) -> tuple[int, tuple[int, ...], int]:
    """Sort key placing a citation where it sits in the document.

    Page first, then the clause number read as a tuple of integers so 12.3 follows
    2.1 rather than preceding it - the string comparison that would otherwise apply
    puts "12" before "2". Citations with neither fall to the end in label order,
    which is the retrieval order they arrived in.
    """
    page = citation.page_start if citation.page_start is not None else 10**6
    parts: tuple[int, ...] = ()
    if citation.clause_number:
        try:
            parts = tuple(int(piece) for piece in citation.clause_number.split(".") if piece)
        except ValueError:
            # A clause numbered "12(a)" or "Schedule 2" is not comparable as
            # integers; page order and the label decide it instead.
            parts = ()
    return (page, parts, citation.label)


__all__ = ["Citation", "ContextAssembler", "ContextPackage"]

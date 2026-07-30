"""Evidence selection - the deterministic pre-filter before any LLM call (§13).

Sending a whole contract to the model for every clause category would be both
ruinously expensive and *less* accurate: a model asked to find a liability cap in
150 pages has 150 pages of opportunity to find something that looks like one. So
each category's ``extraction_rule`` (headings, keywords, exclusions, minimum size)
selects a small, ranked evidence set, and the model is asked to read only that.

Three consequences worth stating plainly:

* **Cost.** On a long agreement this is where most of the saving comes from - a
  handful of chunks per category instead of the full document.
* **Grounding.** The model can only cite what it was given, and the validator
  checks quotes against exactly this bundle. A narrow bundle is a narrow surface
  for invention.
* **Honest absence.** When no chunk matches, the category is reported as *not
  found* without an LLM call at all. That is a real answer - a contract with no
  liability cap must produce "missing", not a guess assembled from adjacent text.

Selection is deterministic: same chunks, same rule, same bundle. Re-running
extraction must not change which evidence was considered.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.ai.rag.providers import estimate_tokens
from app.core.enums import ChunkType

#: Weight per signal. Headings dominate because a contract that titles a section
#: "Limitation of Liability" is stating the category directly, whereas keywords
#: recur in cross-references ("subject to Section 11 (Limitation of Liability)").
_HEADING_WEIGHT = 10.0
_KEYWORD_WEIGHT = 2.0
_TITLE_KEYWORD_WEIGHT = 3.0
_SYNONYM_WEIGHT = 4.0

#: Preference between chunk types when scores are otherwise equal. A clause chunk
#: is the tightest unit that still contains a whole obligation.
_TYPE_BONUS: dict[ChunkType, float] = {
    ChunkType.CLAUSE: 2.0,
    ChunkType.SECTION: 1.0,
    ChunkType.LIST: 0.75,
    ChunkType.TABLE: 0.5,
    ChunkType.PARAGRAPH: 0.25,
}

#: Default evidence budget for one category, in tokens. Roughly 10-15 clauses -
#: enough for a cap plus its carve-outs and any cross-referenced sub-clause.
DEFAULT_EVIDENCE_BUDGET = 6_000
DEFAULT_EVIDENCE_LIMIT = 12


@dataclass(slots=True)
class CandidateChunk:
    """A chunk as the selector sees it.

    A plain dataclass rather than the ORM row so the selector can be exercised
    against fixture documents with no database, and so the engine never holds a
    live ORM object across an ``await`` on a provider call.
    """

    chunk_id: str
    text: str
    chunk_type: ChunkType
    reading_order: int
    token_count: int = 0
    section_id: str | None = None
    section_title: str | None = None
    clause_number: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    bounding_boxes: list[dict[str, Any]] = field(default_factory=list)
    parent_id: str | None = None
    level: int = 0

    @classmethod
    def from_row(cls, row: Any) -> CandidateChunk:
        """Adapt a ``chunks`` row."""
        return cls(
            chunk_id=str(row.id),
            text=row.text_content,
            chunk_type=row.chunk_type,
            reading_order=row.reading_order,
            token_count=row.token_count or estimate_tokens(row.text_content),
            section_id=row.section_id,
            section_title=row.section_title,
            clause_number=row.clause_number,
            page_start=row.page_start,
            page_end=row.page_end,
            bounding_boxes=list(row.bounding_boxes or []),
            parent_id=str(row.parent_chunk_id) if row.parent_chunk_id else None,
            level=row.level,
        )

    @property
    def heading(self) -> str:
        """Heading text for matching: the clause number and section title."""
        parts = [self.clause_number or "", self.section_title or ""]
        return " ".join(part for part in parts if part).strip()


@dataclass(slots=True)
class ScoredChunk:
    """A candidate with its score and the signals that produced it."""

    chunk: CandidateChunk
    score: float
    matched_headings: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        if self.matched_headings:
            return f"heading:{self.matched_headings[0]}"
        if self.matched_keywords:
            return f"keyword:{self.matched_keywords[0]}"
        return "context"


@dataclass(slots=True)
class EvidenceBundle:
    """The evidence one category's extraction call will see."""

    category: str
    scored: list[ScoredChunk] = field(default_factory=list)
    #: Chunks pulled in for context rather than because they matched - a matched
    #: clause's parent section, so a cap that says "subject to Section 9" is
    #: readable.
    context: list[CandidateChunk] = field(default_factory=list)
    considered: int = 0
    excluded: int = 0
    truncated: bool = False

    @property
    def chunks(self) -> list[CandidateChunk]:
        """Every chunk in the bundle, in document order."""
        seen: set[str] = set()
        ordered: list[CandidateChunk] = []
        for chunk in [s.chunk for s in self.scored] + self.context:
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            ordered.append(chunk)
        ordered.sort(key=lambda c: c.reading_order)
        return ordered

    @property
    def is_empty(self) -> bool:
        return not self.scored

    @property
    def chunk_ids(self) -> set[str]:
        return {chunk.chunk_id for chunk in self.chunks}

    @property
    def total_tokens(self) -> int:
        return sum(chunk.token_count for chunk in self.chunks)

    def by_id(self, chunk_id: str) -> CandidateChunk | None:
        return next((c for c in self.chunks if c.chunk_id == chunk_id), None)

    def render(self) -> str:
        """Format the bundle for the prompt.

        Each chunk is labelled with the id the model must cite and the page it sits
        on. The label is what makes a citation checkable: the validator matches the
        returned ``evidence_chunk_ids`` against these, and any id the model did not
        receive is a fabrication.
        """
        blocks: list[str] = []
        for chunk in self.chunks:
            header = f"[{chunk.chunk_id}]"
            if chunk.clause_number:
                header += f" clause {chunk.clause_number}"
            if chunk.section_title:
                header += f" - {chunk.section_title}"
            pages = (
                f"p.{chunk.page_start}"
                if chunk.page_start == chunk.page_end or chunk.page_end is None
                else f"pp.{chunk.page_start}-{chunk.page_end}"
            )
            blocks.append(f"{header} ({pages})\n{chunk.text}")
        return "\n\n---\n\n".join(blocks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "selected": len(self.scored),
            "context": len(self.context),
            "considered": self.considered,
            "excluded": self.excluded,
            "truncated": self.truncated,
            "total_tokens": self.total_tokens,
            "chunks": [
                {
                    "chunk_id": s.chunk.chunk_id,
                    "score": round(s.score, 3),
                    "reason": s.reason,
                    "clause_number": s.chunk.clause_number,
                    "page_start": s.chunk.page_start,
                }
                for s in self.scored
            ],
        }


class EvidenceSelector:
    """Applies a category's extraction rule to a contract's chunks."""

    def __init__(self, chunks: Sequence[CandidateChunk]) -> None:
        self._chunks = sorted(chunks, key=lambda c: c.reading_order)
        self._by_id = {chunk.chunk_id: chunk for chunk in self._chunks}

    @property
    def chunks(self) -> list[CandidateChunk]:
        return list(self._chunks)

    def select(
        self,
        *,
        category: str,
        rule: dict[str, Any],
        synonyms: Sequence[str] = (),
        budget_tokens: int = DEFAULT_EVIDENCE_BUDGET,
        limit: int = DEFAULT_EVIDENCE_LIMIT,
        include_parents: bool = True,
    ) -> EvidenceBundle:
        headings = [h.lower() for h in rule.get("heading_patterns", []) if h]
        keywords = [k.lower() for k in rule.get("keywords", []) if k]
        forbidden = [f.lower() for f in rule.get("must_not_contain", []) if f]
        min_tokens = int(rule.get("min_tokens", 0) or 0)
        synonym_terms = [s.lower() for s in synonyms if s]

        bundle = EvidenceBundle(category=category)
        scored: list[ScoredChunk] = []

        for chunk in self._chunks:
            bundle.considered += 1
            lowered = chunk.text.lower()

            if forbidden and any(term in lowered for term in forbidden):
                # An explicit exclusion, e.g. a definitions section that merely
                # names the concept. Counted so the bundle can explain itself.
                bundle.excluded += 1
                continue

            # Tables and lists are exempt from the size floor: a three-row fee
            # schedule is short and is exactly the evidence a payment-terms
            # extraction needs.
            structural = chunk.chunk_type in {ChunkType.TABLE, ChunkType.LIST}
            if min_tokens and chunk.token_count < min_tokens and not structural:
                bundle.excluded += 1
                continue

            heading_text = chunk.heading.lower()
            matched_headings = [h for h in headings if h and h in heading_text]
            # A heading pattern appearing in the first line of the body counts too:
            # not every parser promotes a bold line to a section title.
            first_line = lowered.split("\n", 1)[0]
            matched_headings += [
                h for h in headings if h not in matched_headings and h in first_line
            ]
            matched_keywords = [k for k in keywords if k in lowered]
            matched_synonyms = [s for s in synonym_terms if s in heading_text or s in first_line]

            if not (matched_headings or matched_keywords or matched_synonyms):
                continue

            score = (
                _HEADING_WEIGHT * len(matched_headings)
                + _SYNONYM_WEIGHT * len(matched_synonyms)
                + _KEYWORD_WEIGHT * len(matched_keywords)
                + _TYPE_BONUS.get(chunk.chunk_type, 0.0)
            )
            # A keyword in the heading is a stronger signal than the same keyword
            # buried in prose.
            score += _TITLE_KEYWORD_WEIGHT * sum(1 for k in keywords if k in heading_text)

            scored.append(
                ScoredChunk(
                    chunk=chunk,
                    score=score,
                    matched_headings=matched_headings + matched_synonyms,
                    matched_keywords=matched_keywords,
                )
            )

        # Descending score, then document order - deterministic on ties.
        scored.sort(key=lambda s: (-s.score, s.chunk.reading_order))

        spent = 0
        for candidate in scored:
            if len(bundle.scored) >= limit or spent + candidate.chunk.token_count > budget_tokens:
                bundle.truncated = True
                break
            bundle.scored.append(candidate)
            spent += candidate.chunk.token_count

        if include_parents:
            bundle.context = self._parents_for(bundle, budget_tokens - spent)

        return bundle

    def _parents_for(self, bundle: EvidenceBundle, remaining: int) -> list[CandidateChunk]:
        """Add each matched chunk's parent, budget permitting.

        A clause chunk read in isolation can be materially misleading - "the
        foregoing limitation shall not apply" means nothing without the limitation
        it refers to.
        """
        selected = bundle.chunk_ids
        parents: list[CandidateChunk] = []
        spent = 0
        for scored in bundle.scored:
            parent_id = scored.chunk.parent_id
            if not parent_id or parent_id in selected:
                continue
            parent = self._by_id.get(parent_id)
            if parent is None or spent + parent.token_count > remaining:
                continue
            parents.append(parent)
            selected.add(parent_id)
            spent += parent.token_count
        return parents

    # ------------------------------------------------------------------ helpers
    def head(self, budget_tokens: int) -> EvidenceBundle:
        """The opening of the document, for document-level facts.

        Parties, title, effective date and governing law are stated at the front and
        in the signature block; scanning the whole contract for them adds cost
        without adding accuracy.
        """
        bundle = EvidenceBundle(category="document_head")
        spent = 0
        for chunk in self._chunks:
            bundle.considered += 1
            if spent + chunk.token_count > budget_tokens:
                bundle.truncated = True
                break
            bundle.scored.append(ScoredChunk(chunk=chunk, score=1.0))
            spent += chunk.token_count
        return bundle

    def of_type(self, *types: ChunkType) -> list[CandidateChunk]:
        wanted = set(types)
        return [chunk for chunk in self._chunks if chunk.chunk_type in wanted]

    def bundle_from(self, category: str, chunks: Iterable[CandidateChunk]) -> EvidenceBundle:
        """Wrap an explicit chunk list as a bundle (used for signature blocks)."""
        bundle = EvidenceBundle(category=category)
        for chunk in chunks:
            bundle.scored.append(ScoredChunk(chunk=chunk, score=1.0))
            bundle.considered += 1
        return bundle


def normalise_quote(text: str) -> str:
    """Normalise text for quote verification.

    Collapses whitespace and unifies the punctuation that PDF extraction mangles -
    curly quotes, en/em dashes, non-breaking spaces, ligatures. Without this, a
    correctly quoted clause fails verification because the model returned a
    straight apostrophe where the PDF had a typographic one, and a true extraction
    would be rejected as a fabrication.
    """
    lowered = text.lower()
    for source, target in (
        ("‘", "'"),
        ("’", "'"),
        ("“", '"'),
        ("”", '"'),
        ("–", "-"),
        ("—", "-"),
        ("−", "-"),
        (" ", " "),
        ("ﬁ", "fi"),
        ("ﬂ", "fl"),
    ):
        lowered = lowered.replace(source, target)
    return re.sub(r"\s+", " ", lowered).strip()


__all__ = [
    "DEFAULT_EVIDENCE_BUDGET",
    "DEFAULT_EVIDENCE_LIMIT",
    "CandidateChunk",
    "EvidenceBundle",
    "EvidenceSelector",
    "ScoredChunk",
    "normalise_quote",
]

"""Semantic chunk model and validation report (§12).

A chunk is a *meaning-preserving* unit. Fixed-size chunking is prohibited: a
window that cuts a termination clause in half produces two fragments that each
read as a different obligation than the contract states, and retrieval then
returns confidently wrong evidence.

Every chunk carries the three things the rest of the platform depends on:

* **hierarchy** (``parent_id``) - so retrieval can walk document → section →
  clause → paragraph,
* **coordinates** - so any retrieved evidence can be highlighted on the page,
* **reading order** - so chunking is deterministic and re-chunking the same
  document reproduces the same boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.ai.cdm.models import Coordinates
from app.core.enums import ChunkType


@dataclass(slots=True)
class SemanticChunk:
    """One semantic chunk, before persistence."""

    chunk_id: str
    chunk_type: ChunkType
    text: str
    reading_order: int
    #: Parent in the chunk hierarchy - the section a clause belongs to, etc.
    parent_id: str | None = None
    section_id: str | None = None
    section_title: str | None = None
    level: int = 0
    page_start: int | None = None
    page_end: int | None = None
    #: One box per page. A clause spanning a page break gets two, so the viewer can
    #: highlight it correctly on both rather than drawing one impossible rectangle.
    bounding_boxes: list[Coordinates] = field(default_factory=list)
    token_count: int = 0
    char_count: int = 0
    language: str | None = None
    #: True when this chunk was reassembled across a page break.
    is_cross_page: bool = False
    #: Structure retained for table chunks so rows are never split mid-record.
    table_data: dict[str, Any] | None = None
    #: Ids of the CDM elements this chunk came from - the audit link back to source.
    source_element_ids: list[str] = field(default_factory=list)
    #: Clause number as printed ("11.2"), preserved for citation.
    clause_number: str | None = None

    def __post_init__(self) -> None:
        self.char_count = len(self.text)

    @property
    def page_range(self) -> str:
        if self.page_start is None:
            return ""
        if self.page_end is None or self.page_end == self.page_start:
            return str(self.page_start)
        return f"{self.page_start}-{self.page_end}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "parent_id": self.parent_id,
            "chunk_type": self.chunk_type.value,
            "text": self.text,
            "reading_order": self.reading_order,
            "section_id": self.section_id,
            "section_title": self.section_title,
            "level": self.level,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "bounding_boxes": [box.to_dict() for box in self.bounding_boxes],
            "token_count": self.token_count,
            "char_count": self.char_count,
            "language": self.language,
            "is_cross_page": self.is_cross_page,
            "table_data": self.table_data,
            "source_element_ids": self.source_element_ids,
            "clause_number": self.clause_number,
        }


class RejectionRule(StrEnum):
    """The specific check that refused a chunk.

    ``reason`` is a category; this is the rule. They differ where one category has
    several causes - "too_small" fires for a boilerplate fragment and for a page of
    OCR noise, and the fix is different in each case. Naming the rule also makes
    the rejections countable per rule, which is what tells you a *threshold* is
    wrong rather than a document.
    """

    EMPTY_TEXT = "empty_text"
    BELOW_MIN_TOKENS = "below_min_tokens"
    ABOVE_MAX_TOKENS = "above_max_tokens"
    ENDS_MID_CLAUSE = "ends_mid_clause"
    #: A section that yielded no text at all, so no chunk was ever built. This one
    #: was previously invisible: the builder returned None and moved on, so the
    #: content vanished without appearing in any count.
    SECTION_PRODUCED_NO_TEXT = "section_produced_no_text"


@dataclass(slots=True)
class ChunkRejection:
    """A chunk the validator refused, with everything needed to diagnose it.

    Recorded at the point of rejection because none of it is recoverable
    afterwards: the chunk is gone, and "129 chunks became 96" gives an operator
    nothing to act on. Page and text preview locate it in the document, the counts
    say how far off the threshold it was, and the rule says which threshold.
    """

    chunk_id: str
    reason: str
    detail: str = ""
    rule: str = ""
    chunk_type: str = ""
    page: int | None = None
    token_count: int = 0
    char_count: int = 0
    section_title: str = ""
    #: Enough text to recognise the content, not enough to bloat the artifact.
    text_preview: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "reason": self.reason,
            "rule": self.rule,
            "detail": self.detail,
            "chunk_type": self.chunk_type,
            "page": self.page,
            "token_count": self.token_count,
            "char_count": self.char_count,
            "section_title": self.section_title,
            "text_preview": self.text_preview,
        }


@dataclass(slots=True)
class ChunkValidationReport:
    """Outcome of chunk validation (§12).

    Rejections are recorded rather than silently dropped: a document that produced
    forty broken chunks is a parser or strategy problem worth surfacing, not
    something to hide behind a lower chunk count.
    """

    total: int = 0
    accepted: int = 0
    rejected: list[ChunkRejection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def rejection_count(self) -> int:
        return len(self.rejected)

    @property
    def is_healthy(self) -> bool:
        """True when enough chunks survived to analyse the document.

        A run that rejects more than a fifth of its chunks has a systemic problem,
        not a handful of edge cases.
        """
        if self.total == 0:
            return False
        return (self.accepted / self.total) >= 0.8

    # ------------------------------------------------------------- aggregation
    @property
    def by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rejection in self.rejected:
            counts[rejection.reason] = counts.get(rejection.reason, 0) + 1
        return counts

    @property
    def by_rule(self) -> dict[str, int]:
        """Rejections per rule - the view that says a threshold is miscalibrated."""
        counts: dict[str, int] = {}
        for rejection in self.rejected:
            if rejection.rule:
                counts[rejection.rule] = counts.get(rejection.rule, 0) + 1
        return counts

    @property
    def by_page(self) -> dict[int, int]:
        """Rejections per page.

        Clustering is the tell. Rejections spread evenly across a document are
        ordinary boilerplate; forty on one page mean the parser mangled that page,
        and no amount of chunking tuning will fix it.
        """
        counts: dict[int, int] = {}
        for rejection in self.rejected:
            if rejection.page is not None:
                counts[rejection.page] = counts.get(rejection.page, 0) + 1
        return counts

    @property
    def by_chunk_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rejection in self.rejected:
            if rejection.chunk_type:
                counts[rejection.chunk_type] = counts.get(rejection.chunk_type, 0) + 1
        return counts

    @property
    def dominant_rule(self) -> str | None:
        """The rule responsible for most rejections, if one clearly is.

        "Most rejections were below_min_tokens" is actionable in a way that a list
        of 96 individual rejections is not.
        """
        counts = self.by_rule
        if not counts:
            return None
        rule, count = max(counts.items(), key=lambda item: item[1])
        return rule if count >= max(2, self.rejection_count // 2) else None

    def diagnostics(self, sample_limit: int = 100) -> dict[str, Any]:
        """The full rejection report.

        Separate from :meth:`as_dict` so the artifact keeps its existing shape
        while the diagnostic view can grow.
        """
        pages = self.by_page
        return {
            "total": self.total,
            "accepted": self.accepted,
            "rejected": self.rejection_count,
            "acceptance_rate": round(self.accepted / self.total, 4) if self.total else 0.0,
            "healthy": self.is_healthy,
            "by_reason": self.by_reason,
            "by_rule": self.by_rule,
            "by_chunk_type": self.by_chunk_type,
            "by_page": {str(page): count for page, count in sorted(pages.items())},
            "worst_pages": [
                {"page": page, "rejected": count}
                for page, count in sorted(pages.items(), key=lambda kv: -kv[1])[:5]
            ],
            "dominant_rule": self.dominant_rule,
            "rejections": [r.as_dict() for r in self.rejected[:sample_limit]],
            "truncated": max(0, self.rejection_count - sample_limit),
            "warnings": self.warnings,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "accepted": self.accepted,
            "rejected": self.rejection_count,
            "rejections_by_reason": self.by_reason,
            "rejections_by_rule": self.by_rule,
            "rejections_by_page": {str(p): c for p, c in sorted(self.by_page.items())},
            "dominant_rule": self.dominant_rule,
            "rejections": [r.as_dict() for r in self.rejected[:100]],
            "warnings": self.warnings,
            "healthy": self.is_healthy,
        }


@dataclass(slots=True)
class ChunkStatistics:
    """Chunk-set statistics, surfaced on the job and the contract detail screen."""

    count: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    total_tokens: int = 0
    mean_tokens: float = 0.0
    min_tokens: int = 0
    max_tokens: int = 0
    cross_page_count: int = 0
    with_coordinates: int = 0
    max_depth: int = 0

    @classmethod
    def build(cls, chunks: list[SemanticChunk]) -> ChunkStatistics:
        if not chunks:
            return cls()
        counts = [chunk.token_count for chunk in chunks]
        by_type: dict[str, int] = {}
        for chunk in chunks:
            key = chunk.chunk_type.value
            by_type[key] = by_type.get(key, 0) + 1
        return cls(
            count=len(chunks),
            by_type=by_type,
            total_tokens=sum(counts),
            mean_tokens=round(sum(counts) / len(counts), 2),
            min_tokens=min(counts),
            max_tokens=max(counts),
            cross_page_count=sum(1 for chunk in chunks if chunk.is_cross_page),
            with_coordinates=sum(1 for chunk in chunks if chunk.bounding_boxes),
            max_depth=max(chunk.level for chunk in chunks),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "by_type": self.by_type,
            "total_tokens": self.total_tokens,
            "mean_tokens": self.mean_tokens,
            "min_tokens": self.min_tokens,
            "max_tokens": self.max_tokens,
            "cross_page_count": self.cross_page_count,
            "with_coordinates": self.with_coordinates,
            "coordinate_coverage": round(self.with_coordinates / self.count, 4)
            if self.count
            else 0.0,
            "max_depth": self.max_depth,
        }


__all__ = [
    "ChunkRejection",
    "ChunkStatistics",
    "ChunkValidationReport",
    "SemanticChunk",
]

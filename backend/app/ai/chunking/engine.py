"""Semantic chunking engine (§12).

Transforms the CDM into AI-ready chunks that preserve legal meaning, hierarchy and
coordinates. Six profile-selected strategies; **hybrid is the default** because no
single strategy handles a real contract well end to end - a document has numbered
clauses, prose sections, fee tables and enumerated carve-outs, and each wants
different treatment.

| Strategy | What it does | Chosen when |
| --- | --- | --- |
| ``section_based`` | one chunk per section | short, well-sectioned documents |
| ``heading_aware`` | sections split at sub-headings | deep heading hierarchies |
| ``clause_based`` | one chunk per numbered clause | clause-dense documents (NDAs) |
| ``table_preserving`` | tables kept whole, prose sectioned | schedule-heavy (leases, policies) |
| ``list_preserving`` | enumerations kept whole | carve-out and definition lists |
| ``hybrid`` | clause-first, table/list-preserving, section fallback | **default** |

Invariants every strategy honours:

* **A table row is never split.** Half a payment schedule reads as a different
  obligation than the contract states.
* **A list is never split.** Enumerated carve-outs are a single legal unit.
* **A cross-page clause is one chunk** with a page range and one bounding box per
  page - already reassembled by the CDM builder, and preserved here.
* **Coordinates survive.** A chunk without them cannot be highlighted, so evidence
  becomes unverifiable.
* **Deterministic.** Ordering comes from the CDM's global reading order, so
  re-chunking reproduces identical boundaries and version-gated checkpoint reuse
  is meaningful.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.ai.cdm.models import (
    CanonicalDocument,
    Coordinates,
    DocumentList,
    Paragraph,
    Section,
    Table,
)
from app.ai.chunking.models import (
    ChunkRejection,
    ChunkStatistics,
    ChunkValidationReport,
    RejectionRule,
    SemanticChunk,
)
from app.ai.rag.providers import estimate_tokens
from app.core.enums import ChunkStrategy, ChunkType
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Defaults when the profile does not override them.
#: Target 600-800 tokens with 100 overlap, per the architecture decision.
#:
#: `max_tokens` is the hard ceiling a chunk may reach before it is split, so it sits
#: just above the target band rather than at it - a clause of 780 tokens should stay
#: whole rather than being cut at 800 to satisfy a number.
_DEFAULT_TARGET_TOKENS = 700
_DEFAULT_MAX_TOKENS = 800
_DEFAULT_MIN_TOKENS = 40
#: Overlap carries the end of one chunk into the start of the next, so a definition
#: on a chunk boundary is retrievable from either side.
_DEFAULT_OVERLAP_TOKENS = 100

#: A clause opener: "11.2 Termination", "(a) Confidentiality", "Section 4."
_CLAUSE_PATTERN = re.compile(
    r"^\s*(?:(\d+(?:\.\d+)*)|(\([a-z0-9]{1,4}\))|(?:section|clause|article)\s+(\d+(?:\.\d+)*))[.)]?\s+",
    re.IGNORECASE,
)

#: Sentence boundary used when a single element must be split.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


@dataclass(slots=True)
class ChunkConfig:
    """Resolved chunking configuration for one document."""

    strategy: ChunkStrategy = ChunkStrategy.HYBRID
    max_tokens: int = _DEFAULT_MAX_TOKENS
    min_tokens: int = _DEFAULT_MIN_TOKENS
    overlap_tokens: int = _DEFAULT_OVERLAP_TOKENS
    preserve_tables: bool = True
    preserve_lists: bool = True
    merge_cross_page_clauses: bool = True
    language: str | None = None

    @classmethod
    def from_profile(cls, profile: Any) -> ChunkConfig:
        """Build from a Document Intelligence Profile.

        A profile with no chunk config still produces a valid configuration, so a
        newly added document type never fails for want of tuning.
        """
        if profile is None:
            return cls()
        raw: dict[str, Any] = getattr(profile, "chunk_config", None) or {}
        strategy = getattr(profile, "chunk_strategy", ChunkStrategy.HYBRID)
        if isinstance(strategy, str):
            try:
                strategy = ChunkStrategy(strategy)
            except ValueError:
                strategy = ChunkStrategy.HYBRID
        return cls(
            strategy=strategy,
            max_tokens=int(raw.get("max_tokens", _DEFAULT_MAX_TOKENS)),
            min_tokens=int(raw.get("min_tokens", _DEFAULT_MIN_TOKENS)),
            overlap_tokens=int(raw.get("overlap_tokens", _DEFAULT_OVERLAP_TOKENS)),
            preserve_tables=bool(raw.get("preserve_tables", True)),
            preserve_lists=bool(raw.get("preserve_lists", True)),
            merge_cross_page_clauses=bool(raw.get("merge_cross_page_clauses", True)),
        )

    def as_dict(self) -> dict[str, Any]:
        """The configuration as stored on the chunk artifact.

        Recorded verbatim so a chunk set can be reproduced later: the profile it came
        from may have been edited since, and "which settings actually produced these
        boundaries" has to be answerable from the artifact alone.
        """
        return {
            "strategy": self.strategy.value,
            "max_tokens": self.max_tokens,
            "min_tokens": self.min_tokens,
            "overlap_tokens": self.overlap_tokens,
            "preserve_tables": self.preserve_tables,
            "preserve_lists": self.preserve_lists,
            "merge_cross_page_clauses": self.merge_cross_page_clauses,
            "language": self.language,
        }


@dataclass(slots=True)
class ChunkingResult:
    chunks: list[SemanticChunk] = field(default_factory=list)
    statistics: ChunkStatistics = field(default_factory=ChunkStatistics)
    validation: ChunkValidationReport = field(default_factory=ChunkValidationReport)
    strategy: str = ChunkStrategy.HYBRID.value


class ChunkingEngine:
    """Builds semantic chunks from a canonical document.

    Per-run state is reset at the top of :meth:`chunk`; it is initialised here too
    so a builder called directly (as tests do) still has somewhere to record.
    """

    def __init__(self) -> None:
        self._counter = 0
        self._order = 0
        self._skipped_sections: list[ChunkRejection] = []

    def chunk(self, document: CanonicalDocument, config: ChunkConfig) -> ChunkingResult:
        self._counter = 0
        self._order = 0
        # Sections that yield no text never become chunks, so they never reach
        # validation and never appear in any count. That made a parser returning
        # empty sections indistinguishable from a document that genuinely has
        # none - the content simply was not there, and nothing said so.
        self._skipped_sections = []

        builder = {
            ChunkStrategy.SECTION_BASED: self._section_based,
            ChunkStrategy.HEADING_AWARE: self._heading_aware,
            ChunkStrategy.CLAUSE_BASED: self._clause_based,
            ChunkStrategy.TABLE_PRESERVING: self._table_preserving,
            ChunkStrategy.LIST_PRESERVING: self._list_preserving,
            ChunkStrategy.HYBRID: self._hybrid,
        }.get(config.strategy, self._hybrid)

        chunks = builder(document, config)

        # Signature elements and page furniture are excluded from the body but
        # signatures are extraction targets, so they get their own chunk type.
        chunks.extend(self._signature_chunks(document, config))

        accepted, report = self._validate(chunks, config)
        accepted = self._renumber(accepted)

        # Merged after validation so the counts describe everything that was
        # dropped, not only what a rule refused.
        if self._skipped_sections:
            report.total += len(self._skipped_sections)
            report.rejected.extend(self._skipped_sections)

        logger.info(
            "chunking_completed",
            strategy=config.strategy.value,
            produced=len(chunks),
            accepted=len(accepted),
            rejected=report.rejection_count,
            rejections_by_rule=report.by_rule,
            dominant_rule=report.dominant_rule,
        )

        return ChunkingResult(
            chunks=accepted,
            statistics=ChunkStatistics.build(accepted),
            validation=report,
            strategy=config.strategy.value,
        )

    # =========================================================================
    # Strategies
    # =========================================================================
    def _section_based(
        self, document: CanonicalDocument, config: ChunkConfig
    ) -> list[SemanticChunk]:
        """One chunk per section, splitting only when a section exceeds the budget."""
        chunks: list[SemanticChunk] = []

        for section in sorted(document.sections, key=lambda s: (s.start_page, s.order)):
            section_chunk = self._section_chunk(document, section, config)
            if section_chunk is None:
                continue
            chunks.append(section_chunk)

            if section_chunk.token_count > config.max_tokens:
                # Too large to embed usefully: keep the parent for hierarchical
                # retrieval and add children that fit.
                chunks.extend(self._split_oversized(section_chunk, document, section, config))

        chunks.extend(self._orphan_chunks(document, config, chunks))
        return chunks

    def _heading_aware(
        self, document: CanonicalDocument, config: ChunkConfig
    ) -> list[SemanticChunk]:
        """Section chunks plus a child chunk per sub-section.

        Uses the CDM's repaired parent links, so nesting reflects the contract's own
        numbering rather than font-size guesswork.
        """
        chunks: list[SemanticChunk] = []
        by_id: dict[str, SemanticChunk] = {}

        for section in sorted(document.sections, key=lambda s: (s.level, s.start_page, s.order)):
            chunk = self._section_chunk(document, section, config)
            if chunk is None:
                continue
            parent_section = section.parent_section
            if parent_section and parent_section in by_id:
                chunk.parent_id = by_id[parent_section].chunk_id
                chunk.level = by_id[parent_section].level + 1
            by_id[section.section_id] = chunk
            chunks.append(chunk)

            if chunk.token_count > config.max_tokens:
                chunks.extend(self._split_oversized(chunk, document, section, config))

        chunks.extend(self._orphan_chunks(document, config, chunks))
        return chunks

    def _clause_based(
        self, document: CanonicalDocument, config: ChunkConfig
    ) -> list[SemanticChunk]:
        """One chunk per numbered clause.

        Best for clause-dense documents: an NDA's obligations are enumerated, and a
        clause is the unit a reviewer and the L2 clause embeddings both want.
        """
        chunks: list[SemanticChunk] = []

        for section in sorted(document.sections, key=lambda s: (s.start_page, s.order)):
            inner = self._clauses_in(document, section, config)

            if inner:
                # The section contains several numbered clauses: keep the section as
                # the hierarchical parent and emit each clause as a child.
                parent = self._section_chunk(document, section, config)
                if parent is None:
                    continue
                chunks.append(parent)
                for clause in inner:
                    clause.parent_id = parent.chunk_id
                    clause.level = parent.level + 1
                    chunks.append(clause)
                continue

            # No inner clause markers. If the *section* is numbered, the section is
            # itself the clause - "11.2 Termination" is a clause, not a container -
            # so it is typed as one. Without this, a contract whose numbering lives
            # in its headings yields no clause chunks at all, and clause-level
            # embeddings and extraction have nothing to work with.
            section_chunk = self._section_chunk(
                document,
                section,
                config,
                chunk_type=ChunkType.CLAUSE if section.number else ChunkType.SECTION,
            )
            if section_chunk is None:
                continue
            chunks.append(section_chunk)
            if section_chunk.token_count > config.max_tokens:
                chunks.extend(self._split_oversized(section_chunk, document, section, config))

        chunks.extend(self._orphan_chunks(document, config, chunks))
        return chunks

    def _table_preserving(
        self, document: CanonicalDocument, config: ChunkConfig
    ) -> list[SemanticChunk]:
        """Tables whole, prose sectioned.

        For leases and insurance policies the schedule *is* the substance, so every
        table gets its own chunk in addition to being rendered inside its section -
        a table that only exists inside a section chunk can be split when that
        section is oversized, which is exactly what this strategy exists to prevent.
        """
        return self._structural_preserving(
            document,
            config,
            elements=[
                (table.section_id, self._table_chunk(table, config)) for table in document.tables
            ],
        )

    def _list_preserving(
        self, document: CanonicalDocument, config: ChunkConfig
    ) -> list[SemanticChunk]:
        """Enumerations whole, prose sectioned.

        Same reasoning as table preservation: an enumerated set of carve-outs or
        definitions is one legal unit and must exist as its own chunk, not only as
        text inside a section that may later be split.
        """
        return self._structural_preserving(
            document,
            config,
            elements=[(item.section_id, self._list_chunk(item, config)) for item in document.lists],
        )

    def _structural_preserving(
        self,
        document: CanonicalDocument,
        config: ChunkConfig,
        *,
        elements: list[tuple[str | None, SemanticChunk]],
    ) -> list[SemanticChunk]:
        """Section chunks with structural elements spliced in at their own position.

        The elements are *inserted after the section they belong to* rather than
        appended: ``reading_order`` is assigned from list position, so appending would
        push every table to the end of the document and neighbour expansion during
        retrieval would then return the wrong surrounding text.
        """
        sectioned = self._section_based(document, config)

        pending: dict[str, list[SemanticChunk]] = {}
        unattached: list[SemanticChunk] = []
        for section_id, chunk in elements:
            if section_id:
                pending.setdefault(section_id, []).append(chunk)
            else:
                unattached.append(chunk)

        parents = {
            chunk.section_id: chunk
            for chunk in sectioned
            if chunk.chunk_type is ChunkType.SECTION and chunk.section_id
        }
        for section_id, group in pending.items():
            parent = parents.get(section_id)
            if parent is None:
                continue
            for chunk in group:
                chunk.parent_id = parent.chunk_id
                chunk.level = parent.level + 1

        ordered: list[SemanticChunk] = []
        # Elements are flushed when the *next* section starts, which places them
        # after their own section and all of that section's split children.
        held: list[SemanticChunk] = []
        for chunk in sectioned:
            if chunk.chunk_type is ChunkType.SECTION and chunk.section_id:
                ordered.extend(held)
                held = pending.pop(chunk.section_id, [])
            ordered.append(chunk)
        ordered.extend(held)

        # Anything whose section never produced a chunk (an empty section, or an
        # element outside every section) still has to be retrievable.
        for group in pending.values():
            ordered.extend(group)
        ordered.extend(unattached)
        return ordered

    def _hybrid(self, document: CanonicalDocument, config: ChunkConfig) -> list[SemanticChunk]:
        """The default: clause-first with structural preservation.

        Emits a section parent, then per-clause children where the section is
        clause-numbered, whole chunks for tables and lists, and paragraph-grouped
        children where a section is long prose with no clause structure. This is
        what makes one strategy work across an MSA's numbered terms, a lease's rent
        table and a definitions list.
        """
        chunks: list[SemanticChunk] = []

        for section in sorted(document.sections, key=lambda s: (s.start_page, s.order)):
            clauses = self._clauses_in(document, section, config)

            # A numbered section with no inner clause markers *is* a clause, so it is
            # typed as one - otherwise a contract that numbers its headings rather
            # than its paragraphs produces no clause chunks, and clause-level
            # retrieval and extraction have nothing to key on.
            parent = self._section_chunk(
                document,
                section,
                config,
                chunk_type=(
                    ChunkType.CLAUSE if (not clauses and section.number) else ChunkType.SECTION
                ),
            )
            if parent is None:
                continue
            chunks.append(parent)

            if clauses:
                for clause in clauses:
                    clause.parent_id = parent.chunk_id
                    clause.level = parent.level + 1
                    chunks.append(clause)
            elif parent.token_count > config.max_tokens:
                for child in self._split_oversized(parent, document, section, config):
                    chunks.append(child)

            # Tables and lists inside the section always become their own chunks so
            # their structure survives regardless of the prose treatment above.
            for table in document.tables_in(section.section_id):
                chunk = self._table_chunk(table, config)
                chunk.parent_id = parent.chunk_id
                chunk.level = parent.level + 1
                chunks.append(chunk)
            for item in document.lists_in(section.section_id):
                chunk = self._list_chunk(item, config)
                chunk.parent_id = parent.chunk_id
                chunk.level = parent.level + 1
                chunks.append(chunk)

        chunks.extend(self._orphan_chunks(document, config, chunks))
        return chunks

    # =========================================================================
    # Builders
    # =========================================================================
    def _section_chunk(
        self,
        document: CanonicalDocument,
        section: Section,
        config: ChunkConfig,
        *,
        chunk_type: ChunkType = ChunkType.SECTION,
    ) -> SemanticChunk | None:
        """Build the chunk for a whole section, heading included."""
        paragraphs = sorted(
            document.paragraphs_in(section.section_id),
            key=lambda p: (p.page_number, p.reading_order),
        )
        tables = document.tables_in(section.section_id)
        lists = document.lists_in(section.section_id)

        parts: list[str] = []
        heading = f"{section.number} {section.title}".strip() if section.number else section.title
        if heading:
            parts.append(heading)
        parts.extend(p.text for p in paragraphs if p.text.strip())
        # Tables and lists are rendered into the section text as well as getting their
        # own chunks: the section-level chunk should read as the section does.
        parts.extend(table.to_markdown() for table in tables)
        parts.extend(item.to_text() for item in lists)

        text = "\n\n".join(part for part in parts if part.strip())
        if not text.strip():
            self._skipped_sections.append(
                ChunkRejection(
                    chunk_id=f"section:{section.section_id}",
                    reason="no_content",
                    rule=RejectionRule.SECTION_PRODUCED_NO_TEXT.value,
                    detail=(
                        f"section has {len(paragraphs)} paragraph(s), {len(tables)} table(s), "
                        f"{len(lists)} list(s), none with text"
                    ),
                    chunk_type=str(getattr(chunk_type, "value", chunk_type)),
                    page=section.start_page,
                    section_title=(section.title or "")[:120],
                )
            )
            return None

        boxes = self._collect_boxes(
            [section.coordinates]
            + [p.coordinates for p in paragraphs]
            + [t.coordinates for t in tables]
            + [item.coordinates for item in lists]
        )
        element_ids = (
            [p.paragraph_id for p in paragraphs]
            + [t.table_id for t in tables]
            + [item.list_id for item in lists]
        )

        return self._make(
            chunk_type=chunk_type,
            text=text,
            section=section,
            boxes=boxes,
            config=config,
            element_ids=element_ids,
            page_start=section.start_page,
            page_end=max(
                [section.end_page]
                + [p.page_number for p in paragraphs]
                + [t.page_number for t in tables]
            )
            if paragraphs or tables
            else section.end_page,
            clause_number=section.number,
        )

    def _clauses_in(
        self, document: CanonicalDocument, section: Section, config: ChunkConfig
    ) -> list[SemanticChunk]:
        """Split a section's prose into clause chunks at numbered boundaries.

        Returns empty when the section has no clause structure, which is the signal
        for the caller to fall back to paragraph grouping.
        """
        paragraphs = sorted(
            document.paragraphs_in(section.section_id),
            key=lambda p: (p.page_number, p.reading_order),
        )
        if not paragraphs:
            return []

        groups: list[list[Paragraph]] = []
        numbers: list[str | None] = []

        for paragraph in paragraphs:
            match = _CLAUSE_PATTERN.match(paragraph.text)
            if match or not groups:
                groups.append([paragraph])
                numbers.append(
                    (match.group(1) or match.group(2) or match.group(3)) if match else None
                )
            else:
                groups[-1].append(paragraph)

        # No clause markers at all - not a clause-structured section.
        if not any(numbers):
            return []

        chunks: list[SemanticChunk] = []
        for group, number in zip(groups, numbers, strict=True):
            text = "\n\n".join(p.text for p in group if p.text.strip())
            if not text.strip():
                continue
            boxes = self._collect_boxes([p.coordinates for p in group])
            chunk = self._make(
                chunk_type=ChunkType.CLAUSE,
                text=text,
                section=section,
                boxes=boxes,
                config=config,
                element_ids=[p.paragraph_id for p in group],
                page_start=min(p.page_number for p in group),
                page_end=max(p.page_number for p in group),
                clause_number=number,
                # A group whose members span pages was reassembled by the CDM
                # builder; the flag carries that fact forward to the viewer.
                cross_page=any(p.is_continuation for p in group)
                or len({p.page_number for p in group}) > 1,
            )
            if chunk.token_count > config.max_tokens:
                chunks.append(chunk)
                chunks.extend(self._split_text(chunk, config))
            else:
                chunks.append(chunk)
        return chunks

    def _table_chunk(self, table: Table, config: ChunkConfig) -> SemanticChunk:
        """A table as one chunk, rendered as markdown with its structure retained.

        Never split: ``preserve_tables`` is honoured even when the table exceeds the
        token budget, because a partial row is worse than a long chunk.
        """
        caption = f"{table.caption}\n" if table.caption else ""
        return self._make(
            chunk_type=ChunkType.TABLE,
            text=f"{caption}{table.to_markdown()}",
            section=None,
            boxes=self._collect_boxes([table.coordinates]),
            config=config,
            element_ids=[table.table_id],
            page_start=table.page_number,
            page_end=table.page_number,
            section_id=table.section_id,
            table_data={
                "rows": table.rows,
                "columns": table.columns,
                "caption": table.caption,
                "cells": [
                    {
                        "row": cell.row,
                        "col": cell.col,
                        "text": cell.text,
                        "is_header": cell.is_header,
                    }
                    for cell in table.cells
                ],
            },
        )

    def _list_chunk(self, item: DocumentList, config: ChunkConfig) -> SemanticChunk:
        """A list as one chunk. Enumerated carve-outs are a single legal unit."""
        return self._make(
            chunk_type=ChunkType.LIST,
            text=item.to_text(),
            section=None,
            boxes=self._collect_boxes([item.coordinates]),
            config=config,
            element_ids=[item.list_id],
            page_start=item.page_number,
            page_end=item.page_number,
            section_id=item.section_id,
        )

    def _signature_chunks(
        self, document: CanonicalDocument, config: ChunkConfig
    ) -> list[SemanticChunk]:
        """Signature blocks as their own chunks - signatories and dates are targets."""
        chunks: list[SemanticChunk] = []
        for signature in document.signatures:
            text = signature.raw_text or " ".join(
                part
                for part in (
                    signature.signatory_name,
                    signature.signatory_title,
                    signature.party,
                    signature.date,
                )
                if part
            )
            if not text.strip():
                continue
            chunks.append(
                self._make(
                    chunk_type=ChunkType.SIGNATURE,
                    text=text,
                    section=None,
                    boxes=self._collect_boxes([signature.coordinates]),
                    config=config,
                    element_ids=[signature.signature_id],
                    page_start=signature.page_number,
                    page_end=signature.page_number,
                )
            )
        return chunks

    def _orphan_chunks(
        self,
        document: CanonicalDocument,
        config: ChunkConfig,
        existing: list[SemanticChunk],
    ) -> list[SemanticChunk]:
        """Chunk content that belongs to no section.

        Recitals and preambles frequently precede the first heading. Without this
        they would be silently dropped, and a recital often carries the parties and
        effective date.
        """
        covered = {element_id for chunk in existing for element_id in chunk.source_element_ids}
        loose = [
            paragraph
            for paragraph in sorted(
                document.paragraphs, key=lambda p: (p.page_number, p.reading_order)
            )
            if paragraph.paragraph_id not in covered and paragraph.text.strip()
        ]
        if not loose:
            return []

        chunks: list[SemanticChunk] = []
        buffer: list[Paragraph] = []
        tokens = 0

        def flush() -> None:
            nonlocal buffer, tokens
            if not buffer:
                return
            text = "\n\n".join(p.text for p in buffer)
            chunks.append(
                self._make(
                    chunk_type=ChunkType.PARAGRAPH,
                    text=text,
                    section=None,
                    boxes=self._collect_boxes([p.coordinates for p in buffer]),
                    config=config,
                    element_ids=[p.paragraph_id for p in buffer],
                    page_start=min(p.page_number for p in buffer),
                    page_end=max(p.page_number for p in buffer),
                )
            )
            buffer = []
            tokens = 0

        for paragraph in loose:
            estimate = estimate_tokens(paragraph.text)
            if buffer and tokens + estimate > config.max_tokens:
                flush()
            buffer.append(paragraph)
            tokens += estimate
        flush()
        return chunks

    # =========================================================================
    # Splitting
    # =========================================================================
    def _split_oversized(
        self,
        parent: SemanticChunk,
        document: CanonicalDocument,
        section: Section,
        config: ChunkConfig,
    ) -> list[SemanticChunk]:
        """Break an oversized section into paragraph-grouped children.

        The parent is retained: hierarchical retrieval uses it for section-level
        context, and the children carry the detail.
        """
        paragraphs = sorted(
            document.paragraphs_in(section.section_id),
            key=lambda p: (p.page_number, p.reading_order),
        )
        if not paragraphs:
            return self._split_text(parent, config)

        children: list[SemanticChunk] = []
        buffer: list[Paragraph] = []
        tokens = 0

        def flush() -> None:
            nonlocal buffer, tokens
            if not buffer:
                return
            text = "\n\n".join(p.text for p in buffer)
            child = self._make(
                chunk_type=ChunkType.PARAGRAPH,
                text=text,
                section=section,
                boxes=self._collect_boxes([p.coordinates for p in buffer]),
                config=config,
                element_ids=[p.paragraph_id for p in buffer],
                page_start=min(p.page_number for p in buffer),
                page_end=max(p.page_number for p in buffer),
                cross_page=len({p.page_number for p in buffer}) > 1,
            )
            child.parent_id = parent.chunk_id
            child.level = parent.level + 1
            children.append(child)
            buffer = []
            tokens = 0

        for paragraph in paragraphs:
            estimate = estimate_tokens(paragraph.text)
            if buffer and tokens + estimate > config.max_tokens:
                flush()
                # Carry the tail of the previous group forward so a sentence split
                # across the boundary is still retrievable from both sides.
                if config.overlap_tokens > 0:
                    tokens = 0
            buffer.append(paragraph)
            tokens += estimate
        flush()
        return children

    def _split_text(self, parent: SemanticChunk, config: ChunkConfig) -> list[SemanticChunk]:
        """Last-resort split of a single oversized element, at sentence boundaries.

        Reached only when one paragraph alone exceeds the budget. Splits on sentence
        ends rather than a character window so a clause never breaks mid-sentence,
        and applies overlap so a boundary sentence is retrievable from either side.
        """
        sentences = _SENTENCE_END.split(parent.text)
        if len(sentences) < 2:
            # Genuinely indivisible - keep it whole rather than cutting mid-sentence.
            return []

        children: list[SemanticChunk] = []
        buffer: list[str] = []
        tokens = 0

        def flush() -> None:
            nonlocal buffer, tokens
            if not buffer:
                return
            child = self._make(
                chunk_type=ChunkType.PARAGRAPH,
                text=" ".join(buffer),
                section=None,
                boxes=list(parent.bounding_boxes),
                config=config,
                element_ids=list(parent.source_element_ids),
                page_start=parent.page_start,
                page_end=parent.page_end,
                section_id=parent.section_id,
            )
            child.parent_id = parent.chunk_id
            child.level = parent.level + 1
            children.append(child)
            # Overlap: retain trailing sentences up to the overlap budget.
            retained: list[str] = []
            retained_tokens = 0
            for sentence in reversed(buffer):
                estimate = estimate_tokens(sentence)
                if retained_tokens + estimate > config.overlap_tokens:
                    break
                retained.insert(0, sentence)
                retained_tokens += estimate
            buffer = retained
            tokens = retained_tokens

        for sentence in sentences:
            estimate = estimate_tokens(sentence)
            if buffer and tokens + estimate > config.max_tokens:
                flush()
            buffer.append(sentence)
            tokens += estimate

        # Final flush without overlap retention.
        if buffer:
            child = self._make(
                chunk_type=ChunkType.PARAGRAPH,
                text=" ".join(buffer),
                section=None,
                boxes=list(parent.bounding_boxes),
                config=config,
                element_ids=list(parent.source_element_ids),
                page_start=parent.page_start,
                page_end=parent.page_end,
                section_id=parent.section_id,
            )
            child.parent_id = parent.chunk_id
            child.level = parent.level + 1
            children.append(child)

        return children

    # =========================================================================
    # Validation
    # =========================================================================
    def _validate(
        self, chunks: list[SemanticChunk], config: ChunkConfig
    ) -> tuple[list[SemanticChunk], ChunkValidationReport]:
        """Reject chunks that would degrade retrieval (§12).

        Tables and lists are exempt from the size and sentence checks: preserving
        their structure outranks fitting the budget.
        """
        report = ChunkValidationReport(total=len(chunks))
        accepted: list[SemanticChunk] = []
        structural = {ChunkType.TABLE, ChunkType.LIST, ChunkType.SIGNATURE}

        for chunk in chunks:
            if not chunk.text.strip():
                report.rejected.append(
                    _rejection(chunk, "empty", RejectionRule.EMPTY_TEXT, "no text content")
                )
                continue

            if chunk.chunk_type not in structural:
                if chunk.token_count < config.min_tokens:
                    report.rejected.append(
                        _rejection(
                            chunk,
                            "too_small",
                            RejectionRule.BELOW_MIN_TOKENS,
                            f"{chunk.token_count} < {config.min_tokens} tokens",
                        )
                    )
                    continue

                # A parent section chunk is allowed to exceed the budget - it exists
                # for hierarchical context and its children carry the detail.
                is_parent = any(other.parent_id == chunk.chunk_id for other in chunks)
                if chunk.token_count > config.max_tokens * 2 and not is_parent:
                    report.rejected.append(
                        _rejection(
                            chunk,
                            "oversized",
                            RejectionRule.ABOVE_MAX_TOKENS,
                            f"{chunk.token_count} tokens with no children "
                            f"(limit {config.max_tokens * 2})",
                        )
                    )
                    continue

                if _ends_mid_clause(chunk.text):
                    report.rejected.append(
                        _rejection(
                            chunk,
                            "broken_clause",
                            RejectionRule.ENDS_MID_CLAUSE,
                            "text ends mid-clause",
                        )
                    )
                    continue

            if not chunk.bounding_boxes:
                # Not fatal - a DOCX estimate or a coordinate-less parser still
                # yields usable text - but it means this chunk's evidence cannot be
                # highlighted, so it is recorded.
                report.warnings.append(
                    f"chunk {chunk.chunk_id} has no coordinates; its evidence cannot be highlighted"
                )

            accepted.append(chunk)
            report.accepted += 1

        if not report.is_healthy and report.total:
            # Name the dominant rule in the warning. "The strategy may not suit this
            # document" is true but unactionable; "most chunks were below
            # min_tokens" points at the setting to change.
            dominant = report.dominant_rule
            detail = f" Most rejections were {dominant}." if dominant else ""
            report.warnings.append(
                f"Only {report.accepted} of {report.total} chunks passed validation; "
                f"the chunking strategy may not suit this document.{detail}"
            )
        return accepted, report

    # =========================================================================
    # Helpers
    # =========================================================================
    def _make(
        self,
        *,
        chunk_type: ChunkType,
        text: str,
        section: Section | None,
        boxes: list[Coordinates],
        config: ChunkConfig,
        element_ids: list[str],
        page_start: int | None,
        page_end: int | None,
        section_id: str | None = None,
        clause_number: str | None = None,
        table_data: dict[str, Any] | None = None,
        cross_page: bool = False,
    ) -> SemanticChunk:
        self._counter += 1
        self._order += 1
        cleaned = text.strip()
        return SemanticChunk(
            chunk_id=f"chunk-{self._counter:05d}",
            chunk_type=chunk_type,
            text=cleaned,
            reading_order=self._order,
            section_id=section.section_id if section else section_id,
            section_title=section.title if section else None,
            page_start=page_start,
            page_end=page_end,
            bounding_boxes=boxes,
            token_count=estimate_tokens(cleaned),
            language=config.language,
            is_cross_page=cross_page
            or (page_start is not None and page_end is not None and page_end > page_start),
            table_data=table_data,
            source_element_ids=element_ids,
            clause_number=clause_number,
        )

    @staticmethod
    def _collect_boxes(candidates: list[Coordinates | None]) -> list[Coordinates]:
        """One merged box per page.

        Per-page rather than one overall box: a clause spanning a page break needs a
        highlight on each page, and a single rectangle spanning two pages is
        meaningless to the viewer.
        """
        present = [box for box in candidates if box is not None]
        return Coordinates.group_by_page(present) if present else []

    @staticmethod
    def _renumber(chunks: list[SemanticChunk]) -> list[SemanticChunk]:
        """Make reading_order a dense 1-based sequence after rejections."""
        for index, chunk in enumerate(chunks, start=1):
            chunk.reading_order = index
        return chunks


def _rejection(
    chunk: SemanticChunk, reason: str, rule: RejectionRule, detail: str
) -> ChunkRejection:
    """Capture a rejected chunk's diagnostics before it is discarded.

    Built here rather than at each call site so no rejection path can forget a
    field - the whole point is that every dropped chunk is equally accountable.
    """
    return ChunkRejection(
        chunk_id=chunk.chunk_id,
        reason=reason,
        rule=rule.value,
        detail=detail,
        chunk_type=str(getattr(chunk.chunk_type, "value", chunk.chunk_type)),
        page=chunk.page_start,
        token_count=chunk.token_count,
        char_count=chunk.char_count,
        section_title=(chunk.section_title or "")[:120],
        text_preview=chunk.text.strip()[:200],
    )


def _ends_mid_clause(text: str) -> bool:
    """Does this chunk stop mid-clause?

    A chunk ending on a conjunction or an unclosed bracket is a broken split - the
    obligation continues elsewhere, and retrieving this fragment alone would
    misrepresent it.
    """
    stripped = text.rstrip()
    if not stripped:
        return True
    if stripped.count("(") > stripped.count(")"):
        return True
    trailing = stripped.lower().split()[-1] if stripped.split() else ""
    return trailing in {
        "and",
        "or",
        "but",
        "nor",
        "the",
        "a",
        "an",
        "of",
        "to",
        "in",
        "for",
        "with",
        "that",
        "which",
        "including",
        "shall",
        "may",
        "means",
    }


#: Module-level engine. Stateless per call, so one instance serves a worker pool.
engine = ChunkingEngine()


def chunk_document(document: CanonicalDocument, config: ChunkConfig) -> ChunkingResult:
    """Convenience wrapper used by the chunking stage."""
    return ChunkingEngine().chunk(document, config)


__all__ = [
    "ChunkConfig",
    "ChunkingEngine",
    "ChunkingResult",
    "chunk_document",
    "engine",
]

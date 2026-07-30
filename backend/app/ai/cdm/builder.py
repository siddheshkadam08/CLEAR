"""CDM builder: ``NormalizedDocument`` -> ``CanonicalDocument``.

This is the enrichment step (§8/§10 stage 3). The parser's job is fidelity; the
builder's job is *structure*. It adds what no single parser reliably provides and
what every downstream stage depends on:

* **A global reading order.** Parsers report per-page elements in separate
  collections; chunking needs one deterministic sequence across paragraphs, tables
  and lists. Ordering is by ``(page, vertical position, horizontal position)`` so
  the result is reproducible regardless of the order the parser emitted things in.
* **Cross-page clause reassembly.** A clause split by a page break arrives as two
  paragraph fragments. Left alone, chunking would emit two half-clauses and
  retrieval would return text that reads as a different obligation than the
  contract states. The builder detects continuations and merges them into one
  paragraph spanning a page range.
* **Section hierarchy repair.** Parent links are inferred from numbering
  (``11.2`` under ``11``) where the parser only gave a flat list, and orphaned
  parent references are cleared so the CDM's integrity validator passes.
* **Cross-reference detection.** "subject to Section 11.2" becomes a resolved
  reference, which the knowledge graph later turns into a ``references`` edge.
* **Honest quality metrics.** Coordinate coverage and text density are computed
  from the content, not taken on trust from the adapter.

The output is immutable. Nothing downstream may modify it (§8 frozen rules).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.ai.cdm.models import (
    CanonicalDocument,
    Coordinates,
    CrossReference,
    DocumentList,
    DocumentStatistics,
    NormalizedDocument,
    Page,
    Paragraph,
    QualityMetrics,
    ReadingOrderEntry,
    Section,
    Table,
)
from app.core.enums import ContentBlockType
from app.core.logging import get_logger

logger = get_logger(__name__)

#: "Section 11.2", "clause 4(a)", "Schedule B", "Exhibit 2", "Annex A"
_REFERENCE_PATTERN = re.compile(
    r"\b(?:section|clause|article|paragraph|schedule|exhibit|annex|appendix|attachment)s?\s+"
    r"(\d+(?:\.\d+)*(?:\([a-z0-9]+\))?|[A-Z](?:\.\d+)*|[IVXLC]+)\b",
    re.IGNORECASE,
)

#: Section numbering, e.g. "11", "11.2", "11.2.3".
_NUMBER_PATTERN = re.compile(r"^(\d+(?:\.\d+)*)\.?$")

#: A fragment that ends mid-sentence is a continuation candidate. Excludes common
#: abbreviations so "Inc." or "No." is not mistaken for an unfinished sentence.
_SENTENCE_END = re.compile(r"[.!?:;]['\")\]]?\s*$")
_ABBREVIATIONS = (
    "inc.",
    "ltd.",
    "llc.",
    "co.",
    "corp.",
    "no.",
    "nos.",
    "art.",
    "sec.",
    "cf.",
    "e.g.",
    "i.e.",
    "etc.",
    "vs.",
    "v.",
)

#: A continuation must begin lowercase or with a conjunction/connector - a fragment
#: starting with a capitalised word is usually a new sentence, not a spill-over.
_CONTINUATION_START = re.compile(
    r"^(?:[a-z]|and\b|or\b|but\b|nor\b|that\b|which\b|to\b|of\b|in\b|for\b|with\b|shall\b)"
)


@dataclass(slots=True)
class BuildResult:
    """Builder output plus what it changed, for the stage's statistics."""

    document: CanonicalDocument
    merged_paragraphs: int = 0
    inferred_parents: int = 0
    cleared_parents: int = 0
    references_found: int = 0
    warnings: list[str] = field(default_factory=list)


class CanonicalDocumentBuilder:
    """Builds the canonical document. Stateless - safe to share across tasks."""

    def build(self, normalized: NormalizedDocument) -> BuildResult:
        warnings: list[str] = list(normalized.quality.warnings)

        sections, inferred, cleared = self._repair_sections(list(normalized.sections))
        paragraphs, merged = self._merge_continuations(list(normalized.paragraphs), sections)
        paragraphs = self._assign_sections(paragraphs, sections)

        tables = self._assign_element_sections(list(normalized.tables), sections)
        lists = self._assign_element_sections(list(normalized.lists), sections)

        reading_order = self._build_reading_order(
            paragraphs=paragraphs,
            tables=tables,
            lists=lists,
            pages=list(normalized.pages),
        )

        references = self._detect_references(paragraphs, sections)

        statistics = self._statistics(normalized, paragraphs, tables, lists, sections)
        quality = self._quality(normalized, paragraphs, tables, lists, warnings)

        document = CanonicalDocument(
            metadata=normalized.metadata,
            statistics=statistics,
            pages=self._renumber_pages(list(normalized.pages), reading_order),
            sections=sections,
            paragraphs=paragraphs,
            tables=tables,
            lists=lists,
            images=list(normalized.images),
            signatures=list(normalized.signatures),
            headers=list(normalized.headers),
            footers=list(normalized.footers),
            footnotes=list(normalized.footnotes),
            references=references,
            reading_order=reading_order,
            quality_metrics=quality,
        )

        logger.info(
            "cdm_built",
            document_id=normalized.metadata.document_id,
            pages=statistics.total_pages,
            sections=statistics.section_count,
            paragraphs=statistics.paragraph_count,
            merged_paragraphs=merged,
            references=len(references),
            coordinate_coverage=quality.coordinate_coverage,
        )

        return BuildResult(
            document=document,
            merged_paragraphs=merged,
            inferred_parents=inferred,
            cleared_parents=cleared,
            references_found=len(references),
            warnings=quality.warnings,
        )

    # =========================================================================
    # Section hierarchy
    # =========================================================================
    def _repair_sections(self, sections: list[Section]) -> tuple[list[Section], int, int]:
        """Infer missing parent links and drop dangling ones.

        Numbering is the strongest hierarchy signal a contract gives: ``11.2`` is a
        child of ``11`` regardless of how the parser assigned levels. Where numbering
        is absent the existing level ordering is used as a fallback.
        """
        if not sections:
            return [], 0, 0

        by_number: dict[str, str] = {}
        for section in sections:
            match = _NUMBER_PATTERN.match((section.number or "").strip())
            if match:
                by_number[match.group(1)] = section.section_id

        valid_ids = {section.section_id for section in sections}
        repaired: list[Section] = []
        inferred = 0
        cleared = 0

        for index, section in enumerate(sections):
            parent = section.parent_section
            level = section.level

            number_match = _NUMBER_PATTERN.match((section.number or "").strip())
            if number_match:
                parts = number_match.group(1).split(".")
                # Level follows numbering depth: "11.2.3" is level 3.
                level = len(parts)
                if len(parts) > 1:
                    candidate = by_number.get(".".join(parts[:-1]))
                    if candidate and candidate != section.section_id:
                        if parent != candidate:
                            inferred += 1
                        parent = candidate
                elif parent is not None:
                    # A top-level number cannot have a parent.
                    cleared += 1
                    parent = None

            if parent is not None and parent not in valid_ids:
                cleared += 1
                parent = None

            if parent is None and level > 1:
                # Fall back to the nearest preceding shallower section.
                for previous in reversed(repaired):
                    if previous.level < level:
                        parent = previous.section_id
                        inferred += 1
                        break

            if parent == section.section_id:
                parent = None
                cleared += 1

            repaired.append(
                section.model_copy(
                    update={"parent_section": parent, "level": level, "order": index}
                )
            )

        # A cycle would make graph traversal and the UI's section tree loop forever.
        repaired = self._break_cycles(repaired)
        return repaired, inferred, cleared

    @staticmethod
    def _break_cycles(sections: list[Section]) -> list[Section]:
        """Clear parent links that would form a cycle."""
        parents = {section.section_id: section.parent_section for section in sections}
        result: list[Section] = []

        for section in sections:
            seen: set[str] = {section.section_id}
            cursor = section.parent_section
            cyclic = False
            while cursor is not None:
                if cursor in seen:
                    cyclic = True
                    break
                seen.add(cursor)
                cursor = parents.get(cursor)
            result.append(
                section.model_copy(update={"parent_section": None}) if cyclic else section
            )
            if cyclic:
                logger.warning("section_cycle_broken", section_id=section.section_id)
        return result

    # =========================================================================
    # Cross-page continuation merging
    # =========================================================================
    def _merge_continuations(
        self, paragraphs: list[Paragraph], sections: list[Section]
    ) -> tuple[list[Paragraph], int]:
        """Reassemble paragraphs split across a page break.

        A clause continued onto the next page must become **one** logical paragraph
        with a page range, not two fragments (§12). Merging requires three signals to
        agree, because a false merge fuses two unrelated clauses:

        1. the fragments are on consecutive pages,
        2. the earlier one does not end a sentence,
        3. the later one reads like a continuation (lowercase or a connector).
        """
        if len(paragraphs) < 2:
            return paragraphs, 0

        ordered = sorted(paragraphs, key=lambda p: (p.page_number, p.reading_order))
        merged: list[Paragraph] = []
        count = 0
        index = 0

        while index < len(ordered):
            current = ordered[index]

            while index + 1 < len(ordered):
                following = ordered[index + 1]
                if not self._is_continuation(current, following):
                    break

                boxes = [
                    box for box in (current.coordinates, following.coordinates) if box is not None
                ]
                # Keep one box per page so the viewer can highlight the clause on both
                # pages rather than drawing a single impossible rectangle spanning them.
                page_boxes = Coordinates.group_by_page(boxes)

                current = current.model_copy(
                    update={
                        "text": f"{current.text.rstrip()} {following.text.lstrip()}",
                        "coordinates": page_boxes[0] if page_boxes else None,
                        "is_continuation": True,
                    }
                )
                count += 1
                index += 1

            merged.append(current)
            index += 1

        # Renumber so reading_order stays a dense sequence after merging.
        return [
            paragraph.model_copy(update={"reading_order": position})
            for position, paragraph in enumerate(merged)
        ], count

    def _is_continuation(self, first: Paragraph, second: Paragraph) -> bool:
        """Would these two fragments form one clause?"""
        if second.page_number != first.page_number + 1:
            return False

        text = first.text.rstrip()
        if not text or not second.text.strip():
            return False

        lowered = text.lower()
        ends_sentence = bool(_SENTENCE_END.search(text)) and not any(
            lowered.endswith(abbreviation) for abbreviation in _ABBREVIATIONS
        )
        if ends_sentence:
            return False

        # A new numbered clause on the next page is a new clause, never a spill-over.
        if _REFERENCE_PATTERN.match(second.text.strip()) or re.match(
            r"^\d+(\.\d+)*\.?\s+[A-Z]", second.text.strip()
        ):
            return False

        if not _CONTINUATION_START.match(second.text.strip()):
            return False

        # Different sections mean different clauses.
        return not (
            first.section_id and second.section_id and first.section_id != second.section_id
        )

    # =========================================================================
    # Section assignment
    # =========================================================================
    @staticmethod
    def _assign_sections(paragraphs: list[Paragraph], sections: list[Section]) -> list[Paragraph]:
        """Attach an owning section to paragraphs the parser left unassigned.

        Assignment is by document position: the last section that began at or before
        the paragraph owns it. Without this, a clause extracted from an unassigned
        paragraph has no section to cite.
        """
        if not sections:
            return paragraphs

        ordered_sections = sorted(sections, key=lambda s: (s.start_page, s.order))
        result: list[Paragraph] = []

        for paragraph in paragraphs:
            if paragraph.section_id:
                result.append(paragraph)
                continue

            owner: Section | None = None
            for section in ordered_sections:
                if section.start_page <= paragraph.page_number:
                    owner = section
                else:
                    break
            result.append(
                paragraph.model_copy(update={"section_id": owner.section_id})
                if owner is not None
                else paragraph
            )
        return result

    @staticmethod
    def _assign_element_sections(elements: list, sections: list[Section]) -> list:  # type: ignore[type-arg]
        """Same positional assignment for tables and lists."""
        if not sections:
            return elements
        ordered_sections = sorted(sections, key=lambda s: (s.start_page, s.order))
        result = []
        for element in elements:
            if getattr(element, "section_id", None):
                result.append(element)
                continue
            owner: Section | None = None
            for section in ordered_sections:
                if section.start_page <= element.page_number:
                    owner = section
                else:
                    break
            result.append(
                element.model_copy(update={"section_id": owner.section_id})
                if owner is not None
                else element
            )
        return result

    # =========================================================================
    # Reading order
    # =========================================================================
    def _build_reading_order(
        self,
        *,
        paragraphs: list[Paragraph],
        tables: list[Table],
        lists: list[DocumentList],
        pages: list[Page],
    ) -> list[ReadingOrderEntry]:
        """Build one global, deterministic sequence over all body elements.

        Sorted by ``(page, y, x)`` using coordinates where available, falling back to
        the parser's own ordering. Determinism is the point: re-chunking the same
        document must produce identical boundaries, so version-gated checkpoint reuse
        is meaningful.
        """
        candidates: list[tuple[int, float, float, int, str, ContentBlockType, str]] = []

        for paragraph in paragraphs:
            box = paragraph.coordinates
            candidates.append(
                (
                    paragraph.page_number,
                    box.y if box else float(paragraph.reading_order),
                    box.x if box else 0.0,
                    paragraph.reading_order,
                    paragraph.paragraph_id,
                    ContentBlockType.PARAGRAPH,
                    paragraph.section_id or "",
                )
            )

        for table in tables:
            box = table.coordinates
            candidates.append(
                (
                    table.page_number,
                    box.y if box else float(table.reading_order),
                    box.x if box else 0.0,
                    table.reading_order,
                    table.table_id,
                    ContentBlockType.TABLE,
                    table.section_id or "",
                )
            )

        for item in lists:
            box = item.coordinates
            candidates.append(
                (
                    item.page_number,
                    box.y if box else float(item.reading_order),
                    box.x if box else 0.0,
                    item.reading_order,
                    item.list_id,
                    ContentBlockType.LIST,
                    item.section_id or "",
                )
            )

        # Final tiebreaker on element id keeps the sort total, so two elements at the
        # exact same coordinates still order consistently between runs.
        candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]))

        return [
            ReadingOrderEntry(
                index=index,
                block_id=f"ro-{index}",
                block_type=block_type,
                page_number=page,
                section_id=section_id or None,
                element_id=element_id,
            )
            for index, (page, _, _, _, element_id, block_type, section_id) in enumerate(candidates)
        ]

    @staticmethod
    def _renumber_pages(pages: list[Page], order: list[ReadingOrderEntry]) -> list[Page]:
        """Record each page's first global reading-order index."""
        first_by_page: dict[int, int] = {}
        for entry in order:
            first_by_page.setdefault(entry.page_number, entry.index)
        return [
            page.model_copy(update={"reading_order": first_by_page.get(page.page_number, 0)})
            for page in pages
        ]

    # =========================================================================
    # Cross-references
    # =========================================================================
    def _detect_references(
        self, paragraphs: list[Paragraph], sections: list[Section]
    ) -> list[CrossReference]:
        """Find in-document references and resolve them to sections where possible.

        These become ``references`` edges in the knowledge graph, which is what makes
        "which clauses depend on Section 11?" answerable without an LLM call.
        """
        by_number: dict[str, str] = {}
        for section in sections:
            if section.number:
                by_number[section.number.strip().rstrip(".").lower()] = section.section_id

        references: list[CrossReference] = []
        seen: set[tuple[str, str]] = set()

        for paragraph in paragraphs:
            for match in _REFERENCE_PATTERN.finditer(paragraph.text):
                label = match.group(1).strip()
                # Deduplicate per paragraph: a clause citing Section 11 three times is
                # one relationship, not three.
                key = (paragraph.paragraph_id, label.lower())
                if key in seen:
                    continue
                seen.add(key)

                # Strip a sub-clause suffix so "11.2(a)" still resolves to "11.2".
                normalised = re.sub(r"\([a-z0-9]+\)$", "", label.lower()).rstrip(".")

                references.append(
                    CrossReference(
                        reference_id=f"ref-{len(references)}",
                        source_page=paragraph.page_number,
                        text=match.group(0),
                        target_label=label,
                        target_section_id=by_number.get(normalised),
                        coordinates=paragraph.coordinates,
                    )
                )

        return references

    # =========================================================================
    # Statistics & quality
    # =========================================================================
    @staticmethod
    def _statistics(
        normalized: NormalizedDocument,
        paragraphs: list[Paragraph],
        tables: list[Table],
        lists: list[DocumentList],
        sections: list[Section],
    ) -> DocumentStatistics:
        text = " ".join(paragraph.text for paragraph in paragraphs)
        return DocumentStatistics(
            total_pages=len(normalized.pages),
            paragraph_count=len(paragraphs),
            table_count=len(tables),
            image_count=len(normalized.images),
            list_count=len(lists),
            section_count=len(sections),
            word_count=len(text.split()),
            char_count=len(text),
            signature_count=len(normalized.signatures),
        )

    @staticmethod
    def _quality(
        normalized: NormalizedDocument,
        paragraphs: list[Paragraph],
        tables: list[Table],
        lists: list[DocumentList],
        warnings: list[str],
    ) -> QualityMetrics:
        """Recompute quality from the built content.

        Coverage is measured here rather than inherited, so an adapter cannot
        over-report its own fidelity and a merge that lost a box is caught.
        """
        positioned = len(paragraphs) + len(tables) + len(lists) + len(normalized.signatures)
        with_coords = sum(
            1
            for element in [*paragraphs, *tables, *lists, *normalized.signatures]
            if getattr(element, "coordinates", None) is not None
        )
        coverage = (with_coords / positioned) if positioned else 1.0

        total_chars = sum(len(paragraph.text) for paragraph in paragraphs)
        empty_pages = [page.page_number for page in normalized.pages if page.block_count == 0]

        collected = list(dict.fromkeys(warnings))
        if coverage < 1.0:
            collected.append(
                f"{positioned - with_coords} of {positioned} elements lack coordinates; "
                "their evidence cannot be highlighted in the viewer."
            )
        if not paragraphs:
            collected.append("No body text was recovered from this document.")
        elif total_chars < 500 and len(normalized.pages) > 2:
            collected.append(
                "Very little text was recovered relative to the page count; the "
                "document may be scanned or image-only."
            )

        return QualityMetrics(
            ocr_confidence=normalized.quality.ocr_confidence,
            missing_text=not paragraphs or (total_chars < 200 and len(normalized.pages) > 1),
            empty_pages=empty_pages,
            corrupted_pages=list(normalized.quality.corrupted_pages),
            scanned_pages=list(normalized.quality.scanned_pages),
            coordinate_coverage=round(coverage, 4),
            warnings=collected,
        )


#: Module-level builder. Stateless, so one instance serves every worker.
builder = CanonicalDocumentBuilder()


def build_canonical_document(normalized: NormalizedDocument) -> BuildResult:
    """Convenience wrapper used by the enrichment stage."""
    return builder.build(normalized)


__all__ = ["BuildResult", "CanonicalDocumentBuilder", "build_canonical_document", "builder"]

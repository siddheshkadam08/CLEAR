"""Typed knowledge extraction, running on what the document pipeline located.

The stage `ai_extraction` used to do this, and its engine still does - untouched.
What changed is where the text comes from and what happens when it finds nothing.

**Where the text comes from.** `ai_extraction` read the `chunks` table, which the
retired chunking stage filled. Nothing writes it now, so this stage builds its
chunks from the parser's cached page JSON instead - the same payloads
`docpipeline` reads, keyed on the file hash, so the vendor is not called again.

**Why they are sections and not paragraphs.** The first version of this stage
emitted one chunk per paragraph and extracted zero clauses from a document
`docpipeline` had found twenty in. Two reasons, both in `EvidenceSelector`:
every rule carries ``min_tokens: 15`` and a chunk under it is dropped before it
is ever scored, and matching leans on `CandidateChunk.heading`, which a
paragraph has nothing to fill. Grouping paragraphs under the heading that
introduces them fixes both - the chunk clears the floor, and it carries the
heading that `heading_patterns` and the title-keyword bonus are written against.

**Evidence selection is still the engine's.** `docpipeline` has already located
every clause and stored its text, so in principle this stage could hand the
engine the right sections instead of letting `EvidenceSelector` search for
them - fewer tokens, and no chance of selecting the wrong passage. It does not,
yet: the engine builds its own selector inside `extract()`, so injecting one
means changing the engine, and the engine is the part worth not breaking. The
selector's keyword rules are proven and the saving is tokens rather than
correctness, so this is deferred until there is a measurement saying it matters.

**Why finding nothing is no longer fatal.** `ai_extraction` raised when a
document produced no clauses, parties or dates - which is precisely how a 13-page
licence agreement ended up marked failed with nothing stored. By the time this
stage runs, `docpipeline` has already persisted the clauses and their geometry.
Losing the risk score must not also lose the clause list, so an empty extraction
is a warning and a review flag.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from typing import Any

from app.ai.cdm.models import Coordinates
from app.ai.chunking.models import SemanticChunk
from app.ai.docpipeline.source import Paragraph, iter_paragraphs, pages_from_payloads
from app.ai.extraction.evidence import CandidateChunk
from app.ai.parsers.fixtures import ObjectCache
from app.ai.rag.providers import estimate_tokens
from app.core.config import get_settings
from app.core.enums import ArtifactKind, ChunkType, EmbeddingLevel, PipelineStage
from app.core.errors import PipelineError
from app.core.logging import get_logger
from app.core.versions import (
    EXTRACTION_CHUNK_STRATEGY_VERSION,
    EXTRACTION_ENGINE_VERSION,
    ComponentVersions,
    current_versions_for_stage,
)
from app.orchestrator.stages.ai_extraction import AIExtractionStage
from app.orchestrator.stages.base import StageArtifact, StageContext, register_stage
from app.repositories.chunk import ChunkRepository
from app.repositories.embedding import EmbeddingRepository

#: Recorded on every row so a later reader can tell where these came from. Not
#: the chunking engine's strategy: these are heading groups from page JSON, and
#: labelling them otherwise would make the version columns describe a run that
#: never happened.
CHUNK_STRATEGY = "section_based"

logger = get_logger(__name__)


class ExtractionStage(AIExtractionStage):
    """`AIExtractionStage` with a different chunk source and a softer failure mode.

    Subclassed rather than rewritten so `cleanup()` and `_persist()` are inherited
    exactly. Both are load-bearing and neither is obvious: `cleanup` deletes
    dependents before clauses because obligations and risks hold a nullable FK to
    `clauses` with ``ON DELETE SET NULL``, and reversing the order leaves detached
    rows in the window between statements; `_persist` writes ten repositories plus
    the `contract_metadata` projection the dashboards read.
    """

    stage = PipelineStage.EXTRACTION
    requires = (ArtifactKind.DOC_PIPELINE,)
    cacheable = True
    retryable = True

    def versions_for(self, ctx: StageContext) -> ComponentVersions:
        return current_versions_for_stage(self.stage, profile_id=None)

    # ------------------------------------------------------------------ input
    async def _load_chunks(self, ctx: StageContext) -> list[CandidateChunk]:
        """Section-level chunks from the parser's cached page JSON."""
        settings = get_settings()
        payloads = await ObjectCache(settings.parser.active_parser).load(ctx.contract.sha256_hash)
        if not payloads:
            raise PipelineError(
                "No cached page JSON for this document. This stage reads the "
                "parser's cache, keyed on the file hash; re-run the parser stage.",
                stage=self.stage.value,
                retryable=False,
                details={"file_hash": ctx.contract.sha256_hash},
            )

        chunks = _section_chunks(iter_paragraphs(pages_from_payloads(payloads)))
        # Field names avoid "token": the log scrubber redacts anything so named,
        # which would blank the two numbers this line exists to show.
        logger.info(
            "extraction_chunks_built",
            contract_id=str(ctx.contract_id),
            sections=len(chunks),
            section_words=sum(len(c.text.split()) for c in chunks),
            too_short_to_score=sum(1 for c in chunks if c.token_count < _RULE_MIN_TOKENS),
        )
        return chunks

    async def _prepare_chunks(
        self, ctx: StageContext, chunks: list[CandidateChunk]
    ) -> dict[str, uuid.UUID]:
        """Write the sections to the `chunks` table and map ref -> database id.

        These chunks are built from page JSON, so nothing has given them a row.
        Persisting them is what reconnects three things that have been quietly
        broken since the chunking stage was retired: `clauses.chunk_id` resolves
        to something again, `GET /evidence/{chunk_id}` stops 404ing, and the
        keyword leg of search - which queries `chunks.search_vector` and nothing
        else - starts returning hits. The tsvector is maintained by a database
        trigger, so inserting the rows is the whole of that last one.

        Written *before* extraction rather than alongside it: the sections derive
        purely from the parser cache and are valid whatever the model then finds,
        so a failed or empty extraction still leaves a searchable document.
        """
        if not chunks:
            return {}

        chunk_ids = await ChunkRepository(ctx.db).persist(
            contract_id=ctx.contract_id,
            project_id=ctx.project_id,
            chunks=[_as_semantic_chunk(chunk) for chunk in chunks],
            strategy=CHUNK_STRATEGY,
            engine_version=EXTRACTION_ENGINE_VERSION,
            strategy_version=EXTRACTION_CHUNK_STRATEGY_VERSION,
            language=ctx.contract.language,
            agreement_type=ctx.contract.agreement_type,
        )
        logger.info(
            "extraction_chunks_persisted",
            contract_id=str(ctx.contract_id),
            rows=len(chunk_ids),
        )
        return chunk_ids

    async def cleanup(self, ctx: StageContext) -> None:
        """The base cleanup, then the chunks this stage also owns.

        Chunks go **last**. `clauses.chunk_id` is ``ON DELETE SET NULL``, so
        deleting them first would fire an UPDATE across every clause row a
        moment before those rows are themselves deleted.

        Chunk-level vectors go first for the opposite reason: `embeddings.ref_id`
        is not a foreign key, so nothing would clear them, and a vector left
        pointing at a deleted chunk is a search hit whose evidence cannot be
        loaded.
        """
        await EmbeddingRepository(ctx.db).delete_for_contract_level(
            ctx.contract_id, ctx.project_id, EmbeddingLevel.CHUNK
        )
        await super().cleanup(ctx)
        removed = await ChunkRepository(ctx.db).delete_for_contract(ctx.contract_id, ctx.project_id)
        if removed:
            logger.info(
                "extraction_chunks_cleared",
                contract_id=str(ctx.contract_id),
                rows=removed,
            )

    def _artifacts(
        self,
        result: Any,
        counts: dict[str, int],
        chunks: list[CandidateChunk] | None = None,
        chunk_ids: dict[str, uuid.UUID] | None = None,
    ) -> list[Any]:
        """The knowledge artifacts, plus the chunk manifest this stage now owns.

        Declared in `STAGE_ARTIFACTS[EXTRACTION]` and required by the embedding
        stage, so it has to be emitted rather than merely listed - a kind named
        there but never produced is the same defect in the other direction: it
        would never be superseded, and a reprocess would leave two rows current.
        """
        artifacts = super()._artifacts(result, counts, chunks, chunk_ids)
        if not chunks:
            return artifacts

        ids = chunk_ids or {}
        artifacts.append(
            StageArtifact(
                kind=ArtifactKind.CHUNKS,
                payload={
                    "strategy": CHUNK_STRATEGY,
                    "chunks": [
                        {
                            "chunk_id": chunk.chunk_id,
                            "db_id": str(ids[chunk.chunk_id]) if chunk.chunk_id in ids else None,
                            "section_title": chunk.section_title,
                            "clause_number": chunk.clause_number,
                            "pages": [chunk.page_start, chunk.page_end],
                            "tokens": chunk.token_count,
                        }
                        for chunk in chunks
                    ],
                },
                summary={
                    "strategy": CHUNK_STRATEGY,
                    "chunks": len(chunks),
                    "persisted": len(ids),
                },
            )
        )
        return artifacts

    # ----------------------------------------------------------------- policy
    @staticmethod
    def _is_useful(result: Any) -> bool:
        """Always true - an empty extraction is reported, not raised.

        The base class treats "no clauses, no parties, no dates" as a broken run
        and fails the job. That was right when this stage was the only thing that
        produced anything; it is wrong now. `docpipeline` has already stored the
        clauses and their geometry by this point, and failing here would discard a
        useful result to signal a missing one.
        """
        return True


register_stage(ExtractionStage())


# =============================================================================
# Paragraphs -> sections
# =============================================================================
#: Mirrors ``min_tokens`` in every seeded Clause Master rule. Used only to report
#: how many chunks the selector will refuse, so a repeat of the zero-clause run
#: is visible in the logs rather than inferred from an empty table.
_RULE_MIN_TOKENS = 15

#: Split a section that runs past this. Nothing breaks above it - the selector
#: has its own evidence budget and truncates - but one 4,000-token section
#: crowds the budget and drags unrelated text into the prompt with it.
_MAX_SECTION_TOKENS = 700

#: A leading clause number, e.g. ``9.3`` in "9.3 Limitation of Liability".
_CLAUSE_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*[.)]?\s+")


def _section_chunks(paragraphs: Sequence[Paragraph]) -> list[CandidateChunk]:
    """Group paragraphs into the sections their headings introduce.

    A heading opens a section and titles it; the paragraphs after it are its
    body. Furniture - running heads, footers, page numbers - is dropped, because
    it matches keywords as readily as real text does and carries no clause.

    Text before the first heading (recitals, the preamble) becomes an untitled
    section rather than being discarded. Parties and dates live there.
    """
    sections: list[list[Paragraph]] = []
    current: list[Paragraph] = []

    for paragraph in paragraphs:
        if paragraph.is_furniture or not paragraph.content.strip():
            continue
        if paragraph.is_heading and current:
            sections.append(current)
            current = []
        current.append(paragraph)
    if current:
        sections.append(current)

    chunks: list[CandidateChunk] = []
    for section in sections:
        parts = _split_oversized(section)
        for number, part in enumerate(parts, start=1):
            chunk = _to_chunk(
                part,
                reading_order=len(chunks) + 1,
                # Only suffixed when a section actually split, so the common case
                # keeps the short ref the model sees in the prompt.
                suffix=f"-{number}" if len(parts) > 1 else "",
            )
            if chunk is not None:
                chunks.append(chunk)
    return chunks


def _split_oversized(section: list[Paragraph]) -> list[list[Paragraph]]:
    """Break a long section on paragraph boundaries, never mid-paragraph.

    Each part keeps the heading, so a split section's second half is still
    matchable by heading and still reads as what it belongs to.
    """
    if estimate_tokens("\n".join(p.content for p in section)) <= _MAX_SECTION_TOKENS:
        return [section]

    heading = section[0] if section[0].is_heading else None
    body = section[1:] if heading else section

    parts: list[list[Paragraph]] = []
    run: list[Paragraph] = []
    budget = 0
    for paragraph in body:
        cost = estimate_tokens(paragraph.content)
        if run and budget + cost > _MAX_SECTION_TOKENS:
            parts.append(([heading] if heading else []) + run)
            run, budget = [], 0
        run.append(paragraph)
        budget += cost
    if run:
        parts.append(([heading] if heading else []) + run)
    return parts or [section]


def _to_chunk(
    section: list[Paragraph], *, reading_order: int, suffix: str = ""
) -> CandidateChunk | None:
    """One section as the selector wants to see it.

    ``token_count`` is set explicitly. Leaving it at the dataclass default of
    zero is what produced the zero-clause run: every rule carries a
    ``min_tokens`` floor, and a chunk reporting zero tokens fails all of them.
    """
    text = "\n".join(p.content.strip() for p in section if p.content.strip())
    if not text:
        return None

    heading = section[0] if section[0].is_heading else None
    title = heading.content.strip() if heading else None
    number = _CLAUSE_NUMBER.match(title) if title else None

    pages = [p.page_number for p in section]
    return CandidateChunk(
        chunk_id=f"{section[0].ref}{suffix}",
        text=text,
        chunk_type=ChunkType.SECTION if heading else ChunkType.PARAGRAPH,
        reading_order=reading_order,
        token_count=estimate_tokens(text),
        section_title=title,
        clause_number=number.group(1) if number else None,
        page_start=min(pages),
        page_end=max(pages),
        bounding_boxes=_page_boxes(section),
    )


def _as_semantic_chunk(chunk: CandidateChunk) -> SemanticChunk:
    """A selector chunk as the thing `ChunkRepository.persist` writes.

    Two representations of the same section: `CandidateChunk` is what the
    extraction engine scores, `SemanticChunk` is what the repository inserts.
    Adapting rather than adding a second writer keeps the parent-ordering and
    batching logic in one place.

    `level` is 0 and `parent_id` is None throughout - the grouping is one level
    deep, and claiming a hierarchy that does not exist would make
    `persist`'s parents-before-children sort meaningless. `section_id` carries
    the engine ref, which is what makes the ref -> uuid mapping auditable later.
    """
    return SemanticChunk(
        chunk_id=chunk.chunk_id,
        chunk_type=chunk.chunk_type,
        text=chunk.text,
        reading_order=chunk.reading_order,
        section_id=chunk.chunk_id,
        section_title=chunk.section_title,
        clause_number=chunk.clause_number,
        level=0,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        bounding_boxes=[Coordinates(**box) for box in chunk.bounding_boxes],
        token_count=chunk.token_count,
        is_cross_page=(chunk.page_start or 0) != (chunk.page_end or 0),
    )


def _page_boxes(section: list[Paragraph]) -> list[dict[str, object]]:
    """One highlight rectangle per page the section covers.

    Merged per page rather than one box per paragraph: the viewer draws every
    box it is given, and a twelve-paragraph clause otherwise renders as twelve
    separate strips instead of one block.

    The shape is `BoundingBox`'s - `page_number`/`x`/`y`/`width`/`height` in
    points - not the `{page, polygon}` inches that `cip_DocContentMaster` holds.
    Those are two different contracts, and writing the second into a column the
    API validates against the first is what made `GET /knowledge` return a 500
    for every contract.
    """
    by_page: dict[int, list[Coordinates]] = {}
    for paragraph in section:
        box = paragraph.box
        if box is not None:
            by_page.setdefault(box.page_number, []).append(box)

    merged: list[dict[str, object]] = []
    for page in sorted(by_page):
        box = Coordinates.merge(by_page[page])
        if box is not None:
            merged.append(box.to_dict())
    return merged

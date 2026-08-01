"""Stage 2: find each expected clause in the document.

Two passes, cheapest first.

**Pass A - section headings.** Azure marks headings with ``role:
"sectionHeading"``. A heading is a short, deliberate statement of what follows,
so matching against it is both the most precise signal available and nearly
free: an exact match costs nothing, and everything left over is resolved by a
single call carrying only headings, never body text.

**Pass B - four-page chunks.** Whatever Pass A missed is searched for in the
body, four pages at a time. A clause not found in one chunk stays outstanding
and is carried into the next, and the next, until either every clause is found
or the document runs out.

That loop rule is what gives the report its meaning. Stopping early *and*
reporting clauses as missing would be a statement about how far the search got,
printed in a way that reads as a statement about the contract. So there are
exactly two ways to finish: everything was found, or everything was read.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.ai.docpipeline.inference import call_structured
from app.ai.docpipeline.mapping import ClauseSpec
from app.ai.docpipeline.source import (
    PageContent,
    Paragraph,
    iter_paragraphs,
    pages_with_geometry,
    per_page_boxes,
)
from app.ai.rag.providers import IInferenceProvider, StructuredResult, get_inference_provider
from app.ai.routing import LLMTask
from app.core.logging import get_logger

logger = get_logger(__name__)

#: How many times the heading-mapping call is sampled. See `_heading_pass`.
HEADING_SAMPLES = 2

#: Pages per fallback call.
DEFAULT_CHUNK_PAGES = 4

#: Pages re-read at the start of each window. See `_windows`.
#:
#: Zero by default, on measurement rather than principle. On a 28-page MSA with
#: the heading pass held fixed, an overlap of 1 on 4-page windows found exactly
#: the same clauses as no overlap - the same five, not five others - while
#: costing two extra calls and 34% more tokens. Boundary repair already covers
#: the seam a clause is most likely to be split across, so the overlap was
#: paying twice for the same protection. Raise it if a document type turns up
#: where clauses genuinely straddle windows.
DEFAULT_CHUNK_OVERLAP = 0

#: How many windows are searched concurrently. Each call is a 30-100s round
#: trip, so this is the difference between a seven-minute run and a one-minute
#: one. See the note in `_chunk_pass` for what it trades away.
DEFAULT_CHUNK_CONCURRENCY = 4

#: A chunk with less text than this is not worth a call. Stamp-paper
#: covers, signature pages and exhibit dividers land here. Recorded as skipped,
#: not as unread - the pages were reached and evaluated.
MIN_CHUNK_CHARS = 200

#: How far past a chunk boundary a clause may be extended. See `_repair_boundary`.
MAX_REPAIR_PARAGRAPHS = 12

_LEADING_NUMBER = re.compile(r"^[\s]*(?:\d+(?:\.\d+)*|[ivxlcIVXLC]+|[a-zA-Z])[.)\]]\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class DetectedClause:
    """A clause located in the document, with the text and geometry behind it."""

    clause: str
    paragraphs: tuple[Paragraph, ...]
    method: str
    boundary_repaired: bool = False

    @property
    def textcontent(self) -> str:
        return "\n".join(paragraph.content for paragraph in self.paragraphs)

    @property
    def page_numbers(self) -> list[int]:
        """Pages this clause has geometry for, aligned with :attr:`polygon`.

        Deliberately the pages that produced a box, not every page the clause
        touches: the two arrays are read together, and a page contributing no
        box would slide every later box onto the wrong page.
        """
        pages = pages_with_geometry(self.paragraphs)
        return pages or sorted({paragraph.page_number for paragraph in self.paragraphs})

    @property
    def polygon(self) -> list[float]:
        """One box per page, 8 floats each, in :attr:`page_numbers` order."""
        return per_page_boxes(self.paragraphs)

    @property
    def page_span(self) -> str:
        pages = self.page_numbers
        if not pages:
            return "-"
        return f"p{pages[0]}" if len(pages) == 1 else f"p{pages[0]}-{pages[-1]}"


@dataclass(slots=True)
class ChunkOutcome:
    """What one four-page window cost and yielded."""

    start_page: int
    end_page: int
    found: list[str] = field(default_factory=list)
    outstanding_after: int = 0
    skipped: bool = False
    skip_reason: str = ""
    chars_sent: int = 0


@dataclass(slots=True)
class ClauseDetection:
    """The full result of stage 2, including what was *not* looked at."""

    detected: list[DetectedClause] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    chunk_outcomes: list[ChunkOutcome] = field(default_factory=list)
    pages_never_read: list[int] = field(default_factory=list)
    early_stopped: bool = False
    heading_exact: int = 0
    heading_llm: int = 0
    total_targets: int = 0
    duration_ms: int = 0

    @property
    def pages_sent_to_chunks(self) -> list[int]:
        pages: list[int] = []
        for outcome in self.chunk_outcomes:
            if not outcome.skipped:
                pages.extend(range(outcome.start_page, outcome.end_page + 1))
        return pages

    @property
    def llm_chunk_calls(self) -> int:
        return sum(1 for outcome in self.chunk_outcomes if not outcome.skipped)


class ClauseDetector:
    """Locates the clauses of a document type within a parsed document."""

    def __init__(self, provider: IInferenceProvider | None = None) -> None:
        self._provider = provider or get_inference_provider()

    async def _structured(
        self, *, system: str, prompt: str, schema: dict[str, Any]
    ) -> StructuredResult:
        """One structured call, on a budget, with headroom if it overflows."""
        return await call_structured(
            self._provider,
            system=system,
            prompt=prompt,
            schema=schema,
            task=LLMTask.CLAUSE_EXTRACTION,
        )

    async def detect(
        self,
        pages: Sequence[PageContent],
        clauses: Sequence[ClauseSpec],
        *,
        chunk_pages: int = DEFAULT_CHUNK_PAGES,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        concurrency: int = DEFAULT_CHUNK_CONCURRENCY,
        early_stop: bool = True,
    ) -> ClauseDetection:
        started = time.perf_counter()
        result = ClauseDetection(total_targets=len(clauses))
        if not clauses:
            return result

        paragraphs = iter_paragraphs(list(pages))
        by_ref = {paragraph.ref: paragraph for paragraph in paragraphs}

        # ---------------------------------------------------------- Pass A
        detected = await self._heading_pass(paragraphs, clauses, result)
        result.detected.extend(detected)

        found_keys = {item.clause for item in detected}
        outstanding = [spec for spec in clauses if spec.clause not in found_keys]

        # ---------------------------------------------------------- Pass B
        if outstanding:
            await self._chunk_pass(
                pages=list(pages),
                paragraphs=paragraphs,
                by_ref=by_ref,
                outstanding=outstanding,
                all_clauses=clauses,
                chunk_pages=max(1, chunk_pages),
                chunk_overlap=max(0, chunk_overlap),
                concurrency=max(1, concurrency),
                early_stop=early_stop,
                result=result,
            )
            found_keys = {item.clause for item in result.detected}

        result.not_found = [spec.clause for spec in clauses if spec.clause not in found_keys]
        result.duration_ms = int((time.perf_counter() - started) * 1000)

        # The invariant this whole design exists to hold. If it ever trips, the
        # report would be claiming a clause is absent from pages it never opened.
        if result.not_found and result.pages_never_read:
            raise AssertionError(
                "Clause detection stopped early while clauses were still outstanding: "
                f"{len(result.not_found)} not found but pages "
                f"{result.pages_never_read} were never read. This is a bug in the "
                "chunk loop, not a property of the document."
            )

        logger.info(
            "docpipeline_clauses_detected",
            targets=result.total_targets,
            detected=len(result.detected),
            not_found=len(result.not_found),
            heading_exact=result.heading_exact,
            heading_llm=result.heading_llm,
            chunk_calls=result.llm_chunk_calls,
            early_stopped=result.early_stopped,
            pages_never_read=len(result.pages_never_read),
            duration_ms=result.duration_ms,
        )
        return result

    # ------------------------------------------------------------- Pass A
    async def _heading_pass(
        self,
        paragraphs: list[Paragraph],
        clauses: Sequence[ClauseSpec],
        result: ClauseDetection,
    ) -> list[DetectedClause]:
        headings = [p for p in paragraphs if p.is_heading]
        if not headings:
            return []

        #: clause name -> (heading paragraph, how it was matched)
        taken: dict[str, tuple[Paragraph, str]] = {}

        # 1. Free exact matching on the normalised heading text.
        unmatched: list[Paragraph] = []
        lookup = _normalised_lookup(clauses)
        for heading in headings:
            clause = lookup.get(_normalise(heading.content))
            if clause is not None and clause not in taken:
                taken[clause] = (heading, "heading:exact")
                result.heading_exact += 1
            else:
                unmatched.append(heading)

        # 2. The rest go to the model. Headings only - no body text is sent, so
        #    this stays "identified by the heading alone" and stays cheap.
        #
        #    Sampled twice and unioned. The same call over the same 41 headings
        #    returned 9, 11 and 13 clauses on three runs; that spread is the
        #    largest single source of variance in the pipeline, larger than
        #    anything chunk geometry can recover. The samples run concurrently,
        #    so the second costs a call and almost no wall clock, and a union
        #    rather than a vote because the problem being solved is recall.
        remaining = [spec for spec in clauses if spec.clause not in taken]
        if unmatched and remaining:
            samples = await asyncio.gather(
                *(self._map_headings(unmatched, remaining) for _ in range(HEADING_SAMPLES))
            )
            heading_by_ref = {heading.ref: heading for heading in unmatched}
            valid = {spec.clause for spec in remaining}
            for assignments in samples:
                for ref, clause in assignments:
                    heading = heading_by_ref.get(ref)
                    if heading is None or clause not in valid or clause in taken:
                        continue
                    taken[clause] = (heading, "heading:llm")
                    result.heading_llm += 1

        return await self._extents(taken, paragraphs)

    async def _extents(
        self,
        taken: dict[str, tuple[Paragraph, str]],
        paragraphs: list[Paragraph],
    ) -> list[DetectedClause]:
        """Turn heading matches into extents, splitting any shared section.

        A heading claimed by one clause takes the whole section. A heading
        claimed by several - `7. TERMS AND TERMINATION` covers the term, the
        renewal and both terminations - would otherwise give each of them the
        identical extent, and therefore identical `textcontent` and identical
        vectors: several rows a retrieval query cannot tell apart. So a shared
        section gets one extra call to divide it up.
        """
        ordered = _ordered(taken, paragraphs)
        shared: dict[str, list[str]] = {}
        for clause, (heading, _method) in ordered:
            shared.setdefault(heading.ref, []).append(clause)

        splits: dict[str, dict[str, list[Paragraph]]] = {}
        contested = [(ref, clauses) for ref, clauses in shared.items() if len(clauses) > 1]
        if contested:
            heading_by_ref = {heading.ref: heading for _c, (heading, _m) in ordered}
            results = await asyncio.gather(
                *(
                    self._split_section(
                        _extent_from_heading(heading_by_ref[ref], paragraphs), clauses
                    )
                    for ref, clauses in contested
                )
            )
            splits = dict(zip([ref for ref, _ in contested], results, strict=True))

        detected: list[DetectedClause] = []
        for clause, (heading, method) in ordered:
            extent = _extent_from_heading(heading, paragraphs)
            portion = splits.get(heading.ref, {}).get(clause)
            if portion:
                extent = tuple(portion)
                method = f"{method}:split"
            detected.append(DetectedClause(clause=clause, paragraphs=extent, method=method))
        return detected

    async def _split_section(
        self, extent: Sequence[Paragraph], clauses: Sequence[str]
    ) -> dict[str, list[Paragraph]]:
        """Divide one section's paragraphs between the clauses that claimed it.

        Best-effort: on any failure the caller keeps the shared extent, which is
        wrong-but-usable rather than missing.
        """
        by_ref = {paragraph.ref: paragraph for paragraph in extent}
        schema = {
            "type": "object",
            "properties": {
                "assignments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "clause": {"type": "string", "enum": list(clauses)},
                            "paragraph_refs": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["clause", "paragraph_refs"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["assignments"],
            "additionalProperties": False,
        }
        try:
            response = await self._structured(
                system=(
                    "One section of a contract covers several clause types. "
                    "Assign each paragraph to the clause type it belongs to.\n\n"
                    "Rules:\n"
                    "- Every clause type listed must get at least one paragraph.\n"
                    "- A paragraph may belong to more than one clause type.\n"
                    "- Use only refs printed in the section, exactly as printed."
                ),
                prompt="\n".join(
                    [
                        "Clause types sharing this section:",
                        *[f"- {clause}" for clause in clauses],
                        "",
                        "Section, as '[ref] text':",
                        "",
                        *[f"[{p.ref}] {p.content}" for p in extent],
                    ]
                ),
                schema=schema,
            )
        except Exception as exc:  # noqa: BLE001 - falling back to the shared extent
            logger.warning("docpipeline_section_split_failed", error=str(exc)[:300])
            return {}

        split: dict[str, list[Paragraph]] = {}
        for entry in response.data.get("assignments") or []:
            if not isinstance(entry, dict):
                continue
            clause = str(entry.get("clause") or "").strip()
            if clause not in set(clauses):
                continue
            chosen = [
                by_ref[ref]
                for ref in (_clean_ref(value) for value in entry.get("paragraph_refs") or [])
                if ref in by_ref
            ]
            if chosen:
                split[clause] = chosen

        # A split that assigned nothing to one of the claimants is not a split;
        # keeping it would leave that clause with no text at all.
        if len(split) < len(clauses):
            logger.warning(
                "docpipeline_section_split_incomplete",
                clauses=list(clauses),
                assigned=sorted(split),
            )
            return {}
        return split

    async def _map_headings(
        self, headings: Sequence[Paragraph], clauses: Sequence[ClauseSpec]
    ) -> list[tuple[str, str]]:
        system = "\n".join(
            [
                "You match section headings from a contract to a list of clause types.",
                "",
                "The clause types, and what each covers:",
                *[spec.as_prompt_line() for spec in clauses],
                "",
                "Rules:",
                "- Only match a heading whose subject is unmistakably the clause type.",
                # Contracts routinely put several clause types under one heading -
                # 'TERMS AND TERMINATION' governs the term, renewal, and both
                # kinds of termination. Forcing one clause per heading loses all
                # but one of them, and the rest are then reported as absent from
                # a document that plainly contains them.
                "- One heading may match several clause types. List each match "
                "separately, repeating the heading ref.",
                "- Each clause type may match at most one heading. If several fit, "
                "choose the most specific.",
                "- Omit headings that match nothing. A wrong match is worse than none: "
                "it stops the clause being searched for in the body.",
                "- Copy clause names exactly as written above.",
            ]
        )
        prompt = "\n".join(
            [
                "Section headings, as 'ref: text'. The ref is page.position.",
                "",
                *[f"{heading.ref}: {heading.content}" for heading in headings],
                "",
                "Which headings correspond to which clause types?",
            ]
        )
        schema = {
            "type": "object",
            "properties": {
                "matches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading_ref": {"type": "string"},
                            "clause": {"type": "string", "enum": [s.clause for s in clauses]},
                        },
                        "required": ["heading_ref", "clause"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["matches"],
            "additionalProperties": False,
        }

        try:
            response = await self._structured(system=system, prompt=prompt, schema=schema)
        except Exception as exc:  # noqa: BLE001 - a failed heading pass is not fatal
            logger.warning("docpipeline_heading_pass_failed", error=str(exc)[:300])
            return []

        # A list of pairs, not a dict keyed on ref: one heading legitimately
        # yields several clauses, and a dict would keep only the last of them.
        matches: list[tuple[str, str]] = []
        for entry in response.data.get("matches") or []:
            if not isinstance(entry, dict):
                continue
            ref = _clean_ref(entry.get("heading_ref") or "")
            clause = str(entry.get("clause") or "").strip()
            if ref and clause:
                matches.append((ref, clause))
        return matches

    # ------------------------------------------------------------- Pass B
    async def _chunk_pass(
        self,
        *,
        pages: list[PageContent],
        paragraphs: list[Paragraph],
        by_ref: dict[str, Paragraph],
        outstanding: list[ClauseSpec],
        all_clauses: Sequence[ClauseSpec],
        chunk_pages: int,
        chunk_overlap: int,
        concurrency: int,
        early_stop: bool,
        result: ClauseDetection,
    ) -> None:
        windows = _windows(pages, chunk_pages, chunk_overlap)
        pending = list(outstanding)
        seen_pages: set[int] = set()
        wave_size = max(1, concurrency)

        # Windows are searched in waves. Within a wave the calls run
        # concurrently against the same outstanding list; between waves the list
        # shrinks and the early stop is checked.
        #
        # Strictly sequential would carry forward more aggressively - each window
        # searching for slightly fewer clauses - but every call is a 30-100s
        # round trip, so a seven-window document spent seven minutes waiting.
        # In the common case the outstanding list does not empty before the last
        # window anyway (a clause genuinely absent is searched for everywhere),
        # and then the sequential version does exactly the same work, serially.
        # A wave costs at most `wave_size - 1` windows of over-reading, and only
        # when the stop would have fired mid-wave.
        for wave_start in range(0, len(windows), wave_size):
            if not pending and early_stop:
                # Everything found. The remaining windows are genuinely unread,
                # and because nothing is outstanding, nothing will be reported
                # missing on the strength of pages we skipped.
                #
                # A page carried by overlap into a later window may already have
                # been read in an earlier one, so "never read" is the pages no
                # window has covered yet - not simply the pages left in the list.
                result.early_stopped = True
                result.pages_never_read = sorted(
                    {
                        page.page_number
                        for later in windows[wave_start:]
                        for page in later
                        if page.page_number not in seen_pages
                    }
                )
                return

            wave = windows[wave_start : wave_start + wave_size]
            snapshot = list(pending)
            planned: list[tuple[ChunkOutcome, list[Paragraph]]] = []

            for window in wave:
                seen_pages.update(page.page_number for page in window)
                outcome = ChunkOutcome(
                    start_page=window[0].page_number,
                    end_page=window[-1].page_number,
                )
                # Every paragraph in the window is sent, including ones already
                # attributed to a clause found by the heading pass.
                #
                # Excluding them looked like free savings and was not. One
                # section routinely holds several clauses - '7. TERMS AND
                # TERMINATION' is the term, the renewal and both terminations -
                # so once the heading pass claimed that section for Term/duration,
                # the text of the three others became invisible to this pass and
                # all three were reported absent from a document that plainly
                # contains them. A false "absent" is far more expensive than the
                # tokens it saved.
                body = [paragraph for page in window for paragraph in page.paragraphs]
                outcome.chars_sent = sum(len(paragraph.content) for paragraph in body)

                if outcome.chars_sent < MIN_CHUNK_CHARS:
                    outcome.skipped = True
                    outcome.skip_reason = (
                        f"{outcome.chars_sent} chars of text (floor {MIN_CHUNK_CHARS})"
                    )
                planned.append((outcome, body))

            searches = await asyncio.gather(
                *(
                    self._search_chunk(body, snapshot, all_clauses)
                    for outcome, body in planned
                    if not outcome.skipped
                )
            )
            results = iter(searches)

            claimed = {item.clause for item in result.detected}
            for outcome, body in planned:
                found = {} if outcome.skipped else next(results)
                for clause, refs in found.items():
                    # An earlier window in the same wave may have claimed this
                    # clause already; the first occurrence in document order wins.
                    if clause in claimed:
                        continue
                    selected = [by_ref[ref] for ref in refs if ref in by_ref]
                    if not selected:
                        continue
                    selected, repaired = _repair_boundary(selected, body, paragraphs)
                    result.detected.append(
                        DetectedClause(
                            clause=clause,
                            paragraphs=tuple(selected),
                            method=f"chunk {outcome.start_page}-{outcome.end_page}",
                            boundary_repaired=repaired,
                        )
                    )
                    claimed.add(clause)
                    outcome.found.append(clause)

                pending = [spec for spec in pending if spec.clause not in claimed]
                outcome.outstanding_after = len(pending)
                result.chunk_outcomes.append(outcome)

        # Fell out of the loop: every window was reached, so nothing is unread.
        result.early_stopped = not pending and early_stop
        result.pages_never_read = []

    async def _search_chunk(
        self,
        body: Sequence[Paragraph],
        pending: Sequence[ClauseSpec],
        all_clauses: Sequence[ClauseSpec],
    ) -> dict[str, list[str]]:
        # The full clause list lives in the system prompt and is byte-identical
        # across every chunk, so a provider-side prefix cache can hit. Only the
        # short outstanding list and the chunk text vary.
        # The taxonomy is reference material, not a permission list. It used to
        # say "the clause types you may report" while the schema enum admitted
        # only the outstanding ones - so the model was invited to name 23 things
        # and allowed to return 10, and anything from the other 13 was dropped
        # without trace. The reference list stays in the system prompt because
        # it is byte-identical across every chunk and a prefix cache can hit it;
        # what to report is an instruction, and instructions belong with the
        # data they apply to.
        system = "\n".join(
            [
                "You locate contract clauses in an extract of a document.",
                "",
                "The full clause taxonomy, for reference - what each type covers:",
                *[spec.as_prompt_line() for spec in all_clauses],
                "",
                "Rules:",
                "- Report only the clause types the request asks for. Others in "
                "the taxonomy above have already been located elsewhere.",
                "- Report a clause only when this extract actually contains its "
                "substance. A passing mention or cross-reference is not the clause.",
                "- Identify it by paragraph refs, never by quoting text.",
                "- Use only refs that appear in the extract, exactly as printed.",
                "- List every paragraph the clause spans, in order.",
                "- Report nothing if the extract contains none of them. An empty "
                "answer is correct and expected.",
            ]
        )
        prompt = "\n".join(
            [
                "Report only these clause types, and no others:",
                *[f"- {spec.clause}" for spec in pending],
                "",
                "Extract, as '[ref] text':",
                "",
                *[f"[{paragraph.ref}] {paragraph.content}" for paragraph in body],
            ]
        )
        schema = {
            "type": "object",
            "properties": {
                "clauses": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "clause": {
                                "type": "string",
                                "enum": [spec.clause for spec in pending],
                            },
                            "paragraph_refs": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["clause", "paragraph_refs"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["clauses"],
            "additionalProperties": False,
        }

        try:
            response = await self._structured(system=system, prompt=prompt, schema=schema)
        except Exception as exc:  # noqa: BLE001 - one bad chunk must not end the run
            logger.warning("docpipeline_chunk_search_failed", error=str(exc)[:300])
            return {}

        allowed = {spec.clause for spec in pending}
        in_chunk = {paragraph.ref for paragraph in body}
        found: dict[str, list[str]] = {}
        for entry in response.data.get("clauses") or []:
            if not isinstance(entry, dict):
                continue
            clause = str(entry.get("clause") or "").strip()
            if clause not in allowed or clause in found:
                continue
            # Refs the model invented are dropped rather than trusted: a
            # fabricated ref would put a real clause name against text that is
            # not there. Refs it merely formatted differently are kept - the
            # extract is printed as "[9.3] text", so the bracketed form is what
            # a careful model copies back, and rejecting it would discard every
            # correct answer while looking exactly like "found nothing".
            refs = [
                cleaned
                for cleaned in (_clean_ref(ref) for ref in (entry.get("paragraph_refs") or []))
                if cleaned in in_chunk
            ]
            if refs:
                found[clause] = refs
        return found


# ---------------------------------------------------------------- helpers
def _clean_ref(value: object) -> str:
    """A paragraph ref as this module writes them: ``9.3``, no brackets.

    Prompts print the extract as ``[9.3] text``, so a model that copies the ref
    faithfully returns ``"[9.3]"``. Both forms mean the same paragraph.
    """
    return str(value).strip().strip("[]").strip()


def _normalise(text: str) -> str:
    """Heading or clause name reduced to comparable form."""
    lowered = text.strip().lower()
    lowered = _LEADING_NUMBER.sub("", lowered)
    lowered = _NON_ALNUM.sub(" ", lowered)
    return _WHITESPACE.sub(" ", lowered).strip()


def _normalised_lookup(clauses: Sequence[ClauseSpec]) -> dict[str, str]:
    """Normalised aliases -> clause name.

    Several taxonomy entries pack alternatives into one name with a slash
    (``Confidentiality/NDA``, ``Warranties/representations``); each side is a
    heading someone would plausibly write, so both are registered.
    """
    lookup: dict[str, str] = {}
    for spec in clauses:
        aliases = [spec.clause, *spec.clause.split("/")]
        for alias in aliases:
            key = _normalise(alias)
            if key:
                lookup.setdefault(key, spec.clause)
    return lookup


def _ordered(
    taken: dict[str, tuple[Paragraph, str]], paragraphs: list[Paragraph]
) -> list[tuple[str, tuple[Paragraph, str]]]:
    """Matched clauses in document order, so the report reads top to bottom."""
    position = {paragraph.ref: index for index, paragraph in enumerate(paragraphs)}
    return sorted(taken.items(), key=lambda item: position.get(item[1][0].ref, 0))


def _extent_from_heading(heading: Paragraph, paragraphs: list[Paragraph]) -> tuple[Paragraph, ...]:
    """The heading plus everything under it, up to the next heading.

    Crosses page boundaries, which is why the destination column for page
    numbers is an array.
    """
    try:
        start = paragraphs.index(heading)
    except ValueError:  # pragma: no cover - heading always comes from the list
        return (heading,)

    extent = [heading]
    for paragraph in paragraphs[start + 1 :]:
        if paragraph.ends_clause_extent:
            break
        extent.append(paragraph)
    return tuple(extent)


def _repair_boundary(
    selected: list[Paragraph],
    body: Sequence[Paragraph],
    paragraphs: list[Paragraph],
) -> tuple[list[Paragraph], bool]:
    """Extend a clause that runs off the end of its chunk.

    A clause straddling a four-page boundary is only half-visible to the call
    that found it. Rather than pay for overlapping windows, the extent is
    extended past the chunk edge up to the next heading - the model already told
    us where the clause starts, and the document structure says where it ends.

    The cap matters. Schedules, annexures and signature pages can run for pages
    without a single heading, so "extend to the next heading" once swallowed 56
    paragraphs across five pages into one clause. A clause continuing past a
    page or so of unbroken text is not a clause continuing; it is the repair
    losing its footing, and stopping short is the safer error.
    """
    if not selected or not body:
        return selected, False
    if selected[-1].ref != body[-1].ref:
        return selected, False

    try:
        start = paragraphs.index(selected[-1])
    except ValueError:  # pragma: no cover
        return selected, False

    extended = list(selected)
    for paragraph in paragraphs[start + 1 :]:
        if paragraph.ends_clause_extent:
            break
        if len(extended) - len(selected) >= MAX_REPAIR_PARAGRAPHS:
            break
        if paragraph.page_number > selected[-1].page_number + 1:
            break
        extended.append(paragraph)
    return extended, len(extended) > len(selected)


def _windows(pages: list[PageContent], size: int, overlap: int = 0) -> list[list[PageContent]]:
    """Consecutive page windows, optionally re-reading the last ``overlap`` pages.

    Overlap exists for the clause whose *identifying* sentence sits on one side
    of a boundary and whose substance sits on the other. Boundary repair already
    rescues a clause that starts near the end of a window; it cannot rescue one
    the model never recognised because the window cut it in half.

    The cost is real - an overlap of 1 on 4-page windows re-reads a quarter of
    the document - so it is a parameter rather than a default.
    """
    size = max(1, size)
    step = max(1, size - max(0, overlap))

    windows: list[list[PageContent]] = []
    for start in range(0, len(pages), step):
        window = pages[start : start + size]
        if not window:
            break
        windows.append(window)
        if start + size >= len(pages):
            # The final window already reaches the end; stepping again would
            # only re-read its tail.
            break
    return windows

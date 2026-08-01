"""Deciding whether a retrieved passage is one the case was asking for.

Every retrieval metric in this package reduces to this one question, so getting
it wrong moves every number at once. Two properties are non-negotiable:

**Precedence, not union.** A case that names clause ids is judged on clause ids.
Only when it names none does the judgement fall back to contract-level, and only
then to headings. Taking the union instead would mean a case naming both a clause
and its contract counts *every* passage from that contract as relevant - which
inflates recall towards 1.0 for precisely the cases whose authors did the most
work to be precise.

**Graded, not binary, where the expectation supports it.** nDCG needs to
distinguish "the exact clause asked for" from "the right contract, wrong page".
Both are better than nothing and the second is worse than the first, so relevance
is a small integer rather than a boolean. Metrics that need a boolean threshold
it; metrics that can use the grade use it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum

from app.evaluation.dataset.models import GoldenExpectation
from app.evaluation.runner.result import RetrievedItem

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


class Grade(IntEnum):
    """How relevant a passage is. Ordered, and the order is the point.

    The values feed nDCG's gain function directly, so the gaps between them are a
    statement about how much better an exact clause match is than a page match.
    Kept small and evenly spaced rather than tuned: an unjustified weighting is
    harder to argue with than an obvious one, which is exactly why it should not
    be smuggled into a constant.
    """

    NONE = 0
    #: Right contract, but nothing more specific was confirmed.
    CONTRACT = 1
    #: Right contract and one of the expected pages.
    PAGE = 2
    #: The section heading the case named.
    HEADING = 3
    #: The exact clause or chunk id the case named. Unambiguous.
    EXACT = 4


#: At or above this, a passage counts as relevant for recall and precision.
#: Contract-level is included: for a "which agreements mention X" question the
#: contract *is* the answer, and excluding it would score those cases at zero
#: however well they performed.
RELEVANT_AT = Grade.CONTRACT


def normalise(value: str | None) -> str:
    """Reduce a heading to comparable form.

    Headings come from three places - the dataset author's typing, the parser's
    extraction, and the document itself - and they disagree on case, punctuation
    and whitespace far more often than on words.
    """
    return _NON_ALNUM.sub(" ", (value or "").strip().lower()).strip()


@dataclass(slots=True)
class RelevanceJudge:
    """Grades retrieved passages against one case's expectations."""

    expectation: GoldenExpectation

    def grade(self, item: RetrievedItem) -> Grade:
        """The strongest grade this passage earns."""
        expected = self.expectation

        if expected.clauses:
            # The precise expectation wins outright. `ref_id` is the clause or
            # chunk the vector represents; `chunk_id` is where a clause was read
            # from, and a case may legitimately name either.
            if item.ref_id in expected.clauses or (
                item.chunk_id is not None and item.chunk_id in expected.clauses
            ):
                return Grade.EXACT
            # Named clauses and this is not one of them. Fall through only if the
            # case *also* offered a coarser expectation to be judged against.
            if not (expected.contracts or expected.headings):
                return Grade.NONE

        if expected.headings and self._heading_matches(item):
            return Grade.HEADING

        if expected.contracts and item.contract_id in expected.contracts:
            if expected.pages and self._page_matches(item):
                return Grade.PAGE
            if expected.pages:
                # Right contract, wrong page. Still evidence from the right
                # document, so not zero - a page number in a golden case is often
                # approximate, and scoring this as a miss punishes the dataset
                # author for being specific.
                return Grade.CONTRACT
            return Grade.CONTRACT

        return Grade.NONE

    def is_relevant(self, item: RetrievedItem) -> bool:
        return self.grade(item) >= RELEVANT_AT

    def grades(self, items: list[RetrievedItem]) -> list[Grade]:
        return [self.grade(item) for item in items]

    @property
    def total_relevant(self) -> int:
        """The recall denominator: how many distinct things should be found.

        Taken from the expectation rather than from what was retrieved, which is
        the whole point of a golden set - a denominator derived from the results
        would make recall unable to fall below 1.0.
        """
        expected = self.expectation
        if expected.clauses:
            return len(expected.clauses)
        if expected.headings:
            return len(expected.headings)
        return len(expected.contracts)

    # ------------------------------------------------------------------ detail
    def _heading_matches(self, item: RetrievedItem) -> bool:
        wanted = {normalise(heading) for heading in self.expectation.headings}
        candidates = {normalise(item.section_title), normalise(item.clause_number)}
        return bool(wanted & {candidate for candidate in candidates if candidate})

    def _page_matches(self, item: RetrievedItem) -> bool:
        if item.page_start is None:
            return False
        end = item.page_end if item.page_end is not None else item.page_start
        return any(item.page_start <= page <= end for page in self.expectation.pages)


def found_units(judge: RelevanceJudge, items: list[RetrievedItem]) -> int:
    """Distinct expected units the retrieved list covers.

    Counted as *units found*, not *passages that matched*. Three chunks of one
    expected clause is one unit, and counting it as three would let a chunking
    change inflate recall without retrieving anything new.
    """
    expected = judge.expectation

    if expected.clauses:
        wanted = set(expected.clauses)
        found = set()
        for item in items:
            if item.ref_id in wanted:
                found.add(item.ref_id)
            if item.chunk_id is not None and item.chunk_id in wanted:
                found.add(item.chunk_id)
        return len(found)

    if expected.headings:
        wanted_headings = {normalise(heading) for heading in expected.headings}
        found_headings = {
            normalise(item.section_title)
            for item in items
            if normalise(item.section_title) in wanted_headings
        }
        return len(found_headings)

    wanted_contracts = set(expected.contracts)
    return len({item.contract_id for item in items if item.contract_id in wanted_contracts})


__all__ = ["RELEVANT_AT", "Grade", "RelevanceJudge", "found_units", "normalise"]

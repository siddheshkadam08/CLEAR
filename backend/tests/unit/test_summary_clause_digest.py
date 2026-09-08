"""The clause-by-clause summary digest.

The digest is rendered as a table that a reviewer reads as a statement of what
the agreement says. Two properties therefore matter more than anything else
about it, and neither can be expressed in the JSON schema the provider enforces
(strict mode rejects `minItems`/`maxItems`, and no schema can say "only these
values"):

* **Nothing invented.** A row naming a clause extraction did not find would
  assert a term that is not in the contract - an indemnity that does not exist
  is worse than no summary at all.
* **Nothing omitted silently mis-ordered.** The table sits beside the clause
  tabs and has to read in the same order.

So both are enforced in `ExtractionEngine._digest`, and pinned here.
"""

from __future__ import annotations

import pytest

from app.ai.extraction.engine import ExtractionEngine
from app.ai.extraction.models import ExtractedClause, ExtractionResult


def _result(*clause_types: str) -> ExtractionResult:
    result = ExtractionResult()
    result.clauses = [
        ExtractedClause(clause_type=key, text=f"text for {key}") for key in clause_types
    ]
    return result


def _row(key: str, heading: str = "Heading", lines: list[str] | None = None) -> dict:
    # `is None`, not `or`: an empty list is a case under test and must survive.
    return {
        "clause_key": key,
        "heading": heading,
        "lines": ["A plain sentence."] if lines is None else lines,
    }


LABELS = {
    "confidentiality": "Confidentiality",
    "indemnification": "Indemnity",
    "termination_for_convenience": "Termination",
}


class TestOnlyFoundClauses:
    def test_a_row_for_an_unextracted_clause_is_dropped(self) -> None:
        """The failure this guard exists for."""
        result = _result("confidentiality")

        digest = ExtractionEngine._digest(
            [_row("confidentiality"), _row("indemnification")], result, LABELS
        )

        assert [entry.clause_key for entry in digest] == ["confidentiality"]

    def test_every_row_is_dropped_when_nothing_was_extracted(self) -> None:
        digest = ExtractionEngine._digest([_row("confidentiality")], _result(), LABELS)
        assert digest == []

    def test_an_unknown_key_is_dropped(self) -> None:
        """A hallucinated key that is not in the taxonomy at all."""
        digest = ExtractionEngine._digest(
            [_row("moon_landing_clause")], _result("confidentiality"), LABELS
        )
        assert digest == []

    def test_a_duplicated_key_appears_once(self) -> None:
        result = _result("confidentiality")
        digest = ExtractionEngine._digest(
            [_row("confidentiality", "First"), _row("confidentiality", "Second")],
            result,
            LABELS,
        )
        assert len(digest) == 1
        assert digest[0].heading == "First"


class TestOrdering:
    def test_rows_follow_the_extraction_order_not_the_model_order(self) -> None:
        result = _result("indemnification", "confidentiality", "termination_for_convenience")

        digest = ExtractionEngine._digest(
            [
                _row("termination_for_convenience"),
                _row("confidentiality"),
                _row("indemnification"),
            ],
            result,
            LABELS,
        )

        assert [entry.clause_key for entry in digest] == [
            "indemnification",
            "confidentiality",
            "termination_for_convenience",
        ]

    def test_repeated_clauses_of_one_type_produce_one_row(self) -> None:
        """Three confidentiality paragraphs are still one line in the table."""
        result = _result("confidentiality", "confidentiality", "indemnification")

        digest = ExtractionEngine._digest(
            [_row("confidentiality"), _row("indemnification")], result, LABELS
        )

        assert [entry.clause_key for entry in digest] == [
            "confidentiality",
            "indemnification",
        ]


class TestContent:
    def test_a_row_with_no_lines_is_dropped(self) -> None:
        """An empty row reads as 'we found nothing to say', which is not the case."""
        result = _result("confidentiality")
        assert ExtractionEngine._digest([_row("confidentiality", lines=[])], result, LABELS) == []

    def test_blank_lines_are_stripped(self) -> None:
        result = _result("confidentiality")
        digest = ExtractionEngine._digest(
            [_row("confidentiality", lines=["  ", "Real sentence.", ""])], result, LABELS
        )
        assert digest[0].lines == ["Real sentence."]

    def test_a_missing_heading_falls_back_to_the_clause_master_name(self) -> None:
        result = _result("indemnification")
        digest = ExtractionEngine._digest(
            [{"clause_key": "indemnification", "lines": ["Mutual."]}], result, LABELS
        )
        assert digest[0].heading == "Indemnity"

    def test_an_unlabelled_clause_falls_back_to_its_key(self) -> None:
        result = _result("exclusivity")
        digest = ExtractionEngine._digest(
            [{"clause_key": "exclusivity", "lines": ["Exclusive."]}], result, LABELS
        )
        assert digest[0].heading == "exclusivity"


class TestMalformedInput:
    @pytest.mark.parametrize("rows", [None, [], "not a list", [None], ["string"], [[]]])
    def test_garbage_does_not_raise(self, rows: object) -> None:
        """The summary is best-effort; a bad payload must not cost the extraction."""
        assert ExtractionEngine._digest(rows, _result("confidentiality"), LABELS) == []

    def test_a_row_without_a_key_is_dropped(self) -> None:
        result = _result("confidentiality")
        assert ExtractionEngine._digest([{"lines": ["Orphan."]}], result, LABELS) == []

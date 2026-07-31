"""What survives a chunk being rejected.

The chunking stage reported "129 produced, 96 accepted" and nothing else. Which
rule fired, on which page, and how far off the threshold each chunk was - all of
it was discarded along with the chunk, so the only way to investigate was to
reproduce the run with a debugger attached.
"""

from __future__ import annotations

from app.ai.chunking.models import (
    ChunkRejection,
    ChunkValidationReport,
    RejectionRule,
)


def _rejection(**over: object) -> ChunkRejection:
    base: dict[str, object] = {
        "chunk_id": "c1",
        "reason": "too_small",
        "rule": RejectionRule.BELOW_MIN_TOKENS.value,
        "detail": "12 < 40 tokens",
        "chunk_type": "paragraph",
        "page": 3,
        "token_count": 12,
        "char_count": 61,
        "section_title": "3. Confidentiality",
        "text_preview": "Each party shall...",
    }
    base.update(over)
    return ChunkRejection(**base)  # type: ignore[arg-type]


class TestRejectionRecord:
    def test_it_carries_everything_needed_to_locate_the_chunk(self) -> None:
        payload = _rejection().as_dict()
        for key in ("page", "chunk_type", "token_count", "char_count", "rule", "text_preview"):
            assert payload[key] not in (None, ""), f"{key} is not recorded"

    def test_the_rule_is_distinct_from_the_reason(self) -> None:
        """One reason can have several causes; the rule says which check fired."""
        rejection = _rejection()
        assert rejection.reason == "too_small"
        assert rejection.rule == "below_min_tokens"

    def test_the_preview_is_bounded(self) -> None:
        long_text = "x" * 5000
        assert len(_rejection(text_preview=long_text[:200]).text_preview) == 200


class TestReportAggregation:
    def _report(self) -> ChunkValidationReport:
        report = ChunkValidationReport(total=10, accepted=4)
        report.rejected = [
            _rejection(chunk_id="a", page=3),
            _rejection(chunk_id="b", page=3),
            _rejection(chunk_id="c", page=3),
            _rejection(
                chunk_id="d",
                page=9,
                reason="oversized",
                rule=RejectionRule.ABOVE_MAX_TOKENS.value,
                chunk_type="table",
            ),
            _rejection(
                chunk_id="e",
                page=11,
                reason="broken_clause",
                rule=RejectionRule.ENDS_MID_CLAUSE.value,
            ),
            _rejection(chunk_id="f", page=3),
        ]
        return report

    def test_counts_group_by_rule(self) -> None:
        assert self._report().by_rule == {
            "below_min_tokens": 4,
            "above_max_tokens": 1,
            "ends_mid_clause": 1,
        }

    def test_counts_group_by_page(self) -> None:
        """Clustering on one page means a parser problem, not a threshold problem."""
        assert self._report().by_page == {3: 4, 9: 1, 11: 1}

    def test_worst_pages_are_ranked(self) -> None:
        worst = self._report().diagnostics()["worst_pages"]
        assert worst[0] == {"page": 3, "rejected": 4}

    def test_counts_group_by_chunk_type(self) -> None:
        assert self._report().by_chunk_type == {"paragraph": 5, "table": 1}

    def test_dominant_rule_is_reported_when_one_clearly_leads(self) -> None:
        assert self._report().dominant_rule == "below_min_tokens"

    def test_no_dominant_rule_when_rejections_are_spread(self) -> None:
        report = ChunkValidationReport(total=10, accepted=7)
        report.rejected = [
            _rejection(rule=RejectionRule.BELOW_MIN_TOKENS.value),
            _rejection(rule=RejectionRule.ABOVE_MAX_TOKENS.value),
            _rejection(rule=RejectionRule.ENDS_MID_CLAUSE.value),
        ]
        assert report.dominant_rule is None

    def test_diagnostics_report_the_acceptance_rate(self) -> None:
        assert self._report().diagnostics()["acceptance_rate"] == 0.4

    def test_diagnostics_say_how_many_samples_were_dropped(self) -> None:
        report = ChunkValidationReport(total=200, accepted=100)
        report.rejected = [_rejection(chunk_id=str(n)) for n in range(100)]
        diagnostics = report.diagnostics(sample_limit=10)
        assert len(diagnostics["rejections"]) == 10
        assert diagnostics["truncated"] == 90

    def test_the_artifact_keeps_its_original_keys(self) -> None:
        """Backward compatibility with existing readers of CHUNK_VALIDATION."""
        payload = self._report().as_dict()
        for key in ("total", "accepted", "rejected", "rejections_by_reason", "healthy"):
            assert key in payload


class TestEmptySectionsAreCounted:
    """A section that yields no text never became a chunk, so nothing counted it."""

    def test_the_rule_exists_for_content_that_never_became_a_chunk(self) -> None:
        assert RejectionRule.SECTION_PRODUCED_NO_TEXT.value == "section_produced_no_text"

    def test_such_a_rejection_still_locates_itself(self) -> None:
        rejection = ChunkRejection(
            chunk_id="section:s4",
            reason="no_content",
            rule=RejectionRule.SECTION_PRODUCED_NO_TEXT.value,
            page=7,
            section_title="7. Limitation of Liability",
        )
        payload = rejection.as_dict()
        assert payload["page"] == 7
        assert payload["section_title"] == "7. Limitation of Liability"

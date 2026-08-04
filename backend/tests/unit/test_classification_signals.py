"""Classification evidence and the fallback path.

Two defects motivated these. The result reported *that* a fallback happened but
not why, so a tie-break provider outage and a genuinely ambiguous contract looked
identical - and they need opposite responses, retry versus human review. And the
matched evidence was a single list of prefixed strings, which is readable but not
answerable: "which keywords fired" and "which rule families fired" are the
questions asked when a profile's hints need tuning.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.ai.classification.classifier import (
    _FALLBACK_EXPLANATIONS,
    ClassificationResult,
    ClassificationSignal,
    DocumentClassifier,
    FallbackReason,
)


class _Profile:
    """Enough of DocumentProfile to score against."""

    def __init__(self, key: str, hints: dict[str, Any] | None = None, **over: Any) -> None:
        self.id = uuid.uuid4()
        self.key = key
        self.name = over.get("name", key.replace("_", " ").title())
        self.version = "1.0.0"
        self.agreement_type = over.get("agreement_type", key)
        self.agreement_subtype = None
        self.classification_hints = hints or {}
        self.is_default = over.get("is_default", False)
        self.priority = over.get("priority", 10)
        self.mandatory_clauses = []
        self.chunk_strategy = "semantic"
        self.confidence_threshold = 0.85


def _score(profile: _Profile, **text: str) -> ClassificationSignal:
    classifier = DocumentClassifier.__new__(DocumentClassifier)
    return classifier._score_profile(
        profile,  # type: ignore[arg-type]
        title_text=text.get("title", ""),
        head_text=text.get("head", ""),
        headings=text.get("headings", ""),
    )


class TestMatchedEvidence:
    def test_keywords_are_the_phrases_themselves(self) -> None:
        """Not `phrase:x` - the caller wants the term, not a rendered label."""
        profile = _Profile("msa", {"required_phrases": ["statement of work", "service levels"]})
        signal = _score(profile, head="this statement of work governs service levels")
        assert set(signal.matched_keywords) == {"statement of work", "service levels"}

    def test_rules_name_the_families_that_fired(self) -> None:
        profile = _Profile(
            "msa",
            {
                "title_patterns": ["master services agreement"],
                "required_phrases": ["statement of work"],
                "heading_patterns": ["term and termination"],
            },
        )
        signal = _score(
            profile,
            title="master services agreement",
            head="statement of work",
            headings="term and termination",
        )
        assert set(signal.matched_rules) == {
            "title_patterns",
            "required_phrases",
            "heading_patterns",
        }

    def test_a_rule_that_did_not_fire_is_not_reported(self) -> None:
        profile = _Profile(
            "msa",
            {"title_patterns": ["master services agreement"], "required_phrases": ["indemnity"]},
        )
        signal = _score(profile, title="master services agreement", head="nothing relevant")
        assert signal.matched_rules == ["title_patterns"]
        assert "indemnity" not in signal.matched_keywords

    def test_a_veto_reports_the_blocking_rule(self) -> None:
        profile = _Profile("nda", {"negative_phrases": ["the premises"]})
        signal = _score(profile, head="the premises shall be maintained")
        assert signal.score == 0.0
        assert signal.blocked_by == ["the premises"]
        assert signal.matched_rules == ["negative_phrases"]

    def test_matched_labels_are_still_produced(self) -> None:
        """Existing readers of `matched` must not break."""
        profile = _Profile("msa", {"required_phrases": ["statement of work"]})
        signal = _score(profile, head="statement of work")
        assert signal.matched == ["phrase:statement of work"]


class TestResultShape:
    """The contract the brief asks the stage to expose."""

    def _result(self, **over: Any) -> ClassificationResult:
        winner = _Profile("msa")
        signals = [
            ClassificationSignal(
                profile_key="msa",
                profile_id=str(winner.id),
                agreement_type="msa",
                score=0.91,
                matched_keywords=["statement of work"],
                matched_rules=["required_phrases"],
            ),
            ClassificationSignal(
                profile_key="nda",
                profile_id=str(uuid.uuid4()),
                agreement_type="nda",
                score=0.22,
            ),
        ]
        base: dict[str, Any] = {
            "profile": winner,
            "agreement_type": "msa",
            "agreement_subtype": None,
            "confidence": 0.91,
            "method": "rules",
            "signals": signals,
        }
        base.update(over)
        return ClassificationResult(**base)  # type: ignore[arg-type]

    def test_classification_is_the_selected_profile_key(self) -> None:
        assert self._result().classification == "msa"

    def test_top_predictions_are_ranked_with_their_evidence(self) -> None:
        top = self._result().top_predictions
        assert [p["profile_key"] for p in top] == ["msa", "nda"]
        assert top[0]["matched_rules"] == ["required_phrases"]

    def test_matched_evidence_comes_from_the_winner_not_the_field(self) -> None:
        """The runner-up's keywords must not be attributed to the decision."""
        result = self._result()
        assert result.matched_keywords == ["statement of work"]
        assert result.matched_rules == ["required_phrases"]

    def test_fallback_used_is_false_on_a_real_match(self) -> None:
        result = self._result()
        assert result.fallback_used is False
        assert result.fallback_reason is FallbackReason.NONE

    def test_artifact_carries_the_full_contract(self) -> None:
        artifact = self._result().as_artifact()
        for key in (
            "classification",
            "confidence",
            "top_predictions",
            "matched_keywords",
            "matched_rules",
            "fallback_used",
            "fallback_reason",
        ):
            assert key in artifact, f"artifact is missing {key}"

    def test_artifact_keeps_the_original_keys(self) -> None:
        """Backward compatibility: existing readers index these."""
        artifact = self._result().as_artifact()
        for key in ("profile_key", "method", "is_fallback", "candidates", "agreement_type"):
            assert key in artifact


class TestNoSilentFallback:
    def test_every_reason_has_operator_facing_text(self) -> None:
        for reason in FallbackReason:
            assert _FALLBACK_EXPLANATIONS[reason.value].strip()

    def test_a_provider_outage_is_not_described_as_an_ambiguous_document(self) -> None:
        """The defect: an outage read as "no type matched with confidence"."""
        outage = _FALLBACK_EXPLANATIONS[FallbackReason.LLM_UNAVAILABLE.value].lower()
        ambiguous = _FALLBACK_EXPLANATIONS[FallbackReason.AMBIGUOUS.value].lower()
        assert outage != ambiguous
        assert "provider" in outage or "could not be reached" in outage
        assert "reprocess" in outage

    def test_a_missing_forced_profile_says_the_request_was_not_honoured(self) -> None:
        text = _FALLBACK_EXPLANATIONS[FallbackReason.FORCED_PROFILE_MISSING.value]
        assert "not" in text.lower()

    def test_missing_rules_is_reported_as_configuration_not_low_confidence(self) -> None:
        text = _FALLBACK_EXPLANATIONS[FallbackReason.NO_RULES_CONFIGURED.value].lower()
        assert "configuration" in text or "configure" in text

    @pytest.mark.asyncio
    async def test_tiebreak_reports_unavailable_when_the_provider_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.errors import ProviderError

        def _boom() -> Any:
            raise ProviderError("gateway down", provider="openai")

        monkeypatch.setattr("app.ai.rag.providers.get_inference_provider", _boom)

        classifier = DocumentClassifier.__new__(DocumentClassifier)
        result, reason = await classifier._llm_tiebreak(
            _StubDocument(),
            [_Profile("msa")],
            [],
            "Some Agreement",  # type: ignore[arg-type]
        )
        assert result is None
        assert reason is FallbackReason.LLM_UNAVAILABLE


class TestClassifyEndToEnd:
    """The reason has to survive the whole call, not just the helper."""

    @staticmethod
    def _classifier(profiles: list[_Profile]) -> DocumentClassifier:
        classifier = DocumentClassifier.__new__(DocumentClassifier)

        async def _load(_project_id: Any) -> list[Any]:
            return profiles  # type: ignore[return-value]

        classifier._load_profiles = _load  # type: ignore[method-assign]
        return classifier

    @pytest.mark.asyncio
    async def test_unscored_document_falls_back_with_low_score(self) -> None:
        profiles = [
            _Profile("msa", {"required_phrases": ["statement of work"]}),
            _Profile("default", {"required_phrases": ["zzz"]}, is_default=True),
        ]
        result = await self._classifier(profiles).classify(
            _StubDocument(),
            use_llm_tiebreak=False,  # type: ignore[arg-type]
        )
        assert result.fallback_used is True
        assert result.fallback_reason is FallbackReason.LLM_DISABLED
        assert result.profile.key == "default"

    @pytest.mark.asyncio
    async def test_no_hints_anywhere_is_a_configuration_fault(self) -> None:
        """Distinct from an ambiguous document, and fixed somewhere else entirely."""
        profiles = [_Profile("msa"), _Profile("default", is_default=True)]
        result = await self._classifier(profiles).classify(
            _StubDocument(),
            use_llm_tiebreak=False,  # type: ignore[arg-type]
        )
        assert result.fallback_reason is FallbackReason.NO_RULES_CONFIGURED

    @pytest.mark.asyncio
    async def test_a_missing_forced_profile_is_reported_not_swallowed(self) -> None:
        """The uploader asked for a profile that does not exist; they must be told."""
        profiles = [_Profile("msa", {"required_phrases": ["x"]}, is_default=True)]
        result = await self._classifier(profiles).classify(
            _StubDocument(),  # type: ignore[arg-type]
            forced_profile_key="does_not_exist",
            use_llm_tiebreak=False,
        )
        assert result.fallback_used is True
        assert result.fallback_reason is FallbackReason.FORCED_PROFILE_MISSING

    @pytest.mark.asyncio
    async def test_a_confident_match_does_not_fall_back(self) -> None:
        profiles = [
            _Profile("msa", {"title_patterns": ["master services agreement"]}),
            _Profile("default", {"required_phrases": ["zzz"]}, is_default=True),
        ]
        document = _StubDocument()
        document.metadata.file_name = "Master Services Agreement.pdf"

        result = await self._classifier(profiles).classify(
            document,
            use_llm_tiebreak=False,  # type: ignore[arg-type]
        )
        assert result.fallback_used is False
        assert result.classification == "msa"
        assert result.method == "rules"
        assert "master services agreement" in result.matched_keywords


class _StubDocument:
    """Minimal CDM stand-in for the tie-break's text sampling."""

    def __init__(self) -> None:
        self.metadata = _StubDocument._Meta()
        self.paragraphs: list[Any] = []
        self.sections: list[Any] = []

    class _Meta:
        language = "en"
        file_name = "agreement.pdf"

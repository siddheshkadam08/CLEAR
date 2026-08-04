"""Document classifier and Document Intelligence Profile selector (§11).

Classification decides *what kind of contract this is*, which decides *which profile
processes it*, which decides everything after: prompts, mandatory clauses, chunking
strategy, risk weighting, compliance packs, review triggers.

The classifier is **rule-scored, not hardcoded**. Every signal comes from a
profile's ``classification_hints`` in the database, so adding a document type is a
new profile row and no code change (§11 frozen rule). Signals are combined rather
than short-circuited because no single one is reliable across a real repository:

* **Title patterns** - strongest signal when present, but many uploads are named
  ``scan_0142.pdf``.
* **Required phrases** - the vocabulary a document type must contain.
* **Negative phrases** - vocabulary that rules a type out (an NDA that mentions
  "the Premises" is probably a lease with a confidentiality clause).
* **Section headings** - a structural signal that survives bad filenames.

An LLM tie-break runs only when rule scoring is inconclusive, so the common case
costs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.cdm.models import CanonicalDocument
from app.core import metrics
from app.core.enums import AgreementType
from app.core.logging import get_logger
from app.core.versions import (
    CLASSIFICATION_ENGINE_VERSION,
    CLASSIFICATION_TAXONOMY_VERSION,
)
from app.models.profile import DocumentProfile

logger = get_logger(__name__)

#: Weights for the four rule signals. Title is weighted highest because when a
#: filename or heading does say "Master Services Agreement", it is almost always
#: right; phrases carry the load when it does not.
_WEIGHT_TITLE = 0.45
_WEIGHT_PHRASES = 0.30
_WEIGHT_HEADINGS = 0.25

#: Score below which the classification is treated as inconclusive.
_DEFAULT_MIN_SCORE = 0.40

#: How much document text the LLM tie-break sees. The first pages carry the title,
#: parties and recitals - the parts that identify a contract type.
_LLM_SAMPLE_CHARS = 6000

#: Operator-facing text per reason. These reach the UI as review notes, so each one
#: says what to *do*, not only what happened.
_FALLBACK_EXPLANATIONS: dict[str, str] = {
    "low_score": "No document type matched with sufficient confidence.",
    "ambiguous": "The top two document types scored too closely to separate.",
    "llm_unavailable": (
        "The document was ambiguous and the tie-break model could not be reached, "
        "so the type was not determined. This is a provider fault, not a property "
        "of the document - reprocessing may classify it correctly."
    ),
    "llm_invalid_response": (
        "The document was ambiguous and the tie-break model returned a document "
        "type that is not configured, so its answer was discarded."
    ),
    "llm_disabled": (
        "Rule scoring was inconclusive and the tie-break model was disabled for this run."
    ),
    "forced_profile_missing": (
        "The uploader pinned a document profile that is not configured. The "
        "document was NOT processed with the requested profile."
    ),
    "no_rules_configured": (
        "No document profile declares any classification hints, so nothing can "
        "score above zero. This is a configuration fault - seed or configure the "
        "profiles' classification_hints."
    ),
    "none": "Classified without a fallback.",
}


#: How many candidates ``top_predictions`` reports. Enough to see the runner-up
#: and why it lost, without dumping every profile in the system.
_TOP_PREDICTIONS = 5


class FallbackReason(StrEnum):
    """Why the default profile was applied.

    A named reason rather than a prose string: the fallback used to report "no
    document type matched with sufficient confidence" no matter what actually
    happened, so a provider outage during the tie-break was indistinguishable
    from a genuinely ambiguous document. The two need completely different
    responses - one is retried, the other is reviewed.
    """

    NONE = "none"
    #: Nothing scored above its own threshold.
    LOW_SCORE = "low_score"
    #: Top two candidates too close to separate.
    AMBIGUOUS = "ambiguous"
    #: The tie-break model was unreachable or errored.
    LLM_UNAVAILABLE = "llm_unavailable"
    #: The model answered with a profile key that does not exist.
    LLM_INVALID_RESPONSE = "llm_invalid_response"
    #: The tie-break was disabled by the caller.
    LLM_DISABLED = "llm_disabled"
    #: The uploader pinned a profile key that is not configured.
    FORCED_PROFILE_MISSING = "forced_profile_missing"
    #: No profile declares any classification hints - nothing can ever score.
    NO_RULES_CONFIGURED = "no_rules_configured"


@dataclass(slots=True)
class ClassificationSignal:
    """One profile's score and the evidence behind it."""

    profile_key: str
    profile_id: str
    agreement_type: str
    score: float
    title_score: float = 0.0
    phrase_score: float = 0.0
    heading_score: float = 0.0
    matched: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    #: The literal phrases found in the document, without the rule prefix.
    matched_keywords: list[str] = field(default_factory=list)
    #: Which rule families contributed: title / required_phrases / heading_patterns.
    matched_rules: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_key": self.profile_key,
            "agreement_type": self.agreement_type,
            "score": round(self.score, 4),
            "title": round(self.title_score, 4),
            "phrases": round(self.phrase_score, 4),
            "headings": round(self.heading_score, 4),
            "matched": self.matched[:12],
            "matched_keywords": self.matched_keywords[:12],
            "matched_rules": self.matched_rules,
            "blocked_by": self.blocked_by,
        }


@dataclass(slots=True)
class ClassificationResult:
    """What the classification stage persists and passes forward."""

    profile: DocumentProfile
    agreement_type: str
    agreement_subtype: str | None
    confidence: float
    method: str
    #: Every candidate's score, so a wrong classification is diagnosable.
    signals: list[ClassificationSignal] = field(default_factory=list)
    detected_title: str | None = None
    language: str | None = None
    #: True when the profile is the configured default rather than a real match.
    is_fallback: bool = False
    #: *Why* the fallback was used. Never NONE when ``is_fallback`` is true.
    fallback_reason: FallbackReason = FallbackReason.NONE
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ views
    @property
    def classification(self) -> str:
        """The decision itself: the profile key that will process the document."""
        return self.profile.key

    @property
    def fallback_used(self) -> bool:
        return self.is_fallback

    @property
    def top_predictions(self) -> list[dict[str, Any]]:
        """The ranked candidates, best first.

        Reported even on a confident match. When a classification is wrong, the
        question is always "what was the runner-up and why did it lose", and that
        is unanswerable after the fact unless it was recorded at the time.
        """
        return [
            {
                "profile_key": signal.profile_key,
                "agreement_type": signal.agreement_type,
                "score": round(signal.score, 4),
                "matched_rules": signal.matched_rules,
                "matched_keywords": signal.matched_keywords[:8],
                "blocked_by": signal.blocked_by,
            }
            for signal in self.signals[:_TOP_PREDICTIONS]
        ]

    @property
    def matched_keywords(self) -> list[str]:
        """Phrases that fired for the *winning* profile."""
        winner = self._winning_signal()
        return list(winner.matched_keywords) if winner else []

    @property
    def matched_rules(self) -> list[str]:
        """Rule families that fired for the winning profile."""
        winner = self._winning_signal()
        return list(winner.matched_rules) if winner else []

    def _winning_signal(self) -> ClassificationSignal | None:
        return next((s for s in self.signals if s.profile_key == self.profile.key), None)

    def as_artifact(self) -> dict[str, Any]:
        return {
            "profile_id": str(self.profile.id),
            "profile_key": self.profile.key,
            "profile_version": self.profile.version,
            "profile_name": self.profile.name,
            "agreement_type": self.agreement_type,
            "agreement_subtype": self.agreement_subtype,
            # The brief's contract, alongside the original keys so existing
            # readers of this artifact keep working.
            "classification": self.classification,
            "confidence": round(self.confidence, 4),
            "top_predictions": self.top_predictions,
            "matched_keywords": self.matched_keywords,
            "matched_rules": self.matched_rules,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason.value,
            "method": self.method,
            "is_fallback": self.is_fallback,
            "detected_title": self.detected_title,
            "language": self.language,
            "candidates": [signal.as_dict() for signal in self.signals],
            "notes": self.notes,
            "engine_version": CLASSIFICATION_ENGINE_VERSION,
            "taxonomy_version": CLASSIFICATION_TAXONOMY_VERSION,
            "mandatory_clauses": list(self.profile.mandatory_clauses or []),
            "chunk_strategy": str(self.profile.chunk_strategy),
        }


class DocumentClassifier:
    """Scores a CDM against the available profiles and picks one."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def classify(
        self,
        document: CanonicalDocument,
        *,
        project_id: Any = None,
        forced_profile_key: str | None = None,
        hinted_type: str | None = None,
        use_llm_tiebreak: bool = True,
    ) -> ClassificationResult:
        """Classify a document and select its profile.

        ``forced_profile_key`` short-circuits everything - an uploader who states the
        document type for a homogeneous batch should not be second-guessed.
        """
        profiles = await self._load_profiles(project_id)
        if not profiles:
            raise LookupError(
                "No active Document Intelligence Profiles are configured. Run the seed step."
            )

        title = self._detect_title(document)
        language = document.metadata.language

        if forced_profile_key:
            forced = next((p for p in profiles if p.key == forced_profile_key), None)
            if forced is not None:
                logger.info("classification_forced", profile_key=forced_profile_key)
                return ClassificationResult(
                    profile=forced,
                    agreement_type=forced.agreement_type,
                    agreement_subtype=forced.agreement_subtype,
                    confidence=1.0,
                    method="forced",
                    detected_title=title,
                    language=language,
                    notes=[f"Profile pinned to '{forced_profile_key}' by the uploader."],
                )
            logger.warning("forced_profile_not_found", profile_key=forced_profile_key)

        signals = self._score_all(document, profiles, title=title)
        signals.sort(key=lambda signal: signal.score, reverse=True)

        best = signals[0] if signals else None
        threshold = self._threshold(profiles, best)

        # An uploader-supplied type is a hint, not an override: it breaks a tie in its
        # own favour but does not beat a confident contrary match.
        if hinted_type and best is not None and best.score < threshold:
            hinted = next((p for p in profiles if p.agreement_type == hinted_type), None)
            if hinted is not None:
                logger.info("classification_from_hint", hinted_type=hinted_type)
                return ClassificationResult(
                    profile=hinted,
                    agreement_type=hinted.agreement_type,
                    agreement_subtype=hinted.agreement_subtype,
                    confidence=0.7,
                    method="upload_hint",
                    signals=signals,
                    detected_title=title,
                    language=language,
                    notes=["Rule scoring was inconclusive; used the uploader's stated type."],
                )

        if best is not None and best.score >= threshold and not self._is_close_call(signals):
            profile = next(p for p in profiles if str(p.id) == best.profile_id)
            return ClassificationResult(
                profile=profile,
                agreement_type=profile.agreement_type,
                agreement_subtype=profile.agreement_subtype,
                confidence=min(best.score, 0.99),
                method="rules",
                signals=signals,
                detected_title=title,
                language=language,
            )

        # Why the rules did not decide. Established *before* the tie-break so it is
        # not overwritten by whatever happens there.
        rule_reason = (
            FallbackReason.AMBIGUOUS if self._is_close_call(signals) else FallbackReason.LOW_SCORE
        )
        if not any(p.classification_hints for p in profiles):
            # Nothing can ever score above zero. Distinct from "this document is
            # ambiguous" - it is a configuration fault, and reporting it as low
            # confidence sends an operator to review documents instead of profiles.
            rule_reason = FallbackReason.NO_RULES_CONFIGURED

        # Inconclusive: either nothing scored well, or the top two are too close to
        # separate. Both are cases where an LLM's judgement is worth its cost.
        reason = rule_reason
        if use_llm_tiebreak:
            resolved, reason = await self._llm_tiebreak(document, profiles, signals, title)
            if resolved is not None:
                return resolved
            # A tie-break that failed for an infrastructure reason must not be
            # reported as an ambiguous document.
            if reason is FallbackReason.NONE:
                reason = rule_reason
        elif rule_reason is not FallbackReason.NO_RULES_CONFIGURED:
            # A misconfiguration outranks "the tie-break was off" - the latter is a
            # choice, the former is broken setup.
            reason = FallbackReason.LLM_DISABLED

        if forced_profile_key:
            # The uploader named a profile that does not exist. Silently classifying
            # by rules instead means a batch pinned to the wrong key processes with
            # the wrong mandatory clauses and nobody is told.
            reason = FallbackReason.FORCED_PROFILE_MISSING

        fallback = self._fallback_profile(profiles)
        explanation = _FALLBACK_EXPLANATIONS[reason]
        # Warning, not info: every fallback is a document processed with a profile
        # nobody chose. At info level this scrolled past unnoticed while every
        # affected contract got the default clause set.
        logger.warning(
            "classification_fallback",
            profile_key=fallback.key,
            best_score=round(best.score, 3) if best else 0.0,
            best_candidate=best.profile_key if best else None,
            fallback_reason=reason.value,
            candidates=len(signals),
        )
        metrics.classification_fallback_total.labels(reason=reason.value).inc()

        return ClassificationResult(
            profile=fallback,
            agreement_type=fallback.agreement_type,
            agreement_subtype=fallback.agreement_subtype,
            # Low confidence is the signal that drives human review, so it is reported
            # honestly rather than inflated to look decisive.
            confidence=round(best.score, 4) if best else 0.0,
            method="fallback",
            signals=signals,
            detected_title=title,
            language=language,
            is_fallback=True,
            fallback_reason=reason,
            notes=[explanation, f"Applied the default profile '{fallback.key}'."],
        )

    # =========================================================================
    # Profile loading
    # =========================================================================
    async def _load_profiles(self, project_id: Any) -> list[DocumentProfile]:
        """Active profiles: project-specific ones plus platform-wide ones.

        A project-scoped profile takes precedence over a global one with the same key,
        so a project can specialise a document type without affecting other projects.
        """
        stmt = select(DocumentProfile).where(
            DocumentProfile.is_active.is_(True),
            DocumentProfile.deleted_at.is_(None),
        )
        if project_id is not None:
            stmt = stmt.where(
                (DocumentProfile.project_id == project_id) | (DocumentProfile.project_id.is_(None))
            )
        else:
            stmt = stmt.where(DocumentProfile.project_id.is_(None))

        rows = list((await self.db.execute(stmt)).scalars().all())

        by_key: dict[str, DocumentProfile] = {}
        for profile in sorted(
            rows,
            # Project-scoped first, then higher priority - so the winner of each key
            # is the most specific, highest-priority profile.
            key=lambda p: (p.project_id is None, -p.priority),
        ):
            by_key.setdefault(profile.key, profile)
        return list(by_key.values())

    @staticmethod
    def _fallback_profile(profiles: list[DocumentProfile]) -> DocumentProfile:
        """The profile marked default, or the highest-priority one."""
        default = next((p for p in profiles if p.is_default), None)
        if default is not None:
            return default
        return max(profiles, key=lambda p: p.priority)

    @staticmethod
    def _threshold(profiles: list[DocumentProfile], best: ClassificationSignal | None) -> float:
        """Minimum score to accept a match, from the winning profile's own hints."""
        if best is None:
            return _DEFAULT_MIN_SCORE
        profile = next((p for p in profiles if str(p.id) == best.profile_id), None)
        if profile is None:
            return _DEFAULT_MIN_SCORE
        hints = profile.classification_hints or {}
        try:
            return float(hints.get("min_score", _DEFAULT_MIN_SCORE))
        except (TypeError, ValueError):
            return _DEFAULT_MIN_SCORE

    @staticmethod
    def _is_close_call(signals: list[ClassificationSignal]) -> bool:
        """Are the top two candidates too close to separate confidently?

        Picking arbitrarily between an MSA and a Vendor Agreement produces the wrong
        mandatory-clause set and a misleading "missing clauses" result, so a near-tie
        is escalated rather than guessed.
        """
        if len(signals) < 2:
            return False
        top, second = signals[0].score, signals[1].score
        return top > 0 and (top - second) < 0.08

    # =========================================================================
    # Rule scoring
    # =========================================================================
    def _score_all(
        self,
        document: CanonicalDocument,
        profiles: list[DocumentProfile],
        *,
        title: str | None,
    ) -> list[ClassificationSignal]:
        # Scoring reads the opening of the document plus every heading: the parts that
        # identify a contract type. Scanning 150 pages adds cost and noise.
        head_text = self._head_text(document).lower()
        headings = " ".join(section.title for section in document.sections).lower()
        title_text = (title or document.metadata.file_name or "").lower()

        return [
            self._score_profile(
                profile,
                title_text=title_text,
                head_text=head_text,
                headings=headings,
            )
            for profile in profiles
        ]

    def _score_profile(
        self,
        profile: DocumentProfile,
        *,
        title_text: str,
        head_text: str,
        headings: str,
    ) -> ClassificationSignal:
        hints = profile.classification_hints or {}
        title_patterns = [str(p).lower() for p in hints.get("title_patterns", [])]
        required = [str(p).lower() for p in hints.get("required_phrases", [])]
        negative = [str(p).lower() for p in hints.get("negative_phrases", [])]
        heading_patterns = [str(p).lower() for p in hints.get("heading_patterns", [])]

        matched: list[str] = []
        blocked: list[str] = []
        # Kept apart from `matched`: the prefixed strings are for a human reading a
        # log line, while these two answer "which words did this?" and "which rule
        # family did this?" - the questions asked when tuning a profile's hints.
        keywords: list[str] = []
        rules: list[str] = []

        # --- negative phrases: a hard veto -----------------------------------
        for phrase in negative:
            if phrase in head_text or phrase in title_text:
                blocked.append(phrase)
        if blocked:
            return ClassificationSignal(
                profile_key=profile.key,
                profile_id=str(profile.id),
                agreement_type=profile.agreement_type,
                score=0.0,
                blocked_by=blocked,
                matched_rules=["negative_phrases"],
            )

        # --- title ------------------------------------------------------------
        title_score = 0.0
        for pattern in title_patterns:
            if pattern in title_text:
                title_score = 1.0
                matched.append(f"title:{pattern}")
                keywords.append(pattern)
                rules.append("title_patterns")
                break
            if pattern in headings:
                # A heading match is weaker than a filename/title match but still
                # strong - contracts usually name themselves in their first heading.
                title_score = max(title_score, 0.7)
                matched.append(f"heading-title:{pattern}")
                keywords.append(pattern)
                rules.append("title_patterns:heading")

        # --- required phrases --------------------------------------------------
        phrase_hits = 0
        for phrase in required:
            if phrase in head_text or phrase in headings:
                phrase_hits += 1
                matched.append(f"phrase:{phrase}")
                keywords.append(phrase)
        phrase_score = (phrase_hits / len(required)) if required else 0.0
        if phrase_hits:
            rules.append("required_phrases")

        # --- structural headings ------------------------------------------------
        matched_headings = [pattern for pattern in heading_patterns if pattern in headings]
        heading_hits = len(matched_headings)
        heading_score = (heading_hits / len(heading_patterns)) if heading_patterns else 0.0
        if heading_hits:
            matched.append(f"headings:{heading_hits}/{len(heading_patterns)}")
            keywords.extend(matched_headings)
            rules.append("heading_patterns")

        # Renormalise across whichever signals this profile actually declares, so a
        # profile that only specifies a title pattern is not penalised for it.
        weights = 0.0
        total = 0.0
        if title_patterns:
            weights += _WEIGHT_TITLE
            total += _WEIGHT_TITLE * title_score
        if required:
            weights += _WEIGHT_PHRASES
            total += _WEIGHT_PHRASES * phrase_score
        if heading_patterns:
            weights += _WEIGHT_HEADINGS
            total += _WEIGHT_HEADINGS * heading_score

        score = (total / weights) if weights else 0.0

        return ClassificationSignal(
            profile_key=profile.key,
            profile_id=str(profile.id),
            agreement_type=profile.agreement_type,
            score=round(score, 4),
            title_score=title_score,
            phrase_score=phrase_score,
            heading_score=heading_score,
            matched=matched,
            matched_keywords=keywords,
            matched_rules=rules,
        )

    # =========================================================================
    # Title detection
    # =========================================================================
    @staticmethod
    def _detect_title(document: CanonicalDocument) -> str | None:
        """Find the document's own title.

        Preference order: the first level-1 section heading that reads like an
        agreement name, then the first substantial line on page one, then the
        filename. Filenames are the least trustworthy - ``scan_0142.pdf`` is common.
        """
        agreement_words = (
            "agreement",
            "contract",
            "deed",
            "lease",
            "policy",
            "amendment",
            "addendum",
            "statement of work",
            "order form",
            "memorandum",
        )

        for section in sorted(document.sections, key=lambda s: (s.start_page, s.order))[:6]:
            lowered = section.title.lower()
            if any(word in lowered for word in agreement_words) and len(section.title) < 200:
                return section.title.strip()

        for paragraph in sorted(
            document.paragraphs, key=lambda p: (p.page_number, p.reading_order)
        )[:8]:
            text = paragraph.text.strip()
            lowered = text.lower()
            if 10 < len(text) < 200 and any(word in lowered for word in agreement_words):
                return text

        name = document.metadata.file_name
        return name.rsplit(".", 1)[0].replace("_", " ").strip() if name else None

    @staticmethod
    def _head_text(document: CanonicalDocument, limit: int = 12000) -> str:
        """The opening of the document, capped."""
        parts: list[str] = []
        length = 0
        for paragraph in sorted(
            document.paragraphs, key=lambda p: (p.page_number, p.reading_order)
        ):
            parts.append(paragraph.text)
            length += len(paragraph.text)
            if length >= limit:
                break
        return "\n".join(parts)

    # =========================================================================
    # LLM tie-break
    # =========================================================================
    async def _llm_tiebreak(
        self,
        document: CanonicalDocument,
        profiles: list[DocumentProfile],
        signals: list[ClassificationSignal],
        title: str | None,
    ) -> tuple[ClassificationResult | None, FallbackReason]:
        """Ask the model to choose when rules cannot.

        Constrained to the configured profile keys and required to return structured
        output, so it selects among real options rather than inventing a type.

        Returns the reason alongside the result because the caller cannot otherwise
        tell a failed call from an ambiguous document - and those need different
        responses. A provider outage is retried; an ambiguous contract is reviewed.
        """
        from app.ai.rag.providers import get_inference_provider
        from app.core.errors import ProviderError

        candidates = [
            {"key": profile.key, "name": profile.name, "type": profile.agreement_type}
            for profile in profiles
        ]

        schema = {
            "type": "object",
            "properties": {
                "profile_key": {
                    "type": "string",
                    "enum": [profile.key for profile in profiles],
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            },
            "required": ["profile_key", "confidence", "reason"],
            "additionalProperties": False,
        }

        prompt = (
            "Classify this contract into exactly one of the provided document types.\n\n"
            f"Available types:\n{candidates}\n\n"
            f"Detected title: {title or 'unknown'}\n\n"
            "Document excerpt:\n"
            f"{self._head_text(document, _LLM_SAMPLE_CHARS)}\n\n"
            "Choose the single best matching profile_key. If the document does not "
            "clearly match any type, choose the closest and report low confidence."
        )

        try:
            provider = get_inference_provider()
            structured = await provider.generate_structured(
                system=(
                    "You classify legal contracts. Answer only from the document text "
                    "provided. Never invent a document type outside the supplied list."
                ),
                prompt=prompt,
                schema=schema,
                purpose="classification",
            )
        except ProviderError as exc:
            logger.warning("classification_llm_unavailable", error=str(exc))
            return None, FallbackReason.LLM_UNAVAILABLE
        except Exception as exc:  # noqa: BLE001 - never fail the pipeline on a tie-break
            logger.warning("classification_llm_failed", error=str(exc))
            return None, FallbackReason.LLM_UNAVAILABLE

        response = structured.data
        key = str(response.get("profile_key", ""))
        profile = next((p for p in profiles if p.key == key), None)
        if profile is None:
            logger.warning(
                "classification_llm_unknown_key",
                key=key,
                configured=[p.key for p in profiles],
            )
            return None, FallbackReason.LLM_INVALID_RESPONSE

        confidence = float(response.get("confidence", 0.5))
        logger.info(
            "classification_llm_resolved",
            profile_key=key,
            confidence=round(confidence, 3),
            model=structured.model,
            tokens=structured.usage.total,
            cost_usd=structured.cost_usd,
        )

        return (
            ClassificationResult(
                profile=profile,
                agreement_type=profile.agreement_type,
                agreement_subtype=profile.agreement_subtype,
                confidence=min(max(confidence, 0.0), 0.95),
                method="llm",
                signals=signals,
                detected_title=title,
                language=document.metadata.language,
                notes=[
                    "Rule scoring was inconclusive; resolved by model.",
                    str(response.get("reason", ""))[:400],
                ],
            ),
            FallbackReason.NONE,
        )


def confidence_to_decimal(value: float) -> Decimal:
    """Confidence as a ``Numeric(5,4)`` value for the contract row."""
    return Decimal(str(round(min(max(value, 0.0), 1.0), 4)))


def agreement_type_or_other(value: str) -> str:
    """Map a profile's contract type onto the taxonomy, tolerating extensions.

    ``agreement_type`` is an extensible column, so an admin-added type is stored as
    given rather than being forced to ``other``.
    """
    try:
        return AgreementType(value).value
    except ValueError:
        return value


__all__ = [
    "ClassificationResult",
    "ClassificationSignal",
    "DocumentClassifier",
    "FallbackReason",
    "agreement_type_or_other",
    "confidence_to_decimal",
]

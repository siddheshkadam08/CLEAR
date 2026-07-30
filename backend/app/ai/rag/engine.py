"""RAG engine - generates the grounded answer (§17).

Consumes a Context Package and a prompt, produces a cited answer. It **never
retrieves** and **never builds prompts** - those are the retrieval and
prompt-orchestration layers, and keeping them out of here is what makes each
testable on its own.

The part that matters is what happens *after* generation. A model asked to cite
will usually cite; the question is whether the citations are real. So every answer
is parsed and checked:

* **Fabricated citations are stripped.** A label the model was never offered cannot
  be resolved to a page, so it is removed from the text rather than shown to a user
  who would reasonably assume a bracketed number is verifiable.
* **Uncited factual answers are flagged.** An answer of any substance with no
  citations at all did not come from the evidence, whatever it says.
* **Confidence reflects grounding, not fluency.** It is computed from citation
  coverage and retrieval scores, because a confident-sounding ungrounded answer is
  precisely the output that causes harm.

A refusal is a first-class outcome, not an exception: contract language around
indemnities and breach sits close enough to safety categories that false positives
are a real operational risk, and the caller needs to distinguish "the model declined"
from "the model failed".
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.ai.rag.orchestrator import GenerationPrompt, PromptOrchestrator
from app.ai.rag.providers import (
    IInferenceProvider,
    TokenUsage,
    get_inference_provider,
)
from app.ai.retrieval.context import Citation, ContextPackage
from app.core import metrics
from app.core.config import get_settings
from app.core.enums import ConfidenceBand, ResponseFormat
from app.core.errors import ProviderError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: ``[1]`` or ``[1][3]`` or ``[1, 3]`` - the citation forms a model actually emits.
_CITATION = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")

#: Below this many characters an answer is a refusal or a clarification, not a
#: factual claim, so the missing-citation rule does not apply to it.
_TRIVIAL_ANSWER_CHARS = 160

#: Phrases that mark an honest "the evidence does not cover this". Recognised so a
#: correct non-answer is not penalised for having no citations.
_NON_ANSWER_MARKERS = (
    "does not address",
    "does not say",
    "does not specify",
    "does not contain",
    "is silent",
    "no evidence",
    "not covered by the retrieved",
    "retrieved material does not",
    "cannot answer",
    "could not find",
    "insufficient evidence",
    "no supporting evidence",
)


@dataclass(slots=True)
class Answer:
    """A generated answer plus everything needed to audit and display it."""

    text: str
    citations: list[Citation] = field(default_factory=list)
    confidence: float = 0.0
    confidence_band: ConfidenceBand = ConfidenceBand.LOW
    response_format: ResponseFormat = ResponseFormat.NATURAL_LANGUAGE
    #: True when safety classifiers declined. Not an error.
    refused: bool = False
    refusal_category: str | None = None
    #: Labels the model emitted that were never offered. Stripped from the text and
    #: recorded, because their presence is a quality signal worth tracking.
    invalid_citations: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    model: str = ""
    prompt_id: str = ""
    prompt_version: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0
    cost_usd: float = 0.0
    served_by_fallback: bool = False
    #: True when the answer needs a human to look at it before being relied on.
    needs_review: bool = False

    @property
    def is_grounded(self) -> bool:
        return bool(self.citations) and not self.invalid_citations

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "citations": [c.as_dict() for c in self.citations],
            "confidence": round(self.confidence, 4),
            "confidence_band": self.confidence_band.value,
            "response_format": self.response_format.value,
            "refused": self.refused,
            "refusal_category": self.refusal_category,
            "invalid_citations": self.invalid_citations,
            "warnings": self.warnings,
            "needs_review": self.needs_review,
            "model": self.model,
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "usage": self.usage.as_dict(),
            "latency_ms": self.latency_ms,
            "cost_usd": round(self.cost_usd, 6),
            "served_by_fallback": self.served_by_fallback,
        }


class RAGEngine:
    """Generates grounded answers from a Context Package."""

    def __init__(
        self,
        provider: IInferenceProvider | None = None,
        orchestrator: PromptOrchestrator | None = None,
    ) -> None:
        settings = get_settings()
        self._provider = provider or get_inference_provider()
        self._orchestrator = orchestrator or PromptOrchestrator(
            organisation_aliases=list(settings.organization_legal_names)
        )

    async def answer(
        self,
        package: ContextPackage,
        *,
        response_format: ResponseFormat | None = None,
        max_tokens: int | None = None,
    ) -> Answer:
        prompt = self._orchestrator.build(package, response_format=response_format)

        if package.is_empty:
            # No evidence: answered without a provider call. Asking a model to answer
            # from nothing invites it to answer from its own knowledge of contracts,
            # which is the one thing this engine must not produce.
            return self._empty_answer(package, prompt)

        started = time.perf_counter()
        try:
            result = await self._provider.generate(
                system=prompt.system,
                prompt=prompt.user,
                purpose=self._purpose(prompt.response_format),
                max_tokens=max_tokens,
                cache_prefix=True,
            )
        except ProviderError as exc:
            metrics.rag_requests_total.labels(
                response_format=prompt.response_format.value, outcome="failed"
            ).inc()
            logger.warning(
                "rag_generation_failed",
                prompt_id=prompt.prompt_id,
                error=str(exc),
            )
            raise

        latency_ms = int((time.perf_counter() - started) * 1000)

        if result.refused:
            # A first-class outcome. Contract language around breach and indemnity
            # sits close to safety categories, so this is reported honestly rather
            # than dressed up as a failure.
            metrics.rag_requests_total.labels(
                response_format=prompt.response_format.value, outcome="failed"
            ).inc()
            logger.info(
                "rag_answer_refused",
                prompt_id=prompt.prompt_id,
                category=result.refusal_category,
            )
            return Answer(
                text=(
                    "The model declined to answer this question. This can happen when "
                    "contract language resembles a restricted topic. Rephrasing the "
                    "question, or narrowing it to a specific clause, usually resolves it."
                ),
                refused=True,
                refusal_category=result.refusal_category,
                response_format=prompt.response_format,
                model=result.model,
                prompt_id=prompt.prompt_id,
                prompt_version=prompt.prompt_version,
                usage=result.usage,
                latency_ms=latency_ms,
                cost_usd=result.cost_usd,
                needs_review=True,
            )

        answer = self._validate(result.text, package, prompt)
        answer.model = result.model
        answer.usage = result.usage
        answer.latency_ms = latency_ms
        answer.cost_usd = result.cost_usd
        answer.served_by_fallback = result.served_by_fallback

        metrics.rag_requests_total.labels(
            response_format=prompt.response_format.value,
            outcome="ok" if answer.is_grounded else "insufficient_evidence",
        ).inc()
        metrics.rag_confidence.observe(answer.confidence)

        logger.info(
            "rag_answer_generated",
            prompt_id=prompt.prompt_id,
            format=prompt.response_format.value,
            citations=len(answer.citations),
            invalid_citations=len(answer.invalid_citations),
            confidence=round(answer.confidence, 3),
            band=answer.confidence_band.value,
            needs_review=answer.needs_review,
            model=result.model,
            tokens=result.usage.total,
            cost_usd=result.cost_usd,
            latency_ms=latency_ms,
        )
        return answer

    async def stream(
        self,
        package: ContextPackage,
        *,
        response_format: ResponseFormat | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """Stream the answer for the Copilot.

        Returns the token iterator and the prompt, so the caller can emit tokens as
        they arrive and still validate citations against the offered labels once the
        stream completes. Validation cannot happen mid-stream - a citation is only
        checkable when the text containing it exists.
        """
        prompt = self._orchestrator.build(package, response_format=response_format)
        if package.is_empty:
            return None, prompt
        return (
            self._provider.stream(
                system=prompt.system,
                prompt=prompt.user,
                purpose=self._purpose(prompt.response_format),
                max_tokens=max_tokens,
                cache_prefix=True,
            ),
            prompt,
        )

    # =========================================================================
    # Validation
    # =========================================================================
    def validate_text(self, text: str, package: ContextPackage, prompt: Any) -> Answer:
        """Public entry point for validating a streamed answer after the fact."""
        return self._validate(text, package, prompt)

    def _validate(self, text: str, package: ContextPackage, prompt: GenerationPrompt) -> Answer:
        """Check the answer's citations and score its grounding."""
        answer = Answer(
            text=text.strip(),
            response_format=prompt.response_format,
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.prompt_version,
        )

        cited, invalid = self._parse_citations(answer.text, prompt.valid_labels)
        answer.invalid_citations = sorted(invalid)

        if invalid:
            # Removed from the displayed text: a bracketed number the UI cannot
            # resolve reads as a verifiable source and is not one.
            answer.text = self._strip_labels(answer.text, invalid)
            answer.warnings.append(
                f"The answer cited {len(invalid)} source(s) that were not provided "
                f"({', '.join(f'[{label}]' for label in sorted(invalid))}); those "
                "references were removed. Verify the answer against the cited pages."
            )
            answer.needs_review = True
            metrics.rag_regenerated_total.labels(reason="unknown_citation_label").inc()

        answer.citations = [citation for citation in package.citations if citation.label in cited]

        substantive = len(answer.text) > _TRIVIAL_ANSWER_CHARS
        honest_non_answer = any(marker in answer.text.lower() for marker in _NON_ANSWER_MARKERS)

        if substantive and not cited and not honest_non_answer:
            # An answer of real length, stating terms, with nothing behind it.
            answer.warnings.append(
                "The answer cites no supporting evidence, so it cannot be verified "
                "against the source documents."
            )
            answer.needs_review = True
            metrics.rag_regenerated_total.labels(reason="uncited_answer").inc()

        answer.confidence = self._confidence(answer, package, honest_non_answer)
        answer.confidence_band = ConfidenceBand.from_score(answer.confidence)
        if answer.confidence_band is ConfidenceBand.LOW:
            answer.needs_review = True

        if package.dropped:
            answer.warnings.append(
                f"{package.dropped} retrieved passage(s) did not fit the context "
                "budget; this answer is based on the highest-scoring subset."
            )
        answer.warnings.extend(package.warnings)
        return answer

    @staticmethod
    def _parse_citations(text: str, valid: set[int]) -> tuple[set[int], set[int]]:
        """Extract cited labels, split into offered and fabricated."""
        cited: set[int] = set()
        for match in _CITATION.finditer(text):
            for part in match.group(1).split(","):
                try:
                    cited.add(int(part.strip()))
                except ValueError:
                    continue
        return cited & valid, cited - valid

    @staticmethod
    def _strip_labels(text: str, invalid: set[int]) -> str:
        """Remove unresolvable labels while leaving valid ones intact."""

        def replace(match: re.Match[str]) -> str:
            labels = []
            for part in match.group(1).split(","):
                try:
                    label = int(part.strip())
                except ValueError:
                    continue
                if label not in invalid:
                    labels.append(label)
            # A group that was entirely fabricated disappears; a mixed group keeps
            # the labels that do resolve.
            return "".join(f"[{label}]" for label in labels)

        return _CITATION.sub(replace, text)

    def _confidence(
        self, answer: Answer, package: ContextPackage, honest_non_answer: bool
    ) -> float:
        """Score how well grounded the answer is.

        Deliberately not a measure of how confident the *model* sounds. It combines
        how much of the offered evidence was used, how strong that evidence scored in
        retrieval, and whether any citation was fabricated - all of which are
        observable, unlike the model's own certainty.
        """
        if answer.refused:
            return 0.0

        if honest_non_answer and not answer.citations:
            # Correctly reporting that the evidence does not cover the question is a
            # good answer, and scoring it as low-confidence would train users to
            # distrust exactly the honesty that should be rewarded.
            return 0.75 if package.citations else 0.6

        if not answer.citations:
            return 0.15

        offered = max(len(package.citations), 1)
        coverage = min(len(answer.citations) / min(offered, 5), 1.0)
        mean_score = sum(c.score for c in answer.citations) / len(answer.citations)
        # Retrieval scores are cosine similarities in [-1, 1]; rescale to [0, 1] so a
        # weak-but-positive match does not read as high confidence.
        evidence_strength = max(0.0, min((mean_score + 1.0) / 2.0, 1.0))

        score = 0.55 * coverage + 0.45 * evidence_strength
        if answer.invalid_citations:
            # A fabricated citation is direct evidence the answer is not fully
            # grounded, whatever the rest of it looks like.
            score *= 0.5
        if package.dropped:
            score *= 0.9
        return round(max(0.0, min(score, 1.0)), 4)

    # =========================================================================
    # Helpers
    # =========================================================================
    def _empty_answer(self, package: ContextPackage, prompt: GenerationPrompt) -> Answer:
        metrics.rag_requests_total.labels(
            response_format=prompt.response_format.value, outcome="insufficient_evidence"
        ).inc()
        return Answer(
            text=(
                "I could not find anything in the accessible contracts that answers "
                "this question. It may help to name a specific contract or clause, "
                "widen the date range, or check that the relevant document has "
                "finished processing."
            ),
            confidence=0.0,
            confidence_band=ConfidenceBand.LOW,
            response_format=prompt.response_format,
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.prompt_version,
            warnings=list(package.warnings),
            needs_review=False,
        )

    @staticmethod
    def _purpose(fmt: ResponseFormat) -> Any:
        """Route the request to a model tier.

        Multi-document reasoning gets the stronger model: a comparison or a risk
        report across several agreements is where a cheaper model starts inventing
        clause text, which is the specific failure this platform cannot ship.
        """
        if fmt in {ResponseFormat.CLAUSE_COMPARISON, ResponseFormat.RISK_REPORT}:
            return "comparison"
        if fmt in {
            ResponseFormat.EXECUTIVE_SUMMARY,
            ResponseFormat.CONTRACT_SUMMARY,
            ResponseFormat.COMPLIANCE_REPORT,
            ResponseFormat.OBLIGATION_REPORT,
        }:
            return "report"
        return "rag"


__all__ = ["Answer", "RAGEngine"]

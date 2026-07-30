"""Prompt Orchestrator - builds the generation prompt (§16).

Turns a Context Package into a prompt. It does not retrieve, and it does not call a
model; it decides what the model is asked and how the answer must be shaped.

The grounding rules are restated here rather than imported from extraction, because
they are not the same rules. Extraction reads a document and must never invent
contract content. Answering reads *retrieved evidence* and must additionally:

* cite by label, so every claim is traceable to a page,
* refuse to answer beyond the evidence rather than filling the gap from general
  knowledge of what contracts usually say - which is the failure mode that makes a
  RAG answer confidently wrong,
* distinguish "the contract does not say" from "I was not given that part".

That last distinction is the one users care most about and models are worst at, so
it is stated explicitly and checked afterwards by the answer validator.

Prompt caching shapes the layout: the system prompt is identical for every question
of a given response format, and everything volatile - the evidence, the history, the
question - goes in the user message after the cache breakpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ai.retrieval.context import ContextPackage
from app.core.enums import QueryIntent, ResponseFormat
from app.core.versions import PROMPT_VERSIONS

# =============================================================================
# The answering grounding rules (§16). Mandatory, and identical per format.
# =============================================================================
ANSWER_GROUNDING_RULES = """\
GROUNDING RULES - these override every other instruction:

1. Answer only from the evidence supplied below. You have no other knowledge of
   these agreements. Do not supplement the evidence with what contracts of this
   kind usually say - a plausible completion is indistinguishable from a fact to
   the reader, and that is the failure this rule exists to prevent.
2. Cite every factual claim with the bracketed label of the evidence it came from,
   like [1] or [2][3]. A sentence stating a contractual term without a citation is
   not acceptable output. Only cite labels that appear in the evidence.
3. Never invent contract content. Do not paraphrase a quotation into something
   cleaner, do not merge two clauses into one statement, and do not restate a term
   more precisely than the wording supports.
4. Distinguish absence from ignorance. If the evidence shows the agreement is
   silent on something, say the agreement does not address it. If the evidence
   simply does not cover the question, say the retrieved material does not answer
   it and what would be needed. These are different answers and must not be
   conflated.
5. State uncertainty plainly. Where wording is ambiguous or two passages conflict,
   say so and quote both rather than choosing one silently.
6. Preserve references. Clause numbers, defined terms, dates and amounts are
   reproduced exactly as the evidence states them."""

_ROLE = """\
You are a contract analyst answering questions about agreements held in a contract \
repository. Your answers are read by legal and commercial reviewers who will act on \
them, and every answer is shown next to the source page it cites."""

#: Per-format instructions. The format decides the *shape* of the answer; the
#: grounding rules never vary.
_FORMAT_INSTRUCTIONS: dict[ResponseFormat, str] = {
    ResponseFormat.NATURAL_LANGUAGE: (
        "Answer directly and concisely in prose. Lead with the answer, then the "
        "supporting detail. Do not pad with restatements of the question."
    ),
    ResponseFormat.EXECUTIVE_SUMMARY: (
        "Write for someone who has not read the agreement: what it is, the "
        "commercial shape, and the terms that carry risk. Six to ten sentences, no "
        "bullet lists."
    ),
    ResponseFormat.RISK_REPORT: (
        "Organise by severity, highest first. For each risk: what the wording is, "
        "why it is a risk, and what to negotiate. Only risks the evidence supports - "
        "do not list generic contract risks."
    ),
    ResponseFormat.COMPLIANCE_REPORT: (
        "State compliance status per requirement, citing the clause that satisfies "
        "it or noting its absence explicitly."
    ),
    ResponseFormat.CLAUSE_COMPARISON: (
        "Compare clause by clause across the agreements. State each position "
        "separately with its own citation before drawing any conclusion, so the "
        "reader can check the comparison."
    ),
    ResponseFormat.TIMELINE: (
        "List dated events in chronological order. Include relative deadlines as "
        "worded - '30 days before expiry' is the obligation, not a computed date."
    ),
    ResponseFormat.ACTION_ITEMS: (
        "List concrete actions, each with the party responsible and the deadline or "
        "trigger, citing the clause it arises from."
    ),
    ResponseFormat.CONTRACT_SUMMARY: (
        "Summarise the agreement's purpose, parties, term, commercial terms and "
        "notable risks, in that order."
    ),
    ResponseFormat.OBLIGATION_REPORT: (
        "List obligations grouped by responsible party, each with its trigger or "
        "deadline and its citation."
    ),
    ResponseFormat.JSON: (
        "Return only data conforming to the requested schema. No prose outside it."
    ),
}

#: Intent -> the format that answers it best, when the caller does not specify one.
_INTENT_FORMAT: dict[QueryIntent, ResponseFormat] = {
    QueryIntent.SUMMARIZATION: ResponseFormat.CONTRACT_SUMMARY,
    QueryIntent.RISK_ASSESSMENT: ResponseFormat.RISK_REPORT,
    QueryIntent.COMPLIANCE: ResponseFormat.COMPLIANCE_REPORT,
    QueryIntent.COMPARISON: ResponseFormat.CLAUSE_COMPARISON,
    QueryIntent.TIMELINE: ResponseFormat.TIMELINE,
    QueryIntent.OBLIGATION_LOOKUP: ResponseFormat.OBLIGATION_REPORT,
}

#: Intent -> prompt id, for version stamping.
_INTENT_PROMPT: dict[QueryIntent, str] = {
    QueryIntent.SUMMARIZATION: "rag.contract_summary",
    QueryIntent.RISK_ASSESSMENT: "rag.risk_report",
    QueryIntent.COMPLIANCE: "rag.compliance_report",
    QueryIntent.COMPARISON: "rag.clause_comparison",
    QueryIntent.TIMELINE: "rag.timeline",
    QueryIntent.OBLIGATION_LOOKUP: "rag.obligation_report",
}


@dataclass(slots=True)
class GenerationPrompt:
    """A ready-to-send answering prompt."""

    system: str
    user: str
    prompt_id: str
    prompt_version: str
    response_format: ResponseFormat
    #: Labels the answer is permitted to cite. The validator's allow-list.
    valid_labels: set[int] = field(default_factory=set)
    schema: dict[str, Any] | None = None

    def as_audit(self) -> dict[str, Any]:
        """Recorded per answer, without the evidence text itself."""
        return {
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "response_format": self.response_format.value,
            "citations_offered": len(self.valid_labels),
            "system_chars": len(self.system),
            "user_chars": len(self.user),
        }


class PromptOrchestrator:
    """Builds answering prompts from a Context Package."""

    def __init__(self, *, organisation_aliases: list[str] | None = None) -> None:
        self._aliases = organisation_aliases or []
        # Built per format and cached: this string is the cached prefix and must be
        # byte-identical across questions for the cache to hit.
        self._systems: dict[ResponseFormat, str] = {}

    def build(
        self,
        package: ContextPackage,
        *,
        response_format: ResponseFormat | None = None,
        schema: dict[str, Any] | None = None,
    ) -> GenerationPrompt:
        fmt = response_format or _INTENT_FORMAT.get(package.intent, ResponseFormat.NATURAL_LANGUAGE)
        prompt_id = _INTENT_PROMPT.get(package.intent, "rag.qa")

        return GenerationPrompt(
            system=self._system_for(fmt),
            user=self._user_message(package),
            prompt_id=prompt_id,
            prompt_version=PROMPT_VERSIONS.get(prompt_id, "unknown"),
            response_format=fmt,
            valid_labels=package.valid_labels,
            schema=schema,
        )

    # ------------------------------------------------------------------ system
    def _system_for(self, fmt: ResponseFormat) -> str:
        cached = self._systems.get(fmt)
        if cached is not None:
            return cached

        parts = [
            _ROLE,
            "",
            ANSWER_GROUNDING_RULES,
            "",
            "ANSWER SHAPE:",
            "",
            _FORMAT_INSTRUCTIONS.get(fmt, _FORMAT_INSTRUCTIONS[ResponseFormat.NATURAL_LANGUAGE]),
        ]

        if self._aliases:
            aliases = ", ".join(f'"{name}"' for name in self._aliases)
            parts += [
                "",
                "WHICH SIDE IS OURS:",
                "",
                f"This repository belongs to an organisation known as: {aliases}. When a "
                "question asks what 'we' or 'our side' may do, it means that party. If "
                "the evidence does not make clear which party that is, say so rather "
                "than assuming.",
            ]

        built = "\n".join(parts)
        self._systems[fmt] = built
        return built

    # -------------------------------------------------------------------- user
    def _user_message(self, package: ContextPackage) -> str:
        """The volatile half: history, evidence, then the question.

        The question goes **last** deliberately. It is the instruction the model
        should be holding when it starts generating, and burying it above several
        thousand tokens of evidence measurably weakens adherence to it.
        """
        sections: list[str] = []

        if package.history:
            lines = ["CONVERSATION SO FAR:", ""]
            for turn in package.history:
                role = turn.get("role", "user").upper()
                content = (turn.get("content") or "").strip()
                if content:
                    lines.append(f"{role}: {content}")
            sections.append("\n".join(lines))

        metadata = package.render_metadata()
        if metadata:
            sections.append(metadata)

        sections.append("EVIDENCE:\n\n" + package.render_evidence())

        if package.dropped:
            sections.append(
                f"NOTE: {package.dropped} further passage(s) were retrieved but did not "
                "fit. If the evidence above is insufficient, say so rather than "
                "speculating about what the omitted material might contain."
            )

        if not package.citations:
            sections.append(
                "NOTE: no supporting evidence was retrieved for this question. Say that "
                "plainly and suggest how the question could be narrowed. Do not answer "
                "from general knowledge."
            )

        sections.append(f"QUESTION:\n\n{package.query}")
        return "\n\n".join(sections)


__all__ = ["ANSWER_GROUNDING_RULES", "GenerationPrompt", "PromptOrchestrator"]

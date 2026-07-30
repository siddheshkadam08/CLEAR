"""Extraction prompt construction (§13, §16).

The grounding rules are not advisory text bolted onto a prompt - they are the
contract this platform makes with its users, and they are enforced twice: stated
here, and checked in :mod:`app.ai.extraction.validation`. A rule that is only
asked for is a rule that will occasionally be ignored.

**Prompt caching shapes this module.** Caching is a prefix match, so the system
prompt is *identical* for every clause category and every contract in a
deployment - the same rules, the same output discipline, the same organisation
aliases. Everything volatile (the category being extracted, the evidence, the
already-known parties) goes in the user message, after the cache breakpoint. Get
this the wrong way round and each call writes a new cache entry instead of reading
one, which on a 23-clause contract is the difference between one cached prefix and
twenty-three.

Prompt text is versioned in :data:`app.core.versions.PROMPT_VERSIONS`. Editing a
prompt without bumping its version would leave two incompatible extractions
indistinguishable in the audit record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ai.extraction.evidence import EvidenceBundle
from app.ai.extraction.models import ContractFacts, ExtractedClause, ExtractedParty
from app.ai.extraction.schemas import (
    CATEGORY_PROMPTS,
    clause_schema,
    dates_schema,
    financial_schema,
    metadata_schema,
    obligations_schema,
    parties_schema,
    relationships_schema,
    risks_schema,
    summary_schema,
)
from app.ai.rag.providers import Purpose
from app.core.config import get_settings
from app.core.versions import PROMPT_VERSIONS

# =============================================================================
# The grounding rules (§16). Mandatory, and identical for every extraction call.
# =============================================================================
GROUNDING_RULES = """\
GROUNDING RULES - these override every other instruction:

1. Answer only from the evidence supplied in this message. You have no other
   knowledge of this agreement. If the evidence does not contain something, the
   answer is that it is absent - not what a contract of this kind usually says.
2. Never invent contract content. Do not compose, complete, correct or normalise
   wording. Quoted text must be copied character for character from the evidence,
   including its numbering, capitalisation and punctuation.
3. Cite the evidence. Every item you return must list the evidence block ids it
   came from, exactly as labelled in square brackets. Never cite an id that does
   not appear in this message.
4. Preserve references. Keep clause numbers as printed. Do not renumber, merge or
   tidy them - a citation has to lead a reader back to the same place in the
   document.
5. State uncertainty. When wording is ambiguous, partial, or spread across
   evidence you were not given, say so in the uncertainty field and lower your
   confidence. A low-confidence honest answer is useful; a confident guess is not.
6. Absence is an answer. A missing clause is a material commercial finding. Report
   it as not found rather than inferring it from an adjacent clause or a
   definition that merely names the concept."""

_OUTPUT_DISCIPLINE = """\
OUTPUT DISCIPLINE:

- Return null for anything the evidence does not state. Never substitute a
  plausible default, a market-standard figure, or a value carried over from
  another clause.
- Amounts are numbers without currency symbols or thousands separators; the
  currency goes in its own field as an ISO 4217 code.
- Dates are YYYY-MM-DD. When the agreement expresses a date relatively ("30 days
  after the Effective Date"), leave the date field null and record the wording in
  the expression field - the relative term is the actual obligation.
- Durations are whole numbers in the unit the field names.
- Confidence is your own assessment, not a formality: below 0.7 when the wording
  is ambiguous or the evidence is partial."""

_ROLE = """\
You are a contract analyst extracting structured data from commercial agreements \
for a legal review platform. Your output is stored as the authoritative record of \
what the agreement says and is shown to reviewers alongside the source page, so it \
must be exactly faithful to the text."""


@dataclass(slots=True)
class PromptSpec:
    """One ready-to-send extraction call."""

    prompt_id: str
    prompt_version: str
    system: str
    user: str
    schema: dict[str, Any]
    purpose: Purpose = "extraction"
    #: Chunk ids the model is allowed to cite - the validator's allow-list.
    allowed_chunk_ids: set[str] = field(default_factory=set)

    def as_audit(self) -> dict[str, Any]:
        """What is recorded about this call, without the evidence itself."""
        return {
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "evidence_blocks": len(self.allowed_chunk_ids),
            "system_chars": len(self.system),
            "user_chars": len(self.user),
        }


class ExtractionPromptBuilder:
    """Builds extraction prompts for one contract.

    Holds the profile so that per-document-type guidance (a profile's
    ``validation_hints``) reaches the model without any document type being named
    in this file.
    """

    def __init__(
        self,
        *,
        profile: Any = None,
        language: str | None = None,
        organisation_aliases: list[str] | None = None,
    ) -> None:
        settings = get_settings()
        self._profile = profile
        self._language = language or "en"
        self._aliases = organisation_aliases or list(settings.organization_legal_names)
        self._hints: list[str] = list(
            (getattr(profile, "extraction_strategy", None) or {}).get("validation_hints", [])
        )
        # Built once and reused: this string is the cached prefix, and it must be
        # byte-identical across every call for the cache to hit.
        self._system = self._build_system()

    # ------------------------------------------------------------------ system
    def _build_system(self) -> str:
        parts = [_ROLE, "", GROUNDING_RULES, "", _OUTPUT_DISCIPLINE]

        if self._aliases:
            aliases = ", ".join(f'"{name}"' for name in self._aliases)
            parts += [
                "",
                "WHICH SIDE IS OURS:",
                "",
                f"This platform is operated by an organisation known as: {aliases}.",
                "When a party name in the agreement matches one of those names, that "
                "party is our_organisation; the other principal party is the "
                "counterparty. Fields naming a side (for example, which party may "
                "terminate, or which party retains pre-existing IP) must be resolved "
                "on that basis. If neither party matches, or the match is uncertain, "
                "return unknown rather than guessing which side we are on.",
            ]

        if self._hints:
            parts += ["", "DOCUMENT-TYPE GUIDANCE:", ""]
            parts += [f"- {hint}" for hint in self._hints]

        return "\n".join(parts)

    @property
    def system(self) -> str:
        return self._system

    # ------------------------------------------------------------------ clauses
    def clause(
        self,
        *,
        clause_key: str,
        clause_name: str,
        attribute_schema: dict[str, Any],
        bundle: EvidenceBundle,
        synonyms: list[str] | None = None,
        standard_text: str | None = None,
        notes: str | None = None,
        parties: list[ExtractedParty] | None = None,
    ) -> PromptSpec:
        """One clause category, over its own pre-filtered evidence."""
        lines = [
            f"TASK: Extract the {clause_name} clause from the evidence below.",
            "",
        ]

        if synonyms:
            lines += [
                f"This clause may be headed {', '.join(synonyms)} or something similar, "
                "or may appear without a heading. Identify it by what it does, not by "
                "its title.",
                "",
            ]

        if notes:
            lines += [f"NOTE: {notes}", ""]

        if parties:
            lines += ["PARTIES ALREADY IDENTIFIED IN THIS AGREEMENT:", ""]
            for party in parties:
                side = "our organisation" if party.is_our_organisation else "counterparty"
                role = f", {party.role}" if party.role else ""
                lines.append(f"- {party.name} ({side}{role})")
            lines.append("")

        if standard_text:
            # Deviation reference, never a template to copy from.
            lines += [
                "REFERENCE WORDING (for judging deviation only - never copy from it, "
                "and never let it influence what you report the agreement as saying):",
                "",
                standard_text.strip(),
                "",
            ]

        lines += [
            "If the agreement contains this clause more than once - for example in the "
            "body and again in a schedule or amendment - return each occurrence "
            "separately with its own clause number and evidence.",
            "",
            "EVIDENCE:",
            "",
            bundle.render() or "(no evidence blocks matched this clause category)",
        ]

        return PromptSpec(
            prompt_id=CATEGORY_PROMPTS["clauses"],
            prompt_version=PROMPT_VERSIONS.get(CATEGORY_PROMPTS["clauses"], "unknown"),
            system=self._system,
            user="\n".join(lines),
            schema=clause_schema(attribute_schema, clause_name=clause_name),
            allowed_chunk_ids=bundle.chunk_ids,
        )

    # -------------------------------------------------------- document metadata
    def metadata(self, bundle: EvidenceBundle) -> PromptSpec:
        user = self._task(
            "Extract the document-level facts of this agreement: its title, what it "
            "does, its value, and its dates.",
            bundle,
            extra=[
                "The evidence is the opening pages and the signature block, which is "
                "where these facts are normally stated. If a fact is not there, return "
                "null - do not derive an effective date from a signature date, or a "
                "value from a fee table you cannot see.",
            ],
        )
        return self._spec("metadata", user, metadata_schema(), bundle)

    def parties(self, bundle: EvidenceBundle) -> PromptSpec:
        user = self._task(
            "Identify every party to this agreement.",
            bundle,
            extra=[
                "Include the defined short forms the agreement uses for each party "
                "(for example 'the Supplier'), because later clauses refer to parties "
                "by those forms rather than by their full names.",
                "Mark as primary only the principal contracting parties - not "
                "affiliates, guarantors or notice recipients mentioned in passing.",
            ],
        )
        return self._spec("parties", user, parties_schema(), bundle)

    def financial(self, bundle: EvidenceBundle) -> PromptSpec:
        user = self._task(
            "Extract the financial terms: total value, currency, payment timing and "
            "any individual charges.",
            bundle,
            extra=[
                "'Payment days' is the number of days within which payment must be "
                "made or received, e.g. 30 for 'net 30' or 'within thirty (30) days "
                "of invoice'.",
                "Where a fee table is present, list its rows as line items and keep "
                "each row's amount with its own description.",
            ],
        )
        return self._spec("financial", user, financial_schema(), bundle)

    # ------------------------------------------------------ derived from clauses
    def obligations(
        self, bundle: EvidenceBundle, *, clauses: list[ExtractedClause] | None = None
    ) -> PromptSpec:
        extra = [
            "An obligation is something a party must do or refrain from doing, with a "
            "party responsible for it. Do not list definitions, recitals, or "
            "statements of fact as obligations.",
            "Where the deadline is relative, record the wording rather than computing "
            "a date - 'within 30 days of written notice' is the obligation.",
        ]
        if clauses:
            extra += [
                "",
                "CLAUSES ALREADY EXTRACTED (attribute the obligation to one of these "
                "categories where it arises from one):",
                *(
                    f"- {clause.clause_type}: {clause.clause_number or 'unnumbered'}"
                    for clause in clauses[:40]
                ),
            ]
        user = self._task(
            "Extract every obligation this agreement imposes on either party.",
            bundle,
            extra=extra,
        )
        return self._spec("obligations", user, obligations_schema(), bundle)

    def dates(self, bundle: EvidenceBundle) -> PromptSpec:
        user = self._task(
            "Extract every date and deadline that carries a consequence.",
            bundle,
            extra=[
                "Include renewal notice deadlines, cure periods, payment due dates, "
                "milestone dates and review dates.",
                "A relative deadline is still a key date: record its wording in the "
                "expression field and leave the date value null.",
            ],
        )
        return self._spec("dates", user, dates_schema(), bundle)

    def risks(
        self, bundle: EvidenceBundle, *, clauses: list[ExtractedClause] | None = None
    ) -> PromptSpec:
        extra = [
            "Report only risks the supplied wording actually creates. Deterministic "
            "rules already flag uncapped liability, missing clauses and similar "
            "structural findings, so concentrate on what those cannot see: one-sided "
            "remedies, internal inconsistencies, unusual drafting, and obligations "
            "whose trigger is undefined.",
            "Do not list generic contract risks. If the evidence does not evidence a "
            "risk, return no risk.",
        ]
        if clauses:
            extra += [
                "",
                "TERMS ALREADY EXTRACTED:",
                *(
                    f"- {clause.clause_type}: {clause.summary or (clause.text[:120] + '...')}"
                    for clause in clauses[:25]
                ),
            ]
        user = self._task(
            "Identify the risks this agreement's wording creates.", bundle, extra=extra
        )
        return self._spec("risks", user, risks_schema(), bundle)

    def relationships(self, bundle: EvidenceBundle) -> PromptSpec:
        user = self._task(
            "Extract the relationships the agreement states explicitly.",
            bundle,
            extra=[
                "Include cross-references between clauses ('as set out in Section 9'), "
                "references to other documents (schedules, exhibits, prior "
                "agreements, purchase orders), and which clauses survive termination.",
                "Only relationships the text states. Do not infer a relationship from "
                "two clauses being adjacent or thematically related.",
            ],
        )
        return self._spec("relationships", user, relationships_schema(), bundle)

    # ----------------------------------------------------------------- summary
    def summary(
        self,
        *,
        facts: ContractFacts,
        clauses: list[ExtractedClause],
        parties: list[ExtractedParty],
        risk_notes: list[str],
    ) -> PromptSpec:
        """Executive summary, written from extracted facts only.

        The evidence here is the extraction output rather than the document, which is
        deliberate: a summary written from raw text can assert a term that extraction
        did not find, and then the summary and the clause list disagree with each
        other in front of the reviewer.
        """
        lines = [
            "TASK: Write an executive summary of this agreement for a reviewer who has "
            "not read it.",
            "",
            "Use only the extracted facts below. Do not add commercial or legal "
            "commentary that these facts do not support, and do not restate a term "
            "more precisely than it is given here.",
            "",
            "AGREEMENT:",
            f"- Title: {facts.title or 'not stated'}",
            f"- Type: {facts.summary or 'not stated'}",
            f"- Value: {facts.contract_value or 'not stated'} {facts.currency or ''}".rstrip(),
            f"- Effective: {facts.effective_date or 'not stated'}",
            f"- Expires: {facts.expiration_date or 'not stated'}",
            f"- Governing law: {facts.governing_law or 'not stated'}",
            "",
            "PARTIES:",
        ]
        lines += [
            f"- {party.name}"
            + (" (our organisation)" if party.is_our_organisation else "")
            + (f" - {party.role}" if party.role else "")
            for party in parties
        ] or ["- none identified"]

        lines += ["", "EXTRACTED TERMS:"]
        lines += [
            f"- {clause.clause_type}"
            + (f" (clause {clause.clause_number})" if clause.clause_number else "")
            + f": {clause.summary or clause.text[:160]}"
            for clause in clauses
        ] or ["- none extracted"]

        if risk_notes:
            lines += ["", "RISK FINDINGS:"]
            lines += [f"- {note}" for note in risk_notes]

        return PromptSpec(
            prompt_id="summary.document",
            prompt_version=PROMPT_VERSIONS.get("summary.document", "unknown"),
            system=self._system,
            user="\n".join(lines),
            schema=summary_schema(),
            purpose="summary",
            # No document evidence in this call, so there is nothing to cite; the
            # validator skips the citation check for the summary and instead checks
            # that no term appears here that extraction did not produce.
            allowed_chunk_ids=set(),
        )

    # ------------------------------------------------------------------ helpers
    def _task(self, task: str, bundle: EvidenceBundle, *, extra: list[str] | None = None) -> str:
        lines = [f"TASK: {task}", ""]
        if extra:
            lines += [*extra, ""]
        lines += [
            "EVIDENCE:",
            "",
            bundle.render() or "(no evidence blocks were supplied)",
        ]
        return "\n".join(lines)

    def _spec(
        self,
        category: str,
        user: str,
        schema: dict[str, Any],
        bundle: EvidenceBundle,
    ) -> PromptSpec:
        prompt_id = CATEGORY_PROMPTS[category]
        return PromptSpec(
            prompt_id=prompt_id,
            prompt_version=PROMPT_VERSIONS.get(prompt_id, "unknown"),
            system=self._system,
            user=user,
            schema=schema,
            allowed_chunk_ids=bundle.chunk_ids,
        )


__all__ = ["GROUNDING_RULES", "ExtractionPromptBuilder", "PromptSpec"]

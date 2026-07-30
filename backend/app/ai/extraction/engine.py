"""The AI extraction engine (§13).

Orchestrates one contract's extraction: which categories to run, in what order,
over which evidence, and what to do when one of them fails.

Design decisions that matter:

* **Priority order is real.** Clauses are extracted in Clause Master priority order,
  so a run that fails or is cancelled part-way through has still produced the terms
  the business cares most about. Limitation of liability is first because that is the
  answer people open the contract for.
* **One call per clause category.** Each category gets its own schema and its own
  pre-filtered evidence. A single call covering all categories would need a union
  schema (weaker validation), the whole document as evidence (more expensive, more
  room to invent), and would lose every category when it failed.
* **A failing category never fails the job.** Categories are independent, so a
  refusal or a timeout on one is recorded on that category and the rest continue.
  A contract with twenty-two of twenty-three clauses extracted is useful; a failed
  job is not.
* **Absence without a call.** When the deterministic pre-filter finds no candidate
  evidence, the category is reported as not found and no tokens are spent. This is
  not a shortcut: a model given unrelated evidence and asked for a liability cap
  will sometimes produce one.
* **The engine never persists.** It returns :class:`ExtractionResult`; the stage
  handler writes it. That is what makes it testable against a fixture with no
  database and no API key.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.ai.extraction.evidence import (
    DEFAULT_EVIDENCE_BUDGET,
    DEFAULT_EVIDENCE_LIMIT,
    CandidateChunk,
    EvidenceBundle,
    EvidenceSelector,
)
from app.ai.extraction.models import (
    CategoryOutcome,
    ContractFacts,
    EvidenceRef,
    ExtractedClause,
    ExtractedItem,
    ExtractedKeyDate,
    ExtractedObligation,
    ExtractedParty,
    ExtractedRelationship,
    ExtractedRisk,
    ExtractionResult,
    ValidationIssue,
)
from app.ai.extraction.prompts import ExtractionPromptBuilder, PromptSpec
from app.ai.extraction.risk import RiskAssessor
from app.ai.extraction.validation import (
    FieldOutcome,
    ValidationContext,
    check_citations,
    check_confidence,
    check_date_order,
    check_generic_attributes,
    check_quote,
    normalise_party_name,
    parse_date,
    validate_against_schema,
    validate_clause_attributes,
)
from app.ai.rag.providers import (
    IInferenceProvider,
    StructuredResult,
    get_inference_provider,
)
from app.core import metrics
from app.core.config import get_settings
from app.core.enums import (
    DateType,
    EntityType,
    ObligationStatus,
    ReviewStatus,
    RiskSeverity,
    RiskType,
)
from app.core.errors import ProviderError, SchemaValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Evidence budget for the document-level metadata and parties calls. The front
#: matter and signature block, not the whole agreement.
_HEAD_BUDGET = 9_000

#: Concurrent provider calls per contract. Bounded so one large contract cannot
#: exhaust the provider rate limit for every other job on the worker.
_DEFAULT_CONCURRENCY = 4


@dataclass(slots=True)
class ClauseDefinition:
    """One clause category to extract.

    Built from a ``ClauseMasterCategory`` row plus its current rule, or from a seed
    in tests. A plain dataclass so the engine holds no ORM objects across an await.
    """

    key: str
    name: str
    priority: int
    mandatory: bool
    extraction_rule: dict[str, Any]
    output_schema: dict[str, Any]
    synonyms: list[str] = field(default_factory=list)
    confidence_threshold: float = 0.85
    standard_text: str | None = None
    notes: str | None = None
    group_name: str | None = None

    @classmethod
    def from_category(cls, category: Any, rule: Any = None) -> ClauseDefinition:
        """Adapt a Clause Master category and its active rule version."""
        rule = rule if rule is not None else getattr(category, "current_rule", None)
        return cls(
            key=str(category.key),
            name=category.name,
            priority=int(category.priority or 999),
            mandatory=bool(category.mandatory),
            extraction_rule=dict(getattr(rule, "extraction_rule", None) or {}),
            output_schema=dict(getattr(rule, "output_schema", None) or {}),
            synonyms=list(getattr(rule, "synonyms", None) or []),
            confidence_threshold=float(category.confidence_threshold or 0.85),
            standard_text=getattr(rule, "standard_text", None),
            notes=category.description,
            group_name=category.group_name,
        )

    @classmethod
    def from_seed(cls, seed: Any) -> ClauseDefinition:
        return cls(
            key=str(seed.key),
            name=seed.name,
            priority=seed.priority,
            mandatory=seed.mandatory,
            extraction_rule=dict(seed.extraction_rule),
            output_schema=dict(seed.output_schema),
            synonyms=list(seed.synonyms),
            confidence_threshold=seed.confidence_threshold,
            notes=seed.notes,
            group_name=seed.group_name,
        )


@dataclass(slots=True)
class ExtractionRequest:
    """Everything the engine needs for one contract."""

    chunks: list[CandidateChunk]
    clause_definitions: list[ClauseDefinition]
    #: The profile, for validation rules, risk weighting and mandatory clauses.
    profile: Any = None
    language: str | None = None
    #: Restrict to these clause keys - used by "re-extract one clause" from the UI.
    only_clauses: list[str] | None = None
    #: Restrict to these categories; defaults to all.
    only_categories: list[str] | None = None
    concurrency: int = _DEFAULT_CONCURRENCY
    evidence_budget: int = DEFAULT_EVIDENCE_BUDGET
    evidence_limit: int = DEFAULT_EVIDENCE_LIMIT
    #: Progress callback, so a 23-clause extraction reports as it goes rather than
    #: appearing stalled for its duration.
    on_progress: Any = None

    @property
    def mandatory_clause_keys(self) -> list[str]:
        from_profile = list(getattr(self.profile, "mandatory_clauses", None) or [])
        if from_profile:
            return [str(key) for key in from_profile]
        return [d.key for d in self.clause_definitions if d.mandatory]


class ExtractionEngine:
    """Extracts structured knowledge from a chunked contract."""

    def __init__(self, provider: IInferenceProvider | None = None) -> None:
        self._provider = provider or get_inference_provider()
        self._settings = get_settings()

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        result = ExtractionResult()

        if not request.chunks:
            result.warnings.append(
                "The contract produced no chunks, so there was nothing to extract."
            )
            return result

        selector = EvidenceSelector(request.chunks)
        builder = ExtractionPromptBuilder(
            profile=request.profile,
            language=request.language,
            organisation_aliases=list(self._settings.organization_legal_names),
        )
        rules = dict(getattr(request.profile, "validation_rules", None) or {})
        aliases = list(self._settings.organization_legal_names)

        # ---- 1. Parties first --------------------------------------------------
        # Every party-side attribute is resolved against these names, so they have
        # to exist before any clause is validated.
        if self._wants(request, "parties"):
            await self._report(request, 62, "identifying parties")
            result.parties = await self._extract_parties(selector, builder, result, rules, aliases)

        party_sides = {
            _normalise(party.name): party.is_our_organisation for party in result.parties
        }

        # ---- 2. Document-level facts ------------------------------------------
        if self._wants(request, "metadata"):
            await self._report(request, 65, "reading document metadata")
            result.facts = await self._extract_metadata(
                selector, builder, result, rules, aliases, party_sides
            )
        if result.parties:
            primary = [p for p in result.parties if p.is_primary] or result.parties
            result.facts.party_a = result.facts.party_a or primary[0].name
            if len(primary) > 1:
                result.facts.party_b = result.facts.party_b or primary[1].name

        # ---- 3. Clauses, in priority order ------------------------------------
        if self._wants(request, "clauses"):
            definitions = self._clause_plan(request)
            await self._report(request, 68, f"extracting {len(definitions)} clause categories")
            result.clauses = await self._extract_clauses(
                definitions,
                selector,
                builder,
                result,
                rules,
                aliases,
                party_sides,
                request,
            )

        # ---- 4. Categories derived from the clauses ---------------------------
        # Run after clauses because their prompts include the extracted terms, which
        # both improves attribution and stops the model re-reading the whole
        # contract to rediscover them.
        derived: list[Any] = []
        if self._wants(request, "financial"):
            derived.append(
                self._run_financial(selector, builder, result, rules, aliases, party_sides)
            )
        if self._wants(request, "obligations"):
            derived.append(
                self._run_obligations(selector, builder, result, rules, aliases, party_sides)
            )
        if self._wants(request, "dates"):
            derived.append(self._run_dates(selector, builder, result, rules, aliases, party_sides))
        if self._wants(request, "risks"):
            derived.append(
                self._run_model_risks(selector, builder, result, rules, aliases, party_sides)
            )
        if self._wants(request, "relationships"):
            derived.append(
                self._run_relationships(selector, builder, result, rules, aliases, party_sides)
            )

        if derived:
            await self._report(request, 76, "extracting obligations, dates and risks")
            await asyncio.gather(*derived)

        # ---- 5. Deterministic risk assessment ---------------------------------
        await self._report(request, 80, "assessing risk")
        assessor = RiskAssessor(
            mandatory_clauses=request.mandatory_clause_keys,
            risk_mapping=dict(getattr(request.profile, "risk_mapping", None) or {}),
        )
        result.assessment = assessor.assess(clauses=result.clauses, model_risks=result.risks)
        result.risks = result.assessment.risks

        # ---- 6. Review triggers -----------------------------------------------
        self._apply_review_triggers(result, request)

        logger.info(
            "extraction_completed",
            clauses=len(result.clauses),
            clause_types=len(result.clause_types_found()),
            parties=len(result.parties),
            obligations=len(result.obligations),
            risks=len(result.risks),
            key_dates=len(result.key_dates),
            risk_score=result.assessment.score,
            risk_band=result.assessment.band.value,
            missing_mandatory=len(result.assessment.missing_mandatory),
            llm_calls=result.llm_calls,
            cost_usd=result.total_cost_usd,
            needs_review=result.needs_review,
            failed_categories=result.failed_categories,
        )
        return result

    # =========================================================================
    # Clause extraction
    # =========================================================================
    def _clause_plan(self, request: ExtractionRequest) -> list[ClauseDefinition]:
        """The clause categories to run, in priority order."""
        definitions = sorted(request.clause_definitions, key=lambda d: (d.priority, d.key))
        if request.only_clauses:
            wanted = set(request.only_clauses)
            definitions = [d for d in definitions if d.key in wanted]
        return definitions

    async def _extract_clauses(
        self,
        definitions: list[ClauseDefinition],
        selector: EvidenceSelector,
        builder: ExtractionPromptBuilder,
        result: ExtractionResult,
        rules: dict[str, Any],
        aliases: list[str],
        party_sides: dict[str, bool],
        request: ExtractionRequest,
    ) -> list[ExtractedClause]:
        """Extract every clause category, bounded concurrency, priority ordered.

        Concurrency does not weaken the priority guarantee: the categories are
        *dispatched* in priority order, so under a rate limit the high-priority ones
        are the ones already in flight.
        """
        semaphore = asyncio.Semaphore(max(1, request.concurrency))
        completed = 0
        total = len(definitions)

        async def run(definition: ClauseDefinition) -> list[ExtractedClause]:
            nonlocal completed
            async with semaphore:
                clauses = await self._extract_one_clause(
                    definition, selector, builder, result, rules, aliases, party_sides, request
                )
            completed += 1
            # 68 -> 76 across the clause categories.
            await self._report(
                request,
                68 + int(8 * completed / max(total, 1)),
                f"clauses {completed}/{total}",
            )
            return clauses

        batches = await asyncio.gather(
            *(run(definition) for definition in definitions), return_exceptions=True
        )

        clauses: list[ExtractedClause] = []
        for definition, batch in zip(definitions, batches, strict=True):
            if isinstance(batch, BaseException):
                # Already logged and recorded by _extract_one_clause for anything it
                # anticipated; this is the unanticipated case.
                logger.exception(
                    "clause_extraction_crashed", clause_key=definition.key, exc_info=batch
                )
                result.outcomes.append(
                    CategoryOutcome(
                        category=f"clause:{definition.key}",
                        prompt_id="extraction.clauses",
                        status="error",
                        detail=str(batch),
                    )
                )
                continue
            clauses.extend(batch)

        # Priority order in the output too: this is the order the UI lists them in.
        order = {definition.key: index for index, definition in enumerate(definitions)}
        clauses.sort(key=lambda c: (order.get(c.clause_type, 999), -c.confidence))
        return clauses

    async def _extract_one_clause(
        self,
        definition: ClauseDefinition,
        selector: EvidenceSelector,
        builder: ExtractionPromptBuilder,
        result: ExtractionResult,
        rules: dict[str, Any],
        aliases: list[str],
        party_sides: dict[str, bool],
        request: ExtractionRequest,
    ) -> list[ExtractedClause]:
        category = f"clause:{definition.key}"
        outcome = CategoryOutcome(category=category, prompt_id="extraction.clauses")

        bundle = selector.select(
            category=definition.key,
            rule=definition.extraction_rule,
            synonyms=definition.synonyms,
            budget_tokens=request.evidence_budget,
            limit=request.evidence_limit,
        )
        outcome.evidence_chunks = len(bundle.chunks)

        if bundle.is_empty:
            # No candidate evidence: report absence rather than spending a call on a
            # question the document does not answer.
            outcome.status = "not_found"
            outcome.detail = "No chunk matched this clause category's extraction rule."
            result.outcomes.append(outcome)
            metrics.extractions_total.labels(category=category, outcome="not_found").inc()
            return []

        spec = builder.clause(
            clause_key=definition.key,
            clause_name=definition.name,
            attribute_schema=definition.output_schema,
            bundle=bundle,
            synonyms=definition.synonyms,
            standard_text=definition.standard_text,
            notes=definition.notes,
            parties=result.parties,
        )

        structured = await self._call(spec, outcome, result)
        if structured is None:
            return []

        ctx = ValidationContext(
            bundle=bundle,
            organisation_aliases=aliases,
            party_sides=party_sides,
            rules=rules,
            confidence_threshold=definition.confidence_threshold,
        )

        clauses = self._parse_clauses(definition, structured.data, spec, ctx, outcome, bundle)
        outcome.item_count = len(clauses)
        if not clauses and outcome.status == "ok":
            outcome.status = "not_found"
            outcome.detail = (
                str(structured.data.get("absence_reason") or "")
                or "The model reported the clause as absent from the evidence."
            )
        result.outcomes.append(outcome)
        metrics.extractions_total.labels(
            category=category,
            outcome="valid" if clauses else outcome.status,
        ).inc()
        if clauses:
            metrics.extracted_items_total.labels(kind="clause").inc(len(clauses))
        return clauses

    def _parse_clauses(
        self,
        definition: ClauseDefinition,
        payload: dict[str, Any],
        spec: PromptSpec,
        ctx: ValidationContext,
        outcome: CategoryOutcome,
        bundle: EvidenceBundle,
    ) -> list[ExtractedClause]:
        """Turn one response into validated clauses, dropping what fails."""
        if not payload.get("found"):
            return []

        raw_clauses = payload.get("clauses")
        if not isinstance(raw_clauses, list):
            outcome.status = "invalid"
            outcome.detail = "The response did not contain a clause list."
            return []

        clauses: list[ExtractedClause] = []
        for index, raw in enumerate(raw_clauses):
            if not isinstance(raw, dict):
                continue

            field_outcome = FieldOutcome()
            valid_ids = check_citations(
                raw.get("evidence_chunk_ids"),
                ctx,
                field_outcome,
                field_name=f"clauses[{index}].evidence_chunk_ids",
            )
            text = str(raw.get("text") or "")
            check_quote(text, ctx, field_outcome, field_name=f"clauses[{index}].text")
            confidence = check_confidence(raw.get("confidence"), field_outcome)

            attributes = raw.get("attributes")
            attributes = dict(attributes) if isinstance(attributes, dict) else {}
            attribute_outcome = validate_clause_attributes(
                clause_key=definition.key,
                attributes=attributes,
                schema=definition.output_schema,
                ctx=ctx,
            )
            field_outcome.merge(attribute_outcome)
            # Corrections are applied, not merely reported: the stored value must be
            # the coherent one, with the correction recorded as an issue so the
            # reviewer can see what changed.
            attributes.update(attribute_outcome.corrections)

            if field_outcome.has_errors:
                # A clause that fails a hard check is discarded rather than stored
                # with a caveat. An unverifiable quote or an incoherent cap in the
                # repository is worse than a gap, because it will be relied on.
                outcome.issues.extend(field_outcome.errors)
                logger.warning(
                    "clause_rejected",
                    clause_key=definition.key,
                    reasons=[issue.code for issue in field_outcome.errors],
                    clause_number=raw.get("clause_number"),
                )
                metrics.extractions_total.labels(
                    category=f"clause:{definition.key}", outcome="business_invalid"
                ).inc()
                continue

            evidence = self._evidence_refs(valid_ids, bundle, quote=text)
            clause = ExtractedClause(
                clause_type=definition.key,
                title=_clean(raw.get("title")),
                text=text,
                summary=_clean(raw.get("summary")),
                clause_number=_clean(raw.get("clause_number")),
                attributes=attributes,
                is_mandatory=definition.mandatory,
                confidence=confidence,
                evidence=evidence,
                issues=field_outcome.issues,
                validation_score=field_outcome.score,
                prompt_id=spec.prompt_id,
                prompt_version=spec.prompt_version,
                model_version=outcome.model,
            )
            if evidence:
                clause.chunk_id = evidence[0].chunk_id
                clause.section_id = evidence[0].section_id
                clause.section_title = evidence[0].section_title
                clause.clause_number = clause.clause_number or evidence[0].clause_number

            uncertainty = _clean(raw.get("uncertainty"))
            if uncertainty:
                clause.issues.append(
                    ValidationIssue(
                        code="model_uncertainty",
                        message=uncertainty,
                        severity="warning",
                    )
                )
            if confidence < ctx.confidence_threshold:
                clause.review_status = ReviewStatus.PENDING
            clauses.append(clause)

        return clauses

    # =========================================================================
    # Document-level categories
    # =========================================================================
    async def _extract_parties(
        self,
        selector: EvidenceSelector,
        builder: ExtractionPromptBuilder,
        result: ExtractionResult,
        rules: dict[str, Any],
        aliases: list[str],
    ) -> list[ExtractedParty]:
        bundle = selector.head(_HEAD_BUDGET)
        outcome = CategoryOutcome(category="parties", prompt_id="extraction.parties")
        outcome.evidence_chunks = len(bundle.chunks)

        spec = builder.parties(bundle)
        structured = await self._call(spec, outcome, result)
        if structured is None:
            return []

        ctx = ValidationContext(bundle=bundle, organisation_aliases=aliases, rules=rules)
        parties: list[ExtractedParty] = []

        for index, raw in enumerate(structured.data.get("parties") or []):
            if not isinstance(raw, dict):
                continue
            name = _clean(raw.get("name"))
            if not name:
                continue

            field_outcome = FieldOutcome()
            valid_ids = check_citations(
                raw.get("evidence_chunk_ids"),
                ctx,
                field_outcome,
                field_name=f"parties[{index}].evidence_chunk_ids",
            )
            confidence = check_confidence(raw.get("confidence"), field_outcome)
            if field_outcome.has_errors:
                outcome.issues.extend(field_outcome.errors)
                continue

            side = ctx.side_of(name)
            party = ExtractedParty(
                name=name,
                legal_name=_clean(raw.get("legal_name")),
                entity_type=_enum_or(EntityType, raw.get("entity_type"), EntityType.PARTY),
                role=_clean(raw.get("role")),
                aliases=[str(a) for a in (raw.get("aliases") or []) if a],
                jurisdiction=_clean(raw.get("jurisdiction")),
                registration_number=_clean(raw.get("registration_number")),
                address=_clean(raw.get("address")),
                contact={
                    key: value
                    for key, value in (
                        ("email", _clean(raw.get("contact_email"))),
                        ("person", _clean(raw.get("contact_person"))),
                    )
                    if value
                },
                is_primary=bool(raw.get("is_primary")),
                is_our_organisation=side.value == "our_organisation",
                confidence=confidence,
                evidence=self._evidence_refs(valid_ids, bundle),
                issues=field_outcome.issues,
                validation_score=field_outcome.score,
                prompt_id=spec.prompt_id,
                prompt_version=spec.prompt_version,
                model_version=outcome.model,
            )
            parties.append(party)

        outcome.item_count = len(parties)
        if not parties:
            outcome.status = "not_found"
        result.outcomes.append(outcome)
        metrics.extracted_items_total.labels(kind="entity").inc(len(parties))
        return parties

    async def _extract_metadata(
        self,
        selector: EvidenceSelector,
        builder: ExtractionPromptBuilder,
        result: ExtractionResult,
        rules: dict[str, Any],
        aliases: list[str],
        party_sides: dict[str, bool],
    ) -> ContractFacts:
        bundle = selector.head(_HEAD_BUDGET)
        outcome = CategoryOutcome(category="metadata", prompt_id="extraction.metadata")
        outcome.evidence_chunks = len(bundle.chunks)

        spec = builder.metadata(bundle)
        structured = await self._call(spec, outcome, result)
        if structured is None:
            return ContractFacts()

        payload = structured.data
        ctx = ValidationContext(
            bundle=bundle,
            organisation_aliases=aliases,
            party_sides=party_sides,
            rules=rules,
        )
        field_outcome = FieldOutcome()
        check_citations(
            payload.get("evidence_chunk_ids"), ctx, field_outcome, field_name="evidence"
        )
        confidence = check_confidence(payload.get("confidence"), field_outcome)
        check_generic_attributes(payload, ctx, field_outcome)
        check_date_order(payload, field_outcome, ctx)

        facts = ContractFacts(
            title=_clean(payload.get("title")),
            summary=_clean(payload.get("summary")),
            key_topics=[str(t) for t in (payload.get("key_topics") or []) if t],
            contract_value=_number(payload.get("contract_value")),
            currency=_currency(payload.get("currency")),
            effective_date=parse_date(payload.get("effective_date")),
            execution_date=parse_date(payload.get("execution_date")),
            expiration_date=parse_date(payload.get("expiration_date")),
            term_months=_integer(payload.get("term_months")),
            confidence=confidence,
            issues=field_outcome.issues,
        )

        outcome.item_count = 1
        result.outcomes.append(outcome)
        return facts

    async def _run_financial(
        self,
        selector: EvidenceSelector,
        builder: ExtractionPromptBuilder,
        result: ExtractionResult,
        rules: dict[str, Any],
        aliases: list[str],
        party_sides: dict[str, bool],
    ) -> None:
        """Financial terms. Folded into the document facts rather than stored apart."""
        bundle = selector.select(
            category="financial",
            rule={
                "heading_patterns": ["fees", "payment", "charges", "price", "compensation"],
                "keywords": [
                    "shall pay",
                    "invoice",
                    "net 30",
                    "payment terms",
                    "fees",
                    "total value",
                ],
                "min_tokens": 10,
            },
            budget_tokens=DEFAULT_EVIDENCE_BUDGET,
        )
        outcome = CategoryOutcome(category="financial", prompt_id="extraction.financial")
        outcome.evidence_chunks = len(bundle.chunks)

        if bundle.is_empty:
            outcome.status = "not_found"
            result.outcomes.append(outcome)
            return

        spec = builder.financial(bundle)
        structured = await self._call(spec, outcome, result)
        if structured is None:
            return

        payload = structured.data
        ctx = ValidationContext(
            bundle=bundle,
            organisation_aliases=aliases,
            party_sides=party_sides,
            rules=rules,
        )
        field_outcome = FieldOutcome()
        check_citations(
            payload.get("evidence_chunk_ids"), ctx, field_outcome, field_name="evidence"
        )
        check_generic_attributes(payload, ctx, field_outcome)

        # Only fill gaps: a value stated in the agreement's own preamble is more
        # authoritative than one reconstructed from a fee schedule.
        facts = result.facts
        facts.contract_value = facts.contract_value or _number(payload.get("total_value"))
        facts.currency = facts.currency or _currency(payload.get("currency"))
        facts.payment_terms_days = _integer(payload.get("payment_days"))
        facts.issues.extend(field_outcome.issues)

        outcome.item_count = 1
        result.outcomes.append(outcome)

    async def _run_obligations(
        self,
        selector: EvidenceSelector,
        builder: ExtractionPromptBuilder,
        result: ExtractionResult,
        rules: dict[str, Any],
        aliases: list[str],
        party_sides: dict[str, bool],
    ) -> None:
        bundle = selector.select(
            category="obligations",
            rule={
                "heading_patterns": [],
                "keywords": [
                    "shall",
                    "must",
                    "agrees to",
                    "undertakes to",
                    "is responsible for",
                    "shall not",
                    "no later than",
                ],
                "min_tokens": 15,
            },
            budget_tokens=int(DEFAULT_EVIDENCE_BUDGET * 2),
            limit=DEFAULT_EVIDENCE_LIMIT * 2,
        )
        outcome = CategoryOutcome(category="obligations", prompt_id="extraction.obligations")
        outcome.evidence_chunks = len(bundle.chunks)

        if bundle.is_empty:
            outcome.status = "not_found"
            result.outcomes.append(outcome)
            return

        spec = builder.obligations(bundle, clauses=result.clauses)
        structured = await self._call(spec, outcome, result)
        if structured is None:
            return

        ctx = ValidationContext(
            bundle=bundle,
            organisation_aliases=aliases,
            party_sides=party_sides,
            rules=rules,
        )
        obligations: list[ExtractedObligation] = []

        for index, raw in enumerate(structured.data.get("obligations") or []):
            if not isinstance(raw, dict):
                continue
            action = _clean(raw.get("action"))
            if not action:
                continue

            field_outcome = FieldOutcome()
            valid_ids = check_citations(
                raw.get("evidence_chunk_ids"),
                ctx,
                field_outcome,
                field_name=f"obligations[{index}].evidence_chunk_ids",
            )
            confidence = check_confidence(raw.get("confidence"), field_outcome)
            check_generic_attributes(raw, ctx, field_outcome)
            if field_outcome.has_errors:
                outcome.issues.extend(field_outcome.errors)
                continue

            evidence = self._evidence_refs(valid_ids, bundle)
            obligations.append(
                ExtractedObligation(
                    action=action,
                    responsible_party=_clean(raw.get("responsible_party")),
                    due_date=parse_date(raw.get("due_date")),
                    due_description=_clean(raw.get("due_description")),
                    trigger_event=_clean(raw.get("trigger_event")),
                    dependency=_clean(raw.get("dependency")),
                    frequency=_clean(raw.get("frequency")),
                    is_recurring=bool(raw.get("is_recurring")),
                    status=ObligationStatus.OPEN,
                    penalty=_clean(raw.get("penalty")),
                    clause_type=_clean(raw.get("clause_type")),
                    chunk_id=evidence[0].chunk_id if evidence else None,
                    confidence=confidence,
                    evidence=evidence,
                    issues=field_outcome.issues,
                    validation_score=field_outcome.score,
                    prompt_id=spec.prompt_id,
                    prompt_version=spec.prompt_version,
                    model_version=outcome.model,
                )
            )

        result.obligations = obligations
        outcome.item_count = len(obligations)
        if not obligations:
            outcome.status = "not_found"
        result.outcomes.append(outcome)
        metrics.extracted_items_total.labels(kind="obligation").inc(len(obligations))

    async def _run_dates(
        self,
        selector: EvidenceSelector,
        builder: ExtractionPromptBuilder,
        result: ExtractionResult,
        rules: dict[str, Any],
        aliases: list[str],
        party_sides: dict[str, bool],
    ) -> None:
        bundle = selector.select(
            category="dates",
            rule={
                "heading_patterns": ["term", "duration", "renewal", "notice", "milestones"],
                "keywords": [
                    "effective date",
                    "commencement",
                    "expire",
                    "expiration",
                    "renew",
                    "days prior",
                    "no later than",
                    "within",
                    "anniversary",
                ],
                "min_tokens": 10,
            },
            budget_tokens=DEFAULT_EVIDENCE_BUDGET,
        )
        outcome = CategoryOutcome(category="dates", prompt_id="extraction.dates")
        outcome.evidence_chunks = len(bundle.chunks)

        if bundle.is_empty:
            outcome.status = "not_found"
            result.outcomes.append(outcome)
            return

        spec = builder.dates(bundle)
        structured = await self._call(spec, outcome, result)
        if structured is None:
            return

        ctx = ValidationContext(
            bundle=bundle,
            organisation_aliases=aliases,
            party_sides=party_sides,
            rules=rules,
        )
        dates: list[ExtractedKeyDate] = []

        for index, raw in enumerate(structured.data.get("dates") or []):
            if not isinstance(raw, dict):
                continue

            field_outcome = FieldOutcome()
            valid_ids = check_citations(
                raw.get("evidence_chunk_ids"),
                ctx,
                field_outcome,
                field_name=f"dates[{index}].evidence_chunk_ids",
            )
            confidence = check_confidence(raw.get("confidence"), field_outcome)

            value = parse_date(raw.get("date_value"))
            expression = _clean(raw.get("date_expression"))
            # A key date with neither an absolute value nor a wording is not a date;
            # storing it would put an empty row on the reviewer's timeline.
            if not field_outcome.check(bool(value or expression)):
                field_outcome.add(
                    "date_without_value",
                    "Neither a date nor a date expression was extracted.",
                    field_name=f"dates[{index}]",
                )
            if field_outcome.has_errors:
                outcome.issues.extend(field_outcome.errors)
                continue

            evidence = self._evidence_refs(valid_ids, bundle)
            dates.append(
                ExtractedKeyDate(
                    date_type=_enum_or(DateType, raw.get("date_type"), DateType.OTHER),
                    date_value=value,
                    date_expression=expression,
                    description=_clean(raw.get("description")),
                    is_recurring=bool(raw.get("is_recurring")),
                    chunk_id=evidence[0].chunk_id if evidence else None,
                    confidence=confidence,
                    evidence=evidence,
                    issues=field_outcome.issues,
                    validation_score=field_outcome.score,
                    prompt_id=spec.prompt_id,
                    prompt_version=spec.prompt_version,
                    model_version=outcome.model,
                )
            )

        result.key_dates = dates
        # Promote the canonical dates onto the document facts where metadata missed
        # them - the term clause states them more reliably than the preamble.
        for entry in dates:
            if entry.date_value is None:
                continue
            if entry.date_type is DateType.EFFECTIVE_DATE and not result.facts.effective_date:
                result.facts.effective_date = entry.date_value
            elif entry.date_type is DateType.EXPIRATION_DATE and not result.facts.expiration_date:
                result.facts.expiration_date = entry.date_value
            elif entry.date_type is DateType.EXECUTION_DATE and not result.facts.execution_date:
                result.facts.execution_date = entry.date_value

        outcome.item_count = len(dates)
        if not dates:
            outcome.status = "not_found"
        result.outcomes.append(outcome)
        metrics.extracted_items_total.labels(kind="timeline").inc(len(dates))

    async def _run_model_risks(
        self,
        selector: EvidenceSelector,
        builder: ExtractionPromptBuilder,
        result: ExtractionResult,
        rules: dict[str, Any],
        aliases: list[str],
        party_sides: dict[str, bool],
    ) -> None:
        """Model-identified risks, merged with the rule-derived ones by the assessor."""
        bundle = selector.select(
            category="risks",
            rule={
                "heading_patterns": [
                    "liability",
                    "indemnification",
                    "termination",
                    "warranty",
                    "remedies",
                ],
                "keywords": [
                    "shall not be liable",
                    "sole remedy",
                    "at its sole discretion",
                    "without cause",
                    "irrevocably",
                    "waives",
                    "notwithstanding",
                ],
                "min_tokens": 20,
            },
            budget_tokens=DEFAULT_EVIDENCE_BUDGET,
        )
        outcome = CategoryOutcome(category="risks", prompt_id="extraction.risks")
        outcome.evidence_chunks = len(bundle.chunks)

        if bundle.is_empty:
            outcome.status = "not_found"
            result.outcomes.append(outcome)
            return

        spec = builder.risks(bundle, clauses=result.clauses)
        structured = await self._call(spec, outcome, result)
        if structured is None:
            return

        ctx = ValidationContext(
            bundle=bundle,
            organisation_aliases=aliases,
            party_sides=party_sides,
            rules=rules,
        )
        risks: list[ExtractedRisk] = []

        for index, raw in enumerate(structured.data.get("risks") or []):
            if not isinstance(raw, dict):
                continue
            description = _clean(raw.get("description"))
            if not description:
                continue

            field_outcome = FieldOutcome()
            valid_ids = check_citations(
                raw.get("evidence_chunk_ids"),
                ctx,
                field_outcome,
                field_name=f"risks[{index}].evidence_chunk_ids",
            )
            confidence = check_confidence(raw.get("confidence"), field_outcome)
            if field_outcome.has_errors:
                outcome.issues.extend(field_outcome.errors)
                continue

            evidence = self._evidence_refs(valid_ids, bundle)
            risks.append(
                ExtractedRisk(
                    risk_type=_enum_or(RiskType, raw.get("risk_type"), RiskType.OTHER).value,
                    severity=_enum_or(RiskSeverity, raw.get("severity"), RiskSeverity.MEDIUM),
                    description=description,
                    recommendation=_clean(raw.get("recommendation")),
                    clause_type=_clean(raw.get("clause_type")),
                    category="model",
                    chunk_id=evidence[0].chunk_id if evidence else None,
                    confidence=confidence,
                    evidence=evidence,
                    issues=field_outcome.issues,
                    validation_score=field_outcome.score,
                    prompt_id=spec.prompt_id,
                    prompt_version=spec.prompt_version,
                    model_version=outcome.model,
                )
            )

        result.risks = risks
        outcome.item_count = len(risks)
        if not risks:
            outcome.status = "not_found"
        result.outcomes.append(outcome)

    async def _run_relationships(
        self,
        selector: EvidenceSelector,
        builder: ExtractionPromptBuilder,
        result: ExtractionResult,
        rules: dict[str, Any],
        aliases: list[str],
        party_sides: dict[str, bool],
    ) -> None:
        bundle = selector.select(
            category="relationships",
            rule={
                "heading_patterns": ["survival", "entire agreement", "schedules", "exhibits"],
                "keywords": [
                    "as set out in",
                    "pursuant to section",
                    "schedule",
                    "exhibit",
                    "annex",
                    "shall survive",
                    "incorporated by reference",
                    "amends",
                    "supersedes",
                ],
                "min_tokens": 10,
            },
            budget_tokens=DEFAULT_EVIDENCE_BUDGET,
        )
        outcome = CategoryOutcome(category="relationships", prompt_id="extraction.relationships")
        outcome.evidence_chunks = len(bundle.chunks)

        if bundle.is_empty:
            outcome.status = "not_found"
            result.outcomes.append(outcome)
            return

        spec = builder.relationships(bundle)
        structured = await self._call(spec, outcome, result)
        if structured is None:
            return

        ctx = ValidationContext(
            bundle=bundle,
            organisation_aliases=aliases,
            party_sides=party_sides,
            rules=rules,
        )
        relationships: list[ExtractedRelationship] = []

        for index, raw in enumerate(structured.data.get("relationships") or []):
            if not isinstance(raw, dict):
                continue
            relation = _clean(raw.get("relation"))
            source = _clean(raw.get("source_ref"))
            target = _clean(raw.get("target_ref"))
            if not (relation and source and target):
                continue

            field_outcome = FieldOutcome()
            valid_ids = check_citations(
                raw.get("evidence_chunk_ids"),
                ctx,
                field_outcome,
                field_name=f"relationships[{index}].evidence_chunk_ids",
            )
            confidence = check_confidence(raw.get("confidence"), field_outcome)
            if field_outcome.has_errors:
                outcome.issues.extend(field_outcome.errors)
                continue

            relationships.append(
                ExtractedRelationship(
                    relation=relation,
                    source_type=_clean(raw.get("source_type")) or "term",
                    source_ref=source,
                    target_type=_clean(raw.get("target_type")) or "term",
                    target_ref=target,
                    label=_clean(raw.get("label")),
                    confidence=confidence,
                    evidence=self._evidence_refs(valid_ids, bundle),
                    issues=field_outcome.issues,
                    validation_score=field_outcome.score,
                    prompt_id=spec.prompt_id,
                    prompt_version=spec.prompt_version,
                    model_version=outcome.model,
                )
            )

        result.relationships = relationships
        outcome.item_count = len(relationships)
        if not relationships:
            outcome.status = "not_found"
        result.outcomes.append(outcome)
        metrics.extracted_items_total.labels(kind="relationship").inc(len(relationships))

    # =========================================================================
    # Summary
    # =========================================================================
    async def summarise(self, result: ExtractionResult) -> None:
        """Add the executive summary, from extracted facts only.

        Separate from ``extract`` because it depends on everything else having
        finished, and because a summary failure must not cost the extraction.
        """
        builder = ExtractionPromptBuilder(
            organisation_aliases=list(self._settings.organization_legal_names)
        )
        outcome = CategoryOutcome(category="summary", prompt_id="summary.document")
        spec = builder.summary(
            facts=result.facts,
            clauses=result.clauses,
            parties=result.parties,
            risk_notes=[
                f"{risk.severity.value}: {risk.description}"
                for risk in result.assessment.risks[:10]
            ],
        )
        structured = await self._call(spec, outcome, result)
        if structured is None:
            return

        payload = structured.data
        result.facts.executive_summary = _clean(payload.get("executive_summary"))
        key_points = [str(point) for point in (payload.get("key_points") or []) if point]
        if key_points and not result.facts.key_topics:
            result.facts.key_topics = key_points[:10]
        outcome.item_count = 1
        result.outcomes.append(outcome)

    # =========================================================================
    # Provider plumbing
    # =========================================================================
    async def _call(
        self, spec: PromptSpec, outcome: CategoryOutcome, result: ExtractionResult
    ) -> StructuredResult | None:
        """Make one structured call, recording cost and translating failures.

        Failures are recorded on the category and swallowed. That is deliberate: the
        engine's contract is that one category cannot take down the extraction, and
        the stage handler decides whether the surviving categories are enough.
        """
        try:
            structured = await self._provider.generate_structured(
                system=spec.system,
                prompt=spec.user,
                schema=spec.schema,
                purpose=spec.purpose,
                cache_prefix=True,
            )
        except ProviderError as exc:
            refused = "declined" in str(exc).lower()
            outcome.status = "refused" if refused else "error"
            outcome.detail = str(exc)
            logger.warning(
                "extraction_call_failed",
                category=outcome.category,
                prompt_id=spec.prompt_id,
                refused=refused,
                error=str(exc),
            )
            metrics.extractions_total.labels(
                category=outcome.category, outcome=outcome.status
            ).inc()
            result.warnings.append(
                f"{outcome.category} could not be extracted: {exc}. "
                "The remaining categories were unaffected."
            )
            return None
        except SchemaValidationError as exc:
            outcome.status = "invalid"
            outcome.detail = str(exc)
            logger.warning("extraction_schema_invalid", category=outcome.category, error=str(exc))
            metrics.extractions_total.labels(
                category=outcome.category, outcome="schema_invalid"
            ).inc()
            result.warnings.append(f"{outcome.category} returned an unusable response.")
            return None
        except Exception as exc:
            outcome.status = "error"
            outcome.detail = str(exc)
            logger.exception("extraction_call_crashed", category=outcome.category)
            metrics.extractions_total.labels(category=outcome.category, outcome="error").inc()
            return None

        usage = structured.usage
        outcome.llm_calls += 1
        outcome.input_tokens += usage.input_tokens
        outcome.output_tokens += usage.output_tokens
        outcome.cache_read_tokens += usage.cache_read_tokens
        outcome.cost_usd += structured.cost_usd
        outcome.latency_ms += structured.inference.latency_ms
        outcome.model = structured.model

        # Second line of defence: the provider constrains output to the schema, but a
        # mock or a non-strict deployment may not.
        for issue in validate_against_schema(structured.data, spec.schema):
            outcome.issues.append(issue)
        return structured

    # =========================================================================
    # Helpers
    # =========================================================================
    def _evidence_refs(
        self, chunk_ids: list[str], bundle: EvidenceBundle, *, quote: str | None = None
    ) -> list[EvidenceRef]:
        """Build evidence references from validated chunk ids.

        Coordinates are copied from the chunk rather than referenced, so the highlight
        survives a later re-chunk that changes chunk ids (§7.2).
        """
        refs: list[EvidenceRef] = []
        for chunk_id in chunk_ids:
            chunk = bundle.by_id(chunk_id)
            if chunk is None:
                continue
            refs.append(
                EvidenceRef(
                    chunk_id=chunk.chunk_id,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    bounding_boxes=list(chunk.bounding_boxes),
                    section_id=chunk.section_id,
                    section_title=chunk.section_title,
                    clause_number=chunk.clause_number,
                    quote=quote,
                )
            )
        return refs

    def _wants(self, request: ExtractionRequest, category: str) -> bool:
        if request.only_categories is None:
            return True
        return category in request.only_categories

    async def _report(self, request: ExtractionRequest, percent: int, note: str) -> None:
        if request.on_progress is None:
            return
        await request.on_progress(percent, note)

    def _apply_review_triggers(self, result: ExtractionResult, request: ExtractionRequest) -> None:
        """Decide whether this extraction needs a human (§13).

        Triggers come from the profile so a document type can be stricter than the
        default without a code change.
        """
        rules = dict(getattr(request.profile, "review_rules", None) or {})
        # (trigger, message). The trigger is the metric label - a bounded set - and
        # the message is what the reviewer reads.
        triggered: list[tuple[str, str]] = []

        threshold = float(getattr(request.profile, "confidence_threshold", None) or 0.85)

        if rules.get("low_confidence", True):
            low = [clause for clause in result.clauses if clause.confidence < threshold]
            if low:
                triggered.append(
                    (
                        "low_confidence",
                        f"{len(low)} clause(s) were extracted with confidence below "
                        f"{threshold:.0%}.",
                    )
                )
                for clause in low:
                    clause.review_status = ReviewStatus.PENDING

        if rules.get("missing_mandatory_clause", True) and result.assessment.missing_mandatory:
            triggered.append(
                (
                    "missing_mandatory_clause",
                    "Mandatory clauses are absent: "
                    + ", ".join(result.assessment.missing_mandatory)
                    + ".",
                )
            )

        if rules.get("high_risk_clause", True):
            critical = [
                risk for risk in result.assessment.risks if risk.severity is RiskSeverity.CRITICAL
            ]
            if critical:
                triggered.append(
                    (
                        "high_risk_clause",
                        f"{len(critical)} critical risk(s) were identified.",
                    )
                )

        if rules.get("validation_failure", True):
            failed = [
                outcome
                for outcome in result.outcomes
                if outcome.status in {"invalid", "error", "refused"}
            ]
            if failed:
                triggered.append(
                    (
                        "validation_failure",
                        f"{len(failed)} extraction categor(ies) failed: "
                        + ", ".join(outcome.category for outcome in failed)
                        + ".",
                    )
                )

        if rules.get("conflicting_dates", True):
            conflicting = [
                issue
                for issue in result.facts.issues
                if issue.code in {"effective_after_expiration", "execution_after_effective"}
            ]
            if conflicting:
                triggered.append(
                    (
                        "conflicting_dates",
                        "The extracted dates are inconsistent with one another.",
                    )
                )

        corrected = sum(
            1
            for item in _all_items(result)
            for issue in item.issues
            if issue.code == "auto_corrected"
        )
        if corrected:
            triggered.append(
                (
                    "auto_corrected",
                    f"{corrected} field(s) were corrected by validation and should be "
                    "confirmed against the source.",
                )
            )

        result.review_reasons = [message for _, message in triggered]
        result.needs_review = bool(triggered)
        for trigger, _ in triggered:
            metrics.review_triggered_total.labels(trigger=trigger).inc()


def _all_items(result: ExtractionResult) -> list[ExtractedItem]:
    items: list[ExtractedItem] = []
    items.extend(result.clauses)
    items.extend(result.parties)
    items.extend(result.obligations)
    items.extend(result.risks)
    items.extend(result.key_dates)
    items.extend(result.relationships)
    return items


def _clean(value: Any) -> str | None:
    """Trim a string, mapping blanks and the model's own null-words to None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() in {"null", "none", "n/a", "not stated", "not specified"}:
        return None
    return text


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _currency(value: Any) -> str | None:
    code = _clean(value)
    if code is None:
        return None
    code = code.upper()
    return code if len(code) == 3 and code.isalpha() else None


def _normalise(value: str) -> str:
    return normalise_party_name(value)


def _enum_or(enum_cls: Any, value: Any, default: Any) -> Any:
    """Coerce to an enum member, falling back rather than raising.

    Model output is data, not code: an unrecognised enum value degrades to the
    default and the item survives, instead of losing the whole extraction to a
    ``ValueError``.
    """
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            logger.debug("enum_coercion_failed", enum=enum_cls.__name__, value=value)
    return default


__all__ = ["ClauseDefinition", "ExtractionEngine", "ExtractionRequest"]

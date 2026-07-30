"""Stage 6 - AI extraction.

The stage that turns a chunked document into the thing users came for: clauses with
typed attributes, parties, obligations, key dates, risks, a 0-100 risk score, and a
list of the mandatory clauses this contract does not contain.

Division of labour:

* :class:`~app.ai.extraction.engine.ExtractionEngine` decides *what* the contract
  says. It holds no session and touches no table, so it can be run against a fixture
  with no database.
* This handler decides *where it goes*: it loads the chunks and the Clause Master,
  runs the engine, and writes the results plus the ``contract_metadata`` projection
  the dashboards read.

Idempotency is the delicate part. Extraction writes to seven tables, and a retry
must replace rather than duplicate - so ``cleanup`` deletes this contract's rows in
dependency order before the stage re-runs. Clause-level embeddings go too, because
they are keyed to clause ids that are about to stop existing, and a vector pointing
at a deleted clause is a retrieval hit with no evidence behind it.

A partially successful extraction is still persisted. Twenty-two clauses out of
twenty-three is a useful contract; a failed job is not. Which categories failed is
recorded on the job and surfaced as a warning, and the job is flagged for review.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.extraction import (
    CandidateChunk,
    ClauseDefinition,
    ExtractionEngine,
    ExtractionRequest,
    ExtractionResult,
)
from app.ai.extraction.models import (
    ExtractedClause,
    ExtractedItem,
    ExtractedRisk,
)
from app.core.enums import (
    ArtifactKind,
    ClauseType,
    EmbeddingLevel,
    LiabilityCapBasis,
    PartySide,
    PipelineStage,
)
from app.core.errors import PipelineError
from app.core.logging import get_logger
from app.core.versions import ComponentVersions, current_versions_for_stage
from app.models.clause_master import ClauseMasterCategory
from app.orchestrator.stages.base import (
    StageArtifact,
    StageContext,
    StageHandler,
    StageResult,
    register_stage,
)
from app.repositories.chunk import ChunkRepository
from app.repositories.contract import ContractMetadataRepository
from app.repositories.embedding import EmbeddingRepository
from app.repositories.knowledge import (
    ClauseRepository,
    ContractSummaryRepository,
    EntityRepository,
    KeyDateRepository,
    KnowledgeRelationshipRepository,
    ObligationRepository,
    RiskRepository,
)

logger = get_logger(__name__)


class AIExtractionStage(StageHandler):
    stage = PipelineStage.AI_EXTRACTION
    requires = (ArtifactKind.CHUNKS, ArtifactKind.CLASSIFICATION)
    cacheable = True
    retryable = True

    def versions_for(self, ctx: StageContext) -> ComponentVersions:
        return current_versions_for_stage(
            ctx.stage,
            profile_id=str(ctx.profile.id) if ctx.profile else None,
            profile_version=ctx.profile.version if ctx.profile else None,
        )

    async def cleanup(self, ctx: StageContext) -> None:
        """Delete this contract's extracted knowledge before re-running.

        Order matters: obligations and risks carry a nullable FK to ``clauses`` with
        ``ON DELETE SET NULL``, so deleting clauses first would leave detached rows
        for the moment between the two statements. Dependents go first.
        """
        contract_id, project_id = ctx.contract_id, ctx.project_id

        # Clause-level vectors reference clause ids that are about to be deleted.
        vectors = await EmbeddingRepository(ctx.db).delete_for_contract_level(
            contract_id, project_id, EmbeddingLevel.CLAUSE
        )
        summaries = await EmbeddingRepository(ctx.db).delete_for_contract_level(
            contract_id, project_id, EmbeddingLevel.DOCUMENT_SUMMARY
        )

        deleted: dict[str, int] = {"clause_vectors": vectors, "summary_vectors": summaries}
        for name, repository in (
            ("obligations", ObligationRepository(ctx.db)),
            ("risks", RiskRepository(ctx.db)),
            ("key_dates", KeyDateRepository(ctx.db)),
            ("relationships", KnowledgeRelationshipRepository(ctx.db)),
            ("entities", EntityRepository(ctx.db)),
            ("summaries", ContractSummaryRepository(ctx.db)),
            ("clauses", ClauseRepository(ctx.db)),
        ):
            deleted[name] = await repository.delete_for_contract(contract_id, project_id)

        if any(deleted.values()):
            logger.info("extraction_cleanup", contract_id=str(contract_id), deleted=deleted)

    async def run(self, ctx: StageContext) -> StageResult:
        await ctx.report_progress(60, "loading chunks")

        chunks = await self._load_chunks(ctx)
        definitions = await self._load_clause_definitions(ctx)

        if not definitions:
            raise PipelineError(
                "The Clause Master contains no active categories, so there is nothing "
                "to extract. Run the seed step.",
                stage=self.stage.value,
                retryable=False,
            )

        engine = ExtractionEngine()
        request = ExtractionRequest(
            chunks=chunks,
            clause_definitions=definitions,
            profile=ctx.profile,
            language=ctx.contract.language,
            only_clauses=ctx.options.get("only_clauses"),
            only_categories=ctx.options.get("only_categories"),
            on_progress=ctx.report_progress,
        )

        result = await engine.extract(request)

        # The summary is written from the extracted facts, so it runs last and its
        # failure is not allowed to cost the extraction.
        if ctx.options.get("skip_summary") is not True:
            try:
                await engine.summarise(result)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "summary_generation_failed",
                    contract_id=str(ctx.contract_id),
                    error=str(exc),
                )
                result.warnings.append(
                    "The executive summary could not be generated; the extracted "
                    "clauses are unaffected."
                )

        if not self._is_useful(result):
            raise PipelineError(
                "Extraction produced no clauses, parties or dates. The document "
                "parsed and chunked, but no contract terms could be identified - "
                "check the profile's clause rules and the parser output.",
                stage=self.stage.value,
                retryable=False,
                details={
                    "chunks": len(chunks),
                    "categories_run": len(result.outcomes),
                    "failed_categories": result.failed_categories,
                },
            )

        await ctx.report_progress(84, "saving extracted knowledge")
        counts = await self._persist(ctx, result)

        await ctx.report_progress(88, "updating contract metadata")
        await self._update_metadata(ctx, result, counts)

        if result.needs_review:
            ctx.contract.needs_review = True
            await ctx.db.flush()

        logger.info(
            "ai_extraction_completed",
            contract_id=str(ctx.contract_id),
            profile_key=getattr(ctx.profile, "key", None),
            **{f"{key}_saved": value for key, value in counts.items()},
            risk_score=result.assessment.score,
            risk_band=result.assessment.band.value,
            missing_mandatory=result.assessment.missing_mandatory,
            llm_calls=result.llm_calls,
            tokens=result.total_tokens,
            cost_usd=result.total_cost_usd,
            needs_review=result.needs_review,
            failed_categories=result.failed_categories,
        )

        return StageResult(
            artifacts=self._artifacts(result, counts),
            stats={
                "clauses_extracted": counts["clauses"],
                "clause_types": len(result.clause_types_found()),
                "entities_extracted": counts["entities"],
                "obligations_extracted": counts["obligations"],
                "risks_identified": counts["risks"],
                "key_dates_extracted": counts["key_dates"],
                "relationships_extracted": counts["relationships"],
                "risk_score": result.assessment.score,
                "risk_band": result.assessment.band.value,
                "missing_mandatory_clauses": len(result.assessment.missing_mandatory),
                "extraction_llm_calls": result.llm_calls,
                "extraction_tokens": result.total_tokens,
                "extraction_cost_usd": result.total_cost_usd,
                "extraction_failed_categories": result.failed_categories,
            },
            context_updates={
                "risk_score": result.assessment.score,
                "risk_band": result.assessment.band.value,
                "clause_count": counts["clauses"],
                "needs_review": result.needs_review,
            },
            warnings=result.warnings + result.review_reasons,
        )

    # =========================================================================
    # Loading
    # =========================================================================
    async def _load_chunks(self, ctx: StageContext) -> list[CandidateChunk]:
        rows = await ChunkRepository(ctx.db).list_for_contract(ctx.contract_id, ctx.project_id)
        if not rows:
            raise PipelineError(
                "No chunks exist for this contract. Re-run chunking.",
                stage=self.stage.value,
            )
        # Adapted to plain dataclasses immediately: the engine awaits provider calls,
        # and holding ORM instances across those awaits invites lazy-load IO on a
        # session that is not expecting it.
        return [CandidateChunk.from_row(row) for row in rows]

    async def _load_clause_definitions(self, ctx: StageContext) -> list[ClauseDefinition]:
        """Load the Clause Master, restricted to what this profile asks for.

        The profile's mandatory and optional lists decide which categories run, so a
        lease is not searched for a data-processing clause it never has. A profile
        that names no clauses gets the whole active master list.
        """
        stmt = (
            select(ClauseMasterCategory)
            .options(selectinload(ClauseMasterCategory.rules))
            .where(
                ClauseMasterCategory.is_active.is_(True),
                ClauseMasterCategory.deleted_at.is_(None),
            )
            .order_by(ClauseMasterCategory.priority, ClauseMasterCategory.key)
        )
        categories = (await ctx.db.execute(stmt)).scalars().all()

        wanted: set[str] = set()
        if ctx.profile is not None:
            wanted = {str(key) for key in (ctx.profile.mandatory_clauses or [])} | {
                str(key) for key in (ctx.profile.optional_clauses or [])
            }

        definitions: list[ClauseDefinition] = []
        for category in categories:
            if wanted and str(category.key) not in wanted:
                continue
            rule = _active_rule(category)
            if rule is None:
                # A category with no rule version has no extraction contract; skipping
                # is correct and worth saying out loud rather than failing the job.
                logger.warning(
                    "clause_category_without_rule",
                    clause_key=str(category.key),
                    contract_id=str(ctx.contract_id),
                )
                continue
            definitions.append(ClauseDefinition.from_category(category, rule))

        # Mandatory clauses are extracted even if the profile omitted them from its
        # own lists: their absence is a finding, and a finding cannot be made about a
        # category that never ran.
        return definitions

    # =========================================================================
    # Persistence
    # =========================================================================
    async def _persist(self, ctx: StageContext, result: ExtractionResult) -> dict[str, int]:
        contract_id, project_id = ctx.contract_id, ctx.project_id
        profile_version = getattr(ctx.profile, "version", None)

        # ---- clauses first: obligations and risks reference them --------------
        clause_ids: dict[int, uuid.UUID] = {}
        clause_rows: list[dict[str, Any]] = []
        for index, clause in enumerate(result.clauses):
            row_id = uuid.uuid4()
            clause_ids[index] = row_id
            clause_rows.append(
                {
                    "id": row_id,
                    "contract_id": contract_id,
                    "project_id": project_id,
                    "chunk_id": _as_uuid(clause.chunk_id),
                    "clause_type": clause.clause_type,
                    "title": clause.title,
                    "text": clause.text,
                    "summary": clause.summary,
                    "section_id": clause.section_id,
                    "section_title": clause.section_title,
                    "clause_number": clause.clause_number,
                    "is_risk_flagged": self._is_risk_flagged(clause, result.risks),
                    "is_mandatory": clause.is_mandatory,
                    "deviation_score": clause.deviation_score,
                    "attributes": clause.attributes,
                    **_provenance(clause, profile_version),
                }
            )
        await ClauseRepository(ctx.db).insert_many(clause_rows)

        # Clause type -> row id, for attaching dependents that name a category
        # rather than a specific clause.
        by_type: dict[str, uuid.UUID] = {}
        for index, clause in enumerate(result.clauses):
            by_type.setdefault(clause.clause_type, clause_ids[index])

        # ---- entities ---------------------------------------------------------
        entity_rows = [
            {
                "id": uuid.uuid4(),
                "contract_id": contract_id,
                "project_id": project_id,
                "chunk_id": _evidence_chunk_id(party),
                "entity_type": party.entity_type,
                "name": party.name,
                "legal_name": party.legal_name,
                "aliases": party.aliases,
                "role": party.role,
                "jurisdiction": party.jurisdiction,
                "registration_number": party.registration_number,
                "address": party.address,
                "contact": party.contact,
                "is_primary": party.is_primary,
                **_provenance(party, profile_version),
            }
            for party in result.parties
        ]
        await EntityRepository(ctx.db).insert_many(entity_rows)

        # ---- obligations ------------------------------------------------------
        obligation_rows = [
            {
                "id": uuid.uuid4(),
                "contract_id": contract_id,
                "project_id": project_id,
                "clause_id": by_type.get(obligation.clause_type or ""),
                "chunk_id": _as_uuid(obligation.chunk_id),
                "responsible_party": obligation.responsible_party,
                "action": obligation.action,
                "due_date": obligation.due_date,
                "due_description": obligation.due_description,
                "trigger_event": obligation.trigger_event,
                "dependency": obligation.dependency,
                "frequency": obligation.frequency,
                "is_recurring": obligation.is_recurring,
                "status": obligation.status,
                "penalty": obligation.penalty,
                **_provenance(obligation, profile_version),
            }
            for obligation in result.obligations
        ]
        await ObligationRepository(ctx.db).insert_many(obligation_rows)

        # ---- risks ------------------------------------------------------------
        risk_rows = [
            {
                "id": uuid.uuid4(),
                "contract_id": contract_id,
                "project_id": project_id,
                "clause_id": by_type.get(risk.clause_type or ""),
                "chunk_id": _as_uuid(risk.chunk_id),
                "risk_type": risk.risk_type,
                "severity": risk.severity,
                "description": risk.description,
                "recommendation": risk.recommendation,
                "score_contribution": risk.score_contribution,
                "category": risk.category,
                "is_omission": risk.is_omission,
                **_provenance(risk, profile_version),
            }
            for risk in result.risks
        ]
        await RiskRepository(ctx.db).insert_many(risk_rows)

        # ---- key dates --------------------------------------------------------
        date_rows = [
            {
                "id": uuid.uuid4(),
                "contract_id": contract_id,
                "project_id": project_id,
                "clause_id": None,
                "chunk_id": _as_uuid(entry.chunk_id),
                "date_type": entry.date_type,
                "date_value": entry.date_value,
                "date_expression": entry.date_expression,
                "description": entry.description,
                "is_recurring": entry.is_recurring,
                **_provenance(entry, profile_version),
            }
            for entry in result.key_dates
        ]
        await KeyDateRepository(ctx.db).insert_many(date_rows)

        # ---- relationships ----------------------------------------------------
        relationship_rows = []
        for relationship in result.relationships:
            relation = _graph_relation(relationship.relation)
            if relation is None:
                # An unrecognised relation would violate the native enum. Dropped with
                # a log rather than failing the stage over one edge.
                logger.debug(
                    "unknown_graph_relation",
                    relation=relationship.relation,
                    contract_id=str(contract_id),
                )
                continue
            relationship_rows.append(
                {
                    "id": uuid.uuid4(),
                    "contract_id": contract_id,
                    "project_id": project_id,
                    "relation": relation,
                    "source_type": relationship.source_type,
                    "source_ref": relationship.source_ref,
                    "target_type": relationship.target_type,
                    "target_ref": relationship.target_ref,
                    "source_id": None,
                    "target_id": None,
                    "label": relationship.label,
                    "attributes": relationship.attributes,
                    # Resolved to concrete ids by the indexing stage, which owns the
                    # graph; extraction only states what the text says.
                    "is_resolved": False,
                    "confidence": _decimal(relationship.confidence),
                    "validation_score": _decimal(relationship.validation_score),
                    "review_status": relationship.review_status.value,
                    "profile_version": profile_version,
                    "prompt_version": relationship.prompt_version,
                    "model_version": relationship.model_version,
                }
            )
        await KnowledgeRelationshipRepository(ctx.db).insert_many(relationship_rows)

        # ---- summary ----------------------------------------------------------
        if result.facts.executive_summary or result.facts.summary:
            await ContractSummaryRepository(ctx.db).upsert(
                contract_id=contract_id,
                project_id=project_id,
                summary_type="executive",
                values={
                    "content": result.facts.executive_summary or result.facts.summary or "",
                    "key_points": result.facts.key_topics,
                    "sections": [],
                    "citations": [
                        ref.as_dict()
                        for clause in result.clauses[:20]
                        for ref in clause.evidence[:1]
                    ],
                    "profile_version": profile_version,
                    "prompt_version": None,
                    "model_version": None,
                },
            )

        return {
            "clauses": len(clause_rows),
            "entities": len(entity_rows),
            "obligations": len(obligation_rows),
            "risks": len(risk_rows),
            "key_dates": len(date_rows),
            "relationships": len(relationship_rows),
        }

    async def _update_metadata(
        self, ctx: StageContext, result: ExtractionResult, counts: dict[str, int]
    ) -> None:
        """Write the ``contract_metadata`` projection the dashboards read.

        A denormalised projection rather than a view: the repository list, the KPI
        tiles and the filter panel all read it on every page load, and recomputing
        risk and clause presence from seven tables per row would not hold up.
        """
        facts = result.facts
        assessment = result.assessment

        liability = result.clause_by_type(ClauseType.LIMITATION_OF_LIABILITY.value)
        renewal = result.clause_by_type(ClauseType.AUTO_RENEWAL.value)
        termination = result.clause_by_type(ClauseType.TERMINATION_FOR_CONVENIENCE.value)
        governing = result.clause_by_type(ClauseType.GOVERNING_LAW.value)
        payment = result.clause_by_type(ClauseType.PAYMENT_TERMS.value)

        cap_basis = liability.attributes.get("cap_basis") if liability else None
        renewal_attrs = renewal.attributes if renewal else {}
        termination_attrs = termination.attributes if termination else {}
        governing_attrs = governing.attributes if governing else {}
        payment_attrs = payment.attributes if payment else {}

        high_risk = sum(
            1 for risk in assessment.risks if risk.severity.value in {"critical", "high"}
        )

        values: dict[str, Any] = {
            # --- dates ---
            "effective_date": facts.effective_date,
            "execution_date": facts.execution_date,
            "expiration_date": facts.expiration_date,
            "notice_deadline": _notice_deadline(result),
            "term_months": facts.term_months,
            # --- legal ---
            "governing_law": facts.governing_law or governing_attrs.get("governing_law"),
            "jurisdiction": facts.jurisdiction or governing_attrs.get("jurisdiction"),
            "country": governing_attrs.get("country"),
            "language": ctx.contract.language,
            # --- commercial ---
            "currency": facts.currency,
            "contract_value": facts.contract_value,
            "payment_terms_days": facts.payment_terms_days or payment_attrs.get("payment_days"),
            # --- parties ---
            "party_a": facts.party_a,
            "party_b": facts.party_b,
            # --- risk ---
            "risk_score": assessment.score,
            "risk_band": assessment.band.value,
            "risk_level": assessment.band.value,
            "risk_factors": assessment.breakdown,
            # --- renewal ---
            "auto_renewal": renewal_attrs.get("auto_renews"),
            "auto_renewal_notice_days": renewal_attrs.get("renewal_notice_days"),
            "renewal_term_months": renewal_attrs.get("renewal_term_months"),
            # --- clause presence, for the filter panel and KPI tiles ---
            "missing_mandatory_clauses": assessment.missing_mandatory,
            "has_unlimited_liability": assessment.has_unlimited_liability,
            "has_liability_cap": bool(
                liability is not None
                and cap_basis
                not in {
                    None,
                    LiabilityCapBasis.UNCAPPED.value,
                    LiabilityCapBasis.NOT_SPECIFIED.value,
                }
            ),
            "liability_cap_amount": (liability.attributes.get("cap_amount") if liability else None),
            "has_termination_for_convenience": termination is not None
            and termination_attrs.get("is_permitted") is not False,
            "has_data_protection_clause": ClauseType.DATA_PROTECTION.value
            in result.clause_types_found(),
            "termination_notice_days": termination_attrs.get("notice_days"),
            # --- counts ---
            "clause_count": counts["clauses"],
            "obligation_count": counts["obligations"],
            "risk_count": counts["risks"],
            "high_risk_count": high_risk,
            # --- narrative ---
            "summary": facts.executive_summary or facts.summary,
            "key_topics": facts.key_topics,
            "extra": {
                # Answers to the questions the clause list was prioritised around,
                # kept where the UI can read them without re-deriving from attributes.
                "can_we_terminate": termination_attrs.get("can_we_terminate")
                or PartySide.UNKNOWN.value,
                "we_retain_pre_existing_ip": _ip_answer(result),
                "liability_cap_basis": cap_basis,
                "liability_cap_multiple": (
                    liability.attributes.get("cap_multiple") if liability else None
                ),
                "liability_carve_outs": (
                    liability.attributes.get("carve_outs") or [] if liability else []
                ),
                "dispute_resolution": facts.dispute_resolution,
                "review_reasons": result.review_reasons,
                "extraction_categories": [o.as_dict() for o in result.outcomes],
            },
        }

        await ContractMetadataRepository(ctx.db).upsert(
            contract_id=ctx.contract_id,
            project_id=ctx.project_id,
            values={key: value for key, value in values.items() if value is not None},
        )

    # =========================================================================
    # Artifacts
    # =========================================================================
    def _artifacts(self, result: ExtractionResult, counts: dict[str, int]) -> list[StageArtifact]:
        """One artifact per knowledge kind, as the stage contract declares (§7.3).

        Split rather than combined because they are consumed separately - the
        knowledge API reads clauses, the alert sweep reads timelines - and neither
        should have to load the other.
        """
        return [
            StageArtifact(
                kind=ArtifactKind.CLAUSES,
                payload={
                    "clauses": [
                        {
                            "clause_type": clause.clause_type,
                            "clause_number": clause.clause_number,
                            "title": clause.title,
                            "text": clause.text,
                            "summary": clause.summary,
                            "attributes": clause.attributes,
                            "evidence": [ref.as_dict() for ref in clause.evidence],
                            **clause.provenance(),
                        }
                        for clause in result.clauses
                    ]
                },
                summary={
                    "count": counts["clauses"],
                    "types": sorted(result.clause_types_found()),
                    "missing_mandatory": result.assessment.missing_mandatory,
                },
            ),
            StageArtifact(
                kind=ArtifactKind.ENTITIES,
                payload={
                    "parties": [
                        {
                            "name": party.name,
                            "legal_name": party.legal_name,
                            "entity_type": party.entity_type.value,
                            "role": party.role,
                            "is_primary": party.is_primary,
                            "is_our_organisation": party.is_our_organisation,
                            "evidence": [ref.as_dict() for ref in party.evidence],
                            **party.provenance(),
                        }
                        for party in result.parties
                    ]
                },
                summary={"count": counts["entities"]},
            ),
            StageArtifact(
                kind=ArtifactKind.OBLIGATIONS,
                payload={
                    "obligations": [
                        {
                            "action": obligation.action,
                            "responsible_party": obligation.responsible_party,
                            "due_date": obligation.due_date.isoformat()
                            if obligation.due_date
                            else None,
                            "due_description": obligation.due_description,
                            "trigger_event": obligation.trigger_event,
                            "clause_type": obligation.clause_type,
                            "evidence": [ref.as_dict() for ref in obligation.evidence],
                            **obligation.provenance(),
                        }
                        for obligation in result.obligations
                    ]
                },
                summary={"count": counts["obligations"]},
            ),
            StageArtifact(
                kind=ArtifactKind.RISKS,
                payload={
                    "assessment": result.assessment.as_dict(),
                    "risks": [
                        {
                            "risk_type": risk.risk_type,
                            "severity": risk.severity.value,
                            "description": risk.description,
                            "recommendation": risk.recommendation,
                            "clause_type": risk.clause_type,
                            "is_omission": risk.is_omission,
                            "score_contribution": risk.score_contribution,
                            "evidence": [ref.as_dict() for ref in risk.evidence],
                            **risk.provenance(),
                        }
                        for risk in result.risks
                    ],
                },
                summary={
                    "count": counts["risks"],
                    "score": result.assessment.score,
                    "band": result.assessment.band.value,
                    "by_severity": result.assessment.counts_by_severity(),
                    "has_unlimited_liability": result.assessment.has_unlimited_liability,
                },
            ),
            StageArtifact(
                kind=ArtifactKind.TIMELINES,
                payload={
                    "key_dates": [
                        {
                            "date_type": entry.date_type.value,
                            "date_value": entry.date_value.isoformat()
                            if entry.date_value
                            else None,
                            "date_expression": entry.date_expression,
                            "description": entry.description,
                            "evidence": [ref.as_dict() for ref in entry.evidence],
                            **entry.provenance(),
                        }
                        for entry in result.key_dates
                    ]
                },
                summary={"count": counts["key_dates"]},
            ),
            StageArtifact(
                kind=ArtifactKind.RELATIONSHIPS,
                payload={
                    "relationships": [
                        {
                            "relation": rel.relation,
                            "source_type": rel.source_type,
                            "source_ref": rel.source_ref,
                            "target_type": rel.target_type,
                            "target_ref": rel.target_ref,
                            "label": rel.label,
                            **rel.provenance(),
                        }
                        for rel in result.relationships
                    ]
                },
                summary={"count": counts["relationships"]},
            ),
            StageArtifact(
                kind=ArtifactKind.EXTRACTION_STATISTICS,
                payload={
                    "statistics": result.statistics(),
                    "facts": result.facts.as_dict(),
                    "categories": [outcome.as_dict() for outcome in result.outcomes],
                    "review_reasons": result.review_reasons,
                    "warnings": result.warnings,
                },
                summary={
                    "llm_calls": result.llm_calls,
                    "tokens": result.total_tokens,
                    "cost_usd": result.total_cost_usd,
                    "needs_review": result.needs_review,
                    "failed_categories": result.failed_categories,
                },
            ),
        ]

    # =========================================================================
    # Helpers
    # =========================================================================
    @staticmethod
    def _is_useful(result: ExtractionResult) -> bool:
        """Did extraction produce anything worth storing?

        Deliberately generous: a contract may genuinely contain none of the clause
        categories a profile expects, but if it produced no clauses, no parties *and*
        no dates then something upstream is wrong and a silent empty result would
        look like a clean run.
        """
        return bool(result.clauses or result.parties or result.key_dates)

    @staticmethod
    def _is_risk_flagged(clause: ExtractedClause, risks: list[ExtractedRisk]) -> bool:
        """True when a non-omission risk was raised against this clause type."""
        return any(
            risk.clause_type == clause.clause_type and not risk.is_omission for risk in risks
        )


def _active_rule(category: ClauseMasterCategory) -> Any:
    """The highest active rule version for a category.

    Versions accumulate - an administrator edits a rule and a new version is written
    rather than the old one mutated - so the current rule is the newest active one.
    """
    active = [rule for rule in (category.rules or []) if rule.is_active]
    if not active:
        return None
    return max(active, key=lambda rule: rule.version)


def _provenance(item: ExtractedItem, profile_version: str | None) -> dict[str, Any]:
    """The ``EvidenceMixin`` + ``ExtractionProvenanceMixin`` columns for one item."""
    primary = item.primary_evidence
    return {
        "page_start": primary.page_start if primary else None,
        "page_end": primary.page_end if primary else None,
        "bounding_boxes": [box for ref in item.evidence for box in ref.bounding_boxes],
        "evidence": {
            "refs": [ref.as_dict() for ref in item.evidence],
            "issues": [issue.as_dict() for issue in item.issues],
        },
        "confidence": _decimal(item.confidence),
        "validation_score": _decimal(item.validation_score),
        "review_status": item.review_status.value,
        "profile_version": profile_version,
        "prompt_version": item.prompt_version,
        "model_version": item.model_version,
        "artifact_version": None,
    }


def _decimal(value: float | None) -> float | None:
    """Round to the 4 decimal places the Numeric(5,4) columns hold.

    Rounded here rather than left to the driver: a value like 0.93000000001 would be
    rejected by the column's scale, failing an insert for a reason that has nothing
    to do with the extraction.
    """
    if value is None:
        return None
    return round(min(max(float(value), 0.0), 1.0), 4)


def _evidence_chunk_id(item: ExtractedItem) -> uuid.UUID | None:
    """The chunk an item's primary evidence points at, if it has any."""
    primary = item.primary_evidence
    return _as_uuid(primary.chunk_id) if primary is not None else None


def _as_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _graph_relation(value: str) -> Any:
    """Coerce a relation name to the ``GraphRelation`` enum, or None."""
    from app.core.enums import GraphRelation

    try:
        return GraphRelation(str(value).strip().lower())
    except ValueError:
        return None


def _notice_deadline(result: ExtractionResult) -> Any:
    """The earliest dated notice deadline, for the renewal alert."""
    from app.core.enums import DateType

    candidates = [
        entry.date_value
        for entry in result.key_dates
        if entry.date_type is DateType.NOTICE_DEADLINE and entry.date_value
    ]
    return min(candidates) if candidates else None


def _ip_answer(result: ExtractionResult) -> str:
    """Whether we retain rights in pre-existing IP - one of the priority questions."""
    clause = result.clause_by_type(ClauseType.INTELLECTUAL_PROPERTY.value)
    if clause is None:
        return PartySide.UNKNOWN.value
    return str(clause.attributes.get("we_retain_pre_existing_ip") or PartySide.UNKNOWN.value)


register_stage(AIExtractionStage())

__all__ = ["AIExtractionStage"]

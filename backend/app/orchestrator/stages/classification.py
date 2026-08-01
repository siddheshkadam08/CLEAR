"""Stage 4 - Classification and profile selection.

Decides what kind of contract this is and therefore **which Document Intelligence
Profile processes it** - which in turn decides prompts, mandatory clauses, chunking
strategy, risk weighting, compliance packs and review triggers (§11).

The profile chosen here is pinned onto both the job and the contract, so a later
revision of that profile never silently rewrites how this contract was interpreted.

Low classification confidence is propagated rather than smoothed over: it is one of
the human-review triggers, and a contract processed under a fallback profile is
exactly the case a reviewer needs to see.
"""

from __future__ import annotations

from app.ai.cdm.models import CanonicalDocument
from app.ai.classification import (
    ClassificationResult,
    DocumentClassifier,
    confidence_to_decimal,
)
from app.core import metrics
from app.core.enums import ArtifactKind, PipelineStage
from app.core.errors import PipelineError
from app.core.logging import get_logger
from app.core.versions import (
    CLASSIFICATION_ENGINE_VERSION,
    CLASSIFICATION_TAXONOMY_VERSION,
    ComponentVersions,
)
from app.models.profile import DocumentProfile
from app.orchestrator.stages.base import (
    StageArtifact,
    StageContext,
    StageHandler,
    StageResult,
    register_stage,
)
from app.repositories.processing import DocumentArtifactRepository

logger = get_logger(__name__)


class ClassificationStage(StageHandler):
    stage = PipelineStage.CLASSIFICATION
    requires = (ArtifactKind.CANONICAL_DOCUMENT,)
    cacheable = True
    retryable = True

    def versions_for(self, ctx: StageContext) -> ComponentVersions:
        """Version by engine and taxonomy only.

        Deliberately excludes the profile: classification *selects* the profile, so
        keying its checkpoint on the previous selection would be circular.
        """
        from app.core.versions import CDM_VERSION

        return ComponentVersions(
            cdm_version=CDM_VERSION,
            classification_engine_version=CLASSIFICATION_ENGINE_VERSION,
            classification_taxonomy_version=CLASSIFICATION_TAXONOMY_VERSION,
        )

    async def run(self, ctx: StageContext) -> StageResult:
        await ctx.report_progress(46, "classifying document")

        document = await self._load_cdm(ctx)
        classifier = DocumentClassifier(ctx.db)

        # An uploader may pin a profile for a batch of known-identical documents, or
        # state the agreement type as a hint. Both arrive through stage options
        # rather than being read off the project here - the project relationship is
        # not eagerly loaded on this session, and touching it would trigger lazy IO
        # inside the async context.
        forced_key = ctx.options.get("profile_key")
        hinted_type = ctx.options.get("agreement_type") or ctx.contract.agreement_type

        try:
            result = await classifier.classify(
                document,
                project_id=ctx.project_id,
                forced_profile_key=forced_key,
                hinted_type=hinted_type,
            )
        except LookupError as exc:
            raise PipelineError(
                str(exc), stage=self.stage.value, details={"remedy": "run the seed step"}
            ) from exc

        profile = result.profile
        threshold = float(profile.confidence_threshold or 0.85)
        needs_review = result.is_fallback or result.confidence < threshold
        metrics.classification_confidence.labels(method=result.method).observe(result.confidence)

        if needs_review:
            # Surfaced rather than smoothed over: a contract classified below its
            # profile's own threshold is a review case, not a silent pass.
            ctx.contract.needs_review = True
            await ctx.db.flush()
            logger.info(
                "classification_flagged_for_review",
                contract_id=str(ctx.contract_id),
                confidence=round(result.confidence, 3),
                threshold=threshold,
                is_fallback=result.is_fallback,
                fallback_reason=result.fallback_reason.value,
                top_predictions=result.top_predictions[:3],
            )

        await ctx.report_progress(49, f"classified as {profile.name}")

        logger.info(
            "classification_completed",
            contract_id=str(ctx.contract_id),
            profile_key=profile.key,
            profile_version=profile.version,
            agreement_type=result.agreement_type,
            method=result.method,
            confidence=round(result.confidence, 4),
            chunk_strategy=str(profile.chunk_strategy),
            mandatory_clauses=len(profile.mandatory_clauses or []),
        )

        return StageResult(
            artifacts=[
                StageArtifact(
                    kind=ArtifactKind.CLASSIFICATION,
                    payload=result.as_artifact(),
                    summary={
                        "profile_key": profile.key,
                        "profile_version": profile.version,
                        "agreement_type": result.agreement_type,
                        "confidence": round(result.confidence, 4),
                        "method": result.method,
                        "is_fallback": result.is_fallback,
                        "fallback_used": result.fallback_used,
                        "fallback_reason": result.fallback_reason.value,
                        "needs_review": needs_review,
                    },
                )
            ],
            stats={
                "classification_method": result.method,
                "classification_confidence": round(result.confidence, 4),
                "classification_profile": profile.key,
                "classification_candidates": len(result.signals),
                "classification_fallback_reason": result.fallback_reason.value,
                "classification_matched_rules": result.matched_rules,
            },
            # Promoted onto the job and contract by the runner: this is what pins the
            # contract to the exact profile version that processed it.
            context_updates={
                "profile_id": str(profile.id),
                "profile_version": profile.version,
                "profile_key": profile.key,
                "agreement_type": result.agreement_type,
                "agreement_subtype": result.agreement_subtype,
                "classification_confidence": confidence_to_decimal(result.confidence),
                "title": result.detected_title,
                "language": result.language,
            },
            # The warning reaches the Processing screen, so it has to name the
            # actual cause. "Low confidence" was shown even when the tie-break
            # provider was down, which sends a reviewer to read a document when the
            # fix is to restore a service and reprocess.
            warnings=self._review_warnings(result, profile, needs_review),
        )

    @staticmethod
    def _review_warnings(
        result: ClassificationResult, profile: DocumentProfile, needs_review: bool
    ) -> list[str]:
        if not needs_review:
            return []
        headline = (
            f"Classified as '{profile.name}' with low confidence "
            f"({result.confidence:.0%}); flagged for human review."
        )
        if not result.fallback_used:
            return [headline]
        return [
            f"Applied the default profile '{profile.key}' - the document type was "
            f"not determined ({result.fallback_reason.value}).",
            *result.notes[:1],
        ]

    async def _load_cdm(self, ctx: StageContext) -> CanonicalDocument:
        artifacts = DocumentArtifactRepository(ctx.db)
        pointer = await artifacts.current(ctx.contract_id, ArtifactKind.CANONICAL_DOCUMENT)
        if pointer is None:
            raise PipelineError(
                "The canonical document artifact is missing. Re-run enrichment.",
                stage=self.stage.value,
            )
        payload = await ctx.storage.get_json(pointer.storage_path)
        try:
            return CanonicalDocument.model_validate(payload)
        except Exception as exc:
            raise PipelineError(
                f"The canonical document artifact is invalid: {exc}",
                stage=self.stage.value,
            ) from exc


register_stage(ClassificationStage())

__all__ = ["ClassificationStage"]

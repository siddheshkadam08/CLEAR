"""Version registry - "Version Everything" (§25).

Every artifact, clause, chunk, embedding and answer records the exact version of
each component that produced it. Two things depend on this:

1. **Reproducibility** - a legal dispute about an extracted clause can be traced
   to the parser, prompt and model that produced it.
2. **Selective regeneration** - the orchestrator compares the versions stored on
   a checkpoint with the versions in this registry; only components whose version
   moved cause their stage (and downstream stages) to re-run. Bumping the chunk
   strategy does not re-parse; bumping the parser does not re-embed unrelated
   vectors.

**Bump a constant whenever the component's output changes semantically.** These
are deliberately hand-maintained rather than derived from the package version:
a refactor should not invalidate a million cached artifacts.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.core.enums import PipelineStage

# =============================================================================
# Component versions
# =============================================================================
#: Canonical Document Model schema version.
CDM_VERSION = "1.0.0"

#: Parser adapter framework version (the contract, not an individual parser).
PARSER_FRAMEWORK_VERSION = "1.0.0"

#: Per-parser adapter versions - bump when an adapter's normalisation changes.
PARSER_ADAPTER_VERSIONS: dict[str, str] = {
    "adi": "1.0.0",
    "pymupdf": "1.0.0",
    "textract": "1.0.0",
    "googledocai": "1.0.0",
}

#: Enrichment (normalized document -> CDM) engine.
ENRICHMENT_ENGINE_VERSION = "1.0.0"

#: Classification engine and its taxonomy.
CLASSIFICATION_ENGINE_VERSION = "1.0.0"
CLASSIFICATION_TAXONOMY_VERSION = "1.0.0"

#: Semantic chunking: the engine, and each strategy independently.
CHUNK_ENGINE_VERSION = "1.0.0"
CHUNK_STRATEGY_VERSIONS: dict[str, str] = {
    "section_based": "1.0.0",
    "heading_aware": "1.0.0",
    "clause_based": "1.0.0",
    "table_preserving": "1.0.0",
    "list_preserving": "1.0.0",
    "hybrid": "1.0.0",
}

#: AI extraction engine, output schemas and validation rules.
EXTRACTION_ENGINE_VERSION = "1.0.0"
EXTRACTION_SCHEMA_VERSION = "1.0.0"
VALIDATION_RULES_VERSION = "1.0.0"

#: Prompt template versions, keyed by template id. Prompts version *independently*
#: of the profile that selects them (§13).
PROMPT_VERSIONS: dict[str, str] = {
    "extraction.metadata": "1.0.0",
    "extraction.parties": "1.0.0",
    "extraction.clauses": "1.0.0",
    "extraction.financial": "1.0.0",
    "extraction.obligations": "1.0.0",
    "extraction.rights": "1.0.0",
    "extraction.risks": "1.0.0",
    "extraction.dates": "1.0.0",
    "extraction.relationships": "1.0.0",
    "classification.document": "1.0.0",
    "summary.document": "1.0.0",
    "rag.qa": "1.0.0",
    "rag.contract_summary": "1.0.0",
    "rag.executive_summary": "1.0.0",
    "rag.risk_report": "1.0.0",
    "rag.compliance_report": "1.0.0",
    "rag.clause_comparison": "1.0.0",
    "rag.timeline": "1.0.0",
    "rag.obligation_report": "1.0.0",
    "rag.action_items": "1.0.0",
    "planner.query_analysis": "1.0.0",
}

#: Embedding strategy (which levels, how text is composed before embedding).
#: The embedding *model* version comes from configuration, not from here.
EMBEDDING_STRATEGY_VERSION = "1.0.0"

#: Search index builder and knowledge graph builder.
INDEX_VERSION = "1.0.0"
GRAPH_VERSION = "1.0.0"

#: Retrieval planner, re-ranking, context assembly, prompt orchestration, RAG.
PLANNER_VERSION = "1.0.0"
RERANKING_VERSION = "1.0.0"
CONTEXT_ENGINE_VERSION = "1.0.0"
PROMPT_ORCHESTRATOR_VERSION = "1.0.0"
RAG_ENGINE_VERSION = "1.0.0"

#: Organisational policy set injected into prompts.
POLICY_VERSION = "1.0.0"

#: Output schema contract for structured RAG responses.
OUTPUT_SCHEMA_VERSION = "1.0.0"

#: Seeded Document Intelligence Profile version. Individual profiles carry their
#: own version in the database; this is the version of the *seed set*.
PROFILE_SEED_VERSION = "1.0.0"

#: Risk scoring model (weights + banding).
RISK_MODEL_VERSION = "1.0.0"


class ComponentVersions(BaseModel):
    """The ``versions`` JSONB payload stored on checkpoints and artifacts.

    Only the fields relevant to the producing stage are populated, so comparing
    two payloads answers "did anything that affects this stage change?".
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    cdm_version: str | None = None
    parser_name: str | None = None
    parser_version: str | None = None
    parser_adapter_version: str | None = None
    enrichment_engine_version: str | None = None
    classification_engine_version: str | None = None
    classification_taxonomy_version: str | None = None
    profile_id: str | None = None
    profile_version: str | None = None
    chunk_engine_version: str | None = None
    chunk_strategy: str | None = None
    chunk_strategy_version: str | None = None
    extraction_engine_version: str | None = None
    extraction_schema_version: str | None = None
    validation_rules_version: str | None = None
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    model_name: str | None = None
    model_version: str | None = None
    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None
    embedding_version: str | None = None
    embedding_strategy_version: str | None = None
    index_version: str | None = None
    graph_version: str | None = None
    risk_model_version: str | None = None
    source_artifact_version: str | None = None

    def matches(self, other: ComponentVersions | dict[str, Any] | None) -> bool:
        """True when every version this payload declares is unchanged in ``other``.

        Asymmetric on purpose: keys absent from ``other`` count as changed, but
        extra keys in ``other`` are ignored. That makes a newly tracked version
        field invalidate old checkpoints (correct - we cannot prove they match)
        without a schema migration.
        """
        if other is None:
            return False
        other_dict = (
            other.model_dump(exclude_none=True) if isinstance(other, ComponentVersions) else other
        )
        mine = self.model_dump(exclude_none=True)
        for key, value in mine.items():
            if not value:
                continue
            if other_dict.get(key) != value:
                return False
        return True

    def diff(self, other: ComponentVersions | dict[str, Any] | None) -> dict[str, tuple[Any, Any]]:
        """``{field: (stored, current)}`` for every version that moved."""
        other_dict: dict[str, Any] = {}
        if other is not None:
            other_dict = (
                other.model_dump(exclude_none=True)
                if isinstance(other, ComponentVersions)
                else dict(other)
            )
        result: dict[str, tuple[Any, Any]] = {}
        for key, value in self.model_dump(exclude_none=True).items():
            if not value:
                continue
            stored = other_dict.get(key)
            if stored != value:
                result[key] = (stored, value)
        return result


def current_versions_for_stage(
    stage: PipelineStage,
    *,
    profile_id: str | None = None,
    profile_version: str | None = None,
    chunk_strategy: str | None = None,
    prompt_ids: list[str] | None = None,
) -> ComponentVersions:
    """Build the version payload a stage should stamp on its checkpoint.

    Each stage declares only what genuinely affects its output. This is what
    keeps regeneration surgical: an embedding-model change must not invalidate
    the chunking checkpoint.
    """
    settings = get_settings()
    parser_name = settings.parser.active_parser

    if stage is PipelineStage.VALIDATION:
        return ComponentVersions()

    if stage is PipelineStage.PARSER:
        return ComponentVersions(
            parser_name=parser_name,
            parser_version=PARSER_FRAMEWORK_VERSION,
            parser_adapter_version=PARSER_ADAPTER_VERSIONS.get(parser_name, "unknown"),
        )

    if stage is PipelineStage.ENRICHMENT:
        return ComponentVersions(
            cdm_version=CDM_VERSION,
            parser_name=parser_name,
            parser_adapter_version=PARSER_ADAPTER_VERSIONS.get(parser_name, "unknown"),
            enrichment_engine_version=ENRICHMENT_ENGINE_VERSION,
        )

    if stage is PipelineStage.CLASSIFICATION:
        return ComponentVersions(
            cdm_version=CDM_VERSION,
            classification_engine_version=CLASSIFICATION_ENGINE_VERSION,
            classification_taxonomy_version=CLASSIFICATION_TAXONOMY_VERSION,
        )

    if stage is PipelineStage.CHUNKING:
        strategy = chunk_strategy or "hybrid"
        return ComponentVersions(
            cdm_version=CDM_VERSION,
            profile_id=profile_id,
            profile_version=profile_version,
            chunk_engine_version=CHUNK_ENGINE_VERSION,
            chunk_strategy=strategy,
            chunk_strategy_version=CHUNK_STRATEGY_VERSIONS.get(strategy, "unknown"),
        )

    if stage is PipelineStage.AI_EXTRACTION:
        return ComponentVersions(
            profile_id=profile_id,
            profile_version=profile_version,
            extraction_engine_version=EXTRACTION_ENGINE_VERSION,
            extraction_schema_version=EXTRACTION_SCHEMA_VERSION,
            validation_rules_version=VALIDATION_RULES_VERSION,
            risk_model_version=RISK_MODEL_VERSION,
            prompt_versions={
                pid: PROMPT_VERSIONS.get(pid, "unknown")
                for pid in (
                    prompt_ids or [k for k in PROMPT_VERSIONS if k.startswith("extraction.")]
                )
            },
            model_name=settings.llm.model,
            model_version=settings.llm.model,
        )

    if stage is PipelineStage.EMBEDDING:
        return ComponentVersions(
            profile_id=profile_id,
            profile_version=profile_version,
            embedding_provider=settings.embedding.provider,
            embedding_model=settings.embedding.model,
            embedding_dim=settings.embedding.dim,
            embedding_version=settings.embedding.version,
            embedding_strategy_version=EMBEDDING_STRATEGY_VERSION,
        )

    if stage is PipelineStage.INDEXING:
        return ComponentVersions(
            index_version=INDEX_VERSION,
            graph_version=GRAPH_VERSION,
            embedding_model=settings.embedding.model,
            embedding_version=settings.embedding.version,
        )

    return ComponentVersions()


def retrieval_versions() -> ComponentVersions:
    """Version payload stamped on a retrieval/answer audit record."""
    settings = get_settings()
    return ComponentVersions(
        embedding_provider=settings.embedding.provider,
        embedding_model=settings.embedding.model,
        embedding_version=settings.embedding.version,
        index_version=INDEX_VERSION,
        graph_version=GRAPH_VERSION,
        model_name=settings.llm.model,
        **{
            "planner_version": PLANNER_VERSION,
            "reranking_version": RERANKING_VERSION,
            "context_engine_version": CONTEXT_ENGINE_VERSION,
            "prompt_orchestrator_version": PROMPT_ORCHESTRATOR_VERSION,
            "rag_engine_version": RAG_ENGINE_VERSION,
            "policy_version": POLICY_VERSION,
            "output_schema_version": OUTPUT_SCHEMA_VERSION,
        },
    )


def prompt_version(template_id: str) -> str:
    """Version of a prompt template; ``unknown`` keeps an audit record honest."""
    return PROMPT_VERSIONS.get(template_id, "unknown")


def platform_versions() -> dict[str, Any]:
    """Everything at once - served by ``GET /api/v1/admin/versions``."""
    settings = get_settings()
    return {
        "cdm": CDM_VERSION,
        "parser_framework": PARSER_FRAMEWORK_VERSION,
        "parser_adapters": PARSER_ADAPTER_VERSIONS,
        "active_parser": settings.parser.active_parser,
        "enrichment_engine": ENRICHMENT_ENGINE_VERSION,
        "classification_engine": CLASSIFICATION_ENGINE_VERSION,
        "classification_taxonomy": CLASSIFICATION_TAXONOMY_VERSION,
        "chunk_engine": CHUNK_ENGINE_VERSION,
        "chunk_strategies": CHUNK_STRATEGY_VERSIONS,
        "extraction_engine": EXTRACTION_ENGINE_VERSION,
        "extraction_schema": EXTRACTION_SCHEMA_VERSION,
        "validation_rules": VALIDATION_RULES_VERSION,
        "prompts": PROMPT_VERSIONS,
        "embedding_strategy": EMBEDDING_STRATEGY_VERSION,
        "embedding_model": settings.embedding.model,
        "embedding_version": settings.embedding.version,
        "embedding_dim": settings.embedding.dim,
        "index": INDEX_VERSION,
        "graph": GRAPH_VERSION,
        "planner": PLANNER_VERSION,
        "reranking": RERANKING_VERSION,
        "context_engine": CONTEXT_ENGINE_VERSION,
        "prompt_orchestrator": PROMPT_ORCHESTRATOR_VERSION,
        "rag_engine": RAG_ENGINE_VERSION,
        "policy": POLICY_VERSION,
        "output_schema": OUTPUT_SCHEMA_VERSION,
        "profile_seed": PROFILE_SEED_VERSION,
        "risk_model": RISK_MODEL_VERSION,
        "llm_model": settings.llm.model,
    }


__all__ = [
    "CDM_VERSION",
    "CHUNK_ENGINE_VERSION",
    "CHUNK_STRATEGY_VERSIONS",
    "CLASSIFICATION_ENGINE_VERSION",
    "CLASSIFICATION_TAXONOMY_VERSION",
    "CONTEXT_ENGINE_VERSION",
    "EMBEDDING_STRATEGY_VERSION",
    "ENRICHMENT_ENGINE_VERSION",
    "EXTRACTION_ENGINE_VERSION",
    "EXTRACTION_SCHEMA_VERSION",
    "GRAPH_VERSION",
    "INDEX_VERSION",
    "OUTPUT_SCHEMA_VERSION",
    "PARSER_ADAPTER_VERSIONS",
    "PARSER_FRAMEWORK_VERSION",
    "PLANNER_VERSION",
    "POLICY_VERSION",
    "PROFILE_SEED_VERSION",
    "PROMPT_ORCHESTRATOR_VERSION",
    "PROMPT_VERSIONS",
    "RAG_ENGINE_VERSION",
    "RERANKING_VERSION",
    "RISK_MODEL_VERSION",
    "VALIDATION_RULES_VERSION",
    "ComponentVersions",
    "current_versions_for_stage",
    "platform_versions",
    "prompt_version",
    "retrieval_versions",
]

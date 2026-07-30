"""Embedding layer (§14).

The mandatory three-level hierarchy - document summary (L1), clause (L2), chunk (L3) -
plus the provider abstraction that makes the vector model swappable by configuration.

:mod:`~app.ai.embedding.providers` owns the vendor boundary and guarantees fixed
dimensionality, unit-length vectors and positional alignment.
:mod:`~app.ai.embedding.engine` owns composition (what text represents each level)
and duplicate reuse (what does not need embedding again).
"""

from app.ai.embedding.diagnostics import EmbeddingDiagnostics, diagnose
from app.ai.embedding.engine import (
    EmbeddingEngine,
    EmbeddingItem,
    EmbeddingPlan,
    EmbeddingRun,
    LevelOutcome,
)
from app.ai.embedding.nvidia import NvidiaEmbeddingProvider
from app.ai.embedding.providers import (
    EmbeddingResult,
    EmbeddingUsage,
    IEmbeddingProvider,
    InputType,
    MockEmbeddingProvider,
    ProviderProbe,
    content_hash,
    embedding_health,
    get_embedding_provider,
    set_embedding_provider,
    validate_vector,
)
from app.ai.embedding.reindex import EmbeddingReindexer

__all__ = [
    "EmbeddingDiagnostics",
    "EmbeddingEngine",
    "EmbeddingItem",
    "EmbeddingPlan",
    "EmbeddingReindexer",
    "EmbeddingResult",
    "EmbeddingRun",
    "EmbeddingUsage",
    "IEmbeddingProvider",
    "InputType",
    "LevelOutcome",
    "MockEmbeddingProvider",
    "NvidiaEmbeddingProvider",
    "ProviderProbe",
    "content_hash",
    "diagnose",
    "embedding_health",
    "get_embedding_provider",
    "set_embedding_provider",
    "validate_vector",
]

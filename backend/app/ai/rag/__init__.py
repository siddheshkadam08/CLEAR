"""Inference layer (§17).

The RAG engine consumes a validated Context Package and produces a grounded,
cited answer. It never retrieves and never builds prompts - those are the
retrieval and prompt-orchestration layers.

:mod:`app.ai.rag.providers` holds the vendor abstraction; the engine itself sits
in :mod:`app.ai.rag.engine`.
"""

from app.ai.rag.providers import (
    IInferenceProvider,
    InferenceResult,
    StructuredResult,
    TokenUsage,
    estimate_tokens,
    get_inference_provider,
    provider_health,
    set_inference_provider,
)

__all__ = [
    "IInferenceProvider",
    "InferenceResult",
    "StructuredResult",
    "TokenUsage",
    "estimate_tokens",
    "get_inference_provider",
    "provider_health",
    "set_inference_provider",
]

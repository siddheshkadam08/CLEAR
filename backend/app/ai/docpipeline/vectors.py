"""Stage 3: embed the text of each detected clause.

Batching happens here rather than in the provider. Under the current
configuration ``get_embedding_provider()`` resolves to the OpenAI-compatible
adapter pointed at OpenRouter, and that adapter posts whatever list it is handed
in a single request - only the NVIDIA adapter batches internally. So a document
with a hundred clauses would become one enormous request unless this module
splits it.

Two properties of this path are worth stating rather than discovering later:

* the ``query:`` / ``passage:`` prefixes Nemotron is trained on are applied only
  by the NVIDIA adapter, so they are not in force when running through
  OpenRouter. Vectors are valid; asymmetric recall is not what the model card
  promises.
* reported cost is always ``0.0``, because the pricing table has no entry for
  this model. Absence of a number, not a claim that it was free.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.ai.embedding.providers import IEmbeddingProvider, get_embedding_provider
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class EmbeddingOutcome:
    """Vectors positionally aligned with the texts that produced them."""

    vectors: list[list[float]] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    dim: int = 0
    batches: int = 0
    duration_ms: int = 0


async def embed_texts(
    texts: Sequence[str], *, provider: IEmbeddingProvider | None = None
) -> EmbeddingOutcome:
    """Embed ``texts`` in batches, preserving order."""
    outcome = EmbeddingOutcome()
    if not texts:
        return outcome

    embedder = provider or get_embedding_provider()
    batch_size = max(1, get_settings().embedding.batch_size)
    started = time.perf_counter()

    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        window = list(texts[start : start + batch_size])
        result = await embedder.embed_many(window, input_type="passage")
        if len(result.vectors) != len(window):
            raise ValueError(
                f"The embedding provider returned {len(result.vectors)} vectors for "
                f"{len(window)} texts. Vectors are matched to clauses by position, "
                "so a short batch would attach the wrong vector to the wrong clause."
            )
        vectors.extend(result.vectors)
        outcome.batches += 1
        outcome.model = result.model or outcome.model
        outcome.provider = result.provider or outcome.provider

    outcome.vectors = vectors
    outcome.dim = len(vectors[0]) if vectors else 0
    outcome.duration_ms = int((time.perf_counter() - started) * 1000)

    logger.info(
        "docpipeline_texts_embedded",
        texts=len(texts),
        batches=outcome.batches,
        batch_size=batch_size,
        model=outcome.model,
        dim=outcome.dim,
        duration_ms=outcome.duration_ms,
    )
    return outcome

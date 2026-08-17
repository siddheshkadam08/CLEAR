"""Chunk models (§12).

What remains of the chunking package after the old pipeline was removed: the
types describing a chunk and the diagnostics for one that was rejected.

The *engine* is gone with the stage that ran it. It chunked a
``CanonicalDocument``, which only the retired ENRICHMENT stage ever produced, so
after that stage was deleted nothing could build its input - and its replay tool
read the same missing artifact. The live ``EXTRACTION`` stage builds its own
section-level chunks from the parser's cached page JSON instead (see
``app.orchestrator.stages.extraction._section_chunks``) and persists them as
:class:`SemanticChunk` rows through ``ChunkRepository``, which is why these
models are still very much alive.
"""

from app.ai.chunking.models import (
    ChunkRejection,
    ChunkStatistics,
    ChunkValidationReport,
    SemanticChunk,
)

__all__ = [
    "ChunkRejection",
    "ChunkStatistics",
    "ChunkValidationReport",
    "SemanticChunk",
]

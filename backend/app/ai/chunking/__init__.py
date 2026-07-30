"""Semantic chunking engine (§12).

Fixed-size chunking is prohibited: six meaning-preserving strategies, hybrid by
default, selected by the document's Document Intelligence Profile.
"""

from app.ai.chunking.engine import (
    ChunkConfig,
    ChunkingEngine,
    ChunkingResult,
    chunk_document,
)
from app.ai.chunking.models import (
    ChunkRejection,
    ChunkStatistics,
    ChunkValidationReport,
    SemanticChunk,
)

__all__ = [
    "ChunkConfig",
    "ChunkRejection",
    "ChunkStatistics",
    "ChunkValidationReport",
    "ChunkingEngine",
    "ChunkingResult",
    "SemanticChunk",
    "chunk_document",
]

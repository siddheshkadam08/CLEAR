"""Knowledge graph (§14).

Builds the per-contract graph that hierarchical and graph-aware retrieval traverse:
contract, parties, clauses, obligations and risks as nodes; containment, assignment,
governance and cross-references as edges.

Traversal never crosses a project boundary (§1.1).
"""

from app.ai.graph.builder import (
    GraphEdge,
    GraphNode,
    GraphResult,
    KnowledgeGraphBuilder,
)

__all__ = [
    "GraphEdge",
    "GraphNode",
    "GraphResult",
    "KnowledgeGraphBuilder",
]

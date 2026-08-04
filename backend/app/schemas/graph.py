"""The knowledge graph, as one contract's viewer reads it.

The graph is *built on demand* rather than read back from storage, because there is
nothing to read back: the ``graph_nodes`` and ``graph_edges`` tables existed for a
long time, were indexed, and were never written to by anything - they have since
been dropped. What the Indexing stage actually persists is the *derived edges*, as
``knowledge_relationships`` rows, which carry string references and no labels.

So nodes have never been stored anywhere, and rebuilding is the only way to have
them. That turns out to be the better answer regardless:
:class:`~app.ai.graph.builder.KnowledgeGraphBuilder` is pure and synchronous, the
rows it needs are the same ones the contract detail screen already loads, and a
rebuilt graph reflects the contract *as it is now* - including a clause a reviewer
corrected an hour ago - rather than as it was when indexing last ran.

``dangling`` and ``is_resolved`` are the fields worth looking at. An unresolved
reference is not a rendering problem; it usually means the text cites a schedule or
an annexe that was never uploaded, which is a real gap in the repository.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import Field

from app.schemas.common import ResponseSchema


class GraphNodeResponse(ResponseSchema):
    """One node. Identified by ``(node_type, ref)`` within a contract."""

    node_type: str
    #: Stable within the contract: a clause number, a party name, a row id. This is
    #: what edges point at, so it is the join key the viewer uses - not ``row_id``,
    #: which is null for nodes that stand for something with no table of its own.
    ref: str
    label: str
    row_id: uuid.UUID | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphEdgeResponse(ResponseSchema):
    """One directed edge."""

    relation: str
    source_type: str
    source_ref: str
    target_type: str
    target_ref: str
    label: str | None = None
    source_id: uuid.UUID | None = None
    target_id: uuid.UUID | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    #: False when an endpoint could not be matched to a node. Kept and drawn, not
    #: dropped: the text said this reference exists, and hiding it would hide the
    #: finding.
    is_resolved: bool = False
    #: ``derived`` - structural, follows from our own rows - or ``extracted``, which
    #: is a claim the document made. Worth distinguishing: one is a fact about the
    #: data model, the other is a fact about the contract.
    origin: str = "derived"


class DanglingReference(ResponseSchema):
    """A reference the text made that nothing in this contract satisfies.

    Mirrors exactly what :class:`~app.ai.graph.builder.KnowledgeGraphBuilder` puts
    in ``GraphResult.dangling`` - the literal text of the reference, the relation it
    would have been, and why it could not be resolved. There is no source or target
    node here because there is no target: that is the whole finding.
    """

    #: The reference as the document wrote it - "Section 9.2", "the Supplier".
    reference: str
    relation: str = ""
    reason: str = ""


class ContractGraphResponse(ResponseSchema):
    """Nodes, edges and the counts behind them."""

    contract_id: uuid.UUID
    contract_title: str | None = None
    nodes: list[GraphNodeResponse] = Field(default_factory=list)
    edges: list[GraphEdgeResponse] = Field(default_factory=list)
    dangling: list[DanglingReference] = Field(default_factory=list)
    #: ``nodes``, ``edges``, ``resolved_edges``, ``unresolved_edges``,
    #: ``dangling_references``, ``by_node_type``, ``by_relation``.
    statistics: dict[str, Any] = Field(default_factory=dict)
    #: Human-readable notes, e.g. that some references could not be resolved.
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "ContractGraphResponse",
    "DanglingReference",
    "GraphEdgeResponse",
    "GraphNodeResponse",
]

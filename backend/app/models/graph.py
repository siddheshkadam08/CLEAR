"""Knowledge graph - business relationships between contract entities (§15).

Answers the questions vector search cannot: "which amendments affect Clause 12?",
"what does this definition govern?", "which obligations flow from this party?".

Stored as a property graph in Postgres (nodes + edges with JSONB attributes)
rather than in a separate graph database: traversals here are shallow (2-3 hops,
bounded by ``RETRIEVAL_GRAPH_MAX_DEPTH``) and must join against project-scoped
relational filters in the same query, which a recursive CTE does well and a
cross-database hop does not.

Every node and edge carries ``project_id``; cross-project traversal is
prohibited, and the recursive traversal query re-applies the filter at every
level so a mis-scoped edge cannot leak data across the boundary.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import GraphNodeType, GraphRelation
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import pg_enum


class GraphNode(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A graph node projecting a relational row (contract, clause, party, ...)."""

    __tablename__ = "graph_nodes"

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: NULL for nodes that span contracts - a vendor appearing in many agreements
    #: is one node, which is what makes cross-contract questions answerable.
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    node_type: Mapped[GraphNodeType] = mapped_column(
        pg_enum(GraphNodeType, "graph_node_type"), nullable=False, index=True
    )
    #: Id of the underlying row (clause id, entity id, contract id). Polymorphic,
    #: so not a foreign key.
    ref_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True
    )
    #: Stable natural key within a project, e.g. ``party:acme-corporation``. Lets
    #: node creation be idempotent and lets two contracts converge on one node.
    natural_key: Mapped[str] = mapped_column(String(512), nullable=False)

    label: Mapped[str] = mapped_column(String(512), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    graph_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")

    outgoing: Mapped[list[GraphEdge]] = relationship(
        "GraphEdge",
        back_populates="source",
        foreign_keys="GraphEdge.from_node",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    incoming: Mapped[list[GraphEdge]] = relationship(
        "GraphEdge",
        back_populates="target",
        foreign_keys="GraphEdge.to_node",
        cascade="all, delete-orphan",
        lazy="noload",
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id", "node_type", "natural_key", name="uq_graph_nodes_project_type_key"
        ),
        Index("ix_graph_nodes_project_type", "project_id", "node_type"),
        Index("ix_graph_nodes_ref", "node_type", "ref_id"),
        Index(
            "ix_graph_nodes_label_trgm",
            "label",
            postgresql_using="gin",
            postgresql_ops={"label": "gin_trgm_ops"},
        ),
        Index("ix_graph_nodes_attributes", "attributes", postgresql_using="gin"),
    )


class GraphEdge(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A directed, typed relationship between two nodes."""

    __tablename__ = "graph_edges"

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_node: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("graph_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    to_node: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("graph_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relation: Mapped[GraphRelation] = mapped_column(
        pg_enum(GraphRelation, "graph_relation"), nullable=False, index=True
    )

    #: Traversal ranking: a strong ``amends`` edge outranks a weak inferred
    #: ``references`` edge when the planner has to prune breadth.
    weight: Mapped[float] = mapped_column(
        Numeric(5, 4), nullable=False, default=1.0, server_default="1.0"
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Where this edge came from - clause text, definition resolution, metadata.
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    graph_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")

    source: Mapped[GraphNode] = relationship(
        "GraphNode", back_populates="outgoing", foreign_keys=[from_node]
    )
    target: Mapped[GraphNode] = relationship(
        "GraphNode", back_populates="incoming", foreign_keys=[to_node]
    )

    __table_args__ = (
        UniqueConstraint(
            "from_node", "to_node", "relation", name="uq_graph_edges_from_to_relation"
        ),
        # Forward traversal: "everything this node points at, by relation".
        Index("ix_graph_edges_from_relation", "from_node", "relation"),
        # Reverse traversal: "everything pointing at this node" - the direction
        # that answers "which amendments affect this clause?".
        Index("ix_graph_edges_to_relation", "to_node", "relation"),
        Index("ix_graph_edges_project_relation", "project_id", "relation"),
        CheckConstraint("from_node <> to_node", name="no_self_loop"),
    )


__all__ = ["GraphEdge", "GraphNode"]

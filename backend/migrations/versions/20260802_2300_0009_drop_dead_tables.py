"""Drop three tables that were never written to.

``graph_nodes`` / ``graph_edges``
---------------------------------
Created by revision 0001 and never inserted into by anything, at any point. The
knowledge graph does exist, but it is stored as ``knowledge_relationships`` rows:
``IndexingStage._persist_edges`` writes there, and
``RetrievalEngine._expand_graph`` reads from there.

What made this hard to see is that ``app.ai.graph.builder`` defines dataclasses
*also* called ``GraphNode`` and ``GraphEdge``. Every apparent use of the models
was really one of those - in-memory objects that never touch a session - and the
docstring on ``KnowledgeRelationship`` claimed "the Indexing stage projects these
into app.models.graph for traversal", which was never true.

``clause_history``
------------------
Same shape of dead: no writer and no reader. The review endpoint it was meant to
serve - ``POST /contracts/{id}/clauses/{clause_id}/review`` - records the decision
as an ``audit_log`` row and stashes the model's original output under
``clauses.evidence["original"]``. ``docs/DATABASE_SCHEMA.md`` pointed at
``clause_history.review_decision`` as the answer to "who approved this
extraction"; the honest answer is those two places, and the doc has been
corrected.

The ``graph_relation`` type stays
---------------------------------
Only ``graph_node_type`` is dropped with the tables. ``graph_relation`` is still
the column type of ``knowledge_relationships.relation``, which is very much
alive - dropping it would take the live graph with it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None

TABLES = ("graph_edges", "graph_nodes", "clause_history")


def upgrade() -> None:
    # `graph_edges` before `graph_nodes`: its foreign keys point at them.
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    # Exclusive to the dropped tables. `graph_relation` is deliberately NOT
    # dropped - see the module docstring.
    op.execute("DROP TYPE IF EXISTS graph_node_type")


def downgrade() -> None:
    """Recreate the tables, empty.

    Restoring the rows is not possible and would be meaningless anyway: there
    were never any. This exists so the revision is reversible in the sense that
    matters - the schema goes back to what it was.
    """
    node_type = postgresql.ENUM(
        "contract",
        "party",
        "vendor",
        "customer",
        "clause",
        "obligation",
        "risk",
        "definition",
        name="graph_node_type",
        create_type=False,
    )
    node_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "graph_nodes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contract_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contracts.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("node_type", node_type, nullable=False),
        sa.Column("ref_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("natural_key", sa.String(512), nullable=False),
        sa.Column("label", sa.String(512), nullable=True),
        sa.Column("attributes", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("graph_version", sa.String(32), nullable=False, server_default="1.0.0"),
        sa.UniqueConstraint(
            "project_id", "node_type", "natural_key", name="uq_graph_nodes_project_type_key"
        ),
    )

    op.create_table(
        "graph_edges",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "from_node",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("graph_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "to_node",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("graph_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "relation",
            postgresql.ENUM(name="graph_relation", create_type=False),
            nullable=False,
        ),
        sa.Column("weight", sa.Numeric(5, 4), nullable=False, server_default="1.0"),
        sa.Column("attributes", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("evidence", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("graph_version", sa.String(32), nullable=False, server_default="1.0.0"),
        sa.UniqueConstraint(
            "from_node", "to_node", "relation", name="uq_graph_edges_from_to_relation"
        ),
        sa.CheckConstraint("from_node <> to_node", name="no_self_loop"),
    )

    op.create_table(
        "clause_history",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "clause_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clauses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contract_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contracts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("change_type", sa.String(64), nullable=False),
        sa.Column("field_name", sa.String(128), nullable=True),
        sa.Column("old_value", sa.Text, nullable=True),
        sa.Column("new_value", sa.Text, nullable=True),
        sa.Column("review_decision", sa.String(32), nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column(
            "source", sa.String(32), nullable=False, server_default="ai_extraction"
        ),
        sa.Column("versions", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )

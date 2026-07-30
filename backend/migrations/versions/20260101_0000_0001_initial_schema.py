"""Initial schema: extensions, all tables, triggers, maintenance functions.

Revision ID: 0001
Revises: None

Why this baseline is metadata-driven
------------------------------------
The 36 tables, 235 indexes and 20-odd native enum types in this schema are
defined once, in ``app.models``, and this revision materialises exactly that
definition via ``Base.metadata.create_all``. Hand-transcribing the same DDL here
would create a second source of truth that drifts from the models on the first
edit someone forgets to mirror - the classic way a baseline migration stops
matching the ORM.

Everything the ORM *cannot* express is written explicitly below: extensions,
trigger functions, the ``tsvector`` maintenance trigger, and the enum types that
must exist before ``create_all`` runs.

**Every subsequent revision uses ``alembic revision --autogenerate``** and
contains explicit ``op.*`` calls. This pattern applies to the baseline only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401  (registers every mapper)
from app.db.base import Base

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# -----------------------------------------------------------------------------
# Extensions
# -----------------------------------------------------------------------------
EXTENSIONS = (
    "uuid-ossp",  # uuid_generate_v4() server-side default
    "vector",     # pgvector: embeddings + HNSW indexes
    "pg_trgm",    # trigram indexes for fuzzy name/title search
    "citext",     # case-insensitive email uniqueness
    "btree_gin",  # composite GIN over scalar + jsonb columns
)


# -----------------------------------------------------------------------------
# Trigger functions
# -----------------------------------------------------------------------------
#: Keeps ``updated_at`` honest even for bulk UPDATEs issued outside the ORM.
UPDATED_AT_FUNCTION = """
CREATE OR REPLACE FUNCTION cip_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

#: Maintains the chunk search vector. Weighted so a match in the section title
#: outranks a match deep in body text, which is what makes keyword search on a
#: 150-page contract return the right clause first.
CHUNK_SEARCH_VECTOR_FUNCTION = """
CREATE OR REPLACE FUNCTION cip_chunks_search_vector()
RETURNS TRIGGER AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', coalesce(NEW.section_title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.text, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

#: Guards the platform's central invariant: derived data may not point at a
#: contract in a different project. A bug that mis-scoped a write would otherwise
#: leak one project's clauses into another project's search results, so it is
#: enforced in the database rather than trusted to application code.
PROJECT_SCOPE_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION cip_assert_project_scope()
RETURNS TRIGGER AS $$
DECLARE
    owner_project uuid;
BEGIN
    SELECT project_id INTO owner_project FROM contracts WHERE id = NEW.contract_id;
    IF owner_project IS NULL THEN
        RETURN NEW;  -- FK will raise; nothing to compare against
    END IF;
    IF NEW.project_id <> owner_project THEN
        RAISE EXCEPTION
            'project isolation violation on %: project_id % does not match contract % (project %)',
            TG_TABLE_NAME, NEW.project_id, NEW.contract_id, owner_project
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

#: Tables carrying both project_id and contract_id, i.e. everything derived from
#: a document.
PROJECT_SCOPED_TABLES = (
    "chunks",
    "clauses",
    "entities",
    "obligations",
    "risks",
    "key_dates",
    "knowledge_relationships",
    "contract_summaries",
    "embeddings",
    "document_artifacts",
    "job_stage_runs",
    "processing_jobs",
    "contract_versions",
    "contract_metadata",
)

#: Tables with an ``updated_at`` column maintained by TimestampMixin.
TIMESTAMPED_TABLES = (
    "users",
    "roles",
    "refresh_tokens",
    "projects",
    "project_members",
    "contracts",
    "contract_metadata",
    "processing_jobs",
    "document_profiles",
    "clauses",
    "entities",
    "obligations",
    "risks",
    "key_dates",
    "knowledge_relationships",
    "contract_summaries",
    "chunks",
    "graph_nodes",
    "graph_edges",
    "chat_sessions",
    "clause_master_categories",
    "clause_master_rules",
    "ai_settings",
    "alerts",
    "alert_rules",
    "export_jobs",
)


def upgrade() -> None:
    bind = op.get_bind()

    # --- extensions (must precede create_all: columns depend on them) --------
    for extension in EXTENSIONS:
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{extension}"')

    # --- tables, indexes, constraints, enum types ---------------------------
    Base.metadata.create_all(bind=bind)

    # --- trigger functions ---------------------------------------------------
    op.execute(UPDATED_AT_FUNCTION)
    op.execute(CHUNK_SEARCH_VECTOR_FUNCTION)
    op.execute(PROJECT_SCOPE_GUARD_FUNCTION)

    for table in TIMESTAMPED_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION cip_set_updated_at();
            """
        )

    op.execute(
        """
        CREATE TRIGGER trg_chunks_search_vector
        BEFORE INSERT OR UPDATE OF text, section_title ON chunks
        FOR EACH ROW EXECUTE FUNCTION cip_chunks_search_vector();
        """
    )

    for table in PROJECT_SCOPED_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_project_scope
            BEFORE INSERT OR UPDATE OF project_id, contract_id ON {table}
            FOR EACH ROW EXECUTE FUNCTION cip_assert_project_scope();
            """
        )

    # --- expression indexes the ORM cannot declare portably ------------------
    # Full-text search over clause text (the `_fts` suffix is excluded from
    # autogenerate comparison in migrations/env.py).
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_contract_metadata_summary_fts
        ON contract_metadata USING gin (to_tsvector('english', coalesce(summary, '')));
        """
    )

    # Case-insensitive contract number lookup - users paste them in any case.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_contracts_number_lower
        ON contracts (project_id, lower(contract_number))
        WHERE contract_number IS NOT NULL;
        """
    )

    # --- statistics targets --------------------------------------------------
    # The planner needs good estimates on the columns every project-scoped query
    # filters by; the default target under-samples high-cardinality UUIDs.
    for table, column in (
        ("chunks", "project_id"),
        ("clauses", "project_id"),
        ("embeddings", "project_id"),
        ("contracts", "project_id"),
    ):
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET STATISTICS 500")

    # --- comments (schema self-documentation) -------------------------------
    op.execute(
        "COMMENT ON TABLE projects IS "
        "'Primary business and security boundary. Every derived row carries project_id.'"
    )
    op.execute(
        "COMMENT ON TABLE embeddings IS "
        "'Three-level vector store: document_summary (L1), clause (L2), chunk (L3).'"
    )
    op.execute(
        "COMMENT ON TABLE job_stage_runs IS "
        "'One row per stage attempt. The latest succeeded row is that stage''s checkpoint.'"
    )


def downgrade() -> None:
    bind = op.get_bind()

    for table in PROJECT_SCOPED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_project_scope ON {table}")
    op.execute("DROP TRIGGER IF EXISTS trg_chunks_search_vector ON chunks")
    for table in TIMESTAMPED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")

    op.execute("DROP FUNCTION IF EXISTS cip_assert_project_scope()")
    op.execute("DROP FUNCTION IF EXISTS cip_chunks_search_vector()")
    op.execute("DROP FUNCTION IF EXISTS cip_set_updated_at()")

    op.execute("DROP INDEX IF EXISTS ix_contracts_number_lower")
    op.execute("DROP INDEX IF EXISTS ix_contract_metadata_summary_fts")

    Base.metadata.drop_all(bind=bind)

    # Native enum types are not dropped by drop_all when created implicitly.
    for enum_name in (
        "auth_provider",
        "role_name",
        "project_status",
        "file_type",
        "contract_status",
        "job_state",
        "job_priority",
        "pipeline_stage",
        "stage_status",
        "artifact_kind",
        "chunk_type",
        "chunk_strategy",
        "entity_type",
        "obligation_status",
        "risk_severity",
        "risk_band",
        "date_type",
        "embedding_level",
        "graph_node_type",
        "graph_relation",
        "search_scope",
        "chat_role",
        "response_format",
        "confidence_band",
        "alert_type",
        "alert_severity",
        "alert_status",
        "export_format",
        "export_status",
        "audit_action",
    ):
        op.execute(sa.text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))

    # Extensions are left in place: they may be shared with other schemas in the
    # same database and dropping them is not this migration's business.

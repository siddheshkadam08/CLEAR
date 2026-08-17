"""Add the docpipeline stage and its artifact to the native enums

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-01

``pipeline_stage`` and ``artifact_kind`` are native Postgres enum types, so
adding a member to the Python ``StrEnum`` is only half the change: the first
attempt to write a ``job_stage_runs`` row for the new stage fails with

    invalid input value for enum clear.pipeline_stage: "docpipeline"

and the queue sees a 500 it can only interpret as a transport failure, retries
three times, and dead-letters the job.

``ALTER TYPE ... ADD VALUE`` is not transactional before PostgreSQL 12 and
cannot be run inside a transaction block there. Alembic runs each migration in
one, so both statements use ``COMMIT`` first via an autocommit block. On 12 and
later the value may be added inside a transaction, but it may not be *used*
until that transaction commits - which is fine here, since nothing in this
migration writes a row using it.

``IF NOT EXISTS`` makes this re-runnable: the deployment and the developer
database are the same database, so this migration may meet a type that already
has the value.
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# Schema comes from DB_SCHEMA at runtime - see _resolve_schema() below.


def upgrade() -> None:
    schema = _resolve_schema()
    # Autocommit: ALTER TYPE ... ADD VALUE historically refuses to run inside a
    # transaction block, and Alembic wraps each migration in one.
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE {schema}.pipeline_stage ADD VALUE IF NOT EXISTS 'docpipeline'")
        op.execute(f"ALTER TYPE {schema}.artifact_kind ADD VALUE IF NOT EXISTS 'doc_pipeline'")


def downgrade() -> None:
    """Deliberately a no-op.

    PostgreSQL cannot remove a value from an enum type. Recreating the type
    without it would mean rewriting every column that uses it, and any
    ``job_stage_runs`` row naming the stage would have to be deleted first -
    destroying the record of runs that actually happened, to undo an addition
    that costs nothing to leave in place.
    """


def _resolve_schema() -> str:
    """The schema the enums live in, honouring DB_SCHEMA."""
    from app.core.config import get_settings

    return get_settings().db.schema_name.strip() or "public"

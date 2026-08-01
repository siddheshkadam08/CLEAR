"""Add the extraction stage to the native pipeline_stage enum

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-01

Same shape as 0004, and for the same reason it was needed: ``pipeline_stage`` is
a native Postgres enum type, so adding a member to the Python ``StrEnum`` is only
half the change. Without this, the first attempt to write a ``job_stage_runs``
row for the new stage fails with

    invalid input value for enum clear.pipeline_stage: "extraction"

the endpoint returns a 500, and the dispatcher - which can only see an HTTP
status - reads that as a transport failure, retries three times and dead-letters
the job. The operator sees a job that stopped with no stage row and no error.

``artifact_kind`` needs nothing here: this stage emits ``clauses``, ``entities``,
``obligations``, ``risks``, ``timelines`` and ``extraction_statistics``, all of
which the old pipeline already declared.

``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction block before
PostgreSQL 12, and Alembic wraps each migration in one, hence the autocommit
block. ``IF NOT EXISTS`` makes it re-runnable, which matters because the
developer database and the deployment are the same database.
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = _resolve_schema()
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE {schema}.pipeline_stage ADD VALUE IF NOT EXISTS 'extraction'")


def downgrade() -> None:
    """A no-op, as in 0004.

    PostgreSQL cannot remove a value from an enum type. Rebuilding the type
    without it would mean rewriting every column that uses it and deleting the
    ``job_stage_runs`` rows naming the stage - destroying the record of runs that
    happened, to reverse an addition that costs nothing to leave in place.
    """


def _resolve_schema() -> str:
    from app.core.config import get_settings

    return get_settings().db.schema_name.strip() or "public"

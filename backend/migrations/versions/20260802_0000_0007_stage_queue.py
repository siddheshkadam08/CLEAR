"""The stage queue, in Postgres.

Replaces the Redis-backed BullMQ dispatcher for deployments that would rather not
run a broker. The whole reason a table can do this job is ``FOR UPDATE SKIP
LOCKED``: several workers select the same rows and Postgres hands each of them a
disjoint set instead of serialising them behind one another.

Why a new table rather than reusing ``job_stage_runs``. That table is the audit
record of every attempt and grows without bound. Using it as the queue would make
the claim query scan history to find the handful of rows that are actually
pending, and its ``attempt`` column already carries a different meaning. This
table stays small: rows leave it once they are ``done``.

``dispatch_id`` is unique, which is what makes deduplication mean the right
thing. A driver retrying the same payload sends the same id and collapses onto
the existing row; asking for the stage again builds a new message with a new id
and therefore runs. BullMQ keyed on ``(job, stage, attempt)``, which also
collapsed a *deliberate* re-run onto the run that had already finished - a
reprocess logged ``stage_enqueued`` and then silently did nothing.

Two partial indexes rather than one full one. Between sweeps the table is mostly
``done`` rows, and neither the claim path nor the reclaim path ever looks at
those; a full index would carry them for nothing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "stage_queue"
STATE_ENUM = "stage_queue_state"


def upgrade() -> None:
    bind = op.get_bind()

    state = postgresql.ENUM(
        "pending", "claimed", "done", "dead", name=STATE_ENUM, create_type=False
    )
    state.create(bind, checkfirst=True)

    # Revision 0001 builds the schema with ``Base.metadata.create_all`` against
    # *live* model metadata, so a database created from scratch today already has
    # this table. Every migration after 0001 has to be a no-op in that case - see
    # 0003's note - and ``create_table`` has no ``IF NOT EXISTS`` of its own, so
    # the guard is explicit.
    #
    # The triggers below are deliberately *outside* this branch. ``create_all``
    # builds tables and indexes but knows nothing about triggers, so a fresh
    # database would otherwise end up with the table and neither of them.
    if not sa.inspect(bind).has_table(TABLE, schema=_resolve_schema()):
        _create_table()

    _ensure_triggers()


def _create_table() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            # `uuid_generate_v4()`, not `gen_random_uuid()`, to match
            # UUIDPrimaryKeyMixin - otherwise the two construction paths disagree
            # on the default and autogenerate reports drift forever.
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("dispatch_id", sa.String(32), nullable=False),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("processing_jobs.id", ondelete="CASCADE"),
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
        # Both of these types already exist - every other pipeline table uses
        # them - so they are referenced, never created.
        sa.Column(
            "stage",
            postgresql.ENUM(name="pipeline_stage", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "priority",
            postgresql.ENUM(name="job_priority", create_type=False),
            nullable=False,
            server_default="normal",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
        # Referenced, not created: `upgrade` created the type before branching here.
        sa.Column(
            "state",
            postgresql.ENUM(name=STATE_ENUM, create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(128), nullable=True),
        sa.Column("last_error", postgresql.JSONB, nullable=True),
        sa.UniqueConstraint("dispatch_id", name="uq_stage_queue_dispatch_id"),
    )

    # Declared by TimestampMixin (``index=True``), so `create_all` makes it on the
    # other path. Without it here the two paths diverge and autogenerate proposes
    # adding it on every subsequent migration.
    op.create_index("ix_stage_queue_created_at", TABLE, ["created_at"])
    op.create_index("ix_stage_queue_job_id", TABLE, ["job_id"])
    op.create_index("ix_stage_queue_contract_id", TABLE, ["contract_id"])
    op.create_index("ix_stage_queue_project_id", TABLE, ["project_id"])
    op.create_index("ix_stage_queue_state_stage", TABLE, ["state", "stage"])

    # The claim query's only access path.
    op.create_index(
        "ix_stage_queue_claimable",
        TABLE,
        ["priority", "available_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    # The reclaim sweep's only access path.
    op.create_index(
        "ix_stage_queue_claimed_at",
        TABLE,
        ["claimed_at"],
        postgresql_where=sa.text("state = 'claimed'"),
    )

def _ensure_triggers() -> None:
    """Attach the two shared triggers, whichever path created the table.

    Both functions are defined by revision 0001; this table simply joins the set
    they cover. The scope guard matters more here than on most tables: a queue row
    names a project *and* a contract, and a mismatch would hand a worker
    cross-project work that every downstream stage would then trust.

    Dropped first so this is safe to re-run - Postgres has no
    ``CREATE TRIGGER IF NOT EXISTS``.
    """
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_updated_at ON {TABLE}")
    op.execute(
        f"""
        CREATE TRIGGER trg_{TABLE}_updated_at
        BEFORE UPDATE ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION cip_set_updated_at();
        """
    )
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_project_scope ON {TABLE}")
    op.execute(
        f"""
        CREATE TRIGGER trg_{TABLE}_project_scope
        BEFORE INSERT OR UPDATE OF project_id, contract_id ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION cip_assert_project_scope();
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_project_scope ON {TABLE}")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_updated_at ON {TABLE}")
    op.drop_table(TABLE)
    # Safe to drop unconditionally: nothing else uses this type, unlike
    # `pipeline_stage` and `job_priority` which this migration only referenced.
    postgresql.ENUM(name=STATE_ENUM, create_type=False).drop(op.get_bind(), checkfirst=True)


def _resolve_schema() -> str | None:
    """The schema the tables live in, honouring DB_SCHEMA.

    ``None`` rather than ``"public"`` when unset: the inspector treats ``None`` as
    "whatever the search path resolves to", which is what the rest of the
    migration relies on for its unqualified table names.
    """
    from app.core.config import get_settings

    return get_settings().db.schema_name.strip() or None

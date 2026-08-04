"""Add agreement_type_clauses: which clauses each agreement type looks for.

Replaces the two JSONB arrays on ``document_profiles``
(``mandatory_clauses`` / ``optional_clauses``) as the *source of truth* for the
clause list. The arrays are left in place and are no longer read - dropping them
in the same revision that starts reading a new table would leave no way back if
the backfill turned out to be wrong.

The arrays could hold a set of keys and nothing else. They had nowhere to record
that a clause is configured for a type but currently switched off, so "stop
looking for this" and "forget this was ever configured" were the same edit. A row
with ``is_active = false`` says the first; an absent row says the second.

Backfilled from the arrays, mandatory and optional both landing active - which is
what the arrays meant. ``display_order`` comes from the category's own
``priority``, so the first render is in the order the taxonomy was curated in
rather than alphabetical.

Keys that name no category are skipped rather than inserted. The foreign key
would reject them anyway; skipping means the migration reports how many it
dropped instead of failing on a profile nobody has looked at in months.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_TABLE = "agreement_type_clauses"


def _schema() -> str | None:
    from app.core.config import get_settings

    name = get_settings().db.schema_name.strip()
    return name or None


def upgrade() -> None:
    schema = _schema()
    bind = op.get_bind()

    # Revision 0001 builds the schema with `create_all` against live metadata, so
    # on a database created after this model was added the table already exists.
    if not sa.inspect(bind).has_table(_TABLE, schema=schema):
        op.create_table(
            _TABLE,
            sa.Column(
                "id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                server_default=sa.text("uuid_generate_v4()"),
                nullable=False,
            ),
            sa.Column("agreement_type", sa.String(64), nullable=False),
            sa.Column("clause_key", sa.String(64), nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column(
                "is_mandatory", sa.Boolean(), server_default=sa.text("false"), nullable=False
            ),
            sa.Column("display_order", sa.Integer(), server_default="100", nullable=False),
            sa.Column("updated_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
            sa.PrimaryKeyConstraint("id", name="pk_agreement_type_clauses"),
            sa.ForeignKeyConstraint(
                ["clause_key"],
                [f"{schema}.clause_master_categories.key" if schema else "clause_master_categories.key"],
                name="fk_agreement_type_clauses_clause_key_clause_master_categories",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["updated_by"],
                [f"{schema}.users.id" if schema else "users.id"],
                name="fk_agreement_type_clauses_updated_by_users",
                ondelete="SET NULL",
            ),
            sa.UniqueConstraint("agreement_type", "clause_key", name="uq_agreement_type_clause"),
            schema=schema,
        )
        op.create_index(
            "ix_agreement_type_clauses_agreement_type",
            _TABLE,
            ["agreement_type"],
            schema=schema,
        )
        op.create_index(
            "ix_agreement_type_clauses_clause_key", _TABLE, ["clause_key"], schema=schema
        )
        op.create_index(
            "ix_agreement_type_clauses_created_at", _TABLE, ["created_at"], schema=schema
        )
        op.create_index(
            "ix_agreement_type_clauses_active",
            _TABLE,
            ["agreement_type", "display_order"],
            schema=schema,
            postgresql_where=sa.text("is_active = true"),
        )

    _backfill(bind, schema)


def _backfill(bind: sa.engine.Connection, schema: str | None) -> None:
    """Copy the profile arrays into rows, once.

    `ON CONFLICT DO NOTHING` so re-running is a no-op and an operator's later
    edits are never overwritten by a second upgrade.
    """
    prefix = f'"{schema}".' if schema else ""

    inserted = bind.execute(
        sa.text(
            f"""
            INSERT INTO {prefix}{_TABLE}
                (agreement_type, clause_key, is_active, is_mandatory, display_order)
            SELECT p.agreement_type,
                   c.key,
                   true,
                   bool_or(source.mandatory),
                   MIN(c.priority)
            FROM {prefix}document_profiles AS p
            CROSS JOIN LATERAL (
                SELECT jsonb_array_elements_text(COALESCE(p.mandatory_clauses, '[]'::jsonb)) AS key,
                       true AS mandatory
                UNION ALL
                SELECT jsonb_array_elements_text(COALESCE(p.optional_clauses, '[]'::jsonb)) AS key,
                       false AS mandatory
            ) AS source
            JOIN {prefix}clause_master_categories AS c
              ON c.key = source.key AND c.deleted_at IS NULL
            WHERE p.is_active AND p.deleted_at IS NULL
            GROUP BY p.agreement_type, c.key
            ON CONFLICT (agreement_type, clause_key) DO NOTHING
            """
        )
    ).rowcount

    # Anything the join dropped: a profile naming a key no category has. The
    # `license_agreement` profile did exactly this with `termination` for months.
    orphans = bind.execute(
        sa.text(
            f"""
            SELECT count(*) FROM (
                SELECT DISTINCT k.key
                FROM {prefix}document_profiles AS p
                CROSS JOIN LATERAL jsonb_array_elements_text(
                    COALESCE(p.mandatory_clauses, '[]'::jsonb)
                    || COALESCE(p.optional_clauses, '[]'::jsonb)
                ) AS k(key)
                LEFT JOIN {prefix}clause_master_categories AS c
                       ON c.key = k.key AND c.deleted_at IS NULL
                WHERE p.is_active AND p.deleted_at IS NULL AND c.key IS NULL
            ) AS missing
            """
        )
    ).scalar()

    print(f"  agreement_type_clauses: {inserted} rows backfilled, {orphans} unknown key(s) skipped")


def downgrade() -> None:
    schema = _schema()
    # The profile arrays were never dropped, so removing this table restores the
    # previous source of truth intact.
    op.drop_table(_TABLE, schema=schema)

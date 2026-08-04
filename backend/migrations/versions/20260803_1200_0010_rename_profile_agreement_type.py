"""Rename document_profiles.contract_type -> agreement_type.

One concept had three names. ``contracts.agreement_type`` and
``document_profiles.contract_type`` hold *identical* ``AgreementType`` values, and
the retired vendor taxonomy called the same thing ``docType`` - which is why
``taxonomy.agreement_type_for`` existed at all. It is now the identity function
for every configured profile, so the translation layer was bridging a naming
difference and nothing else.

``contract_subtype`` moves with it. It has no readers today, but leaving one half
of a pair renamed is how the next inconsistency starts.

Renames rather than add-copy-drop: the column keeps its data, its type and its
position, and there is no window where two columns disagree.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_TABLE = "document_profiles"
_INDEX = "ix_document_profiles_type_active"


def _schema() -> str | None:
    from app.core.config import get_settings

    name = get_settings().db.schema_name.strip()
    return name or None


def _qualified(schema: str | None, name: str) -> str:
    return f'"{schema}".{name}' if schema else name


def _has_column(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns(_TABLE, schema=_schema())}
    return name in columns


def upgrade() -> None:
    schema = _schema()

    # Guarded because revision 0001 builds the schema with `create_all` against
    # live metadata: on a database created after this rename the column is
    # already `agreement_type`, and an unguarded ALTER would fail the migration
    # on precisely the deployments that need it least.
    if _has_column("contract_type"):
        op.alter_column(
            _TABLE, "contract_type", new_column_name="agreement_type", schema=schema
        )
    if _has_column("contract_subtype"):
        op.alter_column(
            _TABLE, "contract_subtype", new_column_name="agreement_subtype", schema=schema
        )

    # The indexes follow the column. Postgres keeps an index working across a
    # column rename, but its *name* still says `contract_type`, and autogenerate
    # compares names - so both would be proposed for drop-and-recreate on the
    # next run. Two indexes cover this column:
    #
    #   ix_document_profiles_contract_type   from `index=True` on the column
    #   ix_document_profiles_type_active     the explicit composite
    op.execute(
        sa.text(
            f'ALTER INDEX IF EXISTS {_qualified(schema, "ix_document_profiles_contract_type")} '
            f'RENAME TO ix_document_profiles_agreement_type'
        )
    )
    op.drop_index(_INDEX, table_name=_TABLE, schema=schema, if_exists=True)
    op.create_index(_INDEX, _TABLE, ["agreement_type", "is_active"], schema=schema)


def downgrade() -> None:
    schema = _schema()

    if _has_column("agreement_type"):
        op.alter_column(
            _TABLE, "agreement_type", new_column_name="contract_type", schema=schema
        )
    if _has_column("agreement_subtype"):
        op.alter_column(
            _TABLE, "agreement_subtype", new_column_name="contract_subtype", schema=schema
        )

    op.execute(
        sa.text(
            f'ALTER INDEX IF EXISTS {_qualified(schema, "ix_document_profiles_agreement_type")} '
            f'RENAME TO ix_document_profiles_contract_type'
        )
    )
    op.drop_index(_INDEX, table_name=_TABLE, schema=schema, if_exists=True)
    op.create_index(_INDEX, _TABLE, ["contract_type", "is_active"], schema=schema)

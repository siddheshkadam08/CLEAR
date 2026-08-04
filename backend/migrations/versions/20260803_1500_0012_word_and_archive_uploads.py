"""Word and archive uploads: original vs processing file paths.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-03

Until now "the file" was one thing, so one column held it. Accepting DOC and DOCX
splits that in two: the bytes the user gave us, and the PDF the pipeline reads.
They are the same object for a PDF upload and different objects for a Word one,
and different callers want different ones - the evidence viewer must show the PDF
the coordinates were computed against, while the download button must return what
was actually uploaded.

Backward compatibility is the constraint that shapes this migration:

* every new column is nullable, so the existing rows are valid the moment it runs;
* `storage_path` is left alone and keeps meaning "the bytes to process", which is
  what a dozen downstream call sites already assume. The requirement was that no
  downstream service change because of this feature, and this is how that is paid
  for - `processing_file_path` is added beside it rather than replacing it;
* the backfill states the invariant for historical rows: everything already in the
  table was a PDF or a natively-parsed DOCX, so its original *is* its processing
  file and there is no converted file.

`file_type` is deliberately not touched. It stays as the type of the file being
processed, so parser selection and every existing filter keep working; what the
user uploaded is recorded separately in `original_file_type`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_SCHEMA = "clear"
_TABLE = "contracts"

_NEW_COLUMNS = (
    "original_file_path",
    "converted_file_path",
    "processing_file_path",
    "original_file_type",
    "processing_sha256",
    "source_archive_id",
    "source_archive_name",
)


def _has_column(name: str) -> bool:
    """Revision 0001 builds the schema with `create_all` against live metadata.

    So a column added to the model is already present on a database created after
    the model changed, and absent on one created before it. Every migration after
    0001 has to tolerate both.
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table AND column_name = :column"
        ),
        {"schema": _SCHEMA, "table": _TABLE, "column": name},
    )
    return rows.first() is not None


def _enum_has_value(enum_name: str, value: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid "
            "JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE n.nspname = :schema AND t.typname = :enum AND e.enumlabel = :value"
        ),
        {"schema": _SCHEMA, "enum": enum_name, "value": value},
    )
    return rows.first() is not None


def upgrade() -> None:
    # --- enum values ------------------------------------------------------
    #
    # `ADD VALUE` cannot run inside a transaction block on older servers and
    # cannot be undone, which is why the downgrade below leaves them in place.
    for enum_name, value in (
        ("file_type", "doc"),
        ("contract_status", "conversion_failed"),
    ):
        if not _enum_has_value(enum_name, value):
            op.execute(f"ALTER TYPE {_SCHEMA}.{enum_name} ADD VALUE IF NOT EXISTS '{value}'")

    # --- columns ----------------------------------------------------------
    definitions = {
        "original_file_path": sa.Column("original_file_path", sa.String(1024), nullable=True),
        "converted_file_path": sa.Column("converted_file_path", sa.String(1024), nullable=True),
        "processing_file_path": sa.Column("processing_file_path", sa.String(1024), nullable=True),
        "original_file_type": sa.Column(
            "original_file_type",
            sa.Enum(name="file_type", schema=_SCHEMA, create_type=False),
            nullable=True,
        ),
        # No unique constraint, unlike `sha256_hash`. This is an integrity check
        # on stored bytes, not an identity: two contracts converted from the same
        # Word document legitimately hold different values here, and two PDFs of
        # identical content legitimately hold the same one.
        "processing_sha256": sa.Column("processing_sha256", sa.String(64), nullable=True),
        "source_archive_id": sa.Column(
            "source_archive_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True
        ),
        "source_archive_name": sa.Column("source_archive_name", sa.String(512), nullable=True),
    }
    for name in _NEW_COLUMNS:
        if not _has_column(name):
            op.add_column(_TABLE, definitions[name], schema=_SCHEMA)

    op.create_index(
        "ix_contracts_source_archive_id",
        _TABLE,
        ["source_archive_id"],
        unique=False,
        schema=_SCHEMA,
        postgresql_where=sa.text("source_archive_id IS NOT NULL"),
        if_not_exists=True,
    )

    # --- backfill ---------------------------------------------------------
    #
    # Everything already stored went straight into the pipeline as uploaded, so
    # original == processing and nothing was converted. Writing that explicitly
    # means the new columns are usable without a "NULL means the old behaviour"
    # special case in every reader.
    op.execute(
        sa.text(
            f"""
            UPDATE {_SCHEMA}.{_TABLE}
               SET original_file_path   = COALESCE(original_file_path, storage_path),
                   processing_file_path = COALESCE(processing_file_path, storage_path),
                   original_file_type   = COALESCE(original_file_type, file_type),
                   processing_sha256    = COALESCE(processing_sha256, sha256_hash)
             WHERE original_file_path IS NULL
                OR processing_file_path IS NULL
                OR original_file_type IS NULL
                OR processing_sha256 IS NULL
            """
        )
    )


def downgrade() -> None:
    """Drops the columns. The enum values stay.

    Postgres cannot remove a value from an enum type, and recreating the type
    would mean rewriting every dependent column - far more destructive than
    leaving two unused labels behind.
    """
    op.drop_index(
        "ix_contracts_source_archive_id", table_name=_TABLE, schema=_SCHEMA, if_exists=True
    )
    for name in _NEW_COLUMNS:
        if _has_column(name):
            op.drop_column(_TABLE, name, schema=_SCHEMA)

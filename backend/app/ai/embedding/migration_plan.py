"""Detect a vector-schema mismatch and write the migration that fixes it.

Deliberately generates and never applies. A migration that retypes the embedding
column also destroys every vector in it - that is unavoidable, because vectors of
different widths from different models cannot be converted - and a tool that does
that as a side effect of a health check is a tool nobody should run in production.

So the flow is: detect, explain in full (what changes, which indexes are rebuilt,
what is lost, what has to be re-run), write the file, and stop. Applying it stays
an explicit ``cip migrate``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.ai.embedding.pgvector import HNSW_MAX_DIMS, PgVectorInfo
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: The column the embedding pipeline writes to.
TARGET_TABLE = "embeddings"
TARGET_COLUMN = "embedding"

#: Per-level partial HNSW indexes, kept in step with ``app.models.embedding``.
LEVEL_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_embeddings_hnsw_document_summary", "document_summary"),
    ("ix_embeddings_hnsw_clause", "clause"),
    ("ix_embeddings_hnsw_chunk", "chunk"),
)


@dataclass(slots=True)
class MigrationPlan:
    """What would have to change, and why."""

    required: bool
    reasons: list[str] = field(default_factory=list)
    current_type: str | None = None
    current_dim: int | None = None
    target_type: str = ""
    target_dim: int = 0
    indexes_rebuilt: list[str] = field(default_factory=list)
    destroys_vectors: bool = False
    existing_vectors: int | None = None
    revision: str | None = None
    path: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "reasons": self.reasons,
            "current": (f"{self.current_type}({self.current_dim})" if self.current_type else None),
            "target": f"{self.target_type}({self.target_dim})",
            "indexes_rebuilt": self.indexes_rebuilt,
            "destroys_vectors": self.destroys_vectors,
            "existing_vectors": self.existing_vectors,
            "generated_revision": self.revision,
            "generated_path": str(self.path) if self.path else None,
        }

    def explain(self) -> str:
        """The operator-facing account of what this migration does."""
        if not self.required:
            return (
                f"No migration required. {TARGET_TABLE}.{TARGET_COLUMN} is already "
                f"{self.target_type}({self.target_dim})."
            )

        lines = [
            f"A migration is required for {TARGET_TABLE}.{TARGET_COLUMN}.",
            "",
            f"  Current : {self.current_type}({self.current_dim})"
            if self.current_type
            else "  Current : (column not found)",
            f"  Target  : {self.target_type}({self.target_dim})",
            "",
            "Why:",
        ]
        lines.extend(f"  - {reason}" for reason in self.reasons)

        if self.indexes_rebuilt:
            lines += [
                "",
                "Indexes dropped and recreated (a column's type cannot change while an",
                "index depends on it, and the HNSW operator class is type-specific):",
            ]
            lines.extend(f"  - {name}" for name in self.indexes_rebuilt)

        if self.destroys_vectors:
            count = (
                f"{self.existing_vectors:,}"
                if self.existing_vectors is not None
                else "all existing"
            )
            lines += [
                "",
                "ALL EMBEDDINGS MUST BE REGENERATED.",
                f"  {count} vector(s) will be deleted. This is not recoverable and not",
                "  avoidable: a vector of one width produced by one model cannot be",
                "  converted to another width for another model. Padding would invent",
                "  dimensions the model never produced, and keeping both is worse than",
                "  deleting - vectors from two models occupy unrelated spaces, so a",
                "  mixed index returns confident nonsense rather than failing.",
                "",
                "  The rows are fully derived data. Every one is regenerable from",
                "  contract text in object storage and chunks in Postgres:",
                "",
                "      cip migrate",
                "      cip reindex-embeddings --all",
                "",
                "  Semantic search is degraded until the re-index completes. Keyword",
                "  search, clause browsing, extraction and exports are unaffected.",
            ]
        return "\n".join(lines)


def detect(
    info: PgVectorInfo,
    *,
    settings: Settings | None = None,
    reported_dim: int | None = None,
    existing_vectors: int | None = None,
) -> MigrationPlan:
    """Compare the live column against the configuration and the live model.

    ``reported_dim`` is the width the provider actually returned on a probe. When
    supplied it wins over ``EMBEDDING_DIM``, because the model is the ground truth
    and a configuration that disagrees with it is itself the bug.
    """
    resolved = settings or get_settings()
    embedding = resolved.embedding

    target_dim = reported_dim or embedding.dim
    target_type = embedding.storage

    # If the model is wider than the chosen storage can index, the storage type is
    # what has to change - silently narrowing the model is not an option.
    if target_dim > HNSW_MAX_DIMS.get(target_type, 0):
        target_type = "halfvec"

    plan = MigrationPlan(
        required=False,
        target_type=target_type,
        target_dim=target_dim,
        existing_vectors=existing_vectors,
    )

    column = info.column(TARGET_TABLE, TARGET_COLUMN)
    if column is None:
        plan.required = True
        plan.reasons.append(
            f"{TARGET_TABLE}.{TARGET_COLUMN} does not exist. Run the initial migration."
        )
        return plan

    plan.current_type = column.type_name
    plan.current_dim = column.dim

    if column.dim != target_dim:
        plan.required = True
        plan.destroys_vectors = True
        source = "the provider" if reported_dim else "EMBEDDING_DIM"
        plan.reasons.append(
            f"The column is {column.dim} dimensions but {source} reports {target_dim}. "
            "Every insert would be rejected."
        )

    if column.type_name != target_type:
        plan.required = True
        plan.destroys_vectors = True
        plan.reasons.append(
            f"The column is '{column.type_name}' but '{target_type}' is required. "
            f"pgvector's HNSW index supports at most "
            f"{HNSW_MAX_DIMS.get(column.type_name, 0)} dimensions for "
            f"'{column.type_name}', and this deployment needs {target_dim}."
        )

    # An index that exists but is invalid is the quietest failure of the three: the
    # planner ignores it and every search silently becomes a sequential scan.
    invalid = [index.name for index in info.indexes_for(TARGET_TABLE) if not index.valid]
    if invalid:
        plan.required = True
        plan.reasons.append(
            f"Invalid index(es) present: {', '.join(invalid)}. The planner will not "
            "use them, so every similarity search is a sequential scan."
        )

    expected = {name for name, _ in LEVEL_INDEXES}
    present = {index.name for index in info.indexes_for(TARGET_TABLE)}
    missing = sorted(expected - present)
    if missing and not plan.required:
        plan.required = True
        plan.reasons.append(
            f"Missing HNSW index(es): {', '.join(missing)}. Similarity search over "
            "those levels is a sequential scan."
        )

    if plan.required:
        plan.indexes_rebuilt = [name for name, _ in LEVEL_INDEXES]

    return plan


def render(plan: MigrationPlan, *, down_revision: str, revision: str) -> str:
    """The Alembic script implementing ``plan``."""
    ops = "halfvec_cosine_ops" if plan.target_type == "halfvec" else "vector_cosine_ops"
    settings = get_settings().embedding
    drops = "\n".join(f'    op.execute("DROP INDEX IF EXISTS {name}")' for name, _ in LEVEL_INDEXES)
    creates = "\n".join(
        f"""    op.execute(
        \"\"\"
        CREATE INDEX {name}
        ON {TARGET_TABLE} USING hnsw ({TARGET_COLUMN} {ops})
        WITH (m = {settings.hnsw_m}, ef_construction = {settings.hnsw_ef_construction})
        WHERE level = '{level}'
        \"\"\"
    )"""
        for name, level in LEVEL_INDEXES
    )
    reasons = "\n".join(f"* {reason}" for reason in plan.reasons)

    # S608 is a false positive: this f-string builds a *migration script*, not a
    # query. Every interpolated value is a module constant or a type name already
    # validated against `HNSW_MAX_DIMS` - never caller input.
    return f'''"""Align the vector column with the active embedding model.

Revision ID: {revision}
Revises: {down_revision}

Generated by ``python -m app.tools.verify_pgvector --generate-migration``.

Why this is required
--------------------
{reasons}

  Current : {plan.current_type}({plan.current_dim})
  Target  : {plan.target_type}({plan.target_dim})

Indexes dropped and recreated
-----------------------------
A column's type cannot change while an index depends on it, and the HNSW operator
class is type-specific ({ops}):

{chr(10).join(f"* {name}" for name, _ in LEVEL_INDEXES)}

Embeddings are deleted, not converted
-------------------------------------
A vector of one width produced by one model cannot be converted to another width
for another model. Padding would invent dimensions the model never produced, and
keeping both is worse than deleting: vectors from two models occupy unrelated
spaces, so a mixed index returns confident nonsense rather than failing.

The rows are fully derived. After applying this::

    cip reindex-embeddings --all

Semantic search is degraded until that completes; keyword search is unaffected.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "{revision}"
down_revision: str | None = "{down_revision}"
branch_labels: str | None = None
depends_on: str | None = None

TARGET_TYPE = "{plan.target_type}"
TARGET_DIM = {plan.target_dim}
PREVIOUS_TYPE = "{plan.current_type}"
PREVIOUS_DIM = {plan.current_dim}


def upgrade() -> None:
    bind = op.get_bind()

    if TARGET_TYPE == "halfvec":
        version = bind.execute(
            sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one_or_none()
        if version is None:
            raise RuntimeError("The pgvector extension is not installed.")
        parts = [int(p) for p in str(version).split(".")[:2] if p.isdigit()]
        if parts < [0, 7]:
            raise RuntimeError(
                f"pgvector {{version}} does not support halfvec (needs >= 0.7.0)."
            )

    existing = bind.execute(sa.text("SELECT count(*) FROM {TARGET_TABLE}")).scalar_one()
    if existing:
        print(  # noqa: T201 - alembic's channel to the operator
            f"  -> discarding {{existing}} embedding(s); they cannot be converted. "
            "Run `cip reindex-embeddings --all` after this migration."
        )

{drops}

    op.execute("DELETE FROM {TARGET_TABLE}")
    op.execute(
        f"ALTER TABLE {TARGET_TABLE} ALTER COLUMN {TARGET_COLUMN} "
        f"TYPE {{TARGET_TYPE}}({{TARGET_DIM}})"
    )
    op.execute(f"ALTER TABLE {TARGET_TABLE} ALTER COLUMN dim SET DEFAULT {{TARGET_DIM}}")

{creates}


def downgrade() -> None:
    """Equally destructive, for the same reason - see the module docstring."""
{drops}

    op.execute("DELETE FROM {TARGET_TABLE}")
    op.execute(
        f"ALTER TABLE {TARGET_TABLE} ALTER COLUMN {TARGET_COLUMN} "
        f"TYPE {{PREVIOUS_TYPE}}({{PREVIOUS_DIM}})"
    )
    op.execute(f"ALTER TABLE {TARGET_TABLE} ALTER COLUMN dim SET DEFAULT {{PREVIOUS_DIM}}")

{creates}
'''


def versions_dir() -> Path:
    # parents[3] is the backend root: this file is app/ai/embedding/…, so
    # parents[2] lands on `app/` and pointed at `app/migrations/versions`, which
    # does not exist. The failure was doubly confusing - the missing directory
    # made `current_head()` glob nothing and fall back to "0001", so the
    # generator both numbered the revision wrong and then crashed writing it.
    return Path(__file__).resolve().parents[3] / "migrations" / "versions"


def current_head() -> str:
    """Highest existing revision id, so the generated script chains correctly."""
    revisions: list[str] = []
    for path in versions_dir().glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("revision:") or line.startswith("revision ="):
                value = line.split("=", 1)[1].strip().strip("\"'")
                if value:
                    revisions.append(value)
                break
    return max(revisions) if revisions else "0001"


def write(plan: MigrationPlan, *, stamp: str | None = None) -> Path:
    """Write the migration file and return its path. Does not apply anything."""
    head = current_head()
    try:
        revision = f"{int(head) + 1:04d}"
    except ValueError:  # pragma: no cover - non-numeric revision scheme
        revision = f"{head}_embedding_dim"

    moment = stamp or datetime.now().strftime("%Y%m%d_%H%M")
    path = versions_dir() / f"{moment}_{revision}_align_vector_dimension.py"
    path.write_text(render(plan, down_revision=head, revision=revision), encoding="utf-8")

    plan.revision = revision
    plan.path = path
    logger.info("migration_generated", revision=revision, path=str(path))
    return path


__all__ = [
    "LEVEL_INDEXES",
    "TARGET_COLUMN",
    "TARGET_TABLE",
    "MigrationPlan",
    "current_head",
    "detect",
    "render",
    "versions_dir",
    "write",
]

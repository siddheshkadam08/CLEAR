"""Declarative base, naming conventions and shared model mixins.

Every table in the platform is built from these primitives, which is what makes
the cross-cutting guarantees mechanical rather than per-model discipline:

* **Deterministic constraint names** - Alembic autogenerate produces stable
  migrations instead of database-assigned names that differ per environment.
* :class:`UUIDPrimaryKeyMixin` - UUID v4 keys generated in Python, so a row's id
  is known before ``flush()`` and can be referenced while building a graph of
  related objects in one transaction.
* :class:`TimestampMixin` - ``created_at``/``updated_at`` maintained by the
  database, not the application clock.
* :class:`SoftDeleteMixin` - ``deleted_at``; nothing legally traceable is ever
  hard-deleted.
* :class:`ProjectScopedMixin` - the ``project_id`` that carries the platform's
  security boundary (§1.1). Any model holding contract-derived data inherits it.
* :class:`VersionedMixin` - optimistic concurrency via a ``version`` counter.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from app.core.enums import ReviewStatus

# Deterministic constraint naming. `ix` includes the column list so composite
# indexes get distinct names.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _configured_schema() -> str | None:
    """The schema every table belongs to, or ``None`` for ``public``.

    Qualifying the metadata is not interchangeable with setting a connection
    ``search_path``, and the difference is destructive rather than cosmetic.
    ``create_all`` runs with ``checkfirst=True``: with only a search_path of
    ``clear,public``, its existence probe for ``contracts`` finds a *different*
    application's ``public.contracts`` sitting later on the path, concludes ours
    already exists, and skips it. Every foreign key then resolves to that foreign
    table, and the migration dies on:

        Key columns "contract_id" and "id" are of incompatible types:
        uuid and integer

    With the schema on the metadata, DDL and existence checks are both
    schema-qualified, so neither can be satisfied by a same-named table
    elsewhere. The search_path is still set on connections, so extension-owned
    objects in ``public`` - the ``vector`` type, ``uuid_generate_v4()`` - keep
    resolving.
    """
    from app.core.config import get_settings

    name = get_settings().db.schema_name.strip()
    return name or None


metadata_obj = MetaData(naming_convention=NAMING_CONVENTION, schema=_configured_schema())


class Base(DeclarativeBase):
    """Declarative base for every model."""

    metadata = metadata_obj

    #: JSONB everywhere - never the JSON type. JSONB is indexable with GIN and
    #: is what the schema design in §7 requires for filterable metadata.
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        list[str]: JSONB,
        list[dict[str, Any]]: JSONB,
    }

    def to_dict(self, exclude: set[str] | None = None) -> dict[str, Any]:
        """Shallow column dict. For API output prefer the Pydantic schemas."""
        exclude = exclude or set()
        return {
            column.key: getattr(self, column.key)
            for column in self.__table__.columns
            if column.key not in exclude
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        identifier = getattr(self, "id", None)
        return f"<{type(self).__name__} id={identifier}>"


# =============================================================================
# Mixins
# =============================================================================
class UUIDPrimaryKeyMixin:
    """UUID primary key generated client-side."""

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )


class TimestampMixin:
    """Database-maintained ``created_at`` / ``updated_at``.

    ``onupdate`` is set both in Python and via the ``updated_at`` trigger created
    in the initial migration, so a bulk ``UPDATE`` issued outside the ORM still
    refreshes the timestamp.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SoftDeleteMixin:
    """Soft deletion. ``deleted_at IS NULL`` is the "live row" predicate.

    Repositories apply the filter centrally; a partial index on the predicate
    keeps the common path cheap.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class VersionedMixin:
    """Monotonic ``version`` counter.

    Used for artifact/record lineage ("which generation of this chunk set is
    this?"). Models that additionally want SQLAlchemy's optimistic-concurrency
    check opt in explicitly with
    ``__mapper_args__ = {"version_id_col": <Model>.version}``, because the check
    raises ``StaleDataError`` on concurrent writes and not every table wants that.
    """

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class ProjectScopedMixin:
    """Adds the ``project_id`` FK that carries the security boundary.

    Every row holding contract-derived data inherits this. ``ON DELETE CASCADE``
    is deliberate: purging a project must leave no derived data behind.
    """

    @declared_attr
    @classmethod
    def project_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            PGUUID(as_uuid=True),
            ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )


class ContractScopedMixin:
    """Adds ``contract_id`` alongside ``project_id`` for per-document artifacts."""

    @declared_attr
    @classmethod
    def contract_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            PGUUID(as_uuid=True),
            ForeignKey("contracts.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )


class EvidenceMixin:
    """Provenance carried by every AI-derived record (§12 explainable AI).

    An extracted fact without evidence is not deliverable, so the columns live in
    one mixin rather than being re-declared (and occasionally forgotten) per
    model. ``bounding_boxes`` is a list of
    ``{page_number, x, y, width, height}`` - exactly what the PDF viewer needs to
    draw a highlight.
    """

    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bounding_boxes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class ExtractionProvenanceMixin:
    """The version set that produced an AI-extracted record (§25).

    Stored per row rather than only per job: a contract can be partially
    reprocessed, leaving clauses from different prompt versions side by side, and
    each must remain individually reproducible.
    """

    #: 0..1 model confidence. Numeric, not float: these values are reported to
    #: users and compared against thresholds, so exact decimal storage matters.
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True, index=True)
    validation_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    review_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ReviewStatus.NOT_REQUIRED.value,
        server_default=ReviewStatus.NOT_REQUIRED.value,
    )
    profile_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    artifact_version: Mapped[str | None] = mapped_column(String(32), nullable=True)


def project_index(table_name: str, *columns: str, unique: bool = False) -> Index:
    """Composite index whose leading column is ``project_id``.

    Project-scoped filtering happens on every read, so it belongs first in the
    index for the planner to use it.
    """
    name = f"ix_{table_name}_project_{'_'.join(columns)}"
    return Index(name, "project_id", *columns, unique=unique)


__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "ContractScopedMixin",
    "EvidenceMixin",
    "ExtractionProvenanceMixin",
    "ProjectScopedMixin",
    "SoftDeleteMixin",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "VersionedMixin",
    "metadata_obj",
    "project_index",
]

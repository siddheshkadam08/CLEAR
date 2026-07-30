"""Export contracts (§23, FR-6).

The exporter is deliberately a *renderer*, not a query: it receives a fully
assembled :class:`ExportDataset` and turns it into bytes. Everything that decides
*which* rows a user may see - project scoping, RBAC, clause masking - happens
while the dataset is built (:mod:`app.export.dataset`), in one place, once.

That split is the point. If each format re-queried the database, each format would
be a fresh opportunity to forget a `project_id` filter, and a CSV exporter that
leaks another project's clauses is the same breach as an API that does. A renderer
that never sees the database cannot leak from it.

Adding a format is therefore one new :class:`IExporter` and a registry entry.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.core.enums import ExportFormat


class ExportEntity(StrEnum):
    """What can be exported. One worksheet per entity in a workbook.

    Stored as plain strings in ``export_jobs.entities`` (JSONB) rather than a
    Postgres enum, so a new entity does not need a migration.
    """

    CONTRACTS = "contracts"
    CLAUSES = "clauses"
    OBLIGATIONS = "obligations"
    RISKS = "risks"
    KEY_DATES = "key_dates"
    ENTITIES = "entities"


#: Order entities appear in when the request does not pin one. Contracts first
#: because every other sheet refers back to a contract by title.
DEFAULT_ENTITIES: tuple[ExportEntity, ...] = (
    ExportEntity.CONTRACTS,
    ExportEntity.CLAUSES,
    ExportEntity.OBLIGATIONS,
    ExportEntity.RISKS,
    ExportEntity.KEY_DATES,
)


@dataclass(frozen=True)
class ExportColumn:
    """One column: where the value comes from and how it should be presented."""

    key: str
    label: str
    #: ``text`` | ``number`` | ``integer`` | ``date`` | ``datetime`` | ``bool``
    kind: str = "text"
    width: int = 18
    #: Long free text (clause bodies) wraps rather than running off the sheet.
    wrap: bool = False


@dataclass(frozen=True)
class ExportSheet:
    """One entity's rows, already flattened to primitives.

    ``rows`` holds dicts keyed by :attr:`ExportColumn.key`. A missing key renders
    blank, which is correct: an attribute the extractor never produced and one it
    produced as null are both "nothing to show" at this level, and the distinction
    that matters is preserved on the clause record itself.
    """

    entity: ExportEntity
    title: str
    columns: Sequence[ExportColumn]
    rows: Sequence[dict[str, Any]]
    #: Shown on the summary sheet when rows were withheld rather than absent.
    notes: Sequence[str] = field(default_factory=tuple)

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class ExportDataset:
    """Everything one export renders, plus the provenance of the request.

    The scope description and filters travel with the data because an exported
    workbook outlives the screen it came from: six months later, "which contracts
    is this actually?" has to be answerable from the file itself.
    """

    export_id: uuid.UUID
    generated_at: datetime
    generated_by: str
    scope_label: str
    project_names: Sequence[str]
    filters: dict[str, Any]
    sheets: Sequence[ExportSheet]
    #: Rows removed by clause masking, surfaced so the file never silently lies
    #: about being complete.
    masked_note: str | None = None

    @property
    def total_rows(self) -> int:
        return sum(sheet.row_count for sheet in self.sheets)


@dataclass(frozen=True)
class ExportArtifact:
    """Rendered bytes plus what the storage layer and the browser need."""

    content: bytes
    file_name: str
    content_type: str
    row_count: int

    @property
    def size(self) -> int:
        return len(self.content)


class IExporter(ABC):
    """Renders an :class:`ExportDataset` into a downloadable file."""

    #: The format this exporter claims in the registry.
    format: ExportFormat
    extension: str
    content_type: str

    @abstractmethod
    def render(self, dataset: ExportDataset) -> ExportArtifact:
        """Produce the file.

        Synchronous on purpose: rendering is CPU-bound, not IO-bound, and pretending
        otherwise with `async def` would block the event loop just as thoroughly
        while removing the caller's ability to hand it to a thread. The service
        calls this through ``asyncio.to_thread``.
        """

    # ------------------------------------------------------------------ shared
    def build_file_name(self, dataset: ExportDataset) -> str:
        """`contracts-export-20260730-1432.xlsx`, safe for a Content-Disposition."""
        stamp = dataset.generated_at.strftime("%Y%m%d-%H%M")
        return f"contract-export-{stamp}.{self.extension}"


# =============================================================================
# Registry
# =============================================================================
_EXPORTERS: dict[ExportFormat, type[IExporter]] = {}


def register_exporter(exporter: type[IExporter]) -> type[IExporter]:
    """Class decorator. Registers by the exporter's declared format."""
    _EXPORTERS[exporter.format] = exporter
    return exporter


def get_exporter(export_format: ExportFormat | str) -> IExporter:
    """Resolve a format to its exporter.

    An unregistered format raises rather than silently falling back to XLSX: a
    user who asked for CSV and received a spreadsheet has been given the wrong
    file, and finding out at the point of request beats finding out at the point
    of download.
    """
    from app.core.errors import ValidationError

    resolved = ExportFormat(export_format)
    exporter = _EXPORTERS.get(resolved)
    if exporter is None:
        available = ", ".join(sorted(fmt.value for fmt in _EXPORTERS))
        raise ValidationError(
            f"Export format '{resolved.value}' is not available. Supported: {available or 'none'}.",
            details={"field": "export_format", "supported": sorted(_EXPORTERS)},
        )
    return exporter()


def available_formats() -> list[ExportFormat]:
    """Formats with a registered implementation, for the API to advertise."""
    return sorted(_EXPORTERS, key=lambda fmt: fmt.value)


__all__ = [
    "DEFAULT_ENTITIES",
    "ExportArtifact",
    "ExportColumn",
    "ExportDataset",
    "ExportEntity",
    "ExportSheet",
    "IExporter",
    "available_formats",
    "get_exporter",
    "register_exporter",
]

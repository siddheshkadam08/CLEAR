"""Export module (§23, FR-6).

Importing this package registers every available exporter, so
:func:`~app.export.base.get_exporter` and
:func:`~app.export.base.available_formats` are usable straight away. Without the
concrete import below the registry would be empty until something happened to
import the implementation module, and "supported formats" would depend on import
order rather than on what is installed.
"""

from __future__ import annotations

from app.export.base import (
    DEFAULT_ENTITIES,
    ExportArtifact,
    ExportColumn,
    ExportDataset,
    ExportEntity,
    ExportSheet,
    IExporter,
    available_formats,
    get_exporter,
    register_exporter,
)
from app.export.dataset import ExportDatasetBuilder
from app.export.service import ExportService

# Registers XlsxExporter as a side effect of import. Kept last so the names above
# are bound before the implementation module imports from this package's siblings.
from app.export.xlsx import XlsxExporter

__all__ = [
    "DEFAULT_ENTITIES",
    "ExportArtifact",
    "ExportColumn",
    "ExportDataset",
    "ExportDatasetBuilder",
    "ExportEntity",
    "ExportService",
    "ExportSheet",
    "IExporter",
    "XlsxExporter",
    "available_formats",
    "get_exporter",
    "register_exporter",
]

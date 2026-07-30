"""XLSX exporter.

Written with XlsxWriter straight to an in-memory buffer: an export worker holding
a temp file open across an await is a file handle that leaks when the job is
cancelled, and the workbook is bounded by the export row cap anyway.

Every workbook opens with a summary sheet. That is not decoration - an exported
file is detached from the screen it came from and outlives it, so the scope,
filters, generation time and any withheld rows have to travel inside the file. A
spreadsheet that cannot say what it contains gets misread as everything.
"""

from __future__ import annotations

import io
from datetime import UTC, date, datetime
from typing import Any

import xlsxwriter

from app.core.enums import ExportFormat
from app.export.base import (
    ExportArtifact,
    ExportColumn,
    ExportDataset,
    ExportSheet,
    IExporter,
    register_exporter,
)

#: Excel's own limits. Rows beyond this cannot be written at all, so the cap is
#: enforced with a visible note rather than by dropping rows quietly.
MAX_ROWS_PER_SHEET = 1_048_575

#: Worksheet names: 31 characters, and none of : \ / ? * [ ]
_INVALID_SHEET_CHARS = str.maketrans(dict.fromkeys(":\\/?*[]", "-"))


@register_exporter
class XlsxExporter(IExporter):
    """Renders a dataset as a multi-sheet workbook."""

    format = ExportFormat.XLSX
    extension = "xlsx"
    content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def render(self, dataset: ExportDataset) -> ExportArtifact:
        buffer = io.BytesIO()
        # `in_memory` keeps XlsxWriter from spilling to temp files; `constant_memory`
        # is deliberately off because it forbids writing cells out of row order,
        # which the autofilter and column sizing below rely on.
        workbook = xlsxwriter.Workbook(
            buffer, {"in_memory": True, "default_date_format": "yyyy-mm-dd"}
        )

        try:
            formats = _Formats(workbook)
            self._write_summary(workbook, formats, dataset)
            for sheet in dataset.sheets:
                self._write_sheet(workbook, formats, sheet)
        finally:
            workbook.close()

        return ExportArtifact(
            content=buffer.getvalue(),
            file_name=self.build_file_name(dataset),
            content_type=self.content_type,
            row_count=dataset.total_rows,
        )

    # ---------------------------------------------------------------- summary
    def _write_summary(self, workbook: Any, formats: _Formats, dataset: ExportDataset) -> None:
        sheet = workbook.add_worksheet("Summary")
        sheet.set_column(0, 0, 26)
        sheet.set_column(1, 1, 82)

        sheet.write(0, 0, "Contract Intelligence Platform export", formats.title)
        row = 2

        def pair(label: str, value: Any) -> None:
            nonlocal row
            sheet.write(row, 0, label, formats.label)
            sheet.write(row, 1, value if value not in (None, "") else "—", formats.body)
            row += 1

        pair("Generated", dataset.generated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"))
        pair("Requested by", dataset.generated_by)
        pair("Scope", dataset.scope_label)
        pair("Projects", ", ".join(dataset.project_names) or "—")
        pair("Export ID", str(dataset.export_id))
        row += 1

        sheet.write(row, 0, "Filters applied", formats.heading)
        row += 1
        if dataset.filters:
            for key, value in sorted(dataset.filters.items()):
                rendered = (
                    ", ".join(str(item) for item in value)
                    if isinstance(value, list)
                    else str(value)
                )
                pair(key.replace("_", " ").capitalize(), rendered)
        else:
            pair("", "None — every contract in scope")
        row += 1

        sheet.write(row, 0, "Contents", formats.heading)
        row += 1
        sheet.write(row, 0, "Sheet", formats.header)
        sheet.write(row, 1, "Rows", formats.header)
        row += 1
        for entry in dataset.sheets:
            sheet.write(row, 0, entry.title, formats.body)
            sheet.write_number(row, 1, entry.row_count, formats.body)
            row += 1

        # Withheld and truncated rows are stated here, at the top of the file. A
        # reader who acts on an incomplete export needs to know it is incomplete
        # before they read it, not after.
        notes = [note for entry in dataset.sheets for note in entry.notes]
        if dataset.masked_note:
            notes.append(dataset.masked_note)
        if notes:
            row += 1
            sheet.write(row, 0, "Please note", formats.heading)
            row += 1
            for note in notes:
                sheet.write(row, 1, note, formats.warning)
                row += 1

        sheet.freeze_panes(2, 0)

    # ------------------------------------------------------------------ sheet
    def _write_sheet(self, workbook: Any, formats: _Formats, sheet: ExportSheet) -> None:
        worksheet = workbook.add_worksheet(_safe_sheet_name(sheet.title))
        columns = list(sheet.columns)

        for index, column in enumerate(columns):
            worksheet.set_column(index, index, column.width)
            worksheet.write(0, index, column.label, formats.header)

        truncated = False
        for row_index, row in enumerate(sheet.rows[:MAX_ROWS_PER_SHEET], start=1):
            for column_index, column in enumerate(columns):
                self._write_cell(
                    worksheet, formats, row_index, column_index, column, row.get(column.key)
                )
        if len(sheet.rows) > MAX_ROWS_PER_SHEET:
            truncated = True

        # An empty sheet says so, rather than presenting bare headers that read as
        # "nothing was found" when the entity simply was not requested.
        if not sheet.rows:
            worksheet.write(1, 0, "No rows matched this export.", formats.muted)
        elif columns:
            last_row = min(len(sheet.rows), MAX_ROWS_PER_SHEET)
            worksheet.autofilter(0, 0, last_row, len(columns) - 1)

        if truncated:
            worksheet.write(
                MAX_ROWS_PER_SHEET + 1,
                0,
                f"Truncated at {MAX_ROWS_PER_SHEET:,} rows — narrow the filters to export the rest.",
                formats.warning,
            )

        worksheet.freeze_panes(1, 0)

    @staticmethod
    def _write_cell(
        worksheet: Any,
        formats: _Formats,
        row: int,
        column_index: int,
        column: ExportColumn,
        value: Any,
    ) -> None:
        if value is None or value == "":
            worksheet.write_blank(row, column_index, None, formats.body)
            return

        style = formats.wrapped if column.wrap else formats.body

        if column.kind == "bool":
            worksheet.write_string(row, column_index, "Yes" if value else "No", style)
        elif column.kind in {"number", "integer"} and isinstance(value, (int, float)):
            worksheet.write_number(
                row, column_index, value, formats.integer if column.kind == "integer" else style
            )
        elif column.kind == "date" and isinstance(value, (date, datetime)):
            worksheet.write_datetime(row, column_index, _naive(value), formats.date)
        elif column.kind == "datetime" and isinstance(value, datetime):
            worksheet.write_datetime(row, column_index, _naive(value), formats.datetime)
        else:
            worksheet.write_string(row, column_index, str(value), style)


class _Formats:
    """Cell formats, created once per workbook (XlsxWriter requires that)."""

    def __init__(self, workbook: Any) -> None:
        self.title = workbook.add_format({"bold": True, "font_size": 14})
        self.heading = workbook.add_format({"bold": True, "font_size": 11})
        self.label = workbook.add_format({"bold": True, "valign": "top"})
        self.header = workbook.add_format(
            {"bold": True, "bg_color": "#EAF0FE", "border": 1, "border_color": "#C9D6F5"}
        )
        self.body = workbook.add_format({"valign": "top"})
        self.wrapped = workbook.add_format({"valign": "top", "text_wrap": True})
        self.integer = workbook.add_format({"valign": "top", "num_format": "0"})
        self.date = workbook.add_format({"valign": "top", "num_format": "yyyy-mm-dd"})
        self.datetime = workbook.add_format({"valign": "top", "num_format": "yyyy-mm-dd hh:mm"})
        self.muted = workbook.add_format({"italic": True, "font_color": "#8B94A3"})
        self.warning = workbook.add_format({"font_color": "#B7791F", "text_wrap": True})


def _safe_sheet_name(name: str) -> str:
    """Excel rejects names over 31 chars or containing `: \\ / ? * [ ]`."""
    cleaned = name.translate(_INVALID_SHEET_CHARS).strip("'")
    return (cleaned or "Sheet")[:31]


def _naive(value: date | datetime) -> date | datetime:
    """Drop the timezone: XlsxWriter cannot serialise an aware datetime.

    Values are normalised to UTC first, so the displayed time is UTC rather than
    whatever the worker's local zone happened to be.
    """
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value
    return value


__all__ = ["MAX_ROWS_PER_SHEET", "XlsxExporter"]

"""Report generation: JSON, CSV, Markdown and self-contained HTML."""

from app.evaluation.reports.charts import (
    Series,
    bar_chart,
    delta_bars,
    line_chart,
    reliability_diagram,
)
from app.evaluation.reports.html_report import write_html_reports
from app.evaluation.reports.structured import (
    render_markdown,
    write_csv_reports,
    write_json_report,
    write_markdown_report,
    write_summary_json,
    write_sweep_report,
)

__all__ = [
    "Series",
    "bar_chart",
    "delta_bars",
    "line_chart",
    "reliability_diagram",
    "render_markdown",
    "write_csv_reports",
    "write_html_reports",
    "write_json_report",
    "write_markdown_report",
    "write_summary_json",
    "write_sweep_report",
]

"""Inline SVG charts.

No charting library and no CDN. Three reasons, in order of weight:

* A bank's build agent has no internet access, and a report whose charts are
  blank rectangles on the machine that produced it is not a report.
* A report is an artefact people email, attach to a change record and open two
  years later. One that depends on a CDN still resolving is one that will
  eventually stop rendering.
* Every strict CSP blocks external scripts, and the reports are meant to be
  servable from the admin UI.

The charts are therefore hand-drawn SVG: a few hundred lines, no dependency, and
they render identically in a browser, in an email client and in a PDF export.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

#: A colourblind-safe qualitative palette. Chosen for distinguishability under
#: deuteranopia, which matters for a chart whose whole job is comparison.
PALETTE = (
    "#2563EB",  # blue
    "#059669",  # green
    "#D97706",  # amber
    "#7C3AED",  # violet
    "#DC2626",  # red
    "#0891B2",  # cyan
)

GOOD = "#059669"
BAD = "#DC2626"
NEUTRAL = "#64748B"
GRID = "#E2E8F0"


@dataclass(frozen=True, slots=True)
class Series:
    label: str
    values: list[float]
    colour: str = PALETTE[0]


def bar_chart(
    labels: list[str],
    values: list[float],
    *,
    width: int = 720,
    height: int = 260,
    colours: list[str] | None = None,
    value_format: str = "{:.3f}",
    max_value: float | None = None,
) -> str:
    """Horizontal-axis bar chart with value labels.

    Values are labelled on the bars rather than left to the axis. A reader
    comparing 0.612 with 0.628 cannot do it by eye at this size, and a chart that
    requires squinting at gridlines is decoration.
    """
    if not values:
        return _empty(width, height, "no data")

    pad_left, pad_right, pad_top, pad_bottom = 48, 16, 16, 48
    plot_width = width - pad_left - pad_right
    plot_height = height - pad_top - pad_bottom
    ceiling = max_value if max_value is not None else max(max(values), 1e-9) * 1.15

    slot = plot_width / len(values)
    bar_width = min(slot * 0.62, 64)
    parts: list[str] = [_axes(pad_left, pad_top, plot_width, plot_height, ceiling)]

    for index, (label, value) in enumerate(zip(labels, values, strict=True)):
        colour = colours[index] if colours and index < len(colours) else PALETTE[0]
        bar_height = max(0.0, (value / ceiling) * plot_height) if ceiling else 0.0
        x = pad_left + slot * index + (slot - bar_width) / 2
        y = pad_top + plot_height - bar_height

        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" '
            f'height="{bar_height:.1f}" rx="3" fill="{colour}"/>'
        )
        parts.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{y - 5:.1f}" text-anchor="middle" '
            f'class="v">{escape(value_format.format(value))}</text>'
        )
        parts.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{pad_top + plot_height + 18:.1f}" '
            f'text-anchor="middle" class="l">{escape(str(label))}</text>'
        )

    return _svg(width, height, "".join(parts))


def line_chart(
    x_labels: list[str],
    series: list[Series],
    *,
    width: int = 720,
    height: int = 280,
    y_max: float | None = None,
) -> str:
    """Multi-series line chart, for trends and sweeps."""
    if not series or not x_labels:
        return _empty(width, height, "no data")

    pad_left, pad_right, pad_top, pad_bottom = 48, 110, 16, 44
    plot_width = width - pad_left - pad_right
    plot_height = height - pad_top - pad_bottom

    everything = [value for item in series for value in item.values]
    ceiling = y_max if y_max is not None else max(max(everything, default=1.0), 1e-9) * 1.15

    parts: list[str] = [_axes(pad_left, pad_top, plot_width, plot_height, ceiling)]
    count = max(len(x_labels) - 1, 1)

    for item in series:
        points = [
            (
                pad_left + (plot_width * index / count),
                pad_top + plot_height - (value / ceiling) * plot_height,
            )
            for index, value in enumerate(item.values)
        ]
        if not points:
            continue
        path = " ".join(
            f"{'M' if index == 0 else 'L'}{x:.1f},{y:.1f}" for index, (x, y) in enumerate(points)
        )
        parts.append(f'<path d="{path}" fill="none" stroke="{item.colour}" stroke-width="2"/>')
        parts.extend(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{item.colour}"/>' for x, y in points
        )

    for index, label in enumerate(x_labels):
        x = pad_left + (plot_width * index / count)
        parts.append(
            f'<text x="{x:.1f}" y="{pad_top + plot_height + 18:.1f}" text-anchor="middle" '
            f'class="l">{escape(str(label))}</text>'
        )

    for index, item in enumerate(series):
        y = pad_top + 14 + index * 18
        parts.append(
            f'<rect x="{width - pad_right + 12}" y="{y - 8}" width="10" height="10" rx="2" '
            f'fill="{item.colour}"/>'
        )
        parts.append(
            f'<text x="{width - pad_right + 28}" y="{y + 1}" class="l">{escape(item.label)}</text>'
        )

    return _svg(width, height, "".join(parts))


def reliability_diagram(
    bins: list[dict[str, float]], *, width: int = 460, height: int = 400
) -> str:
    """Confidence against observed accuracy, with the diagonal.

    The single most informative chart in the report. Bars below the diagonal are
    over-confidence - the platform claiming more certainty than it earned - and
    that is the direction that causes harm, so it is coloured as a fault while the
    other direction is not.
    """
    if not bins:
        return _empty(width, height, "no calibration samples")

    pad = 52
    size = min(width, height) - pad * 2
    parts: list[str] = [
        f'<rect x="{pad}" y="{pad}" width="{size}" height="{size}" fill="none" stroke="{GRID}"/>',
        # Perfect calibration.
        f'<line x1="{pad}" y1="{pad + size}" x2="{pad + size}" y2="{pad}" '
        f'stroke="{NEUTRAL}" stroke-width="1.5" stroke-dasharray="4 4"/>',
    ]

    slot = size / max(len(bins), 1)
    for index, item in enumerate(bins):
        count = int(item.get("count", 0))
        if not count:
            continue
        accuracy = float(item.get("observed_accuracy", 0.0))
        confidence = float(item.get("mean_confidence", 0.0))
        bar_height = accuracy * size
        x = pad + slot * index + slot * 0.15
        bar_width = slot * 0.7

        parts.append(
            f'<rect x="{x:.1f}" y="{pad + size - bar_height:.1f}" width="{bar_width:.1f}" '
            f'height="{bar_height:.1f}" rx="2" fill="{PALETTE[0]}" opacity="0.85"/>'
        )
        # The gap to the diagonal, drawn only when over-confident.
        if confidence > accuracy:
            gap_top = pad + size - confidence * size
            parts.append(
                f'<rect x="{x:.1f}" y="{gap_top:.1f}" width="{bar_width:.1f}" '
                f'height="{(confidence - accuracy) * size:.1f}" fill="{BAD}" opacity="0.25"/>'
            )

    parts.append(
        f'<text x="{pad + size / 2:.0f}" y="{pad + size + 32}" text-anchor="middle" '
        f'class="l">confidence</text>'
    )
    parts.append(
        f'<text x="18" y="{pad + size / 2:.0f}" class="l" '
        f'transform="rotate(-90 18 {pad + size / 2:.0f})" text-anchor="middle">'
        "observed accuracy</text>"
    )
    return _svg(width, height, "".join(parts))


def delta_bars(
    labels: list[str],
    deltas: list[float],
    *,
    width: int = 720,
    height: int = 260,
    lower_is_better: set[str] | None = None,
) -> str:
    """Signed bars around a zero line, for regression and ablation reports.

    Colour follows *goodness*, not sign: a fall in hallucination rate is green
    even though the bar points down. Colouring by sign would make the most
    important chart in the regression report actively misleading.
    """
    if not deltas:
        return _empty(width, height, "no data")

    lower = lower_is_better or set()
    pad_left, pad_right, pad_top, pad_bottom = 24, 16, 20, 56
    plot_width = width - pad_left - pad_right
    plot_height = height - pad_top - pad_bottom
    extent = max((abs(value) for value in deltas), default=1e-9) * 1.2 or 1e-9
    midline = pad_top + plot_height / 2

    parts: list[str] = [
        f'<line x1="{pad_left}" y1="{midline:.1f}" x2="{pad_left + plot_width}" '
        f'y2="{midline:.1f}" stroke="{NEUTRAL}" stroke-width="1"/>'
    ]

    slot = plot_width / len(deltas)
    bar_width = min(slot * 0.6, 48)

    for index, (label, delta) in enumerate(zip(labels, deltas, strict=True)):
        improved = (delta < 0) if label in lower else (delta > 0)
        colour = GOOD if improved else (BAD if delta else NEUTRAL)
        magnitude = (abs(delta) / extent) * (plot_height / 2)
        x = pad_left + slot * index + (slot - bar_width) / 2
        y = midline - magnitude if delta > 0 else midline

        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" '
            f'height="{magnitude:.1f}" rx="2" fill="{colour}"/>'
        )
        parts.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{pad_top + plot_height + 16:.1f}" '
            f'text-anchor="middle" class="l">{escape(label)}</text>'
        )
        parts.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{pad_top + plot_height + 30:.1f}" '
            f'text-anchor="middle" class="v" fill="{colour}">{delta:+.3f}</text>'
        )

    return _svg(width, height, "".join(parts))


# =============================================================================
# Primitives
# =============================================================================
def _axes(
    pad_left: int, pad_top: int, plot_width: float, plot_height: float, ceiling: float
) -> str:
    """Four gridlines and their labels. Enough to read a value, not more."""
    parts: list[str] = []
    for step in range(5):
        fraction = step / 4
        y = pad_top + plot_height - plot_height * fraction
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{pad_left + plot_width}" y2="{y:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_left - 8}" y="{y + 4:.1f}" text-anchor="end" class="l">'
            f"{ceiling * fraction:.2f}</text>"
        )
    return "".join(parts)


def _svg(width: int, height: int, body: str) -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" xmlns="http://www.w3.org/2000/svg" class="chart">'
        f"<style>.l{{font:11px system-ui,sans-serif;fill:#64748B}}"
        f".v{{font:600 11px system-ui,sans-serif;fill:#0F172A}}</style>"
        f"{body}</svg>"
    )


def _empty(width: int, height: int, message: str) -> str:
    return _svg(
        width,
        height,
        f'<text x="{width // 2}" y="{height // 2}" text-anchor="middle" class="l">'
        f"{escape(message)}</text>",
    )


__all__ = [
    "BAD",
    "GOOD",
    "GRID",
    "NEUTRAL",
    "PALETTE",
    "Series",
    "bar_chart",
    "delta_bars",
    "line_chart",
    "reliability_diagram",
]

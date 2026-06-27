"""Operator Portal — server-rendered SVG chart generators (Sprint P3).

Pure functions: data in, a complete ``<svg>`` string out. No data reading, no
aiohttp. The dashboard fragment routes call a telemetry reader, then pass the
result here and wrap the returned SVG in an HTML card.

Charting is LOCKED to server-rendered SVG — no Chart.js, no Canvas, no client
JS. HTMX swaps these SVG strings exactly as it swaps the P2 HTML fragments.

A4 — SVG presentation attributes (``fill``/``stroke``) do NOT resolve CSS
``var()``. The dark-theme palette is therefore mirrored as Python constants
below and used as literal hex in the markup. Keep these in sync with the
portal stylesheet's ``:root`` values by intent, not by reference.

NO SILENT DEGRADATION. Empty or all-zero data renders an explicit "No data"
SVG, never a blank or malformed chart.
"""

from __future__ import annotations

import html

# Dark-theme palette (A4) — literal hex mirrors of the portal :root tokens.
ACCENT = "#60a5fa"
GREEN = "#4ade80"
YELLOW = "#facc15"
RED = "#f87171"
TEXT = "#e2e8f0"
BG = "#1e293b"
MUTED = "#94a3b8"

# Internal-only helpers (not part of the A4 contract): a subtle grid line and a
# categorical cycle for series that don't carry an explicit per-item color.
_GRID = "#334155"
_PALETTE = (ACCENT, GREEN, YELLOW, RED, "#a78bfa", "#fb923c", "#34d399", "#f472b6")

_FONT = "sans-serif"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    """Escape a scalar for safe interpolation into SVG text/attributes."""
    return html.escape("" if value is None else str(value), quote=True)


def _fmt(value) -> str:
    """Format a number: integer when whole, one decimal otherwise."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return _esc(value)
    if f == int(f):
        return str(int(f))
    return f"{f:.1f}"


def _open(width: int, height: int) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" font-family="{_FONT}">'
    )


def _bg(width: int, height: int) -> str:
    return f'<rect width="{width}" height="{height}" fill="{BG}"/>'


def _title(title: str, width: int) -> str:
    if not title:
        return ""
    return (
        f'<text x="10" y="20" fill="{TEXT}" font-size="13" '
        f'font-weight="600">{_esc(title)}</text>'
    )


def _no_data(width: int, height: int, title: str = "") -> str:
    parts = [_open(width, height), _bg(width, height), _title(title, width)]
    parts.append(
        f'<text x="{width / 2:.0f}" y="{height / 2 + 4:.0f}" fill="{MUTED}" '
        f'font-size="13" text-anchor="middle">No data</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _values(items) -> list:
    return [float(it.get("value", 0) or 0) for it in items]


# ---------------------------------------------------------------------------
# 4 — bar chart (vertical | horizontal)
# ---------------------------------------------------------------------------


def bar_chart_svg(
    items: list,
    title: str,
    width: int = 400,
    height: int = 250,
    orientation: str = "vertical",
) -> str:
    """Render a bar chart. ``items`` = ``[{label, value, color?}]``.

    Bars are proportional to the max value; labels sit along the axis and the
    value is printed at the end of each bar. Empty or all-zero ``items`` renders
    an explicit "No data" chart.
    """
    items = [it for it in (items or []) if it is not None]
    values = _values(items)
    if not items or max(values, default=0) <= 0:
        return _no_data(width, height, title)
    max_val = max(values)

    if orientation == "horizontal":
        return _bar_horizontal(items, values, max_val, title, width, height)
    return _bar_vertical(items, values, max_val, title, width, height)


def _bar_vertical(items, values, max_val, title, width, height) -> str:
    pad_top, pad_bottom, pad_x = 34, 28, 10
    plot_w = width - 2 * pad_x
    plot_h = height - pad_top - pad_bottom
    base_y = pad_top + plot_h
    n = len(items)
    slot = plot_w / n
    bar_w = slot * 0.6

    parts = [_open(width, height), _bg(width, height), _title(title, width)]
    for i, it in enumerate(items):
        v = values[i]
        color = it.get("color") or _PALETTE[i % len(_PALETTE)]
        bh = (v / max_val) * plot_h
        x = pad_x + i * slot + (slot - bar_w) / 2
        y = base_y - bh
        cx = x + bar_w / 2
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{bh:.1f}" fill="{_esc(color)}" rx="2"/>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{y - 4:.1f}" fill="{TEXT}" font-size="11" '
            f'text-anchor="middle">{_esc(_fmt(v))}</text>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{base_y + 15:.1f}" fill="{MUTED}" '
            f'font-size="11" text-anchor="middle">{_esc(it.get("label", ""))}</text>'
        )
    parts.append(
        f'<line x1="{pad_x}" y1="{base_y:.1f}" x2="{width - pad_x}" '
        f'y2="{base_y:.1f}" stroke="{MUTED}" stroke-width="1"/>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _bar_horizontal(items, values, max_val, title, width, height) -> str:
    pad_top, pad_bottom, pad_left, pad_right = 34, 10, 92, 44
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    n = len(items)
    slot = plot_h / n
    bar_h = min(slot * 0.6, 26)

    parts = [_open(width, height), _bg(width, height), _title(title, width)]
    for i, it in enumerate(items):
        v = values[i]
        color = it.get("color") or _PALETTE[i % len(_PALETTE)]
        bw = (v / max_val) * plot_w
        y = pad_top + i * slot + (slot - bar_h) / 2
        cy = y + bar_h / 2
        parts.append(
            f'<rect x="{pad_left}" y="{y:.1f}" width="{bw:.1f}" '
            f'height="{bar_h:.1f}" fill="{_esc(color)}" rx="2"/>'
        )
        parts.append(
            f'<text x="{pad_left - 6}" y="{cy + 4:.1f}" fill="{MUTED}" '
            f'font-size="11" text-anchor="end">{_esc(it.get("label", ""))}</text>'
        )
        parts.append(
            f'<text x="{pad_left + bw + 4:.1f}" y="{cy + 4:.1f}" fill="{TEXT}" '
            f'font-size="11">{_esc(_fmt(v))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 5 — line chart
# ---------------------------------------------------------------------------


def _label_indices(n: int) -> list:
    """Pick x-axis label positions: all when few, ~6 evenly spaced when many."""
    if n <= 0:
        return []
    if n <= 7:
        return list(range(n))
    step = max(1, n // 6)
    idxs = list(range(0, n, step))
    if (n - 1) not in idxs:
        idxs.append(n - 1)
    return idxs


def line_chart_svg(
    series: list, title: str, width: int = 500, height: int = 200
) -> str:
    """Render a line chart. ``series`` = ``[{date, value}]`` pre-sorted by date.

    A ``<polyline>`` with horizontal grid lines, value labels on the left axis,
    and date labels along the bottom. Empty ``series`` renders "No data".
    """
    series = [p for p in (series or []) if p is not None]
    if not series:
        return _no_data(width, height, title)
    values = _values(series)
    max_val = max(values) or 1.0

    pad_top, pad_bottom, pad_left, pad_right = 34, 30, 46, 12
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    base_y = pad_top + plot_h
    n = len(series)

    def xpos(i: int) -> float:
        if n == 1:
            return pad_left + plot_w / 2
        return pad_left + (i / (n - 1)) * plot_w

    def ypos(v: float) -> float:
        return base_y - (v / max_val) * plot_h

    parts = [_open(width, height), _bg(width, height), _title(title, width)]

    # Horizontal grid + left-axis value labels (5 divisions).
    for g in range(5):
        gy = pad_top + (g / 4) * plot_h
        parts.append(
            f'<line x1="{pad_left}" y1="{gy:.1f}" x2="{width - pad_right}" '
            f'y2="{gy:.1f}" stroke="{_GRID}" stroke-width="1"/>'
        )
        gval = max_val * (1 - g / 4)
        parts.append(
            f'<text x="{pad_left - 6}" y="{gy + 3:.1f}" fill="{MUTED}" '
            f'font-size="10" text-anchor="end">{_esc(_fmt(gval))}</text>'
        )

    points = " ".join(f"{xpos(i):.1f},{ypos(values[i]):.1f}" for i in range(n))
    parts.append(
        f'<polyline points="{points}" fill="none" stroke="{ACCENT}" '
        f'stroke-width="2"/>'
    )
    for i in range(n):
        parts.append(
            f'<circle cx="{xpos(i):.1f}" cy="{ypos(values[i]):.1f}" r="2.5" '
            f'fill="{ACCENT}"/>'
        )
    for i in _label_indices(n):
        parts.append(
            f'<text x="{xpos(i):.1f}" y="{base_y + 14:.1f}" fill="{MUTED}" '
            f'font-size="10" text-anchor="middle">{_esc(series[i].get("date", ""))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 6 — micro stacked bar (KPI strip tier split)
# ---------------------------------------------------------------------------


def micro_bar_svg(segments: list, width: int = 120, height: int = 16) -> str:
    """Render a thin horizontal stacked bar. ``segments`` =
    ``[{label, value, color}]``.

    Segment widths are proportional to the segment total. With no positive
    total the track renders alone (the KPI card prints its own "No data" text);
    the result is always a valid ``<svg>``.
    """
    segments = [s for s in (segments or []) if s is not None]
    total = sum(float(s.get("value", 0) or 0) for s in segments)

    parts = [_open(width, height)]
    parts.append(f'<rect width="{width}" height="{height}" rx="3" fill="{BG}"/>')
    if total > 0:
        x = 0.0
        for i, s in enumerate(segments):
            v = float(s.get("value", 0) or 0)
            if v <= 0:
                continue
            w = (v / total) * width
            color = s.get("color") or _PALETTE[i % len(_PALETTE)]
            parts.append(
                f'<rect x="{x:.2f}" y="0" width="{w:.2f}" height="{height}" '
                f'fill="{_esc(color)}"><title>{_esc(s.get("label", ""))}: '
                f'{_esc(_fmt(v))}</title></rect>'
            )
            x += w
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 7 — status dot
# ---------------------------------------------------------------------------


def status_dot_svg(status: str, size: int = 12) -> str:
    """Render a status dot: green for "ok", red for any other status."""
    color = GREEN if status == "ok" else RED
    r = size / 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" role="img"><title>{_esc(status)}</title>'
        f'<circle cx="{r:.1f}" cy="{r:.1f}" r="{r - 1:.1f}" fill="{color}"/></svg>'
    )

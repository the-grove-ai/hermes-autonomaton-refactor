"""Operator Portal — telemetry dashboard fragment routes (Sprint P3).

The dashboard is a new view in the existing HTMX portal. ``overview`` returns
the dashboard layout that HTMX swaps into ``#center-panel``; each KPI card,
chart, and the connector sidebar is its own ``/portal/fragments/dashboard/*``
route so the operator can refresh any tile independently. These handlers read
telemetry via the Phase-1 readers and render with the Phase-1 SVG generators —
no charting JS, server-rendered SVG only (the locked P3 approach).

NO SILENT DEGRADATION. An empty or absent data source renders an explicit
"No data" card or "No data" SVG; a chart is never silently omitted. The reader
layer logs a loud WARNING on a malformed record and skips it.
"""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from grove.api import svg_charts as sc
from grove.api.fragments import _esc, _html_fragment
from grove.api.telemetry_readers import (
    read_connector_health,
    read_flywheel_activity,
    read_intent_summary,
)
from grove.intent_store import get_store
from hermes_constants import get_hermes_home

# Cost gradient for the tier bars: T0 pattern-cache (cheap, green) escalating to
# T3 apex cognition (expensive, red). Mirrors the Pattern v1.3 tier hierarchy.
_TIER_COLORS = {
    "T0": sc.GREEN,
    "T1": sc.ACCENT,
    "T2": sc.YELLOW,
    "T3": sc.RED,
}

# Connector-status badge classes (reuse the portal's zone badge styles).
_STATUS_BADGE = {
    "ok": "badge-green",
    "reauth": "badge-yellow",
    "unreachable": "badge-red",
}

_DASH = "/portal/fragments/dashboard"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _days(request: web.Request) -> int:
    """Resolve the ``?days`` lookback window. Defaults to 7; the toggle sends
    7 or 30. An unparseable or non-positive value falls back to 7 rather than
    erroring the tile (the window is presentational, not load-bearing)."""
    raw = request.query.get("days", "7")
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return 7
    if days <= 0:
        return 7
    return min(days, 365)


def _ledger_dir() -> Path:
    return get_hermes_home() / ".kaizen_ledger"


def _proposals_path() -> Path:
    return get_hermes_home() / "memory_proposals.jsonl"


def _kpi_card(label: str, value_html: str, sub: str = "") -> str:
    sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    return (
        f'<div class="kpi-card">'
        f'<div class="kpi-label">{_esc(label)}</div>'
        f'<div class="kpi-value">{value_html}</div>'
        f"{sub_html}"
        f"</div>"
    )


def _chart_card(svg: str) -> str:
    return f'<div class="chart-card">{svg}</div>'


# ---------------------------------------------------------------------------
# KPI fragments
# ---------------------------------------------------------------------------


async def handle_kpi_turns(request: web.Request) -> web.Response:
    days = _days(request)
    summary = read_intent_summary(get_store(), days=days)
    total = summary["total_turns"]
    return _html_fragment(
        _kpi_card("Turns", _esc(total), sub=f"last {days}d")
    )


async def handle_kpi_tier_split(request: web.Request) -> web.Response:
    days = _days(request)
    summary = read_intent_summary(get_store(), days=days)
    tier_counts = summary["tier_counts"]
    total = sum(tier_counts.values())
    if total <= 0:
        return _html_fragment(
            _kpi_card("Tier split", '<span class="kpi-nodata">No data</span>',
                      sub=f"last {days}d")
        )
    segments = [
        {"label": tier, "value": tier_counts[tier], "color": _TIER_COLORS[tier]}
        for tier in ("T0", "T1", "T2", "T3")
    ]
    bar = sc.micro_bar_svg(segments)
    legend = " ".join(
        f'<span class="tier-legend"><i style="background:{_TIER_COLORS[t]}"></i>'
        f"{t} {tier_counts[t]}</span>"
        for t in ("T0", "T1", "T2", "T3")
        if tier_counts[t]
    )
    value_html = f'<div class="kpi-microbar">{bar}</div><div class="tier-legends">{legend}</div>'
    return _html_fragment(_kpi_card("Tier split", value_html, sub=f"last {days}d"))


async def handle_kpi_connectors(request: web.Request) -> web.Response:
    # Connector health is live, point-in-time process state (no time window).
    health = read_connector_health()
    total = len(health)
    if total == 0:
        return _html_fragment(
            _kpi_card("Connectors", '<span class="kpi-nodata">No data</span>',
                      sub="MCP servers")
        )
    healthy = sum(1 for h in health if h["status"] == "ok")
    dots = "".join(sc.status_dot_svg(h["status"]) for h in health)
    value_html = f'{healthy}/{total} <span class="kpi-dots">{dots}</span>'
    return _html_fragment(_kpi_card("Connectors", value_html, sub="healthy / total"))


async def handle_kpi_proposals(request: web.Request) -> web.Response:
    days = _days(request)
    flywheel = read_flywheel_activity(_ledger_dir(), _proposals_path(), days=days)
    status_counts = flywheel["proposal_status_counts"]
    total = sum(status_counts.values())
    if total == 0:
        return _html_fragment(
            _kpi_card("Proposals", '<span class="kpi-nodata">No data</span>',
                      sub="memory queue")
        )
    pending = status_counts.get("pending", 0)
    return _html_fragment(
        _kpi_card("Proposals", _esc(pending), sub=f"pending of {total}")
    )


# ---------------------------------------------------------------------------
# Chart fragments
# ---------------------------------------------------------------------------


async def handle_chart_tier_distribution(request: web.Request) -> web.Response:
    days = _days(request)
    summary = read_intent_summary(get_store(), days=days)
    tier_counts = summary["tier_counts"]
    items = [
        {"label": tier, "value": tier_counts[tier], "color": _TIER_COLORS[tier]}
        for tier in ("T0", "T1", "T2", "T3")
    ]
    svg = sc.bar_chart_svg(items, f"Tier distribution · {days}d", width=440, height=240)
    return _html_fragment(_chart_card(svg))


async def handle_chart_system_effort(request: web.Request) -> web.Response:
    days = _days(request)
    summary = read_intent_summary(get_store(), days=days)
    # Effort proxy = API calls per day (cost descoped — cost_usd not populated).
    series = [
        {"date": row["date"][5:], "value": row["api_calls"]}
        for row in summary["daily_effort"]
    ]
    svg = sc.line_chart_svg(series, f"API calls / day · {days}d", width=520, height=240)
    return _html_fragment(_chart_card(svg))


async def handle_chart_intent_taxonomy(request: web.Request) -> web.Response:
    days = _days(request)
    summary = read_intent_summary(get_store(), days=days)
    counts = summary["intent_class_counts"]
    items = [{"label": name, "value": value} for name, value in counts.items()]
    # Horizontal — class names are long and there can be up to 11 of them.
    svg = sc.bar_chart_svg(
        items, f"Intent taxonomy · {days}d", width=440,
        height=max(240, 30 * len(items) + 60), orientation="horizontal",
    )
    return _html_fragment(_chart_card(svg))


async def handle_chart_flywheel_activity(request: web.Request) -> web.Response:
    days = _days(request)
    flywheel = read_flywheel_activity(_ledger_dir(), _proposals_path(), days=days)
    counts = flywheel["event_type_counts"]
    items = [
        {"label": name, "value": value}
        for name, value in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
    svg = sc.bar_chart_svg(
        items, f"Flywheel activity · {days}d", width=440,
        height=max(240, 30 * len(items) + 60), orientation="horizontal",
    )
    return _html_fragment(_chart_card(svg))


# ---------------------------------------------------------------------------
# Sidebar fragment — connector health
# ---------------------------------------------------------------------------


async def handle_sidebar_connector_health(request: web.Request) -> web.Response:
    health = read_connector_health()
    if not health:
        markup = (
            '<div class="sidebar-section"><h3>Connector health</h3>'
            '<p class="placeholder">No MCP servers configured.</p></div>'
        )
        return _html_fragment(markup)
    rows = "".join(
        f'<li class="connector-row">{sc.status_dot_svg(h["status"])}'
        f'<span class="connector-name">{_esc(h["name"])}</span>'
        f'<span class="badge {_STATUS_BADGE.get(h["status"], "badge-red")}">'
        f'{_esc(h["status"])}</span></li>'
        for h in health
    )
    markup = (
        '<div class="sidebar-section"><h3>Connector health</h3>'
        f'<ul class="connector-list">{rows}</ul></div>'
    )
    return _html_fragment(markup)


# ---------------------------------------------------------------------------
# Overview — the dashboard page itself
# ---------------------------------------------------------------------------


def _slot(route: str, days: int, cls: str, loading: str) -> str:
    """A lazy tile: an empty box that hx-gets its fragment on load."""
    return (
        f'<div class="{cls}" hx-get="{_DASH}/{route}?days={days}" '
        f'hx-trigger="load"><span class="spinner">{_esc(loading)}</span></div>'
    )


async def handle_dashboard_overview(request: web.Request) -> web.Response:
    days = _days(request)
    active7 = "active" if days != 30 else ""
    active30 = "active" if days == 30 else ""

    toggle = (
        '<div class="time-toggle" role="group" aria-label="Time window">'
        f'<a class="{active7}" hx-get="{_DASH}/overview?days=7" '
        f'hx-target="#center-panel" hx-push-url="true">7 days</a>'
        f'<a class="{active30}" hx-get="{_DASH}/overview?days=30" '
        f'hx-target="#center-panel" hx-push-url="true">30 days</a>'
        "</div>"
    )

    kpi_strip = (
        '<div class="kpi-strip">'
        + _slot("kpi-turns", days, "kpi-slot", "…")
        + _slot("kpi-tier-split", days, "kpi-slot", "…")
        + _slot("kpi-connectors", days, "kpi-slot", "…")
        + _slot("kpi-proposals", days, "kpi-slot", "…")
        + "</div>"
    )

    chart_grid = (
        '<div class="chart-grid">'
        + _slot("chart-tier-distribution", days, "chart-slot", "loading chart…")
        + _slot("chart-system-effort", days, "chart-slot", "loading chart…")
        + _slot("chart-intent-taxonomy", days, "chart-slot", "loading chart…")
        + _slot("chart-flywheel-activity", days, "chart-slot", "loading chart…")
        + "</div>"
    )

    # OOB-swap the right panel to load the connector-health sidebar, mirroring
    # the P2 cellar-detail pattern (replace #right-panel wholesale, child fires
    # its own hx-get on load).
    oob_sidebar = (
        '<aside id="right-panel" class="right-panel" hx-swap-oob="true" '
        'aria-label="Context">'
        f'<div hx-get="{_DASH}/sidebar-connector-health" hx-trigger="load">'
        '<p class="placeholder">Loading connector health…</p></div>'
        "</aside>"
    )

    markup = (
        '<div id="dashboard" class="dashboard">'
        '<div class="dash-header"><h1>Telemetry Dashboard</h1>'
        f"{toggle}</div>"
        f"{kpi_strip}"
        f"{chart_grid}"
        "</div>"
        f"{oob_sidebar}"
    )
    return _html_fragment(markup)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_dashboard_routes(app: web.Application) -> None:
    """Register the 10 ``/portal/fragments/dashboard/*`` routes.

    Called from ``api_server.connect()`` immediately after
    ``register_fragment_routes`` so the dashboard shares the portal's auth
    middleware and static mount.
    """
    add = app.router.add_get
    # Overview (the dashboard page swapped into #center-panel).
    add(f"{_DASH}/overview", handle_dashboard_overview)
    # KPI cards.
    add(f"{_DASH}/kpi-turns", handle_kpi_turns)
    add(f"{_DASH}/kpi-tier-split", handle_kpi_tier_split)
    add(f"{_DASH}/kpi-connectors", handle_kpi_connectors)
    add(f"{_DASH}/kpi-proposals", handle_kpi_proposals)
    # Charts.
    add(f"{_DASH}/chart-tier-distribution", handle_chart_tier_distribution)
    add(f"{_DASH}/chart-system-effort", handle_chart_system_effort)
    add(f"{_DASH}/chart-intent-taxonomy", handle_chart_intent_taxonomy)
    add(f"{_DASH}/chart-flywheel-activity", handle_chart_flywheel_activity)
    # Sidebar.
    add(f"{_DASH}/sidebar-connector-health", handle_sidebar_connector_health)

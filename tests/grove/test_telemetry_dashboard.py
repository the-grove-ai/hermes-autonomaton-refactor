"""Sprint P3 (portal-dashboard-v1) — telemetry dashboard unit tests.

Phase 1 covers the pure layers in isolation: the SVG generators
(``grove.api.svg_charts``) against known inputs, and the telemetry readers
(``grove.api.telemetry_readers``) against in-memory/temp-file fixtures. Phase 3
adds route-level integration tests over a live aiohttp TestClient.

NO SILENT DEGRADATION is asserted directly: empty inputs must produce an
explicit "No data" SVG, and a malformed ledger line must be skipped (the
surrounding good records still counted), never crash the reader or the route.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from grove.api import svg_charts as sc
from grove.api import telemetry_readers as tr
from grove.api.dashboard_fragments import register_dashboard_routes


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

# Fixed "now" so the lookback window is deterministic across runs.
NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class FakeRecord:
    """Minimal stand-in for IntentRecord — only the fields the reader reads."""

    timestamp: str
    intent_class: str = "conversation"
    tier_selected: Optional[str] = None
    api_calls: int = 0
    duration_ms: float = 0.0


class FakeStore:
    """Object with a ``records()`` iterator, like IntentStore."""

    def __init__(self, records):
        self._records = list(records)

    def records(self):
        yield from self._records


# ---------------------------------------------------------------------------
# SVG generators
# ---------------------------------------------------------------------------


def test_bar_chart_vertical_has_rects_and_labels():
    items = [
        {"label": "T0", "value": 2},
        {"label": "T1", "value": 540},
        {"label": "T2", "value": 140},
        {"label": "T3", "value": 71},
    ]
    out = sc.bar_chart_svg(items, "Tier distribution")
    assert out.startswith("<svg")
    assert out.rstrip().endswith("</svg>")
    assert out.count("<rect") >= 4  # one per bar (+bg is also a rect)
    assert "Tier distribution" in out
    assert ">T1<" in out  # axis label
    assert ">540<" in out  # value label
    assert sc.ACCENT in out  # palette applied as literal hex (A4)


def test_bar_chart_horizontal_orientation():
    items = [{"label": "analysis", "value": 20}, {"label": "planning", "value": 30}]
    out = sc.bar_chart_svg(items, "Intent", orientation="horizontal")
    assert out.startswith("<svg")
    assert "<rect" in out
    assert ">analysis<" in out and ">30<" in out


def test_bar_chart_empty_renders_no_data():
    out = sc.bar_chart_svg([], "Empty")
    assert out.startswith("<svg")
    assert "No data" in out


def test_bar_chart_all_zero_renders_no_data():
    out = sc.bar_chart_svg([{"label": "a", "value": 0}], "Zeros")
    assert "No data" in out


def test_line_chart_has_polyline_and_grid():
    series = [
        {"date": "06-24", "value": 10},
        {"date": "06-25", "value": 25},
        {"date": "06-26", "value": 5},
    ]
    out = sc.line_chart_svg(series, "Effort")
    assert out.startswith("<svg")
    assert "<polyline" in out
    assert "<line" in out  # grid lines
    assert ">06-24<" in out  # date axis label
    assert "Effort" in out


def test_line_chart_single_point():
    out = sc.line_chart_svg([{"date": "06-27", "value": 7}], "One")
    assert "<polyline" in out
    assert ">06-27<" in out


def test_line_chart_empty_renders_no_data():
    out = sc.line_chart_svg([], "Empty")
    assert out.startswith("<svg")
    assert "No data" in out


def test_micro_bar_stacks_segments():
    segments = [
        {"label": "T1", "value": 6, "color": sc.GREEN},
        {"label": "T2", "value": 4, "color": sc.YELLOW},
    ]
    out = sc.micro_bar_svg(segments)
    assert out.startswith("<svg")
    assert out.count("<rect") >= 3  # track + 2 segments
    assert sc.GREEN in out and sc.YELLOW in out
    assert "T1" in out  # <title> tooltip


def test_micro_bar_empty_is_valid_svg_track_only():
    out = sc.micro_bar_svg([])
    assert out.startswith("<svg")
    assert "<rect" in out  # the track still renders


def test_status_dot_ok_is_green():
    out = sc.status_dot_svg("ok")
    assert out.startswith("<svg")
    assert sc.GREEN in out
    assert sc.RED not in out


def test_status_dot_failure_is_red():
    for status in ("reauth", "unreachable"):
        out = sc.status_dot_svg(status)
        assert sc.RED in out
        assert sc.GREEN not in out


# ---------------------------------------------------------------------------
# Reader 1 — read_intent_summary
# ---------------------------------------------------------------------------


def test_read_intent_summary_counts_tiers_and_aggregates_daily():
    records = [
        FakeRecord("2026-06-26T10:00:00+00:00", "conversation", "T1", 2, 100.0),
        FakeRecord("2026-06-26T14:00:00+00:00", "analysis", "T2", 3, 250.5),
        FakeRecord("2026-06-27T09:00:00+00:00", "conversation", "T1", 1, 50.0),
        FakeRecord("2026-06-27T11:00:00+00:00", "research", "T3", 5, 900.0),
        # Outside the 7-day window — must be excluded.
        FakeRecord("2026-06-01T00:00:00+00:00", "conversation", "T1", 99, 9999.0),
        # No tier (finalization-style record) — counted in total, not in tiers.
        FakeRecord("2026-06-27T11:30:00+00:00", "conversation", None, 0, 0.0),
    ]
    summary = tr.read_intent_summary(FakeStore(records), days=7, now=NOW)

    assert summary["total_turns"] == 5  # the 06-01 record excluded
    assert summary["tier_counts"] == {"T0": 0, "T1": 2, "T2": 1, "T3": 1}
    # Three in-window conversation records: 06-26 10:00, 06-27 09:00, and the
    # tier-less 06-27 11:30 record (still classified conversation).
    assert summary["intent_class_counts"]["conversation"] == 3
    assert summary["intent_class_counts"]["analysis"] == 1

    daily = {d["date"]: d for d in summary["daily_effort"]}
    assert set(daily) == {"2026-06-26", "2026-06-27"}
    assert daily["2026-06-26"]["api_calls"] == 5  # 2 + 3
    assert daily["2026-06-26"]["duration_ms"] == 350.5  # 100 + 250.5
    assert daily["2026-06-27"]["api_calls"] == 6  # 1 + 5 + 0
    # daily_effort is sorted ascending by date
    assert [d["date"] for d in summary["daily_effort"]] == [
        "2026-06-26",
        "2026-06-27",
    ]


def test_read_intent_summary_top10_plus_other():
    # 12 distinct classes, 1 record each → top 10 kept, 2 folded into "other".
    records = [
        FakeRecord("2026-06-27T00:00:00+00:00", f"class_{i:02d}", "T1", 1, 1.0)
        for i in range(12)
    ]
    summary = tr.read_intent_summary(FakeStore(records), days=7, now=NOW)
    counts = summary["intent_class_counts"]
    assert len(counts) == 11  # 10 named + "other"
    assert "other" in counts
    assert counts["other"] == 2


def test_read_intent_summary_empty_store():
    summary = tr.read_intent_summary(FakeStore([]), days=7, now=NOW)
    assert summary["total_turns"] == 0
    assert summary["tier_counts"] == {"T0": 0, "T1": 0, "T2": 0, "T3": 0}
    assert summary["intent_class_counts"] == {}
    assert summary["daily_effort"] == []


# ---------------------------------------------------------------------------
# Reader 2 — read_flywheel_activity
# ---------------------------------------------------------------------------


def _write_jsonl(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_read_flywheel_activity_counts_events_and_dispositions(tmp_path):
    ledger = tmp_path / ".kaizen_ledger"
    ledger.mkdir()
    _write_jsonl(
        ledger / "sess_a.jsonl",
        [
            '{"event_type": "tool_selection", "timestamp": "2026-06-26T10:00:00+00:00"}',
            '{"event_type": "andon_halt", "timestamp": "2026-06-26T10:01:00+00:00"}',
            '{"event_type": "andon_disposition", "disposition": "always", "timestamp": "2026-06-26T10:02:00+00:00"}',
        ],
    )
    _write_jsonl(
        ledger / "sess_b.jsonl",
        [
            '{"event_type": "andon_disposition", "disposition": "deny", "timestamp": "2026-06-27T09:00:00+00:00"}',
            # Outside the window — excluded from counts.
            '{"event_type": "tool_selection", "timestamp": "2026-05-01T00:00:00+00:00"}',
        ],
    )
    proposals = tmp_path / "memory_proposals.jsonl"
    _write_jsonl(
        proposals,
        [
            '{"status": "pending"}',
            '{"status": "pending"}',
            '{"status": "approved"}',
        ],
    )

    out = tr.read_flywheel_activity(ledger, proposals, days=7, now=NOW)
    assert out["event_type_counts"]["tool_selection"] == 1  # the 05-01 one excluded
    assert out["event_type_counts"]["andon_halt"] == 1
    assert out["event_type_counts"]["andon_disposition"] == 2
    assert out["disposition_counts"] == {"always": 1, "deny": 1}
    assert out["proposal_status_counts"] == {"pending": 2, "approved": 1}


def test_read_flywheel_activity_skips_malformed_lines(tmp_path):
    ledger = tmp_path / ".kaizen_ledger"
    ledger.mkdir()
    _write_jsonl(
        ledger / "sess.jsonl",
        [
            '{"event_type": "tool_selection", "timestamp": "2026-06-27T00:00:00+00:00"}',
            "{ this is not valid json",  # malformed — skipped + logged
            '{"event_type": "final_response", "timestamp": "2026-06-27T00:05:00+00:00"}',
        ],
    )
    proposals = tmp_path / "memory_proposals.jsonl"
    proposals.write_text("", encoding="utf-8")

    out = tr.read_flywheel_activity(ledger, proposals, days=7, now=NOW)
    # The two well-formed records are counted; the malformed line did not crash.
    assert out["event_type_counts"] == {"tool_selection": 1, "final_response": 1}


def test_read_flywheel_activity_absent_sources_return_empty(tmp_path):
    out = tr.read_flywheel_activity(
        tmp_path / "nope", tmp_path / "missing.jsonl", days=7, now=NOW
    )
    assert out == {
        "disposition_counts": {},
        "event_type_counts": {},
        "proposal_status_counts": {},
    }


# ---------------------------------------------------------------------------
# Reader 3 — read_connector_health
# ---------------------------------------------------------------------------


def test_read_connector_health_merges_status_and_breaker():
    statuses = [
        {"name": "notion", "connected": True},
        {"name": "gmail", "connected": True},
        {"name": "drive", "connected": False},
    ]
    failures = {"gmail": "reauth", "drive": "unreachable"}

    health = tr.read_connector_health(
        get_failures=lambda: failures, get_status=lambda: statuses
    )
    by_name = {h["name"]: h["status"] for h in health}
    assert by_name == {"notion": "ok", "gmail": "reauth", "drive": "unreachable"}
    # Sorted by name.
    assert [h["name"] for h in health] == ["drive", "gmail", "notion"]


def test_read_connector_health_surfaces_failure_missing_from_status():
    # A breaker entry for a server the status accessor didn't enumerate must
    # still surface (never silently dropped).
    health = tr.read_connector_health(
        get_failures=lambda: {"ghost": "reauth"},
        get_status=lambda: [{"name": "notion", "connected": True}],
    )
    by_name = {h["name"]: h["status"] for h in health}
    assert by_name == {"notion": "ok", "ghost": "reauth"}


def test_read_connector_health_empty():
    health = tr.read_connector_health(get_failures=lambda: {}, get_status=lambda: [])
    assert health == []


# ===========================================================================
# Phase 3 — route-level integration tests (live aiohttp TestClient)
# ===========================================================================

# All ten dashboard routes, for the blanket "every fragment returns 200" sweep.
_KPI_ROUTES = (
    "kpi-turns",
    "kpi-tier-split",
    "kpi-connectors",
    "kpi-proposals",
)
_CHART_ROUTES = (
    "chart-tier-distribution",
    "chart-system-effort",
    "chart-intent-taxonomy",
    "chart-flywheel-activity",
)
_DASH = "/portal/fragments/dashboard"


def _intent_line(ts: str, intent_class="conversation", tier="T1", api_calls=2, duration_ms=100.0):
    """A full IntentRecord JSONL line (all required fields present)."""
    return json.dumps(
        {
            "timestamp": ts,
            "session_id": "sess",
            "turn_id": f"turn-{ts}",
            "user_message_stem": "x",
            "pattern_hash": "h",
            "intent_class": intent_class,
            "register_class": "technical",
            "complexity_signal": "moderate",
            "confidence": 0.7,
            "outcome": "success",
            "tier_selected": tier,
            "api_calls": api_calls,
            "duration_ms": duration_ms,
        }
    )


def _seed_intents(home, lines):
    (home / "intent_records.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_ledger(home, filename, lines):
    ledger = home / ".kaizen_ledger"
    ledger.mkdir(exist_ok=True)
    (ledger / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_proposals(home, statuses):
    (home / "memory_proposals.jsonl").write_text(
        "\n".join(json.dumps({"status": s}) for s in statuses) + "\n", encoding="utf-8"
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate GROVE_HOME to a temp dir and bind the IntentStore singleton to it.

    The store's ``records()`` reopens the file each call, so tests may seed
    ``intent_records.jsonl`` after the bind and the route still sees it.
    """
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    import grove.intent_store as ist

    monkeypatch.setattr(ist, "_default_store", ist.IntentStore())
    return tmp_path


@pytest.fixture
async def client(home, monkeypatch):
    # Connector health is live process state; stub it so route tests are
    # hermetic (no tools.mcp_tool import / no live MCP dependency).
    import grove.api.dashboard_fragments as df

    monkeypatch.setattr(
        df,
        "read_connector_health",
        lambda *a, **k: [
            {"name": "notion", "status": "ok"},
            {"name": "gmail", "status": "reauth"},
        ],
    )
    app = web.Application()
    register_dashboard_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_overview_returns_html(client):
    r = await client.get(f"{_DASH}/overview")
    assert r.status == 200
    assert r.headers["Content-Type"].startswith("text/html")
    body = await r.text()
    assert 'id="dashboard"' in body
    # Lazy tiles + OOB sidebar swap into the right panel.
    assert body.count('hx-trigger="load"') == 9
    assert 'id="right-panel" class="right-panel" hx-swap-oob="true"' in body
    assert f"{_DASH}/kpi-turns" in body and f"{_DASH}/chart-tier-distribution" in body


async def test_all_kpi_and_chart_fragments_return_200(client):
    # Every KPI and chart fragment answers 200 text/html even with empty stores.
    for route in _KPI_ROUTES + _CHART_ROUTES:
        r = await client.get(f"{_DASH}/{route}")
        assert r.status == 200, route
        assert r.headers["Content-Type"].startswith("text/html"), route


async def test_sidebar_connector_health_populated(client):
    r = await client.get(f"{_DASH}/sidebar-connector-health")
    assert r.status == 200
    body = await r.text()
    assert "Connector health" in body
    assert ">notion<" in body and ">gmail<" in body
    assert "<svg" in body  # status dots
    assert "badge-green" in body and "badge-yellow" in body


async def test_days_param_changes_range(home, client):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=2)).isoformat()
    older = (now - timedelta(days=20)).isoformat()
    _seed_intents(
        home,
        [_intent_line(recent) for _ in range(3)]
        + [_intent_line(older) for _ in range(4)],
    )

    r7 = await client.get(f"{_DASH}/kpi-turns?days=7")
    r30 = await client.get(f"{_DASH}/kpi-turns?days=30")
    body7 = await r7.text()
    body30 = await r30.text()
    # 7-day window sees only the 3 recent records; 30-day sees all 7.
    assert '<div class="kpi-value">3</div>' in body7
    assert "last 7d" in body7
    assert '<div class="kpi-value">7</div>' in body30
    assert "last 30d" in body30


async def test_malformed_ledger_records_skipped_gracefully(home, client):
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(days=1)).isoformat()
    _seed_ledger(
        home,
        "sess.jsonl",
        [
            json.dumps({"event_type": "tool_selection", "timestamp": ts}),
            "{ not valid json at all",  # malformed — skipped, must not crash
            json.dumps({"event_type": "andon_halt", "timestamp": ts}),
        ],
    )
    r = await client.get(f"{_DASH}/chart-flywheel-activity")
    assert r.status == 200
    body = await r.text()
    assert "<svg" in body
    # Both well-formed events rendered; "No data" not shown.
    assert "No data" not in body
    assert ">tool_selection<" in body and ">andon_halt<" in body


async def test_empty_sources_render_no_data_not_broken_svg(home, client):
    # No intent_records, no ledger, no proposals seeded under this GROVE_HOME.
    for route in _CHART_ROUTES:
        r = await client.get(f"{_DASH}/{route}")
        assert r.status == 200, route
        body = await r.text()
        assert "<svg" in body, route          # a valid SVG, never blank
        assert "</svg>" in body, route
        assert "No data" in body, route        # explicit, never a silent omit

    # KPI tiles that depend on empty stores also say so explicitly.
    tier = await (await client.get(f"{_DASH}/kpi-tier-split")).text()
    assert "No data" in tier
    proposals = await (await client.get(f"{_DASH}/kpi-proposals")).text()
    assert "No data" in proposals

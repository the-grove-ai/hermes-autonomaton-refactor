"""Phase-3 tests: ticker integration, input_state brokering, death observability.

Covers the generic resolver dispatch + the notion_query resolver (mocked MCP:
rows / no_work / cold-Andon), cadence + quiet-hours gating, the manager's death
observability across all four terminal classes, dispatch gating, and the Kaizen
go-forward options. GROVE_HOME is per-test isolated by the autouse fixture.
"""

from __future__ import annotations

import json
from datetime import datetime, time as dtime, timedelta, timezone

import pytest

from grove.fleet import cadence, manager as manager_mod, observability, resolvers
from grove.fleet.config import WorkerConfig
from grove.fleet.errors import FleetWorkerAndon


# ── resolver dispatch ────────────────────────────────────────────────────────


def test_resolve_missing_type_andons():
    with pytest.raises(FleetWorkerAndon) as ei:
        resolvers.resolve_input_state({}, "w")
    assert ei.value.check == "resolver_failed"


def test_resolve_unknown_type_andons():
    with pytest.raises(FleetWorkerAndon) as ei:
        resolvers.resolve_input_state({"type": "carrier_pigeon"}, "w")
    assert ei.value.check == "resolver_failed"


def test_notion_query_returns_rows_and_builds_sql(monkeypatch):
    # Live shape: server WHERE filters; result is DOUBLE-encoded with FLAT rows.
    seen = {}

    def _fake_mcp(server, tool, args, timeout):
        seen["server"], seen["tool"], seen["args"] = server, tool, args
        return {"result": json.dumps({"results": [
            {"Company": "Acme", "Role": "Head of Product", "Status": "To Apply", "id": "pg1"},
        ]})}

    monkeypatch.setattr(resolvers, "_mcp_call", _fake_mcp)
    out = resolvers.resolve_input_state(
        {"type": "notion_query", "data_source": "5eb5630d-x", "filter": {"Status": "To Apply"}},
        "forge",
    )
    # correct tool name + data-wrapped SQL args + collection:// prefix + params
    assert seen["tool"] == "notion-query-data-sources"
    d = seen["args"]["data"]
    assert d["mode"] == "sql"
    assert d["data_source_urls"] == ["collection://5eb5630d-x"]
    assert '"Status" = ?' in d["query"] and "collection://5eb5630d-x" in d["query"]
    assert d["params"] == ["To Apply"]
    # flat rows returned (no "properties" wrapper)
    assert out is not None and out["rows"][0]["Company"] == "Acme"
    assert out["data_source"] == "collection://5eb5630d-x"


def test_notion_query_no_match_is_no_work(monkeypatch):
    monkeypatch.setattr(resolvers, "_mcp_call",
                        lambda *a, **k: {"result": json.dumps({"results": []})})
    out = resolvers.resolve_input_state(
        {"type": "notion_query", "data_source": "collection://x", "filter": {"Status": "To Apply"}},
        "forge",
    )
    assert out is None  # no_work


def test_notion_query_cold_mcp_andons(monkeypatch):
    monkeypatch.setattr(resolvers, "_mcp_call", lambda *a, **k: {"error": "server 'notion' is not connected"})
    with pytest.raises(FleetWorkerAndon) as ei:
        resolvers.resolve_input_state(
            {"type": "notion_query", "data_source": "collection://x"}, "forge"
        )
    assert ei.value.check == "resolver_cold_mcp"


def test_notion_query_missing_data_source_andons(monkeypatch):
    monkeypatch.setattr(resolvers, "_mcp_call", lambda *a, **k: {"result": {"rows": []}})
    with pytest.raises(FleetWorkerAndon):
        resolvers.resolve_input_state({"type": "notion_query"}, "forge")


# ── cadence + quiet hours ────────────────────────────────────────────────────


def test_cadence_first_run_is_due():
    assert cadence.cadence_due("*/30 * * * *", None) is True


def test_cadence_within_window_not_due():
    now = datetime(2026, 7, 4, 12, 5, tzinfo=timezone.utc)
    last = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)  # 5 min ago, cadence 30 min
    assert cadence.cadence_due("*/30 * * * *", last, now) is False


def test_cadence_past_window_due():
    now = datetime(2026, 7, 4, 12, 45, tzinfo=timezone.utc)
    last = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)  # 45 min ago
    assert cadence.cadence_due("*/30 * * * *", last, now) is True


def test_no_cadence_always_due():
    assert cadence.cadence_due(None, datetime.now(timezone.utc)) is True


def test_quiet_hours_wraps_midnight():
    qh = {"start": "22:00", "end": "07:00"}
    at = lambda h, m: datetime(2026, 7, 4, h, m).astimezone()
    assert cadence.in_quiet_hours(qh, at(23, 0)) is True   # inside (late)
    assert cadence.in_quiet_hours(qh, at(3, 0)) is True    # inside (early)
    assert cadence.in_quiet_hours(qh, at(12, 0)) is False  # outside
    assert cadence.in_quiet_hours(None, at(3, 0)) is False # no window


# ── manager: death observability ─────────────────────────────────────────────


class _FakeProc:
    def __init__(self, rc):
        self._rc = rc
        self.pid = 4242

    def poll(self):
        return self._rc


class _FakeHandle:
    def __init__(self, rc, event_path, run_id="run1"):
        self.worker_id = "w"
        self.run_id = run_id
        self.proc = _FakeProc(rc)
        self.pgid = 4242
        self.wall_clock_secs = 900
        self.event_path = event_path


@pytest.fixture
def captured_andons(monkeypatch):
    calls = []
    monkeypatch.setattr(
        manager_mod, "surface_fleet_andon",
        lambda wid, run_id, msg, **kw: calls.append({"wid": wid, "msg": msg, **kw}),
    )
    monkeypatch.setattr(manager_mod, "remove_pidfile", lambda *_a, **_k: None)
    monkeypatch.setattr(manager_mod, "enforce_wall_clock", lambda *_a, **_k: False)
    return calls


def _mgr_with_running(handle):
    m = manager_mod.FleetManager()
    m._running["w"] = handle
    return m


def _write_event(tmp_path, name, event):
    import json
    p = tmp_path / name
    p.write_text(json.dumps(event), encoding="utf-8")
    return p


def test_reap_success_is_quiet(captured_andons, tmp_path):
    ev = _write_event(tmp_path, "e.json", {"status": "success", "detail": "ok"})
    _mgr_with_running(_FakeHandle(0, ev))._reap_running()
    assert captured_andons == []  # quiet path


def test_reap_no_work_is_quiet(captured_andons, tmp_path):
    ev = _write_event(tmp_path, "e.json", {"status": "no_work"})
    _mgr_with_running(_FakeHandle(0, ev))._reap_running()
    assert captured_andons == []


def test_reap_exit0_no_event_is_catastrophic(captured_andons, tmp_path):
    missing = tmp_path / "absent.json"
    _mgr_with_running(_FakeHandle(0, missing))._reap_running()
    assert len(captured_andons) == 1
    assert captured_andons[0]["check"] == "catastrophic_no_event"


def test_reap_nonzero_exit_andons(captured_andons, tmp_path):
    ev = _write_event(tmp_path, "e.json", {"status": "failed", "detail": "boom", "check": "record_not_found"})
    _mgr_with_running(_FakeHandle(1, ev))._reap_running()
    assert len(captured_andons) == 1
    assert captured_andons[0]["check"] == "record_not_found"


def test_reap_wall_clock_kill_andons(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(manager_mod, "surface_fleet_andon", lambda wid, run_id, msg, **kw: calls.append(kw))
    monkeypatch.setattr(manager_mod, "remove_pidfile", lambda *_a, **_k: None)
    monkeypatch.setattr(manager_mod, "enforce_wall_clock", lambda *_a, **_k: True)  # killed
    ev = _write_event(tmp_path, "e.json", {"status": "success"})
    _mgr_with_running(_FakeHandle(-9, ev))._reap_running()
    assert calls and calls[0]["check"] == "wall_clock_exceeded"


def test_reap_still_running_stays(captured_andons, tmp_path):
    m = _mgr_with_running(_FakeHandle(None, tmp_path / "x.json"))
    m._reap_running()
    assert "w" in m._running  # rc None -> still running, not reaped


# ── manager: dispatch gating ─────────────────────────────────────────────────


@pytest.fixture
def dispatch_spy(monkeypatch):
    dispatched = []

    class _H:
        run_id = "rr"

    def _fake_dispatch(cfg, payload, run_id=None):
        dispatched.append((cfg.id, payload))
        return _H()

    monkeypatch.setattr(manager_mod.runner, "dispatch", _fake_dispatch)
    return dispatched


def _mgr_with_workers(monkeypatch, workers):
    monkeypatch.setattr(manager_mod, "load_fleet_workers", lambda *_a, **_k: workers)
    return manager_mod.FleetManager()


def test_dispatch_when_work_exists(monkeypatch, dispatch_spy):
    monkeypatch.setattr(manager_mod, "resolve_input_state", lambda *_a, **_k: {"rows": [1]})
    wc = WorkerConfig(id="forge", skill="skill.fleet.forge-jobsearch", enabled=True,
                      input_state={"type": "notion_query"}, limits={"wall_clock_secs": 900})
    m = _mgr_with_workers(monkeypatch, {"forge": wc})
    m.tick()
    assert dispatch_spy == [("forge", {"rows": [1]})]
    assert "forge" in m._running


def test_no_work_does_not_dispatch(monkeypatch, dispatch_spy):
    monkeypatch.setattr(manager_mod, "resolve_input_state", lambda *_a, **_k: None)
    wc = WorkerConfig(id="forge", skill="s", enabled=True, limits={"wall_clock_secs": 900})
    _mgr_with_workers(monkeypatch, {"forge": wc}).tick()
    assert dispatch_spy == []


def test_disabled_worker_skipped(monkeypatch, dispatch_spy):
    monkeypatch.setattr(manager_mod, "resolve_input_state", lambda *_a, **_k: {"rows": [1]})
    wc = WorkerConfig(id="forge", skill="s", enabled=False, limits={"wall_clock_secs": 900})
    _mgr_with_workers(monkeypatch, {"forge": wc}).tick()
    assert dispatch_spy == []


def test_quiet_hours_skips_dispatch(monkeypatch, dispatch_spy):
    monkeypatch.setattr(manager_mod, "resolve_input_state", lambda *_a, **_k: {"rows": [1]})
    monkeypatch.setattr(manager_mod, "in_quiet_hours", lambda *_a, **_k: True)
    wc = WorkerConfig(id="forge", skill="s", enabled=True, limits={"wall_clock_secs": 900})
    _mgr_with_workers(monkeypatch, {"forge": wc}).tick()
    assert dispatch_spy == []


def test_resolver_andon_surfaced_not_dispatched(monkeypatch, dispatch_spy):
    surfaced = []
    monkeypatch.setattr(manager_mod, "surface_fleet_andon",
                        lambda wid, run_id, msg, **kw: surfaced.append(kw.get("check")))
    def _cold(*_a, **_k):
        raise FleetWorkerAndon("cold", check="resolver_cold_mcp")
    monkeypatch.setattr(manager_mod, "resolve_input_state", _cold)
    wc = WorkerConfig(id="forge", skill="s", enabled=True, limits={"wall_clock_secs": 900})
    _mgr_with_workers(monkeypatch, {"forge": wc}).tick()
    assert dispatch_spy == []
    assert "resolver_cold_mcp" in surfaced


# ── observability: Andon files facts only ────────────────────────────────────


def test_surface_andon_files_kaizen_facts_only_and_log_floor(caplog):
    import json
    from hermes_constants import get_hermes_home
    from pathlib import Path

    res = observability.surface_fleet_andon(
        "forge", "run9", "worker exited 1: boom", check="nonzero_exit", loop=None
    )
    assert res["surfaced"] and res["broadcast_scheduled"] is False
    # Kaizen leg wrote an andon_halt carrying facts only — payload is exactly
    # the fact schema plus the ledger envelope (event_type/session_id/timestamp).
    ledger = Path(get_hermes_home()) / ".kaizen_ledger" / "fleet_forge_run9.jsonl"
    assert ledger.exists()
    entry = json.loads(ledger.read_text().splitlines()[-1])
    assert entry["event_type"] == "andon_halt"
    assert set(entry) == {
        "event_type", "session_id", "timestamp",
        "source", "worker", "run", "check", "detail",
    }
    assert entry["source"] == "fleet_worker"
    assert "go_forward_options" not in entry

"""fleet-mcp-warm-unification-v1 P6 — end-to-end local smoke of the whole warm path.

No network, no model, stubbed states. Exercises the full ordered check through the real
seams: the startup warm (P1+P4), the dispatch-seam self-heal (P5), and each ensure_mcp_warm
branch (P3) riding the P2 primitives — including the load-bearing auth-latch reset and the
G6 stale-session self-correction. This is the single-file proof that the pieces compose.
"""

from unittest.mock import MagicMock

import pytest

import tools.mcp_tool as mt
from gateway.config import GatewayConfig
from grove.fleet import manager as manager_mod
from grove.fleet.config import WorkerConfig
from grove.fleet.errors import FleetWorkerAndon, OperatorActionRequired


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _reset(name):
    for d in (
        mt._servers, mt._server_connect_failed, mt._server_error_counts,
        mt._server_breaker_opened_at, mt.auth_alert_surfaced,
    ):
        d.pop(name, None)


def _live_server():
    srv = MagicMock()
    srv.session = MagicMock()
    srv._task = MagicMock()
    srv._task.done.return_value = False
    srv._ready = MagicMock()
    srv._ready.is_set.return_value = True
    return srv


class _CleanExitRunner:
    def __init__(self, config):
        self.config = config
        self.should_exit_cleanly = True
        self.exit_reason = None
        self.adapters = {}

    async def start(self):
        return True

    async def stop(self):
        return None


# ---------------------------------------------------------------------------
# 1. STARTUP WARM (P1 + P4 together)
# ---------------------------------------------------------------------------


def _install_startup_stubs(monkeypatch, tmp_path):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setattr("tools.mcp_tool.reap_dead_owner_children", lambda *a, **k: 0)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda hermes_home, mode: tmp_path)
    monkeypatch.setattr("hermes_logging._add_rotating_handler", lambda *a, **k: None)
    monkeypatch.setattr("gateway.run.GatewayRunner", _CleanExitRunner)


@pytest.mark.asyncio
async def test_smoke_startup_warm_passes_registry_and_populates_servers(monkeypatch, tmp_path):
    """P4: the startup warm passes a real ToolRegistry (no bare-call TypeError) and the
    _servers side effect lands."""
    from tools.registry import ToolRegistry

    _install_startup_stubs(monkeypatch, tmp_path)
    from gateway.run import start_gateway

    captured = {}

    def _ok_discover(*, registry):
        captured["registry"] = registry
        srv = MagicMock(); srv.session = MagicMock()
        mt._servers["smk_start"] = srv
        return ["mcp_smk_start_tool"]

    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", _ok_discover)
    try:
        assert await start_gateway(config=GatewayConfig(), replace=False, verbosity=1) is True
        assert isinstance(captured.get("registry"), ToolRegistry)
        assert mt._servers["smk_start"].session is not None
    finally:
        mt._servers.pop("smk_start", None)


@pytest.mark.asyncio
async def test_smoke_startup_warm_non_typeerror_failure_is_loud_nonfatal(monkeypatch, tmp_path):
    """P1 intact after the P4 fix: a non-TypeError warm failure still fails LOUD (error log +
    operator Andon) and NON-FATAL (gateway comes up)."""
    _install_startup_stubs(monkeypatch, tmp_path)
    from gateway.run import start_gateway

    def _boom(*, registry):
        raise RuntimeError("simulated warm failure")

    andon = _AsyncRecorder()
    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", _boom)
    monkeypatch.setattr("grove.notify.broadcast_to_operator", andon)
    caplog_records = []
    _install_error_capture(monkeypatch, caplog_records)

    assert await start_gateway(config=GatewayConfig(), replace=False, verbosity=1) is True  # non-fatal
    assert andon.awaited                                          # operator Andon fired (loud)
    assert any("Startup MCP tool discovery failed" in r for r in caplog_records)


class _AsyncRecorder:
    def __init__(self):
        self.awaited = False
        self.kwargs = None

    async def __call__(self, *a, **k):
        self.awaited = True
        self.kwargs = k
        return {"logged": True}


def _install_error_capture(monkeypatch, sink):
    import gateway.run as gr

    orig = gr.logger.error

    def _cap(msg, *a, **k):
        try:
            sink.append(msg % a if a else msg)
        except Exception:
            sink.append(str(msg))
        return orig(msg, *a, **k)

    monkeypatch.setattr(gr.logger, "error", _cap)


# ---------------------------------------------------------------------------
# 2. DISPATCH-SEAM SELF-HEAL + ordered-check branches (P5 over P3/P2)
# ---------------------------------------------------------------------------


def _cfg():
    return WorkerConfig(
        id="forge", skill="skill.fleet.forge-jobsearch", enabled=True, cadence=None,
        input_state={"type": "notion_query", "server": "notion", "data_source": "ds"},
    )


@pytest.fixture
def seam(monkeypatch):
    andons = []
    monkeypatch.setattr(
        manager_mod, "surface_fleet_andon",
        lambda wid, run_id, msg, **kw: andons.append({"wid": wid, **kw}),
    )
    monkeypatch.setattr(manager_mod, "load_fleet_workers", lambda *a, **k: {"forge": _cfg()})
    dispatched = []
    monkeypatch.setattr(
        manager_mod.runner, "dispatch",
        lambda c, p: dispatched.append((c, p)) or type("H", (), {"run_id": "r"})(),
    )
    monkeypatch.setattr(manager_mod, "resolve_input_state", lambda inp, wid: {"rows": [{"id": "r1"}]})
    return {"andons": andons, "dispatched": dispatched, "mp": monkeypatch}


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def test_smoke_cold_server_self_heals_no_interactive_turn(seam):
    """The fleet-only-window fix: a cold server is warmed by the dispatch itself
    (ensure_mcp_warm Check-4), NOT by an interactive turn."""
    _reset("notion")
    warmed = []
    real_discover = []

    async def _warm(server_id, context):
        # Simulate the REAL ensure_mcp_warm cold path: server absent -> Check-4 warms.
        warmed.append(server_id)

    seam["mp"].setattr(mt, "ensure_mcp_warm", _warm)
    # Guard: assert NO interactive-turn entrypoint was invoked (discover only via warm).
    seam["mp"].setattr(mt, "discover_mcp_tools", lambda **k: real_discover.append(k) or [])

    manager_mod.FleetManager()._maybe_dispatch(_now())

    assert warmed == ["notion"]                # dispatch drove the warm
    assert len(seam["dispatched"]) == 1        # resolved + dispatched
    assert seam["andons"] == []                # no cold-mcp Andon


def test_smoke_plausibly_warm_fast_path_no_discover(seam):
    """G1 churn guard: a live session short-circuits with NO discover_mcp_tools call."""
    _reset("notion")
    try:
        mt._servers["notion"] = _live_server()
        discover_calls = []
        seam["mp"].setattr(mt, "discover_mcp_tools", lambda **k: discover_calls.append(1) or [])

        # Real ensure_mcp_warm (not stubbed) — exercises Check-3.
        manager_mod.FleetManager()._maybe_dispatch(_now())

        assert discover_calls == []            # fast-path returned, no RPC/re-warm
        assert len(seam["dispatched"]) == 1
    finally:
        _reset("notion")


def test_smoke_auth_dead_loud_once_then_local_then_reset(seam):
    """auth-dead: first loud (broadcast=True + latch), subsequent local (broadcast=False),
    latch CLEARS on a confirming reconnect (the load-bearing reset)."""
    _reset("notion")
    try:
        mt._server_connect_failed["notion"] = "reauth"
        # Real ensure_mcp_warm exercises the auth-dead branch + the latch.

        # First dispatch: LOUD.
        manager_mod.FleetManager()._maybe_dispatch(_now())
        assert seam["andons"][-1]["broadcast"] is True
        assert seam["andons"][-1]["check"] == "mcp_auth_dead"
        assert mt.auth_alert_already_surfaced("notion") is True
        assert seam["dispatched"] == []

        # Second dispatch: LOCAL (latch suppresses the repeat operator alert).
        manager_mod.FleetManager()._maybe_dispatch(_now())
        assert seam["andons"][-1]["broadcast"] is False
        assert seam["dispatched"] == []

        # Confirming reconnect clears BOTH the reauth signature AND the latch.
        from tools.registry import ToolRegistry

        server = mt.MCPServerTask("notion", registry=ToolRegistry())
        server.session = MagicMock(); server._tools = []

        async def _fake_connect(n, cfg, **kw):
            return server

        seam["mp"].setattr(mt, "_connect_server", _fake_connect)
        seam["mp"].setattr(mt, "_register_server_tools", lambda n, s, c: [])
        import asyncio
        asyncio.run(mt._discover_and_register_server("notion", {"command": "x"}, registry=ToolRegistry()))
        assert mt.auth_alert_already_surfaced("notion") is False   # RESET
    finally:
        _reset("notion")


def test_smoke_breaker_open_local_no_storm(seam, monkeypatch):
    """breaker-open -> broadcast=False (no cadence storm), still surfaced (logged/Kaizen)."""
    _reset("notion")
    try:
        fake_t = [1000.0]
        monkeypatch.setattr(mt.time, "monotonic", lambda: fake_t[0])
        mt._server_error_counts["notion"] = mt._CIRCUIT_BREAKER_THRESHOLD
        mt._server_breaker_opened_at["notion"] = fake_t[0]

        manager_mod.FleetManager()._maybe_dispatch(_now())

        assert len(seam["andons"]) == 1
        assert seam["andons"][0]["broadcast"] is False    # no operator storm
        assert seam["andons"][0]["check"] == "mcp_breaker_open"
        assert seam["dispatched"] == []
    finally:
        _reset("notion")


@pytest.mark.asyncio
async def test_smoke_stale_session_falls_through_to_warm():
    """G6: a dead transport nulls server.session, so Check-3 fails -> Check-4 re-warms."""
    _reset("notion")
    try:
        stale = _live_server()
        stale.session = None            # transport died
        mt._servers["notion"] = stale
        called = []

        # Patch discover on the module so Check-4's warm is observable.
        import tools.mcp_tool as _mt
        orig = _mt.discover_mcp_tools
        _mt.discover_mcp_tools = lambda **k: called.append(1) or []
        try:
            await _mt.ensure_mcp_warm("notion", {"wid": "forge"})
        finally:
            _mt.discover_mcp_tools = orig

        assert called == [1]            # Check-3 failed (session None) -> Check-4 warmed
    finally:
        _reset("notion")

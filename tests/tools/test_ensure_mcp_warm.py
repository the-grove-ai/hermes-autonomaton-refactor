"""fleet-mcp-warm-unification-v1 P3 — the ensure_mcp_warm ordered check.

Each branch of the LOCK-2 sequence (auth-dead → breaker → plausibly-warm → warm-if-
cold) plus the G6 stale-session self-correction. The order is load-bearing: auth-dead
is checked before the breaker so a dead secret is never buried under a generic timeout.
"""

from unittest.mock import MagicMock

import pytest

pytest.importorskip("mcp.client.auth.oauth2")

from grove.fleet.errors import FleetWorkerAndon, OperatorActionRequired


def _reset(name):
    from tools import mcp_tool

    for d in (
        mcp_tool._servers,
        mcp_tool._server_connect_failed,
        mcp_tool._server_error_counts,
        mcp_tool._server_breaker_opened_at,
        mcp_tool.auth_alert_surfaced,
    ):
        d.pop(name, None)


def _live_server(name):
    """A plausibly-warm server: session non-None, task alive, ready set."""
    srv = MagicMock()
    srv.session = MagicMock()
    srv._task = MagicMock()
    srv._task.done.return_value = False
    srv._ready = MagicMock()
    srv._ready.is_set.return_value = True
    return srv


@pytest.mark.asyncio
async def test_auth_dead_loud_once_then_local(monkeypatch):
    from tools import mcp_tool

    name = "notion_ad"
    _reset(name)
    try:
        mcp_tool._server_connect_failed[name] = "reauth"
        # Discover must NOT run on the auth-dead path.
        called = []
        monkeypatch.setattr(mcp_tool, "discover_mcp_tools", lambda **k: called.append(1) or [])

        # First call: LOUD (broadcast=True) + latch set.
        with pytest.raises(OperatorActionRequired) as ei1:
            await mcp_tool.ensure_mcp_warm(name, {"wid": "forge"})
        assert ei1.value.broadcast is True
        assert ei1.value.check == "mcp_auth_dead"
        assert mcp_tool.auth_alert_already_surfaced(name) is True

        # Second call: LOCAL (broadcast=False) — no operator storm.
        with pytest.raises(OperatorActionRequired) as ei2:
            await mcp_tool.ensure_mcp_warm(name, {"wid": "forge"})
        assert ei2.value.broadcast is False
        assert called == []  # never warmed on the auth-dead path
    finally:
        _reset(name)


@pytest.mark.asyncio
async def test_breaker_open_local_no_storm(monkeypatch):
    from tools import mcp_tool

    name = "srv_brk"
    _reset(name)
    try:
        # No reauth signature; call-time breaker OPEN within cooldown.
        fake_t = [1000.0]
        monkeypatch.setattr(mcp_tool.time, "monotonic", lambda: fake_t[0])
        mcp_tool._server_error_counts[name] = mcp_tool._CIRCUIT_BREAKER_THRESHOLD
        mcp_tool._server_breaker_opened_at[name] = fake_t[0]
        called = []
        monkeypatch.setattr(mcp_tool, "discover_mcp_tools", lambda **k: called.append(1) or [])

        with pytest.raises(FleetWorkerAndon) as ei:
            await mcp_tool.ensure_mcp_warm(name, {"wid": "forge"})
        assert ei.value.broadcast is False           # no cadence storm
        assert ei.value.check == "mcp_breaker_open"
        assert called == []                           # breaker short-circuits before warm
    finally:
        _reset(name)


@pytest.mark.asyncio
async def test_plausibly_warm_returns_without_rpc_or_rewarm(monkeypatch):
    from tools import mcp_tool

    name = "srv_warm"
    _reset(name)
    try:
        mcp_tool._servers[name] = _live_server(name)
        called = []
        monkeypatch.setattr(mcp_tool, "discover_mcp_tools", lambda **k: called.append(1) or [])

        result = await mcp_tool.ensure_mcp_warm(name, {"wid": "forge"})
        assert result is None
        assert called == []   # G1 churn guard: a live session is NOT re-warmed
    finally:
        _reset(name)


@pytest.mark.asyncio
async def test_cold_triggers_warm(monkeypatch):
    from tools import mcp_tool

    name = "srv_cold"
    _reset(name)
    try:
        # Not in _servers -> cold -> Check-4 must run discover.
        called = []
        monkeypatch.setattr(mcp_tool, "discover_mcp_tools", lambda **k: called.append(k) or [])

        result = await mcp_tool.ensure_mcp_warm(name, {"wid": "forge"})
        assert result is None
        assert len(called) == 1                       # warm attempted
        assert "registry" in called[0]                # canonical warm called with a registry
    finally:
        _reset(name)


@pytest.mark.asyncio
async def test_warm_failure_is_loud(monkeypatch):
    from tools import mcp_tool

    name = "srv_warmfail"
    _reset(name)
    try:
        def _boom(**k):
            raise RuntimeError("connect blew up")

        monkeypatch.setattr(mcp_tool, "discover_mcp_tools", _boom)

        with pytest.raises(FleetWorkerAndon) as ei:
            await mcp_tool.ensure_mcp_warm(name, {"wid": "forge"})
        assert ei.value.broadcast is True             # genuine fault -> operator sees it
        assert ei.value.check == "mcp_warm_failed"
    finally:
        _reset(name)


@pytest.mark.asyncio
async def test_stale_session_falls_through_to_warm(monkeypatch):
    """G6 self-correction: a dead transport nulls server.session (run()'s except +
    finally), so Check-3 FAILS on session=None and falls to Check-4 (re-warm) — no
    infinite non-healing loop on a plausibly-but-not-really-warm server."""
    from tools import mcp_tool

    name = "srv_stale"
    _reset(name)
    try:
        stale = _live_server(name)
        stale.session = None            # transport died -> run() nulled the session
        mcp_tool._servers[name] = stale
        called = []
        monkeypatch.setattr(mcp_tool, "discover_mcp_tools", lambda **k: called.append(1) or [])

        result = await mcp_tool.ensure_mcp_warm(name, {"wid": "forge"})
        assert result is None
        assert called == [1]            # Check-3 failed (session None) -> Check-4 re-warmed
    finally:
        _reset(name)

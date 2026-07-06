"""fleet-mcp-warm-unification-v1 P4 — the two bare discover_mcp_tools calls now pass
a fresh registry.

Pre-fix, gateway startup (:17346) and /reload-mcp (:12434) called
``discover_mcp_tools`` bare — a TypeError (missing required ``registry``) that was
swallowed, so the warms never populated ``_servers`` and only interactive turns
warmed MCP. These assert the FIX: each site passes a real ``ToolRegistry`` (no
TypeError), the warm COMPLETES, and the ``_servers`` side effect lands (not merely
"no exception"). The P1 fail-loud regression (non-TypeError still surfaces loud) is
covered by ``test_runner_startup_failures.test_startup_mcp_warm_failure_is_loud_and_nonfatal``.
"""

from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig


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


@pytest.mark.asyncio
async def test_startup_warm_passes_registry_and_populates_servers(monkeypatch, tmp_path):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.registry import ToolRegistry

    captured = {}

    def _fake_discover(*, registry):
        # The fix: the site MUST pass a registry (bare -> TypeError pre-fix).
        captured["registry"] = registry
        srv = MagicMock()
        srv.session = MagicMock()          # warm: session populated
        mcp_tool._servers["p4start"] = srv  # the load-bearing _servers side effect
        return ["mcp_p4start_tool"]         # non-empty tool surface

    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", _fake_discover)
    monkeypatch.setattr("tools.mcp_tool.reap_dead_owner_children", lambda *a, **k: 0)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda hermes_home, mode: tmp_path)
    monkeypatch.setattr("hermes_logging._add_rotating_handler", lambda *args, **kwargs: None)
    monkeypatch.setattr("gateway.run.GatewayRunner", _CleanExitRunner)

    from gateway.run import start_gateway

    try:
        ok = await start_gateway(config=GatewayConfig(), replace=False, verbosity=1)

        assert ok is True                                      # completed, no TypeError
        assert isinstance(captured.get("registry"), ToolRegistry)  # registry passed, not bare
        assert mcp_tool._servers.get("p4start") is not None    # _servers side effect landed
        assert mcp_tool._servers["p4start"].session is not None  # session warmed
    finally:
        mcp_tool._servers.pop("p4start", None)


@pytest.mark.asyncio
async def test_reload_warm_passes_registry_and_reports_accurate_count(monkeypatch, tmp_path):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))

    from gateway.run import GatewayRunner
    from tools import mcp_tool
    from tools.registry import ToolRegistry

    captured = {}

    def _fake_discover(*, registry):
        captured["registry"] = registry
        srv = MagicMock()
        srv.session = MagicMock()
        mcp_tool._servers["p4reload"] = srv     # reconnect side effect
        return ["mcp_p4reload_a", "mcp_p4reload_b"]   # 2 tools

    # shutdown clears _servers (the reload's real behavior), so discover reconnects all.
    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", _fake_discover)
    monkeypatch.setattr("tools.mcp_tool.shutdown_mcp_servers", lambda: mcp_tool._servers.clear())

    runner = GatewayRunner(GatewayConfig())
    event = MagicMock()
    event.source = MagicMock()

    try:
        result = await runner._execute_mcp_reload(event)

        assert isinstance(captured.get("registry"), ToolRegistry)  # registry passed, not bare
        assert "failed" not in result.lower()                      # reload COMPLETED
        assert mcp_tool._servers.get("p4reload") is not None       # _servers re-populated
        assert "2" in result                                       # accurate "N tools" count
    finally:
        mcp_tool._servers.pop("p4reload", None)

"""Sprint 47.5 Phase 4 — hermes doctor crash-recovery verification.

Covers: orphan discovery (PPID-tree, never by name), reap orchestration,
the lock re-check guard, dry-run safety, and the shutdown_mcp_servers
idempotency guard (Phase 3 / Andon A2).
"""

from __future__ import annotations

import fcntl
import os
from unittest.mock import MagicMock

import pytest

import hermes_cli.doctor_lifecycle as dl


def _fake_proc(pid, ppid, cmdline):
    class _P:
        info = {"pid": pid, "ppid": ppid, "cmdline": cmdline}
    return _P()


# ── orphan discovery: PPID tree, never by name ────────────────────────


def test_orphan_scan_excludes_live_tree_and_never_matches_node(monkeypatch):
    import psutil
    procs = [
        # the live gateway (PID-1-parented under launchd) — must be PROTECTED
        _fake_proc(12345, 1, ["python", "-m", "hermes_cli.main", "gateway", "run"]),
        # a genuinely orphaned Grove chat (PPID 1, not in the live tree)
        _fake_proc(999, 1, ["python", "-m", "hermes_cli.main", "chat"]),
        # Notion.app node helper — MUST NEVER match (the "never pkill node" rule)
        _fake_proc(500, 1, ["node", "/Applications/Notion.app/Notion Helper"]),
        # a Grove process whose parent is alive (not reparented) — not an orphan
        _fake_proc(600, 4000, ["python", "-m", "hermes_cli.main", "gateway", "run"]),
        # console-script form, orphaned — should match via the bin/hermes marker
        _fake_proc(777, 1, ["/Users/op/.venv/bin/hermes", "chat"]),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))

    orphans = dl.find_orphaned_grove_processes(protected={12345})
    pids = {o["pid"] for o in orphans}
    assert pids == {999, 777}  # live gateway protected; node never matched; live-parent & non-orphan excluded


# ── retention-reaper guard: spare systemd-launched oneshots (R-T6) ────


def test_orphan_scan_spares_active_service_cgroup(monkeypatch, caplog):
    """A PID-1-parented Grove CLI process that sits inside an active *.service
    cgroup (a systemd-launched oneshot like the ledger-retention timer's
    service) is SPARED and logged — it is not a crash orphan. A shell-
    reparented process in a user session scope (no .service) is still reaped.
    Closes the watchdog-vs-retention SIGTERM collision (test-baseline-hygiene
    R-T6)."""
    import logging
    import psutil
    procs = [
        # the retention oneshot: Type=oneshot, reparented to PID 1, living in
        # its service cgroup — must be SPARED, not SIGTERM'd mid-pass
        _fake_proc(531668, 1, ["/home/hermes/.venv/bin/hermes", "flywheel", "maintain", "--retention"]),
        # a genuinely stranded chat reparented into a user session scope
        # (no .service segment) — must still be REAPED
        _fake_proc(999, 1, ["python", "-m", "hermes_cli.main", "chat"]),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
    cgroups = {
        531668: "0::/system.slice/grove-ledger-retention.service\n",
        999: "0::/user.slice/user-1000.slice/session-3.scope\n",
    }
    monkeypatch.setattr(dl, "_read_proc_cgroup", lambda pid: cgroups.get(pid), raising=False)

    with caplog.at_level(logging.INFO):
        orphans = dl.find_orphaned_grove_processes(protected=set())
    pids = {o["pid"] for o in orphans}
    assert pids == {999}  # retention oneshot spared; shell-scope orphan reaped
    assert "531668" in caplog.text  # spared pid is logged


def test_service_cgroup_guard_is_noop_without_proc(monkeypatch):
    """macOS / no-/proc path: _read_proc_cgroup returns None, so the guard is a
    no-op and a genuine Grove orphan is still reaped (behavior unchanged)."""
    import psutil
    procs = [_fake_proc(999, 1, ["python", "-m", "hermes_cli.main", "chat"])]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
    monkeypatch.setattr(dl, "_read_proc_cgroup", lambda pid: None, raising=False)

    orphans = dl.find_orphaned_grove_processes(protected=set())
    assert {o["pid"] for o in orphans} == {999}


def test_is_grove_cli_cmd_never_matches_bare_node():
    assert dl._is_grove_cli_cmd("python -m hermes_cli.main gateway run")
    assert dl._is_grove_cli_cmd("/x/.venv/bin/hermes chat")
    assert not dl._is_grove_cli_cmd("node /Applications/Notion.app/Notion Helper")
    assert not dl._is_grove_cli_cmd("npm exec @notionhq/notion-mcp-server")


# ── reap orchestration ────────────────────────────────────────────────


def test_reap_acts_via_registry_and_signals_orphans(monkeypatch):
    monkeypatch.setattr(dl, "find_live_gateway_pid", lambda: None)
    monkeypatch.setattr(dl, "gateway_tree_pids", lambda gw: set())
    reap_mock = MagicMock(return_value=2)
    monkeypatch.setattr("tools.mcp_tool.reap_dead_owner_children", reap_mock)
    orphans = [{"pid": 999, "cmd": "hermes_cli.main chat"}]
    monkeypatch.setattr(dl, "find_orphaned_grove_processes", lambda protected: orphans)
    signal_mock = MagicMock()
    monkeypatch.setattr(dl, "_signal_orphans", signal_mock)
    monkeypatch.setattr(dl, "clean_stale_locks", lambda *a, **k: [])
    monkeypatch.setattr(dl, "checkpoint_unowned_dbs", lambda *a, **k: [])

    report = dl.reap(dry_run=False)

    reap_mock.assert_called_once()         # MCP orphans via the registry primitive only
    signal_mock.assert_called_once_with(orphans)
    assert report["mcp_reaped"] == 2


def test_reap_leaves_live_gateway_alone(monkeypatch):
    """A live gateway tree is protected: it is never in the orphan set, and
    --reap (no --force) never signals it."""
    monkeypatch.setattr(dl, "find_live_gateway_pid", lambda: 12345)
    monkeypatch.setattr(dl, "gateway_tree_pids", lambda gw: {12345, 12346})
    # Real orphan scan with the protected set excludes the gateway tree.
    monkeypatch.setattr(
        dl, "find_orphaned_grove_processes",
        lambda protected: [] if 12345 in protected and 12346 in protected else [{"pid": 12345}],
    )
    signal_mock = MagicMock()
    monkeypatch.setattr(dl, "_signal_orphans", signal_mock)
    monkeypatch.setattr("tools.mcp_tool.reap_dead_owner_children", MagicMock(return_value=0))
    monkeypatch.setattr(dl, "clean_stale_locks", lambda *a, **k: [])
    monkeypatch.setattr(dl, "checkpoint_unowned_dbs", lambda *a, **k: [])

    report = dl.reap(dry_run=False)

    assert report["orphans"] == []
    signal_mock.assert_not_called()  # live gateway never signalled


# ── lock re-check guard ───────────────────────────────────────────────


def test_held_lock_is_never_deleted(tmp_path):
    lock = tmp_path / ".held.lock"
    lock.write_text("")
    fd = os.open(str(lock), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # hold it for the duration
    try:
        assert dl._lock_is_held(lock) is True
        results = dl.clean_stale_locks(tmp_path, dry_run=False, gateway_live=False)
        assert lock.exists()  # the guard prevented deletion
        action = next(r["action"] for r in results if r["path"] == str(lock))
        assert "held" in action
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_unheld_lock_removed_when_no_gateway(tmp_path):
    lock = tmp_path / ".stale.lock"
    lock.write_text("")
    results = dl.clean_stale_locks(tmp_path, dry_run=False, gateway_live=False)
    assert not lock.exists()
    assert any(r["action"] == "removed" for r in results)


def test_live_gateway_locks_are_skipped(tmp_path):
    """When a gateway is live it owns its cycled advisory locks — skip them
    even though they read as free at the sampling instant."""
    lock = tmp_path / ".mcp-children.lock"
    lock.write_text("")
    results = dl.clean_stale_locks(tmp_path, dry_run=False, gateway_live=True)
    assert lock.exists()  # not removed — gateway owns the environment
    assert any("live gateway" in r["action"] for r in results)


# ── dry-run safety ────────────────────────────────────────────────────


def test_dry_run_reports_but_does_not_act(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "find_live_gateway_pid", lambda: None)
    monkeypatch.setattr(dl, "gateway_tree_pids", lambda gw: set())
    monkeypatch.setattr(dl, "find_orphaned_grove_processes", lambda protected: [{"pid": 999, "cmd": "x"}])
    signal_mock = MagicMock()
    monkeypatch.setattr(dl, "_signal_orphans", signal_mock)
    reap_mock = MagicMock()
    monkeypatch.setattr("tools.mcp_tool.reap_dead_owner_children", reap_mock)
    monkeypatch.setattr(dl, "_grove_home", lambda: tmp_path)
    monkeypatch.setattr(dl, "checkpoint_unowned_dbs", lambda *a, **k: [])
    lock = tmp_path / ".stale.lock"
    lock.write_text("")

    report = dl.reap(dry_run=True)

    signal_mock.assert_not_called()       # no orphan killing
    reap_mock.assert_not_called()         # no registry reaping
    assert lock.exists()                  # no lock removal
    assert report["dry_run"] is True


# ── shutdown_mcp_servers idempotency (Phase 3 / Andon A2) ─────────────


def test_shutdown_mcp_servers_idempotent(monkeypatch):
    import tools.mcp_tool as m

    monkeypatch.setattr(m, "_mcp_shutdown_completed", False)
    with m._lock:
        m._servers.clear()
    stop_calls = []
    monkeypatch.setattr(m, "_stop_mcp_loop", lambda: stop_calls.append(1))

    m.shutdown_mcp_servers()  # empty fast path → stops loop, marks completed
    assert m._mcp_shutdown_completed is True
    m.shutdown_mcp_servers()  # guard: completed AND no servers → no-op
    m.shutdown_mcp_servers()  # still a no-op

    assert len(stop_calls) == 1  # the loop-stop ran exactly once

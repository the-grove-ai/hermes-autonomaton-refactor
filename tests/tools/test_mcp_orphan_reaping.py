"""Tests for the MCP orphan-reaping machinery (mcp-orphan-reaping-v1).

Three concerns:
  * ``_safe_killpg_or_kill`` reaps a wrapper + its descendants, and refuses
    to target the caller's own process group.
  * The persisted registry round-trips correctly, writes atomically, and
    serializes concurrent writes via ``fcntl.flock``.
  * ``reap_dead_owner_children`` kills only entries owned by a dead
    process — a running sibling's MCP children are never disturbed.

killpg / flock / start_new_session are POSIX-only, so the whole module
is skipped on Windows.
"""
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "killpg"),
    reason="killpg / start_new_session / flock semantics are POSIX-only",
)


# ---------- helpers ---------------------------------------------------------


def _spawn_group_with_child() -> subprocess.Popen:
    """Spawn a shell that backgrounds one sleep and execs another.

    Two members in a fresh session/process group; the Popen handle is the
    group leader. Mirrors the ``npm exec`` + ``node`` shape we need to
    reap.
    """
    return subprocess.Popen(
        ["sh", "-c", "sleep 5 & exec sleep 5"],
        start_new_session=True,
    )


def _alive_not_zombie(pid: int) -> bool:
    """True iff the pid points at a running process (not gone, not a zombie)."""
    try:
        state = subprocess.check_output(
            ["ps", "-o", "stat=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except subprocess.CalledProcessError:
        return False
    return bool(state) and not state.startswith("Z")


@pytest.fixture
def isolated_grove_home(tmp_path, monkeypatch):
    """Point GROVE_HOME at a tmp dir so the registry never touches real state."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    yield tmp_path


# ---------- _safe_killpg_or_kill --------------------------------------------


class TestSafeKillpg:
    def test_killpg_reaps_wrapper_and_descendants(self):
        """The whole process group dies — wrapper + the child it spawned."""
        from tools.mcp_tool import _safe_killpg_or_kill

        p = _spawn_group_with_child()
        try:
            time.sleep(0.2)
            pgid = os.getpgid(p.pid)
            members = subprocess.check_output(
                ["ps", "-o", "pid=", "-g", str(pgid)]
            ).decode().split()
            assert len(members) >= 2, members

            _safe_killpg_or_kill(p.pid, pgid, signal.SIGTERM)
            p.wait(timeout=3)
            time.sleep(0.3)

            try:
                survivors = subprocess.check_output(
                    ["ps", "-o", "pid=", "-g", str(pgid)],
                    stderr=subprocess.DEVNULL,
                ).decode().split()
            except subprocess.CalledProcessError:
                survivors = []
            running = [s for s in survivors if _alive_not_zombie(int(s))]
            assert not running, f"orphans survived: {running}"
        finally:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=2)

    def test_guard_against_self_kill(self):
        """When pgid would target our own group, fall back to single-PID kill."""
        from tools.mcp_tool import _safe_killpg_or_kill

        # Spawn a child without start_new_session — it shares our group.
        p = subprocess.Popen(["sleep", "5"])
        try:
            time.sleep(0.1)
            assert os.getpgid(p.pid) == os.getpgrp()
            # If the guard fails, this kills the test runner itself.
            _safe_killpg_or_kill(p.pid, os.getpgrp(), signal.SIGTERM)
            p.wait(timeout=3)
            assert p.returncode is not None
        finally:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=2)


# ---------- persisted registry ----------------------------------------------


class TestPersistedRegistry:
    def test_round_trip(self, isolated_grove_home):
        from tools.mcp_tool import (
            _registry_add,
            _registry_load,
            _registry_remove,
        )

        _registry_add(1234, 1234, "notion", 9999)
        _registry_add(5678, 5678, "filesystem", 9999)
        entries = _registry_load()
        assert {e["pid"] for e in entries} == {1234, 5678}
        required = {"pid", "pgid", "server", "owner_pid", "spawned_at"}
        for e in entries:
            assert required <= set(e)

        _registry_remove([1234])
        assert {e["pid"] for e in _registry_load()} == {5678}

    def test_replace_on_same_pid(self, isolated_grove_home):
        from tools.mcp_tool import _registry_add, _registry_load

        _registry_add(1234, 1234, "first", 1)
        _registry_add(1234, 1234, "second", 1)
        entries = _registry_load()
        assert len(entries) == 1
        assert entries[0]["server"] == "second"

    def test_atomic_write_leaves_no_tmp(self, isolated_grove_home):
        from tools.mcp_tool import _registry_add

        for i in range(5):
            _registry_add(1000 + i, 1000 + i, "s", 1)
        leftovers = [f for f in os.listdir(isolated_grove_home) if f.endswith(".tmp")]
        assert leftovers == []

    def test_load_tolerates_corrupt_json(self, isolated_grove_home):
        from tools.mcp_tool import _registry_load, _registry_path

        with open(_registry_path(), "w", encoding="utf-8") as f:
            f.write("not json {{{")
        assert _registry_load() == []

    def test_load_returns_empty_when_file_missing(self, isolated_grove_home):
        from tools.mcp_tool import _registry_load
        assert _registry_load() == []

    def test_concurrent_adds_under_flock(self, isolated_grove_home):
        """Many concurrent _registry_add calls — flock prevents lost updates."""
        from tools.mcp_tool import _registry_add, _registry_load

        def add(p):
            _registry_add(p, p, f"s{p}", 1)

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(add, range(10000, 10020)))

        assert {e["pid"] for e in _registry_load()} == set(range(10000, 10020))


# ---------- reap_dead_owner_children ----------------------------------------


class TestReapDeadOwnerChildren:
    def test_dead_owner_reaped_live_owner_spared(self, isolated_grove_home):
        """The sibling-safety guarantee, end to end with real subprocesses."""
        from tools.mcp_tool import (
            _registry_add,
            _registry_load,
            reap_dead_owner_children,
            _OWNER_PID,
        )

        # Manufacture a dead owner PID via a short-lived subprocess —
        # cleaner than os.fork() under pytest-xdist's multi-threaded workers.
        _short = subprocess.Popen(["true"])
        _short.wait()
        dead_owner_pid = _short.pid
        assert dead_owner_pid != _OWNER_PID

        dead = _spawn_group_with_child()
        live = _spawn_group_with_child()
        try:
            time.sleep(0.2)
            _registry_add(
                dead.pid, os.getpgid(dead.pid), "fake-dead", dead_owner_pid,
            )
            _registry_add(
                live.pid, os.getpgid(live.pid), "fake-live", _OWNER_PID,
            )

            processed = reap_dead_owner_children()
            assert processed >= 1

            dead.wait(timeout=4)  # reap zombie so ps is clean
            assert not _alive_not_zombie(dead.pid)
            assert _alive_not_zombie(live.pid), \
                "live-owner sibling must not be killed"

            after_pids = {e["pid"] for e in _registry_load()}
            assert dead.pid not in after_pids
            assert live.pid in after_pids
        finally:
            for p in (dead, live):
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=2)

    def test_dead_owner_dead_child_is_dropped(self, isolated_grove_home):
        """An entry whose owner AND child are both gone is just removed."""
        from tools.mcp_tool import (
            _registry_add,
            _registry_load,
            reap_dead_owner_children,
        )

        # PIDs essentially guaranteed not to exist
        _registry_add(999_999_999, 999_999_999, "ghost", 999_999_998)
        processed = reap_dead_owner_children()
        assert processed >= 1
        assert _registry_load() == []

    def test_noop_when_registry_empty(self, isolated_grove_home):
        from tools.mcp_tool import reap_dead_owner_children
        assert reap_dead_owner_children() == 0

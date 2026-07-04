"""Phase-2 process hardening tests (background-worker-runtime-v1).

Real subprocesses: a sleeper launched in its own session/group via the runtime's
``build_preexec``, so setsid / group-kill / orphan-reap / wall-clock enforcement
are exercised end-to-end, not mocked. GROVE_HOME is per-test isolated by the
autouse conftest fixture, so pidfiles land in a tempdir.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from grove.fleet import config, paths, reap, runner
from grove.fleet.errors import FleetWorkerAndon
from grove.fleet.limits import build_preexec, group_alive, safe_kill_group


def _spawn_sleeper(seconds: int = 30) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        preexec_fn=build_preexec(),
    )


def _reap_dead(p: subprocess.Popen, timeout: float = 5.0) -> None:
    """Confirm the killed sleeper terminated, reaping the child so it does not
    linger as a zombie. (Only needed because the test process is its parent; a
    production orphan is re-parented to init and reaped by the OS, and the ticker
    reaps its own workers via poll().)"""
    rc = p.wait(timeout=timeout)
    assert rc is not None


@pytest.fixture
def sleeper():
    procs = []

    def _make(seconds: int = 30) -> subprocess.Popen:
        p = _spawn_sleeper(seconds)
        procs.append(p)
        return p

    yield _make
    for p in procs:
        safe_kill_group(p.pid, p.pid, signal.SIGKILL)


# ── process-group isolation + group kill ─────────────────────────────────────


def test_preexec_puts_child_in_own_group(sleeper):
    p = sleeper()
    # setsid => the child is a session/group leader => pgid == pid, distinct from
    # this test process's group.
    assert os.getpgid(p.pid) == p.pid
    assert os.getpgid(p.pid) != os.getpgrp()


def test_safe_kill_group_reaps_the_group(sleeper):
    p = sleeper()
    assert group_alive(p.pid, p.pid)
    safe_kill_group(p.pid, p.pid, signal.SIGKILL)
    _reap_dead(p)


def test_safe_kill_group_never_targets_caller_group():
    # A pgid equal to the caller's own group must fall back to a single-PID probe,
    # never killpg the caller. group_alive on our own pid stays True.
    assert group_alive(os.getpid(), os.getpgrp())


# ── pidfile lifecycle ────────────────────────────────────────────────────────


def test_pidfile_round_trip():
    reap.write_pidfile("w1", "r1", pid=4242, pgid=4242, wall_clock_secs=900, started_at="t")
    rec = reap.read_pidfile("w1")
    assert rec["pid"] == 4242 and rec["pgid"] == 4242 and rec["worker_id"] == "w1"
    reap.remove_pidfile("w1")
    assert reap.read_pidfile("w1") is None


# ── startup orphan-reap ──────────────────────────────────────────────────────


def test_sweep_kills_live_orphan_and_removes_pidfile(sleeper):
    p = sleeper()
    reap.write_pidfile("orphan", "r", pid=p.pid, pgid=p.pid, wall_clock_secs=900, started_at="t")
    reaped = reap.sweep_orphans()
    assert [r["worker_id"] for r in reaped] == ["orphan"]
    _reap_dead(p)
    assert reap.read_pidfile("orphan") is None  # pidfile cleaned


def test_sweep_removes_stale_dead_pidfile_without_killing():
    # A pid that is (almost certainly) dead: a freshly-exited child.
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    reap.write_pidfile("dead", "r", pid=p.pid, pgid=p.pid, wall_clock_secs=900, started_at="t")
    reaped = reap.sweep_orphans()
    assert reaped == []  # nothing alive to reap
    assert reap.read_pidfile("dead") is None  # stale file still removed


def test_sweep_skips_malformed_pidfile_and_continues(sleeper):
    # A malformed pidfile must not abort the sweep of a good one.
    bad = paths.pid_path("badworker")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json", encoding="utf-8")
    p = sleeper()
    reap.write_pidfile("goodworker", "r", pid=p.pid, pgid=p.pid, wall_clock_secs=900, started_at="t")
    reaped = reap.sweep_orphans()
    assert "goodworker" in [r["worker_id"] for r in reaped]
    _reap_dead(p)


# ── wall-clock enforcement ───────────────────────────────────────────────────


class _Handle:
    def __init__(self, proc, deadline):
        self.worker_id = "w"
        self.run_id = "r"
        self.proc = proc
        self.pgid = proc.pid
        self.wall_clock_secs = 1
        self.deadline_monotonic = deadline


def test_enforce_wall_clock_kills_past_deadline(sleeper):
    p = sleeper()
    h = _Handle(p, deadline=time.monotonic() - 0.1)  # already past
    assert reap.enforce_wall_clock(h) is True
    _reap_dead(p)


def test_enforce_wall_clock_noop_within_window(sleeper):
    p = sleeper()
    h = _Handle(p, deadline=time.monotonic() + 100)
    assert reap.enforce_wall_clock(h) is False
    assert group_alive(p.pid, p.pid)


def test_enforce_wall_clock_noop_when_already_exited():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    h = _Handle(p, deadline=time.monotonic() - 1)
    assert reap.enforce_wall_clock(h) is False


# ── runner: mandatory wall-clock bound + pidfile/handle bookkeeping ───────────


def test_dispatch_requires_positive_wall_clock():
    wc = config.WorkerConfig(
        id="nobound", skill="skill.fleet.scout", enabled=True, limits={}
    )
    with pytest.raises(FleetWorkerAndon) as ei:
        runner.dispatch(wc, {"x": 1})
    assert ei.value.check == "missing_wall_clock"


def test_dispatch_writes_pidfile_and_deadline(monkeypatch, sleeper):
    # Swap the real worker launch for a controllable sleeper (started with the
    # SAME preexec, so pgid == pid holds) to assert the bookkeeping the ticker
    # relies on without running a full worker.
    made = {}

    def _fake_spawn(self, worker_id, run_id, limits=None):
        p = sleeper()
        made["p"] = p
        return p

    monkeypatch.setattr(runner.KanbanRunner, "_spawn", _fake_spawn)
    wc = config.WorkerConfig(
        id="bound", skill="skill.fleet.scout", enabled=True,
        limits={"wall_clock_secs": 900, "mem_mb": 512},
    )
    handle = runner.dispatch(wc, {"row": 1}, run_id="rr")
    # pidfile written with the process group
    rec = reap.read_pidfile("bound")
    assert rec["pid"] == made["p"].pid and rec["pgid"] == made["p"].pid
    assert rec["wall_clock_secs"] == 900
    # handle carries the wall-clock deadline for the ticker
    assert handle.pgid == made["p"].pid
    assert handle.wall_clock_secs == 900
    assert handle.deadline_monotonic > time.monotonic()
    # inbox brokered
    assert json.loads(paths.inbox_path("bound", "rr").read_text())["payload"] == {"row": 1}

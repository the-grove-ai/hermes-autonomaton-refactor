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
    reap.write_pidfile("w1", "r1", pid=4242, pgid=4242, wall_clock_secs=900)
    rec = reap.read_pidfile("w1")
    assert rec["pid"] == 4242 and rec["pgid"] == 4242 and rec["worker_id"] == "w1"
    reap.remove_pidfile("w1")
    assert reap.read_pidfile("w1") is None


# ── startup orphan-reap ──────────────────────────────────────────────────────


def test_sweep_kills_live_orphan_and_removes_pidfile(runid_sleeper):
    # A faithful orphan: the live process's argv carries the run_id, exactly as
    # a real worker spawned by _spawn does (--run-id <hex>), so the identity
    # gate confirms it is ours and it is reaped.
    p = runid_sleeper("r")
    reap.write_pidfile("orphan", "r", pid=p.pid, pgid=p.pid, wall_clock_secs=900)
    reaped = reap.sweep_orphans()
    assert [r["worker_id"] for r in reaped] == ["orphan"]
    _reap_dead(p)
    assert reap.read_pidfile("orphan") is None  # pidfile cleaned


def test_sweep_removes_stale_dead_pidfile_without_killing():
    # A pid that is (almost certainly) dead: a freshly-exited child.
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    reap.write_pidfile("dead", "r", pid=p.pid, pgid=p.pid, wall_clock_secs=900)
    reaped = reap.sweep_orphans()
    assert reaped == []  # nothing alive to reap
    assert reap.read_pidfile("dead") is None  # stale file still removed


def test_sweep_skips_malformed_pidfile_and_continues(runid_sleeper):
    # A malformed pidfile must not abort the sweep of a good one. The good
    # worker carries its run_id in argv so it passes the identity gate.
    bad = paths.pid_path("badworker")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json", encoding="utf-8")
    p = runid_sleeper("r")
    reap.write_pidfile("goodworker", "r", pid=p.pid, pgid=p.pid, wall_clock_secs=900)
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


# ── C2a: identity gate — never signal a process we cannot prove is ours ──────
#
# The live process's argv carries --run-id <hex> (runner._spawn). The sweep
# verifies that the run_id from the pidfile is an ELEMENT of the target's
# cmdline before signalling. A recycled pid holding our stored integers fails
# the check and is left alone. group_alive stays the liveness probe; identity
# is a second, stronger gate.

_RUN_ID = "a1b2c3d4e5f6a1b2"


@pytest.fixture
def runid_sleeper():
    """A sleeper whose argv carries --run-id <hex>, a faithful stand-in for a
    real worker (its cmdline passes the identity gate). Own group via setsid."""
    procs = []

    def _make(run_id: str, seconds: int = 30) -> subprocess.Popen:
        p = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({seconds})",
             "--run-id", run_id],
            preexec_fn=build_preexec(),
        )
        procs.append(p)
        return p

    yield _make
    for p in procs:
        safe_kill_group(p.pid, p.pid, signal.SIGKILL)
        try:
            p.wait(timeout=5)
        except Exception:
            pass


@pytest.fixture
def captured_andons(monkeypatch):
    calls = []

    def _spy(worker_id, run_id, message, *, check=None, loop=None, **kw):
        calls.append(
            {"worker_id": worker_id, "run_id": run_id, "message": message,
             "check": check}
        )
        return {"surfaced": True}

    monkeypatch.setattr("grove.fleet.observability.surface_fleet_andon", _spy)
    return calls


def test_sweep_does_not_kill_or_unlink_unverifiable_identity(sleeper, captured_andons):
    # A recycled pid: the live process (a plain sleeper) does NOT carry our
    # stored run_id in its argv. It must NOT be killed, and its pidfile survives.
    p = sleeper()  # argv has no run_id
    reap.write_pidfile(
        "recycled", _RUN_ID, pid=p.pid, pgid=p.pid, wall_clock_secs=900
    )
    reaped = reap.sweep_orphans()

    assert group_alive(p.pid, p.pid)  # NOT killed — a wrong kill is unrecoverable
    assert reap.read_pidfile("recycled") is not None  # pidfile left for inspection
    assert "recycled" not in [r["worker_id"] for r in reaped]
    # C2b: we did not cause this death — write NO receipt.
    assert not paths.event_path("recycled", _RUN_ID).exists()

    # The Andon must be ACTIONABLE: pid, expected run_id, and the observed cmdline.
    andon = [a for a in captured_andons if a["check"] == "orphan_identity_unverified"]
    assert len(andon) == 1
    msg = andon[0]["message"]
    assert str(p.pid) in msg
    assert _RUN_ID in msg
    assert "time.sleep" in msg  # the actual cmdline is named


def test_sweep_kills_and_unlinks_when_identity_confirmed(runid_sleeper):
    p = runid_sleeper(_RUN_ID)
    reap.write_pidfile(
        "ours", _RUN_ID, pid=p.pid, pgid=p.pid, wall_clock_secs=900
    )
    reaped = reap.sweep_orphans()
    assert "ours" in [r["worker_id"] for r in reaped]
    _reap_dead(p)
    assert reap.read_pidfile("ours") is None  # confirmed dead -> unlinked


def test_sweep_keeps_pidfile_and_andons_when_kill_unconfirmed(
    runid_sleeper, captured_andons, monkeypatch
):
    # Identity confirms (argv carries the run_id) but the SIGKILL does not take
    # effect (uninterruptible sleep) — simulated by neutering safe_kill_group so
    # the group stays alive. The pidfile must be KEPT (next boot retries) and an
    # Andon surfaced; it must NOT be counted as reaped.
    p = runid_sleeper(_RUN_ID)
    reap.write_pidfile(
        "stubborn", _RUN_ID, pid=p.pid, pgid=p.pid, wall_clock_secs=900
    )
    monkeypatch.setattr(reap, "safe_kill_group", lambda *a, **k: None)

    reaped = reap.sweep_orphans()

    assert group_alive(p.pid, p.pid)  # still alive (kill was a no-op)
    assert reap.read_pidfile("stubborn") is not None  # pidfile retained
    assert "stubborn" not in [r["worker_id"] for r in reaped]
    # C2b: kill not confirmed — claim no death, write NO receipt.
    assert not paths.event_path("stubborn", _RUN_ID).exists()
    andon = [a for a in captured_andons if a["check"] == "orphan_kill_unconfirmed"]
    assert len(andon) == 1
    assert str(p.pid) in andon[0]["message"]
    assert _RUN_ID in andon[0]["message"]


# ── C2b ruling (a): started_at is a written-but-unread field — removed ───────


def test_pidfile_does_not_carry_started_at():
    """started_at was written by write_pidfile and read by nothing (C2b R1).
    A field nothing consults is a drift magnet: it is gone, and this pin fails
    if it returns — in the signature or in the persisted record."""
    import inspect

    assert "started_at" not in inspect.signature(reap.write_pidfile).parameters
    reap.write_pidfile("nostamp", "r", pid=4242, pgid=4242, wall_clock_secs=900)
    rec = reap.read_pidfile("nostamp")
    assert "started_at" not in rec


def test_sweep_confirmed_dead_writes_reaped_at_restart_receipt(runid_sleeper):
    # A live orphan we can prove is ours, killed at boot -> a unit-attributable
    # receipt so it is countable (reaped_at_restart does NOT count against the
    # retry cap; that is YAML policy P3 rules, not baked into the record).
    from grove.fleet import runner
    p = runid_sleeper("reaprun1")
    runner.write_dispatch_record("reapee", "reaprun1", "unit-reap")
    reap.write_pidfile("reapee", "reaprun1", pid=p.pid, pgid=p.pid, wall_clock_secs=900)
    reaped = reap.sweep_orphans()
    assert "reapee" in [r["worker_id"] for r in reaped]
    _reap_dead(p)
    ev = json.loads(paths.event_path("reapee", "reaprun1").read_text(encoding="utf-8"))
    assert ev["status"] == "failed"
    assert ev["check"] == "reaped_at_restart"
    assert ev["unit_id"] == "unit-reap"

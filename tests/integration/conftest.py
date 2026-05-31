"""Sprint 51 — live CLI integration test fixtures.

Provides three pieces of test-suite infrastructure:

1. **Serial-execution guard.** ``pyproject.toml`` has
   ``addopts = "-n auto"`` which spreads tests across xdist workers by
   default. Live CLI tests share the operator's real ``~/.grove/``
   (config, memory files, telemetry DB, kaizen ledger) and spawn
   subprocesses with the same MCP children — racing them across
   workers corrupts state. ``pytest_collection_modifyitems`` adds
   ``xdist_group("integration_live_cli")`` to every test in this
   directory so they all execute on a single worker, in series.

2. **Pre/post state hygiene.** Before each test: clear stale WAL +
   shm + lock files under ``~/.grove/`` (these can survive a kill -9
   on a prior CLI invocation and block fresh sqlite/file-lock
   acquires). After each test: same cleanup, plus enforce the
   no-orphan invariant.

3. **PID-snapshot orphan assertion.** Sprint 51 GATE-A Phase 1
   guardrail #2: the fixture MUST NOT clobber ``hermes chat``
   processes the operator started outside the test session (e.g. a
   live interactive shell in another terminal). The fixture
   snapshots all ``hermes chat`` PIDs that existed BEFORE the test
   yielded, then asserts only on the DELTA at teardown. Any
   ``hermes chat`` process that appeared during the test and is
   still alive afterwards is OUR orphan and triggers a fatal
   assertion. Sessions started before the test are someone else's
   business and pass through untouched.

   ``notion-mcp-server`` orphans are tracked the same way but
   surfaced as a warning log, not an assertion — per the operator's
   Q1 GATE-A decision, MCP child-process lifecycle is out of scope
   for Sprint 51 (see ``GATE-A § Q1`` in the workstream).
"""

from __future__ import annotations

import glob
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)


# ── per-test timeout override ─────────────────────────────────────────
#
# tests/conftest.py installs an autouse SIGALRM(30s) per test. Live CLI
# tests routinely cross that ceiling: the binary's cold-start (plugin
# discovery + skill sync + provider init) takes 4-8s by itself, a
# T2-Sonnet call adds 2-5s, and PTY tests on top of that need the
# prompt_toolkit Application to actually paint the welcome banner.
# T2's memory round-trip is two full ``hermes chat`` invocations
# back-to-back. SIGALRM at 30s fires mid-PTY-wait and looks like a
# harness bug.
#
# This fixture has the same name as the global one so pytest's
# subdirectory conftest resolution uses ours INSTEAD of the parent's
# for any test under ``tests/integration/``. We raise the ceiling to
# 240s (matches the harness's longest single-call timeout plus
# headroom for the cleanup fixture's pre/post work).

def _integration_timeout_handler(signum, frame):
    raise TimeoutError(
        "live CLI integration test exceeded 240s ceiling — "
        "see grove/eval/integration_runner.py for hard timeouts on each "
        "harness operation"
    )


@pytest.fixture(autouse=True)
def _enforce_test_timeout():
    """Override the global tests/conftest.py SIGALRM(30) for live CLI tests."""
    if sys.platform == "win32":
        yield
        return
    old = signal.signal(signal.SIGALRM, _integration_timeout_handler)
    signal.alarm(240)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)

# Live tests target the operator's real GROVE_HOME, NOT the per-test
# tempdir the global tests/conftest.py installs. The harness in
# ``grove/eval/integration_runner.py`` overrides ``GROVE_HOME`` in the
# subprocess env so the CLI binary picks up the real ``config.yaml``
# and ``.env``. The conftest's pre/post cleanup operates on the same
# real path.
GROVE_HOME = Path(os.path.expanduser("~/.grove"))


# ── serial-execution guard ────────────────────────────────────────────


def pytest_collection_modifyitems(config, items):
    """Force every test in this directory onto a single xdist worker.

    Without this, ``pyproject.toml``'s ``addopts = "-n auto"`` would
    spread the live CLI tests across workers, racing on the shared
    ``~/.grove/`` state and the singleton MCP child processes.
    """
    this_dir = Path(__file__).resolve().parent
    for item in items:
        item_path = Path(str(item.fspath)).resolve()
        try:
            item_path.relative_to(this_dir)
        except ValueError:
            continue
        item.add_marker(pytest.mark.xdist_group("integration_live_cli"))


# ── pgrep helpers ─────────────────────────────────────────────────────


def _pgrep_pids(pattern: str) -> set[str]:
    """Return the set of PIDs whose full command line matches ``pattern``.

    Uses ``pgrep -f`` so we match against the argv string, not just the
    executable basename. Returns ``set()`` on any error so the fixture
    never crashes on a missing pgrep.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    return {p for p in result.stdout.strip().split() if p}


def _kill_pids(pids: set[str], sig: int) -> None:
    """SIGTERM/SIGKILL the entire process group of each pid.

    ``os.killpg`` reaps the wrapper + MCP children together (Sprint 50
    pattern). Tolerates ``ProcessLookupError`` — by the time we kill,
    the pid may already be gone.
    """
    for pid_str in pids:
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            pass


# ── lock / WAL cleanup ────────────────────────────────────────────────


def _clean_locks_and_wals() -> None:
    """Remove stale sqlite WAL/shm + Andon pending + .lock files.

    A SIGKILLed prior run leaves these behind; the next run blocks on
    them. Safe to call when files don't exist (``unlink(missing_ok=True)``).
    """
    if not GROVE_HOME.exists():
        return
    for name in ("telemetry.db-wal", "telemetry.db-shm", "pending_andon"):
        (GROVE_HOME / name).unlink(missing_ok=True)
    for pattern in ("**/*.lock", "**/*.lck"):
        for lock in glob.glob(str(GROVE_HOME / pattern), recursive=True):
            try:
                Path(lock).unlink()
            except OSError:
                pass


# ── the autouse fixture ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _live_cli_state_reset():
    """Per-test pre/post hygiene + orphan-PID delta assertion.

    Sprint 51 Phase 1 guardrail #2: the orphan check uses a pre/post
    PID DELTA, not a blanket ``pkill``. Processes the operator had
    running before the test are not the test suite's concern; only
    ``hermes chat`` invocations that appeared during the test and
    failed to clean themselves up are flagged.
    """
    pre_hermes = _pgrep_pids("hermes chat")
    pre_notion = _pgrep_pids("notion-mcp-server")
    _clean_locks_and_wals()

    yield

    _clean_locks_and_wals()
    post_hermes = _pgrep_pids("hermes chat")
    post_notion = _pgrep_pids("notion-mcp-server")

    # MCP children — log only (out of scope per GATE-A Q1).
    notion_orphans = post_notion - pre_notion
    if notion_orphans:
        logger.warning(
            "live CLI integration test leaked notion-mcp-server "
            "PIDs %s — non-fatal; MCP lifecycle is out of Sprint 51 scope",
            sorted(notion_orphans),
        )

    # hermes chat orphans — fatal. Best-effort reap so subsequent tests
    # aren't poisoned, then assert.
    hermes_orphans = post_hermes - pre_hermes
    if hermes_orphans:
        _kill_pids(hermes_orphans, signal.SIGTERM)
        time.sleep(1.0)
        still_alive = _pgrep_pids("hermes chat") - pre_hermes
        if still_alive:
            _kill_pids(still_alive, signal.SIGKILL)
            time.sleep(0.5)
        final = _pgrep_pids("hermes chat") - pre_hermes
        assert not final, (
            f"Sprint 51 invariant violated: live CLI integration test "
            f"leaked hermes chat PIDs that survived SIGTERM+SIGKILL: "
            f"{sorted(final)}. "
            f"This usually indicates a deadlock in the shutdown path "
            f"(see Sprint 50 worker_threads_lock fix)."
        )
        # Reaped successfully; surface as a warning so reviewers see
        # the test had a shutdown-cleanup defect even though we cleaned up.
        logger.warning(
            "live CLI test created hermes chat orphans %s that did not "
            "self-exit; reaped via SIGTERM",
            sorted(hermes_orphans),
        )

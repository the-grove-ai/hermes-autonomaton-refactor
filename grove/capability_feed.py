"""Unified capability-telemetry feed — GRV-009 E3 (telemetry-convergence-v1).

ONE append-only stream of every TOOL INVOCATION, capability-attributed or not,
so the Skill Flywheel (E7) reads a single feed instead of scattering across the
kaizen ledger, telemetry.db, the intent store, and disclosure logs.

Design (GATE-A locked):
  * Format/location: JSONL append-only at ``<grove home>/.capability_feed/
    feed.jsonl`` (grove home per machine: ``~/.grove`` on Mac,
    ``/mnt/grove-data/.grove`` on the VM). Size-based rotation to
    ``feed-<seq>.jsonl``.
  * Schema: thirteen fields (``FIELDS``). ``capability_id`` is NULLABLE — null
    marks a non-capability invocation (terminal, web_search, …). ``invocation``
    is an explicit kind — ``native`` / ``mcp`` / ``agent-tool`` — NOT derived
    from a name prefix: name-prefix derivation is an implicit convention that
    drifts, and E4 lands MCP invocations in this same stream the Flywheel learns
    from, so the kind is a first-class column.
  * Write contract: EXECUTED-ONLY, written from ``AIAgent._invoke_tool`` (the
    sole per-invocation chokepoint both entrypoints share — concurrent
    ``tool_executor.py:484`` and sequential ``tool_executor.py:885`` both call
    ``side_effects.invoke_tool = self._invoke_tool``). Halted/blocked
    invocations are NOT written here — they remain in the kaizen ``andon_halt``
    event this sprint (first-class feed treatment banked for E7).
  * Carve-out (a): the disclosure-meta pull tools ``read_tool_schema`` /
    ``read_goal_context`` are intercepted before the executor
    (``run_agent._intercept_pull_intents``) and never reach ``_invoke_tool`` —
    so they are excluded from the feed by contract, not by a filter here.
  * Async + durability: a thread-safe ``queue.Queue`` + a single background
    daemon drainer. ``enqueue`` is a near-free ``put`` on the turn path
    (measured sub-microsecond); all file I/O happens off-path on the drainer.
    ``flush()`` drains + fsyncs and is called from the gateway shutdown
    sequence so a clean restart loses nothing. A SIGKILL drops queued-unflushed
    records — accepted and documented.
  * Failure isolation (A7, ABSOLUTE): a write/drain failure logs an error and
    raises a dedicated ``observability_telemetry_failure`` alert with its own
    threshold. It NEVER touches the capability-execution circuit breaker, and
    no exception ever crosses back into the turn — ``enqueue`` swallows
    everything. Telemetry can never affect capability execution.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)
# Dedicated sink for the failure alert — its own logger so an operator/monitor
# can threshold on it independently of normal telemetry noise (A7).
_obs_logger = logging.getLogger("grove.observability")

__all__ = [
    "FIELDS",
    "enqueue",
    "flush",
    "feed_path",
    "feed_dir",
    "reset",
    "utc_now_iso",
]

# The thirteen locked fields, in canonical order.
FIELDS = (
    "ts",
    "session_id",
    "turn_id",
    "capability_id",   # nullable — null = non-capability invocation
    "tool_name",
    "intent_class",
    "tier",
    "zone",
    "invocation",      # explicit kind: native / mcp / agent-tool
    "result_status",
    "cost_usd",        # nullable
    "latency_ms",
    "human_feedback",  # nullable
)

# Size-based rotation threshold for the live window; current + rolled files are
# both read by the parity harness / insights shim.
_MAX_BYTES = 50 * 1024 * 1024

# Consecutive drain failures before the dedicated alert escalates to CRITICAL.
_ALERT_THRESHOLD = 5

# Control sentinels carried on the queue alongside record dicts.
_FLUSH = "__flush__"
_STOP = "__stop__"


def utc_now_iso() -> str:
    """Timezone-aware UTC ISO-8601, matching the kaizen ledger's stamp so the
    parity harness compares like-for-like."""
    return datetime.now(timezone.utc).isoformat()


def feed_dir() -> Path:
    """``<grove home>/.capability_feed`` — resolved fresh so a test that
    redirects ``GROVE_HOME`` is honored without restarting the drainer."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / ".capability_feed"


def feed_path() -> Path:
    return feed_dir() / "feed.jsonl"


class _Feed:
    """Module singleton: an unbounded queue + one daemon drainer thread."""

    def __init__(self) -> None:
        self._q: "queue.Queue[Any]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._consecutive_failures = 0

    # ── turn-path: the only hot call ────────────────────────────────────────
    def enqueue(self, record: Dict[str, Any]) -> None:
        """Near-free, thread-safe, NEVER raises (A7). Starts the drainer on the
        first record so import has no side effects."""
        try:
            self._ensure_thread()
            self._q.put(record)
        except Exception as exc:  # pragma: no cover — put on an unbounded queue
            self._alert("enqueue failed", exc)

    def flush(self, timeout: float = 5.0) -> None:
        """Block until everything queued so far is written + fsynced. Called
        from the gateway shutdown sequence (clean restart loses nothing)."""
        if self._thread is None:
            return
        done = threading.Event()
        try:
            self._q.put((_FLUSH, done))
            done.wait(timeout)
        except Exception as exc:  # pragma: no cover
            self._alert("flush failed", exc)

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            t = threading.Thread(
                target=self._drain_loop, name="capability-feed-drainer", daemon=True
            )
            self._thread = t
            t.start()

    # ── off-path: the drainer owns all file I/O ─────────────────────────────
    def _drain_loop(self) -> None:
        fh = None
        try:
            while True:
                item = self._q.get()
                if isinstance(item, tuple) and item and item[0] in (_FLUSH, _STOP):
                    kind, ev = item
                    fh = self._sync(fh)
                    if isinstance(ev, threading.Event):
                        ev.set()
                    if kind == _STOP:
                        return
                    continue
                fh = self._write_one(fh, item)
        except Exception as exc:  # pragma: no cover — keep the process alive
            self._alert("drain loop crashed", exc)
        finally:
            try:
                if fh is not None:
                    fh.flush()
                    fh.close()
            except Exception:
                pass

    def _open(self):
        d = feed_dir()
        d.mkdir(parents=True, exist_ok=True)
        return open(feed_path(), "a", encoding="utf-8")

    def _write_one(self, fh, record: Dict[str, Any]):
        try:
            if fh is None:
                fh = self._open()
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            fh.flush()
            self._consecutive_failures = 0
            fh = self._maybe_rotate(fh)
        except Exception as exc:
            self._alert("feed write failed", exc)
            try:
                if fh is not None:
                    fh.close()
            except Exception:
                pass
            fh = None  # reopen on the next record
        return fh

    def _sync(self, fh):
        """Flush + fsync the current handle for the durability contract."""
        try:
            if fh is None:
                return None
            fh.flush()
            os.fsync(fh.fileno())
        except Exception as exc:
            self._alert("feed fsync failed", exc)
        return fh

    def _maybe_rotate(self, fh):
        try:
            if fh.tell() < _MAX_BYTES:
                return fh
            fh.flush()
            os.fsync(fh.fileno())
            fh.close()
            base = feed_dir()
            seq = 0
            while (base / f"feed-{seq}.jsonl").exists():
                seq += 1
            feed_path().rename(base / f"feed-{seq}.jsonl")
            return self._open()
        except Exception as exc:
            self._alert("feed rotation failed", exc)
            return fh

    def _alert(self, reason: str, exc: Exception) -> None:
        """Dedicated observability alert. NEVER the capability circuit breaker;
        never re-raises (A7)."""
        self._consecutive_failures += 1
        logger.error("[capability_feed] %s: %r", reason, exc)
        level = logging.CRITICAL if self._consecutive_failures >= _ALERT_THRESHOLD else logging.ERROR
        _obs_logger.log(
            level,
            "observability_telemetry_failure feed=capability_feed reason=%r "
            "consecutive_failures=%d threshold=%d",
            reason, self._consecutive_failures, _ALERT_THRESHOLD,
        )


_feed = _Feed()


def enqueue(record: Dict[str, Any]) -> None:
    _feed.enqueue(record)


def flush(timeout: float = 5.0) -> None:
    _feed.flush(timeout)


def reset() -> None:
    """Test seam: stop the current drainer and replace the singleton so a test
    with a redirected GROVE_HOME starts clean. Not used in production."""
    global _feed
    old = _feed
    try:
        if old._thread is not None and old._thread.is_alive():
            done = threading.Event()
            old._q.put((_STOP, done))
            done.wait(2.0)
    except Exception:
        pass
    _feed = _Feed()

"""Capability-refusals feed — operator-mutable-admission-v1 Phase 3.

A DEDICATED, deterministic JSONL sink for C-SEAM5 execution-admission refusals so
the Skill Flywheel can observe admission friction. Until now a refused tool
emitted NO capability-feed record (the feed is EXECUTED-ONLY by contract,
grove/capability_feed.py) and its detail died in the log — the Flywheel was blind
to the recurring (tool, intent) friction that the Phase 4 ``admission_friction``
producer needs to see.

SEPARATE from the capability feed (GRV-009 E3), by construction (I5): a distinct
directory + file, distinct writer, no shared state. Execution-telemetry consumers
never see refusals; this feed never sees executions.

Path convention MIRRORS the capability feed (grove/capability_feed.py:8-11 and
:102-111): JSONL append-only under ``<grove home>/…`` resolved fresh so a
redirected ``GROVE_HOME`` is honored. Here: ``<grove home>/.capability_refusals/
refusals.jsonl``. ``utc_now_iso`` matches the capability-feed / kaizen-ledger
stamp for like-for-like comparison.

Two deliberate DIVERGENCES from the capability feed:

  * SYNCHRONOUS, not a background drainer. Refusals are rare (only a
    named-but-unoffered tool), so the hot-path async machinery is unwarranted.
  * FAIL-LOUD, not swallow-and-alert. The capability feed's A7 contract swallows
    write failures so telemetry never crosses into the turn. Here the OPPOSITE
    stance is correct at the callsite: a write failure must be a LOUD Andon. But
    the loudness lives in the CALLER (run_agent._emit_capability_refusal), which
    surfaces the failure AND still returns the refusal — governance is never
    downstream of telemetry. This module simply raises on I/O error; it never
    decides the verdict.

Retention/rotation: NOT wired here (see the sprint SPEC note). A rare-event feed
does not yet need rotation; the retention-engine hook is a Phase-5 concern, to be
ruled banked-vs-wired before deploy.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

__all__ = ["refusals_dir", "refusals_path", "emit", "utc_now_iso", "reset"]


def utc_now_iso() -> str:
    """Timezone-aware UTC ISO-8601 — matches capability_feed.utc_now_iso."""
    return datetime.now(timezone.utc).isoformat()


def refusals_dir() -> Path:
    """``<grove home>/.capability_refusals`` — resolved fresh (GROVE_HOME honored)."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / ".capability_refusals"


def refusals_path() -> Path:
    return refusals_dir() / "refusals.jsonl"


def emit(record: Dict[str, Any]) -> None:
    """Append ONE refusal record as a JSONL line — synchronous, fsync'd.

    Adds ``ts`` when absent. RAISES on any I/O error: the caller catches it and
    raises a loud Andon while STILL returning the refusal (this module never
    participates in the admission verdict). Deterministic — a pure function of
    *record* (plus the timestamp)."""
    rec = dict(record)
    rec.setdefault("ts", utc_now_iso())
    d = refusals_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(refusals_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def reset() -> None:
    """Test seam: remove the refusals feed under a redirected GROVE_HOME so a
    test starts clean. Not used in production."""
    d = refusals_dir()
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)

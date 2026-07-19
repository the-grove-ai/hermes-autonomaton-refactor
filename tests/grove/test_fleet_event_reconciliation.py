"""fleet-event-reconciliation-v1 — orphaned terminal events reach the fold.

Pins (the E4 coverage hole, written fresh): reap-across-restart simulation
(event on disk, no handle → reconciled through the live fold, card emitted),
a gated-success event (redraft_count=1) traversing the fold, already-
classified skip (no duplicate card AND no duplicate Andon — the marker's
real job), the >7d trace-only window (ts authoritative, named in the WARNING
summary, no card), meta_defect reconciled events still Andon, reconciled
FAILED events Andon (gate ruling d corollary), content-addressed dedup on a
double-classify (the correctness wall), the live-run ordering pin (a run_id
in self._running is never touched), first-tick-as-boot source labeling +
the RC-2 tick tripwire, and the live-reap path writing the same marker.

GROVE_HOME is per-test isolated (autouse conftest), so the fleet root, the
proposal queue, and the ledger all land in a tempdir.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grove.eval import proposal_queue as pq
from grove.fleet import manager as manager_mod
from grove.fleet import paths as fleet_paths
from grove.fleet.manager import (
    FleetManager,
    _classified_marker_path,
    _event_timestamp,
)


def _write_event(wid="forge", run_id="run-1", ts=None, **over):
    ev = {
        "worker_id": wid, "run_id": run_id,
        "skill": "skill.fleet.forge-jobsearch", "status": "success",
        "detail": "completed=True", "staged": ["x"], "check": None,
        "slug": "260718-acme-pm", "row_id": "pg1", "fit_score": 90,
        "quality_score": None, "rubric_version": None, "redraft_count": None,
        "evaluator_model": None, "meta_defect": None,
        "ts": (ts or datetime.now(timezone.utc)).isoformat(),
    }
    ev.update(over)
    p = fleet_paths.event_path(wid, run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ev), encoding="utf-8")
    return p, ev


@pytest.fixture
def captured(monkeypatch):
    emits, andons = [], []
    monkeypatch.setattr(
        pq, "file_agentless",
        lambda **kw: (emits.append(kw), ("sha256:x", True))[1],
    )
    monkeypatch.setattr(
        manager_mod, "surface_fleet_andon",
        lambda wid, run_id, msg, **kw: andons.append(kw.get("check")),
    )
    return emits, andons


def _mgr():
    return FleetManager(loop=None)


# ── reap-across-restart: the incident shape ─────────────────────────────────


def test_orphaned_success_event_reconciled_to_card(captured):
    emits, andons = captured
    p, _ = _write_event(run_id="cf577af0-sim")
    _mgr()._reconcile_events("boot")
    assert len(emits) == 1
    assert emits[0]["payload"]["slug"] == "260718-acme-pm"
    assert emits[0]["payload"]["row_id"] == "pg1"
    assert andons == []
    assert _classified_marker_path(p).exists()


def test_gated_success_with_redraft_traverses_fold(captured):
    # E4 hole: a redraft_count=1 event (the cf577af0 shape) reaches the fold.
    emits, _ = captured
    _write_event(
        run_id="redraft-sim", quality_score=0.82, rubric_version="1.0",
        redraft_count=1, evaluator_model="m",
    )
    _mgr()._reconcile_events("boot")
    assert len(emits) == 1  # forge payload; the event's riders never block it


def test_already_classified_skipped_no_card_no_andon(captured):
    emits, andons = captured
    p, _ = _write_event(run_id="done-1", meta_defect="missing:row_id")
    _classified_marker_path(p).touch()
    _mgr()._reconcile_events("boot")
    assert emits == [] and andons == []


def test_stale_event_trace_only(captured, caplog):
    emits, _ = captured
    old = datetime.now(timezone.utc) - timedelta(days=8)
    p, _ = _write_event(run_id="stale-1", ts=old)
    with caplog.at_level(logging.WARNING):
        _mgr()._reconcile_events("boot")
    assert emits == []  # no card
    assert _classified_marker_path(p).exists()
    summary = "\n".join(r.getMessage() for r in caplog.records)
    assert "forge/stale-1" in summary and "trace-only" in summary


def test_ts_field_authoritative_over_mtime():
    old = datetime.now(timezone.utc) - timedelta(days=30)
    p, ev = _write_event(run_id="ts-1", ts=old)
    # mtime is NOW, ts says 30d ago — ts wins.
    assert _event_timestamp(ev, p) < datetime.now(timezone.utc) - timedelta(days=29)
    # unparseable ts fails open to mtime (recent → lands in the fold window)
    assert _event_timestamp({"ts": "not-a-date"}, p) > (
        datetime.now(timezone.utc) - timedelta(days=1)
    )


def test_meta_defect_reconciled_event_still_andons(captured):
    emits, andons = captured
    _write_event(run_id="defect-1", meta_defect="missing:company,role,row_id")
    _mgr()._reconcile_events("boot")
    assert andons == ["forge_meta_incomplete"]
    assert len(emits) == 1
    assert emits[0]["payload"]["meta_defect"] == "missing:company,role,row_id"


def test_failed_event_reconciled_andons(captured):
    # Gate ruling (d) corollary: restart-orphaned FAILURES were exactly as
    # invisible as successes. A reconciled failed event Andons like a live reap.
    emits, andons = captured
    p, _ = _write_event(
        run_id="fail-1", status="failed", check="worker_boom",
        detail="exploded",
    )
    _mgr()._reconcile_events("boot")
    assert emits == []
    assert andons == ["worker_boom"]
    assert _classified_marker_path(p).exists()


def test_double_classify_dedups_on_content_address():
    # The correctness wall: NO marker, real queue — classify twice, one card.
    _write_event(run_id="dedup-1")
    m = _mgr()
    m._reconcile_events("boot")
    p, _ = _write_event(run_id="dedup-1")  # rewrite → marker still present? no:
    _classified_marker_path(p).unlink()    # force a re-scan of the same event
    m._reconcile_events("tick")
    live = [r for r in pq.read_all() if r.type == "forge_artifact_pending"]
    assert len(live) == 1  # append dedup'd the second classify


def test_live_run_ordering_pin(captured):
    # A run_id the ticker owns is never touched (gate ruling e condition).
    emits, _ = captured
    p, _ = _write_event(run_id="live-1")
    m = _mgr()
    m._running["forge"] = type(
        "H", (), {"run_id": "live-1", "event_path": p},
    )()
    m._reconcile_events("boot")
    assert emits == []
    assert not _classified_marker_path(p).exists()


def test_first_tick_is_boot_then_tick_tripwire(captured, caplog, monkeypatch):
    emits, _ = captured
    m = _mgr()
    # Neutralize the rest of the tick (no workers configured, no dispatch).
    monkeypatch.setattr(m, "_maybe_dispatch", lambda now: None)
    monkeypatch.setattr(m, "_maybe_emit_publish_digest", lambda: None)
    _write_event(run_id="boot-1")
    with caplog.at_level(logging.WARNING):
        m.tick()
    assert "boot reconciliation" in caplog.text
    assert "RC-2 tripwire" not in caplog.text
    caplog.clear()
    _write_event(run_id="tick-1")
    with caplog.at_level(logging.WARNING):
        m.tick()
    # Second pass is tick-sourced: the RC-2 stall tripwire fires.
    assert "RC-2 tripwire" in caplog.text
    assert "forge/tick-1" in caplog.text


def test_torn_event_marked_and_skipped(captured, caplog):
    emits, andons = captured
    p = fleet_paths.event_path("forge", "torn-1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        _mgr()._reconcile_events("boot")
    assert emits == [] and andons == []
    assert _classified_marker_path(p).exists()
    assert "unreadable orphan event" in caplog.text


def test_live_reap_path_writes_marker(captured, monkeypatch):
    # Gate ruling (a): both paths converge on one legibility story.
    emits, _ = captured
    p, ev = _write_event(run_id="reap-1")

    class _Proc:
        def poll(self):
            return 0

    class _H:
        run_id = "reap-1"
        wall_clock_secs = 900
        event_path = p
        proc = _Proc()

    monkeypatch.setattr(manager_mod, "enforce_wall_clock", lambda h: False)
    monkeypatch.setattr(manager_mod, "remove_pidfile", lambda wid: None)
    m = _mgr()
    m._running["forge"] = _H()
    m._reap_one("forge", m._running["forge"])
    assert len(emits) == 1
    assert _classified_marker_path(p).exists()
    # A follow-up reconcile pass sees the marker and does nothing.
    m._reconcile_events("tick")
    assert len(emits) == 1

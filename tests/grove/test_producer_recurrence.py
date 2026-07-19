"""detector-sweep-resilience-v1 P3 — recurrence detector + pause card.

Pins: the distinct-day predicate (a same-day storm never trips; window
boundary excludes aged failures), already-paused skip, R-7 disposition
suppression within the window + post-expiry re-staging of the SAME
content-addressed id (the A1 proof), approve → sanctioned pause write with
proposal_id provenance → the sweep guard skips the producer end-to-end,
one-card-max idempotence, the renderer/handler census (boot census + verb
affordance), the two push-priority entries (gate ruling b), and the
load-bearing third-sibling ordering (gate ruling c condition): the
recurrence detector runs strictly AFTER both sweep sites at Dispatcher
init, so a same-init failure is visible to the same init's scan.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from grove.eval import proposal_queue as pq
from grove.eval.producer_pauses import set_producer_pause
from grove.eval.producer_recurrence import (
    ProducerResilienceThresholds,
    build_producer_recurrence_proposals,
    load_producer_resilience_thresholds,
)
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_PRODUCER_FAILURE_RECURRENCE as RT,
    compute_proposal_id,
)
from grove.kaizen_ledger import KaizenLedger, default_ledger_dir

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _file_failure(producer, ts, error="RuntimeError('boom')"):
    """Write a producer_failure event with a controlled timestamp."""
    ledger_dir = default_ledger_dir()
    ledger_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "event_type": "producer_failure",
        "session_id": "sweep-test",
        "timestamp": ts.isoformat(),
        "producer": producer,
        "error": error,
    }, sort_keys=True) + "\n"
    with open(ledger_dir / "sweep-test.jsonl", "a", encoding="utf-8") as fh:
        fh.write(line)


def _days_ago(n):
    return NOW - timedelta(days=n)


def _cards():
    return [p for p in pq.read_all() if p.type == RT]


# ── predicate ───────────────────────────────────────────────────────────────


def test_same_day_storm_never_trips():
    for hour in (1, 2, 3, 4, 5):
        _file_failure("freshness_detector", _days_ago(1) + timedelta(hours=hour))
    assert build_producer_recurrence_proposals(now=NOW) == []
    assert _cards() == []


def test_three_distinct_days_stage_card_with_evidence():
    for d in (1, 3, 5):
        _file_failure("freshness_detector", _days_ago(d))
    staged = build_producer_recurrence_proposals(now=NOW)
    assert len(staged) == 1
    (card,) = _cards()
    assert card.payload == {"producer": "freshness_detector"}
    assert card.detail["failure_count"] == 3
    assert len(card.detail["distinct_dates"]) == 3
    assert card.detail["window_days"] == 14
    assert "boom" in card.detail["last_error"]
    assert card.proposer == "producer_recurrence_detector"


def test_window_boundary_excludes_aged_failures():
    # 3 distinct days but the oldest is outside the 14d window → only 2 count.
    for d in (1, 3, 20):
        _file_failure("freshness_detector", _days_ago(d))
    assert build_producer_recurrence_proposals(now=NOW) == []


def test_one_card_max_per_producer_per_run():
    for d in (1, 2, 3):
        _file_failure("freshness_detector", _days_ago(d))
    first = build_producer_recurrence_proposals(now=NOW)
    assert len(first) == 1
    # Re-run against a GROWN ledger: same identity, dedup'd — no second card.
    _file_failure("freshness_detector", _days_ago(4))
    assert build_producer_recurrence_proposals(now=NOW) == []
    assert len(_cards()) == 1


# ── skips ───────────────────────────────────────────────────────────────────


def test_already_paused_producer_skipped():
    set_producer_pause("freshness_detector", True)
    for d in (1, 2, 3):
        _file_failure("freshness_detector", _days_ago(d))
    assert build_producer_recurrence_proposals(now=NOW) == []


def test_disposition_suppresses_in_window_then_expires():
    # The A1 proof: rejected in-window → no re-stage; post-window → the SAME
    # content-addressed id stages again (no-tombstone queue).
    for d in (1, 2, 3):
        _file_failure("freshness_detector", _days_ago(d))
    pid = compute_proposal_id(
        type=RT, payload={"producer": "freshness_detector"},
        evidence=("freshness_detector",),
    )
    # In-window rejection disposition.
    KaizenLedger(session_id="disp-test").record(
        "kaizen_disposition", proposal_id=pid, proposal_type=RT,
        disposition="rejected",
    )
    assert build_producer_recurrence_proposals(now=NOW) == []
    # Post-expiry: the disposition ts falls outside a window that starts
    # LATER (simulate by moving `now` forward past the window).
    later = NOW + timedelta(days=15)
    for d in (1, 2, 3):
        _file_failure("freshness_detector", later - timedelta(days=d))
    staged = build_producer_recurrence_proposals(now=later)
    assert staged == [pid]  # SAME identity re-staged


# ── approve → pause → sweep skip (end-to-end) ───────────────────────────────


def test_approve_writes_pause_with_provenance_then_sweep_skips(
    monkeypatch, tmp_path, caplog
):
    import logging

    from grove.flywheel_cli import _approve_producer_pause
    from tests.grove.test_detector_sweep_resilience import _shell, _stub_sweep

    for d in (1, 2, 3):
        _file_failure("dock_mutation_detector", _days_ago(d))
    (pid,) = build_producer_recurrence_proposals(now=NOW)
    (card,) = _cards()

    # Stub FIRST (repoints GROVE_HOME at tmp_path), approve THROUGH it so the
    # pause write and the sweep's read agree on the path.
    calls: list = []
    _stub_sweep(monkeypatch, tmp_path, calls)
    target, applied = _approve_producer_pause(card)
    assert target == "dock_mutation_detector"
    assert applied == {"producer": "dock_mutation_detector", "paused": True,
                       "status": "applied"}
    # Provenance landed in the pause file.
    import yaml

    data = yaml.safe_load(
        (tmp_path / "flywheel" / "producer_pauses.yaml").read_text(
            encoding="utf-8"
        )
    )
    entry = data["producers"]["dock_mutation_detector"]
    assert entry["proposal_id"] == pid
    assert "3 distinct day(s)" in entry["reason"]

    with caplog.at_level(logging.INFO):
        _shell()._extract_memory_from_dormant_sessions(["sess-1"])
    assert "dock_mutation" not in calls  # guard skipped it
    assert "paused by operator" in caplog.text


# ── thresholds loader ───────────────────────────────────────────────────────


def test_thresholds_defaults_and_validation(tmp_path):
    assert load_producer_resilience_thresholds(
        tmp_path / "absent.yaml"
    ) == ProducerResilienceThresholds(distinct_days=3, window_days=14)
    cfg = tmp_path / "flywheel.config.yaml"
    cfg.write_text("producer_resilience:\n  distinct_days: 1\n",
                   encoding="utf-8")
    assert load_producer_resilience_thresholds(cfg).distinct_days == 1
    cfg.write_text("producer_resilience:\n  distinct_days: 0\n",
                   encoding="utf-8")
    with pytest.raises(ValueError):
        load_producer_resilience_thresholds(cfg)


# ── registry census + priority (gate ruling b) ─────────────────────────────


def test_handlers_row_census_and_no_reject_callback():
    from grove.flywheel_cli import PROPOSAL_HANDLERS, RENDER_REGISTRY

    row = PROPOSAL_HANDLERS[RT]
    assert callable(row.summary_renderer) and callable(row.diff_renderer)
    assert callable(row.apply_callback)
    assert row.reject_callback is None  # disposition-only (R-7)
    assert RT in RENDER_REGISTRY  # boot census seeded it


def test_push_priority_entries():
    from grove.eval.proposal_queue import PROPOSAL_TYPE_EXPLORATION_NUDGE
    from grove.flywheel_cli import _PUSH_PRIORITY

    # Incident family: shares the 0.5 slot (gate ruling b).
    assert _PUSH_PRIORITY[RT] == 0.5
    # R-8 fold: exploration_nudge explicit beside model_binding's family.
    assert _PUSH_PRIORITY[PROPOSAL_TYPE_EXPLORATION_NUDGE] == 3


def test_card_copy_names_pause_semantics():
    from grove.kaizen.rendering import _summary_producer_recurrence

    line = _summary_producer_recurrence(SimpleNamespace(
        payload={"producer": "freshness_detector"},
        detail={"failure_count": 4, "distinct_dates": ["2026-07-16",
                "2026-07-17", "2026-07-19"], "window_days": 14,
                "last_error": "RuntimeError('boom')"},
    ))
    assert "freshness_detector" in line
    assert "producer_pauses.yaml" in line          # approve semantics
    assert "reject suppresses" in line             # reject semantics


# ── third-sibling ordering (gate ruling c condition) ───────────────────────


def test_recurrence_sibling_runs_last(monkeypatch, tmp_path):
    # ORDERING IS LOAD-BEARING: the sweeps file producer_failure
    # synchronously, so the recurrence scan must run AFTER both to see the
    # same init's failures. A reorder silently degrades same-init detection
    # to next-init detection — this pin fails it loud.
    from grove.dispatcher import Dispatcher
    from grove.intent_store import IntentStore

    order: list = []
    monkeypatch.setattr(
        "grove.memory.lifecycle.dormant_session_ids",
        lambda store, minutes=30: ["sess-x"],
    )
    monkeypatch.setattr(
        Dispatcher, "_extract_memory_from_dormant_sessions",
        lambda self, sids: order.append("memory_sweep"),
    )
    monkeypatch.setattr(
        "grove.dock.attachment.run_goal_attachment_sweep",
        lambda: order.append("goal_attachment"),
    )
    monkeypatch.setattr(
        "grove.eval.producer_recurrence.build_producer_recurrence_proposals",
        lambda: order.append("recurrence"),
    )
    store = IntentStore(store_path=tmp_path / "records.jsonl")
    Dispatcher(intent_store=store, session_db=SimpleNamespace())
    assert order == ["memory_sweep", "goal_attachment", "recurrence"]

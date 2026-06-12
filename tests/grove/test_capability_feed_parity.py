"""Tests for the feed<->ledger parity harness (GRV-009 E3 C2).

Synthetic fixtures exercise the locked normalization rules: matched
invocations, real mismatches, multiset counts, carve-out exclusion, timestamp
skew, halted-tool absence, and restart-boundary classification.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grove import capability_feed_parity as P

T0 = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)


def _ts(dt: datetime) -> str:
    return dt.isoformat()


def _write_feed(d: Path, rows):
    d.mkdir(parents=True, exist_ok=True)
    with (d / "feed.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _write_ledger(d: Path, session_id: str, events):
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{session_id}.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def _feed_row(session, tool, ts, turn="s1#1"):
    return {"ts": _ts(ts), "session_id": session, "turn_id": turn,
            "tool_name": tool, "invocation": "native", "result_status": "ok"}


def _batch(session, tools, ts):
    return {"event_type": "tool_batch_executed", "session_id": session,
            "timestamp": _ts(ts), "batch_size": len(tools),
            "intents": [{"call_id": f"c{i}", "tool_name": t} for i, t in enumerate(tools)]}


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "feed", tmp_path / "ledger"


def test_matched_invocations(dirs):
    feed_d, led_d = dirs
    _write_feed(feed_d, [_feed_row("s1", "calendar_list", T0 + timedelta(minutes=1)),
                         _feed_row("s1", "terminal", T0 + timedelta(minutes=2))])
    _write_ledger(led_d, "s1", [_batch("s1", ["calendar_list", "terminal"], T0 + timedelta(minutes=1))])
    rep = P.run_parity(T0, T1, feed_dir=feed_d, ledger_dir=led_d)
    assert rep.matched == 2
    assert rep.real_mismatches == []
    assert rep.match_rate == 1.0


def test_real_mismatch_both_directions(dirs):
    feed_d, led_d = dirs
    _write_feed(feed_d, [_feed_row("s1", "calendar_list", T0 + timedelta(minutes=1)),
                         _feed_row("s1", "web_search", T0 + timedelta(minutes=3))])   # feed-only
    _write_ledger(led_d, "s1", [_batch("s1", ["calendar_list", "drive_search"], T0 + timedelta(minutes=1))])  # drive_search ledger-only
    rep = P.run_parity(T0, T1, feed_dir=feed_d, ledger_dir=led_d)
    assert rep.matched == 1
    feed_only = {m["tool_name"] for m in rep.feed_only}
    ledger_only = {m["tool_name"] for m in rep.ledger_only}
    assert feed_only == {"web_search"}
    assert ledger_only == {"drive_search"}
    assert len(rep.real_mismatches) == 2


def test_multiset_count_mismatch(dirs):
    feed_d, led_d = dirs
    _write_feed(feed_d, [_feed_row("s1", "calendar_list", T0 + timedelta(minutes=1)),
                         _feed_row("s1", "calendar_list", T0 + timedelta(minutes=2))])  # x2
    _write_ledger(led_d, "s1", [_batch("s1", ["calendar_list"], T0 + timedelta(minutes=1))])  # x1
    rep = P.run_parity(T0, T1, feed_dir=feed_d, ledger_dir=led_d)
    assert rep.matched == 1
    assert [m["tool_name"] for m in rep.feed_only] == ["calendar_list"]
    assert rep.real_mismatches  # the extra feed calendar_list is a real diff


def test_carve_out_pull_tools_excluded(dirs):
    feed_d, led_d = dirs
    _write_feed(feed_d, [_feed_row("s1", "read_tool_schema", T0 + timedelta(minutes=1)),
                         _feed_row("s1", "calendar_list", T0 + timedelta(minutes=2))])
    _write_ledger(led_d, "s1", [_batch("s1", ["read_goal_context", "calendar_list"], T0 + timedelta(minutes=2))])
    rep = P.run_parity(T0, T1, feed_dir=feed_d, ledger_dir=led_d)
    assert rep.matched == 1   # only calendar_list; pull-meta excluded both sides
    assert rep.real_mismatches == []


def test_timestamp_skew_boundary_not_one_sided(dirs):
    feed_d, led_d = dirs
    # Feed enqueued 1s AFTER the window end (invoke-start), ledger at window end.
    _write_feed(feed_d, [_feed_row("s1", "calendar_list", T1 + timedelta(seconds=1))])
    _write_ledger(led_d, "s1", [_batch("s1", ["calendar_list"], T1)])
    rep = P.run_parity(T0, T1, feed_dir=feed_d, ledger_dir=led_d)
    assert rep.matched == 1            # skew grace keeps both in-window
    assert rep.real_mismatches == []


def test_halted_tool_not_compared(dirs):
    feed_d, led_d = dirs
    # Halted invocation lives in andon_halt, NOT tool_batch_executed; the feed
    # (executed-only) never recorded it. The harness must not flag it.
    _write_feed(feed_d, [_feed_row("s1", "calendar_list", T0 + timedelta(minutes=1))])
    _write_ledger(led_d, "s1", [
        {"event_type": "andon_halt", "session_id": "s1", "timestamp": _ts(T0 + timedelta(minutes=1)),
         "intents": [{"call_id": "c0", "tool_name": "terminal"}]},
        _batch("s1", ["calendar_list"], T0 + timedelta(minutes=1)),
    ])
    rep = P.run_parity(T0, T1, feed_dir=feed_d, ledger_dir=led_d)
    assert rep.matched == 1
    assert rep.real_mismatches == []   # terminal (halted) ignored


def test_restart_boundary_classifies_explainable(dirs):
    feed_d, led_d = dirs
    boundary = T0 + timedelta(minutes=30)
    # Ledger recorded a tool the feed lost to a SIGKILL at the boundary.
    _write_feed(feed_d, [_feed_row("s1", "calendar_list", T0 + timedelta(minutes=1))])
    _write_ledger(led_d, "s1", [
        _batch("s1", ["calendar_list"], T0 + timedelta(minutes=1)),
        _batch("s1", ["gmail_send"], boundary),   # lost from feed at SIGKILL
    ])
    rep = P.run_parity(T0, T1, feed_dir=feed_d, ledger_dir=led_d, restart_boundaries=[boundary])
    assert rep.matched == 1
    assert [m["tool_name"] for m in rep.ledger_only] == ["gmail_send"]
    assert rep.ledger_only[0]["classification"] == "restart-explainable"
    assert rep.real_mismatches == []   # explained by the restart, not a real miss

"""operator-mutable-admission-v1 Phase 3 — the capability_refusals feed.

A dedicated, deterministic JSONL sink for C-SEAM5 execution-admission refusals so
the Flywheel can observe admission friction (previously refusals emitted NOTHING
to telemetry — detail died in the log). Pins:

  I5 — SEPARATE from the capability feed: a refusal writes the refusals sink and
       NOT the capability feed; the two sinks are structurally distinct paths.
  GOVERNANCE-NOT-DOWNSTREAM-OF-TELEMETRY — a refusals-write failure is a loud
       Andon but NEVER flips the refuse verdict and NEVER swallows the refusal.
  DETERMINISTIC — pure function of inputs (mirror of _seam5_refusal_message); no
       LLM in the emit path; repeat refusals are byte-identical modulo timestamp.
"""
from __future__ import annotations

import json
import logging

import pytest

import grove.capability_feed as capfeed
import grove.capability_refusals as refusals
import grove.providers as P
import run_agent


@pytest.fixture(autouse=True)
def _clean_feeds():
    refusals.reset()
    capfeed.reset()
    yield
    refusals.reset()
    capfeed.reset()


def _agent(offered, session_id="s1", platform="cli"):
    a = object.__new__(run_agent.AIAgent)
    a.tools = [
        {"type": "function", "function": {"name": n}}
        for n in ("read_file", "write_file", "web_search")
    ]
    a._tools_for_turn = [
        {"type": "function", "function": {"name": n}} for n in offered
    ]
    a.session_id = session_id
    a.platform = platform
    return a


def _set_class(monkeypatch, intent, tier):
    monkeypatch.setattr(
        P, "current_classification",
        lambda: type("C", (), {"intent_class": intent})(),
    )
    monkeypatch.setattr(P, "current_tier", lambda: tier)


def _records():
    return [json.loads(ln) for ln in refusals.refusals_path().read_text().splitlines()]


# ── exactly one record, all fields ─────────────────────────────────────────

def test_refusal_writes_exactly_one_record_with_all_fields(monkeypatch):
    _set_class(monkeypatch, "creative_writing", "T2")
    a = _agent(offered=["read_file"])
    payload = a._seam5_admission_refusal("write_file")     # unoffered → refused

    assert json.loads(payload)["andon"] == "execution_admission"   # verdict returned
    recs = _records()
    assert len(recs) == 1, "exactly one refusals record per refusal"
    r = recs[0]
    assert r["tool"] == "write_file"
    assert r["intent"] == "creative_writing"
    assert r["tier"] == "T2"
    assert r["reason"] == "not in the per-turn offered surface"
    assert r["governing_record"] == "write_file"
    assert r["session_id"] == "s1"
    assert r["platform"] == "cli"
    assert r.get("ts")


# ── I5: separate from the capability feed ──────────────────────────────────

def test_refusal_does_not_touch_capability_feed_and_sinks_are_separate(monkeypatch):
    _set_class(monkeypatch, "creative_writing", "T2")
    a = _agent(offered=["read_file"])
    a._seam5_admission_refusal("write_file")

    assert refusals.refusals_path().exists()            # refusals feed wrote
    assert not capfeed.feed_path().exists()             # capability feed untouched
    # structurally distinct sinks (no shared path/dir)
    assert refusals.refusals_path() != capfeed.feed_path()
    assert refusals.refusals_dir() != capfeed.feed_dir()


# ── governance is not downstream of telemetry ──────────────────────────────

def test_write_failure_verdict_byte_identical_and_loud_andon(monkeypatch, caplog):
    # This is the one assertion that separates a telemetry feed from a
    # gate-opener: the admission verdict must be TOTALLY independent of the
    # refusals-write outcome.
    _set_class(monkeypatch, "creative_writing", "T2")

    # Baseline: the refusals write SUCCEEDS — capture the exact verdict bytes.
    payload_ok = _agent(offered=["read_file"])._seam5_admission_refusal("write_file")
    assert payload_ok is not None

    # Now force the refusals JSONL write to THROW.
    def _boom(record):
        raise OSError("disk full")

    monkeypatch.setattr(refusals, "emit", _boom)
    with caplog.at_level(logging.CRITICAL):
        payload_fail = _agent(offered=["read_file"])._seam5_admission_refusal("write_file")

    # (a) the tool stays REFUSED when the write fails (non-None, andon verdict).
    assert payload_fail is not None
    assert json.loads(payload_fail)["andon"] == "execution_admission"

    # (b) the refusal verdict is BYTE-FOR-BYTE unchanged vs the write-succeeds case.
    assert payload_fail == payload_ok

    # (c) the write failure fails LOUD (Andon), not swallowed.
    assert any(
        "refusals" in r.message.lower() and r.levelno >= logging.CRITICAL
        for r in caplog.records
    ), "a loud Andon must fire on a refusals-write failure"


def test_offered_tool_is_not_refused_and_emits_nothing(monkeypatch):
    _set_class(monkeypatch, "creative_writing", "T2")
    a = _agent(offered=["read_file", "write_file"])
    assert a._seam5_admission_refusal("write_file") is None   # offered → admitted
    assert not refusals.refusals_path().exists()              # no refusal → no record


# ── deterministic (no LLM) ─────────────────────────────────────────────────

def test_emit_is_deterministic_modulo_timestamp(monkeypatch):
    _set_class(monkeypatch, "creative_writing", "T2")
    a = _agent(offered=["read_file"])
    a._seam5_admission_refusal("write_file")
    a._seam5_admission_refusal("write_file")
    recs = _records()
    assert len(recs) == 2
    for r in recs:
        r.pop("ts")
    assert recs[0] == recs[1], "emit must be a pure function of inputs (no LLM)"

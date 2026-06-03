"""Sprint 48 Phase 4 — T0 pattern compiler verification (scanner, compiler,
proposal flow, threshold configurability, rejection) + T20 integration."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import grove.pattern_cache as pc
from grove.pattern_cache import PatternCacheStore, STATUS_ACTIVE, STATUS_REJECTED
from grove.intent_store import IntentRecord, IntentStore
from grove.eval.pattern_compiler import (
    Candidate,
    compile_candidate,
    propose_pattern_promotions,
    scan_candidates,
)
from grove.eval.proposal_queue import PROPOSAL_TYPE_PATTERN_PROMOTION, read_all

_CFG = {
    "enabled": True, "min_repetitions": 5, "within_days": 14,
    "max_rejections": 0, "max_response_variance": 0,
    "exclude_intents": ["unknown", "system_admin"],
}


def _store(tmp_path) -> IntentStore:
    return IntentStore(store_path=tmp_path / "intent.jsonl")


def _seed(store, msg, intent, n, *, response="The answer.", tool_inv=None,
          outcomes=None, hours_apart=1):
    now = datetime.now(timezone.utc)
    for i in range(n):
        ts = (now - timedelta(hours=(n - i) * hours_apart)).isoformat()
        outcome = (outcomes[i] if outcomes else "success")
        store.append(IntentRecord(
            timestamp=ts, session_id="s", turn_id=f"{intent}-{msg[:4]}-{i}",
            user_message_stem=msg, pattern_hash="h", intent_class=intent,
            register_class="r", complexity_signal="simple", confidence=0.95,
            outcome=outcome, response_content=response, tool_invocation=tool_inv,
        ))


# ── scanner ────────────────────────────────────────────────────────────


def test_enough_evidence_yields_candidate(tmp_path):
    s = _store(tmp_path)
    _seed(s, "what is our mission", "factual_lookup", 5)
    cands = scan_candidates(s, _CFG)
    assert len(cands) == 1
    assert cands[0].intent_class == "factual_lookup"
    assert cands[0].cacheable_type == "static"
    assert cands[0].repetition_count == 5


def test_not_enough_evidence_no_candidate(tmp_path):
    s = _store(tmp_path)
    _seed(s, "what is our mission", "factual_lookup", 4)  # below min_repetitions
    assert scan_candidates(s, _CFG) == []


def test_excluded_intents_dropped(tmp_path):
    s = _store(tmp_path)
    _seed(s, "http://localhost/?state=x", "system_admin", 11)  # OAuth-callback noise
    assert scan_candidates(s, _CFG) == []


def test_correction_disqualifies(tmp_path):
    s = _store(tmp_path)
    _seed(s, "what is our mission", "factual_lookup", 5,
          outcomes=["success", "success", "success", "success", "correction"])
    assert scan_candidates(s, _CFG) == []


def test_threshold_configurability(tmp_path):
    s = _store(tmp_path)
    _seed(s, "what is our mission", "factual_lookup", 4)
    assert scan_candidates(s, _CFG) == []                 # default min 5
    loose = dict(_CFG, min_repetitions=3)
    assert len(scan_candidates(s, loose)) == 1            # custom threshold → candidate


def test_executable_cacheable_type_inferred(tmp_path):
    s = _store(tmp_path)
    _seed(s, "what's on my calendar", "scheduling", 5,
          tool_inv=json.dumps({"tool": "calendar_read", "args": {"date": "today"}}))
    cands = scan_candidates(s, _CFG)
    assert len(cands) == 1 and cands[0].cacheable_type == "executable"


# ── compiler ───────────────────────────────────────────────────────────


def _candidate(turn_ids, intent="factual_lookup", ctype="static"):
    return Candidate(
        t0_key="sha256:k", intent_class=intent, cacheable_type=ctype,
        repetition_count=len(turn_ids), time_span_days=1.0, rejection_count=0,
        sample_queries=("q",), evidence_turn_ids=tuple(turn_ids),
    )


def _rec(tid, resp=None, inv=None):
    return IntentRecord(
        timestamp="2026-06-01T00:00:00+00:00", session_id="s", turn_id=tid,
        user_message_stem="q", pattern_hash="h", intent_class="factual_lookup",
        register_class="r", complexity_signal="simple", confidence=0.9,
        outcome="success", response_content=resp, tool_invocation=inv,
    )


def test_compile_static_identical_responses(tmp_path):
    cand = _candidate(["a", "b", "c"])
    ev = [_rec("a", "X"), _rec("b", "X"), _rec("c", "X")]
    cp = compile_candidate(cand, ev)
    assert cp is not None and cp.cached_response == "X" and cp.cacheable_type == "static"


def test_compile_static_variance_blocks(tmp_path):
    cand = _candidate(["a", "b"])
    assert compile_candidate(cand, [_rec("a", "X"), _rec("b", "Y")]) is None


def test_compile_static_legacy_records_blocked(tmp_path):
    cand = _candidate(["a", "b"])
    assert compile_candidate(cand, [_rec("a", None), _rec("b", None)]) is None


def test_compile_executable_same_tool(tmp_path):
    cand = _candidate(["a", "b"], intent="scheduling", ctype="executable")
    inv = json.dumps({"tool": "calendar_read", "args": {"d": "today"}})
    cp = compile_candidate(cand, [_rec("a", inv=inv), _rec("b", inv=inv)])
    assert cp is not None and json.loads(cp.compiled_invocation)["tool"] == "calendar_read"


def test_compile_executable_tool_drift_blocks(tmp_path):
    cand = _candidate(["a", "b"], intent="scheduling", ctype="executable")
    cp = compile_candidate(cand, [
        _rec("a", inv=json.dumps({"tool": "calendar_read", "args": {}})),
        _rec("b", inv=json.dumps({"tool": "terminal", "args": {}})),
    ])
    assert cp is None


# ── proposal flow + T20 ────────────────────────────────────────────────


def test_T20_full_lifecycle(tmp_path, monkeypatch):
    """Seed → scan → propose → approve → pattern active at T0; re-scan skips."""
    import grove.flywheel_cli as fc

    s = _store(tmp_path)
    _seed(s, "what is our mission statement", "factual_lookup", 6,
          response="To build sovereign reference software.")
    db = tmp_path / "pattern_cache.db"
    monkeypatch.setattr(pc, "default_pattern_cache_path", lambda: db)
    pstore = PatternCacheStore(db)
    qpath = tmp_path / "proposals.jsonl"

    # scan finds the candidate
    assert len(scan_candidates(s, _CFG)) == 1

    # propose (Sprint 56: returns a PromotionResult; .proposed is the id list)
    queued = propose_pattern_promotions(s, pstore, queue_path=qpath, config=_CFG).proposed
    assert len(queued) == 1
    props = read_all(path=qpath)
    assert props[0].type == PROPOSAL_TYPE_PATTERN_PROMOTION
    pid = props[0].payload["pattern_id"]
    assert pstore.get(pid).status == "suspended"

    # approve → active, queue emptied
    short = props[0].proposal_id.split(":")[-1][:12]
    assert fc.cli_approve(short, queue_path=qpath) == 0
    promoted = pstore.get(pid)
    assert promoted.status == STATUS_ACTIVE
    assert promoted.cached_response == "To build sovereign reference software."
    assert pstore.get_active(pid) is not None
    assert read_all(path=qpath) == []

    # re-propose skips the now-active pattern
    assert propose_pattern_promotions(s, pstore, queue_path=qpath, config=_CFG).proposed == []


def test_rejected_pattern_never_reproposed(tmp_path, monkeypatch):
    import grove.flywheel_cli as fc

    s = _store(tmp_path)
    _seed(s, "what is our mission", "factual_lookup", 5, response="X")
    db = tmp_path / "pattern_cache.db"
    monkeypatch.setattr(pc, "default_pattern_cache_path", lambda: db)
    pstore = PatternCacheStore(db)
    qpath = tmp_path / "proposals.jsonl"

    propose_pattern_promotions(s, pstore, queue_path=qpath, config=_CFG)
    p = read_all(path=qpath)[0]
    pid = p.payload["pattern_id"]

    fc.cli_reject(p.proposal_id.split(":")[-1][:12], reason="not stable", queue_path=qpath)
    assert pstore.get(pid).status == STATUS_REJECTED
    # never re-proposed
    assert propose_pattern_promotions(s, pstore, queue_path=qpath, config=_CFG).proposed == []
    assert read_all(path=qpath) == []

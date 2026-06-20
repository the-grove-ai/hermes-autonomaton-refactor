"""Phase 3 tests — memory_context proposal type + Kaizen digest engine.

The digest engine (MemoryProposalHandler + run_digest) is the
approval-application layer: it renders staged proposals, applies the
operator's per-proposal decision (approve → MemoryStore event; reject →
status flip), and records a kaizen_disposition ledger event for each
action. The decision source is injected via the ``decide`` callback so the
engine is surface-agnostic (TTY, conversational push, or test stub).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grove.memory.digest import MemoryProposalHandler, run_digest
from grove.memory.store import MemoryStore


@pytest.fixture()
def store(tmp_path):
    return MemoryStore(base_dir=tmp_path)


def _create_proposal(content="A fact.", entity_type="DomainFact", confidence=0.9,
                     dock_goal_ref=None):
    return {
        "action": "create",
        "target_id": None,
        "dock_goal_ref": dock_goal_ref,
        "proposed_record": {
            "entity_type": entity_type,
            "content": content,
            "confidence": confidence,
            "justification": "why it matters",
        },
    }


def _stage(path: Path, session_id: str, proposal: dict):
    rec = {"session_id": session_id, "status": "pending",
           "timestamp": "2026-06-01T00:00:00+00:00", "proposal": proposal}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _records(path: Path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _dispositions(ledger_dir: Path):
    out = []
    for p in Path(ledger_dir).glob("*.jsonl"):
        for ln in p.read_text().splitlines():
            if ln.strip():
                ev = json.loads(ln)
                if ev.get("event_type") == "kaizen_disposition":
                    out.append(ev)
    return out


# 1. Proposal surfacing — rendered through the handler

def test_surfacing_renders_through_handler(store, tmp_path):
    ppath = tmp_path / "memory_proposals.jsonl"
    prop = _create_proposal(content="Notion is the tracker.")
    _stage(ppath, "s1", prop)

    seen = []

    def decide(summary, proposal):
        seen.append(summary)
        return "defer"

    run_digest(store=store, proposals_path=ppath, decide=decide,
               ledger_dir=tmp_path / "led")

    handler = MemoryProposalHandler(store)
    assert seen == [handler.summary_renderer(prop)]
    assert "[create] DomainFact: Notion is the tracker. (Confidence: 0.9)" in seen[0]


# 2. Approve — MemoryCreated event in store, index updated

def test_approve_creates_record(store, tmp_path):
    ppath = tmp_path / "memory_proposals.jsonl"
    _stage(ppath, "s1", _create_proposal(content="Approved fact."))

    run_digest(store=store, proposals_path=ppath,
               decide=lambda s, p: "approve", ledger_dir=tmp_path / "led")

    active = [r for r in store.projected_records().values() if r.status == "active"]
    assert any(r.content == "Approved fact." for r in active)
    # proposal record flipped to approved
    rec = [r for r in _records(ppath) if r.get("proposal")][0]
    assert rec["status"] == "approved"


# 3. Reject — status=rejected, disposition recorded

def test_reject_marks_and_records(store, tmp_path):
    ppath = tmp_path / "memory_proposals.jsonl"
    led = tmp_path / "led"
    _stage(ppath, "s1", _create_proposal(content="Rejected fact."))

    run_digest(store=store, proposals_path=ppath,
               decide=lambda s, p: "reject", ledger_dir=led)

    rec = [r for r in _records(ppath) if r.get("proposal")][0]
    assert rec["status"] == "rejected"
    # nothing applied to the store
    assert all(r.content != "Rejected fact."
               for r in store.projected_records().values())
    disp = _dispositions(led)
    assert any(d["disposition"] == "rejected"
               and d["proposal_type"] == "memory_context" for d in disp)


# 4. Zero proposals — silent passthrough, no handler invocation

def test_zero_proposals_no_decide_call(store, tmp_path):
    ppath = tmp_path / "memory_proposals.jsonl"  # never created
    called = []

    def decide(summary, proposal):
        called.append(1)
        return "approve"

    result = run_digest(store=store, proposals_path=ppath, decide=decide,
                        ledger_dir=tmp_path / "led")
    assert called == []
    assert result == {"approved": 0, "rejected": 0, "deferred": 0}


def test_processing_lock_only_no_decide_call(store, tmp_path):
    ppath = tmp_path / "memory_proposals.jsonl"
    # a bare processing lock (no "proposal" key) must not be surfaced
    with open(ppath, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"session_id": "s1", "status": "processing",
                             "timestamp": "2026-06-01T00:00:00+00:00"}) + "\n")
    called = []
    run_digest(store=store, proposals_path=ppath,
               decide=lambda s, p: called.append(1) or "approve",
               ledger_dir=tmp_path / "led")
    assert called == []


# 5. Disposition recording on both approve and reject

def test_disposition_recorded_both(store, tmp_path):
    ppath = tmp_path / "memory_proposals.jsonl"
    led = tmp_path / "led"
    _stage(ppath, "s1", _create_proposal(content="First."))
    _stage(ppath, "s2", _create_proposal(content="Second."))

    decisions = iter(["approve", "reject"])
    run_digest(store=store, proposals_path=ppath,
               decide=lambda s, p: next(decisions), ledger_dir=led)

    kinds = sorted(d["disposition"] for d in _dispositions(led))
    assert kinds == ["applied", "rejected"]


# 6. Supersede approval — MemorySuperseded event, old record superseded

def test_supersede_approval(store, tmp_path):
    handler = MemoryProposalHandler(store)
    handler.apply(_create_proposal(content="Old head."))
    old_id = next(r.id for r in store.projected_records().values()
                  if r.content == "Old head.")

    ppath = tmp_path / "memory_proposals.jsonl"
    sup = {
        "action": "supersede",
        "target_id": old_id,
        "dock_goal_ref": None,
        "proposed_record": {"entity_type": "DomainFact", "content": "New head.",
                            "confidence": 0.95, "justification": "newer"},
    }
    _stage(ppath, "s1", sup)

    run_digest(store=store, proposals_path=ppath,
               decide=lambda s, p: "approve", ledger_dir=tmp_path / "led")

    idx = store.projected_records()
    assert idx[old_id].status == "superseded"
    assert any(r.content == "New head." and r.status == "active"
               for r in idx.values())


# Fail-loud write-boundary: supersede naming a missing target raises (the
# Phase 1 commitment — validate before the immutable append).

def test_supersede_missing_target_raises(store):
    handler = MemoryProposalHandler(store)
    with pytest.raises(ValueError):
        handler.apply({
            "action": "supersede", "target_id": "mem_nonexistent",
            "dock_goal_ref": None,
            "proposed_record": {"entity_type": "DomainFact", "content": "X.",
                                "confidence": 0.9, "justification": "j"},
        })
    # nothing was appended to the event log
    assert list(store.read_events()) == []

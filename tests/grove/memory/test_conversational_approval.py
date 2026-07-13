"""Phase 3.2 — unified conversational approval across routing + memory.

The three flywheel governance tools (review_proposals / approve_proposal /
reject_proposal) become store-aware: one Kaizen voice, both proposal stores.
Routing path stays byte-for-byte unchanged; memory ids route to the
self-contained memory CLI apply path via probe-in-order resolution.

Tests rely on the conftest-isolated GROVE_HOME: routing proposals stage to
$GROVE_HOME/proposals.jsonl, memory proposals to
$GROVE_HOME/memory_proposals.jsonl, both read through the tools' defaults.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_constants import get_hermes_home

from grove.eval.proposal_queue import (
    RoutingProposal,
    append as queue_append,
    compute_proposal_id,
)
from grove.memory.cli import memory_proposal_short_id
from grove.memory.store import MemoryStore
from tools.flywheel_review_tool import (
    APPROVE_PROPOSAL_SCHEMA,
    approve_proposal,
    reject_proposal,
    review_proposals,
)

_TS = "2026-06-01T00:00:00+00:00"


def _stage_routing(intent="code_generation"):
    payload = {"rule": "upward", "add_intents": [intent]}
    evidence = ("turn-1",)
    pid = compute_proposal_id(
        type="routing_adjustment", payload=payload, evidence=evidence,
    )
    queue_append(RoutingProposal(
        proposal_id=pid, type="routing_adjustment", payload=payload,
        evidence=evidence, eval_hash="", created_at=_TS,
        source_patterns=("cluster-1",),
    ))
    return pid, pid.split(":")[-1][:12]


def _memory_proposal(content="Operator prefers the CLI.", confidence=0.9,
                     action="create", target_id=None):
    return {
        "action": action, "target_id": target_id, "dock_goal_ref": None,
        "proposed_record": {"entity_type": "OperatorPreference",
                            "content": content, "confidence": confidence,
                            "justification": "j"},
    }


def _stage_memory(proposal, *, status="pending"):
    rec = {"session_id": "s", "status": status, "timestamp": _TS,
           "proposal": proposal}
    path = Path(get_hermes_home()) / "memory_proposals.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return memory_proposal_short_id(proposal)


def _memory_records():
    path = Path(get_hermes_home()) / "memory_proposals.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# ── review_proposals (unified listing) ────────────────────────────────────

def test_review_only_routing():
    """Ruled STALE, realigned per ccaaf3ae4 (structured proposals with ordinals)."""
    _stage_routing()
    out = json.loads(review_proposals())
    assert out["success"] is True
    assert out["pending_count"] == 1
    blob = " ".join(p["display"] for p in out["proposals"])
    assert "code_generation" in blob
    assert "memory_context" not in blob


def test_review_only_memory():
    """Ruled STALE, realigned per ccaaf3ae4 (structured proposals with ordinals)."""
    short = _stage_memory(_memory_proposal(content="Prefers CLI for deploys."))
    out = json.loads(review_proposals())
    assert out["success"] is True
    assert out["pending_count"] == 1
    blob = " ".join(p["display"] for p in out["proposals"])
    assert "memory_context" in blob
    assert "Prefers CLI for deploys." in blob
    assert short in {p["id"] for p in out["proposals"]}


def test_review_both_types():
    """Ruled STALE, realigned per ccaaf3ae4 (structured proposals with ordinals)."""
    _stage_routing()
    short = _stage_memory(_memory_proposal(content="A learned fact."))
    out = json.loads(review_proposals())
    assert out["pending_count"] == 2
    blob = " ".join(p["display"] for p in out["proposals"])
    assert "code_generation" in blob          # routing
    assert "A learned fact." in blob          # memory
    assert short in {p["id"] for p in out["proposals"]}


def test_review_none():
    out = json.loads(review_proposals())
    assert out["success"] is True
    assert out["pending_count"] == 0
    assert "No pending proposals" in out.get("message", "")


# ── approve_proposal (probe-in-order) ─────────────────────────────────────

def test_approve_routing_id_routes_to_cli_approve(monkeypatch):
    full_id, short = _stage_routing()
    calls = {"routing": 0, "memory": 0}

    import grove.flywheel_cli as fcli
    import grove.memory.cli as mcli
    monkeypatch.setattr(fcli, "cli_approve",
                        lambda pid, **kw: (calls.__setitem__("routing", 1), 0)[1])
    monkeypatch.setattr(mcli, "cli_memory_approve",
                        lambda pid, **kw: (calls.__setitem__("memory", 1), 0)[1])

    out = json.loads(approve_proposal(short))
    assert out["success"] is True
    assert calls == {"routing": 1, "memory": 0}   # routing path only


def test_approve_memory_id_commits():
    short = _stage_memory(_memory_proposal(content="Take Flight uses Notion."))
    out = json.loads(approve_proposal(short))
    assert out["success"] is True

    # committed to the memory store
    store = MemoryStore(base_dir=Path(get_hermes_home()))
    active = [r for r in store.projected_records().values() if r.status == "active"]
    assert any(r.content == "Take Flight uses Notion." for r in active)
    # proposal record flipped
    rec = [r for r in _memory_records() if r.get("proposal")][0]
    assert rec["status"] == "approved"


def test_approve_unknown_id_errors():
    out = json.loads(approve_proposal("deadbeefcafe"))
    assert out["success"] is False
    assert "No proposal matches" in out["message"]


def test_reject_memory_id_dismisses():
    short = _stage_memory(_memory_proposal(content="Not useful."))
    out = json.loads(reject_proposal(short, reason="declined"))
    assert out["success"] is True
    rec = [r for r in _memory_records() if r.get("proposal")][0]
    assert rec["status"] == "rejected"
    # nothing committed to the store
    store = MemoryStore(base_dir=Path(get_hermes_home()))
    assert all(r.content != "Not useful."
               for r in store.projected_records().values())


def test_review_then_approve_memory_roundtrip():
    """Ruled STALE, realigned per ccaaf3ae4 (structured proposals with ordinals)."""
    short = _stage_memory(_memory_proposal(content="Round-trip fact."))
    listing = json.loads(review_proposals())
    ids = {p["id"] for p in listing["proposals"]}
    assert short in ids                        # id discoverable via review

    out = json.loads(approve_proposal(short))  # ...and accepted by approve
    assert out["success"] is True


def test_approve_schema_has_disambiguation_guidance():
    desc = APPROVE_PROPOSAL_SCHEMA["parameters"]["properties"]["proposal_id"]["description"]
    full = APPROVE_PROPOSAL_SCHEMA["description"] + " " + desc
    assert "multiple" in full.lower()

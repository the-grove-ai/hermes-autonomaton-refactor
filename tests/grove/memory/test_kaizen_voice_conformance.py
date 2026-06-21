"""kaizen-voice-conformance hotfix — operator-facing surfaces speak the
deployed Kaizen voice: conversational, inline, no CLI bounce.

Proves: push notes carry zero CLI syntax (no backtick commands, no "flywheel",
no id/SHA) and use approve/dismiss language; the model gets inline-approval
guidance when the approval tools are present; the review->approve loop still
commits memory.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from hermes_constants import get_hermes_home

from grove.eval.proposal_queue import RoutingProposal, compute_proposal_id
from grove.memory.cli import memory_proposal_short_id
from grove.memory.push import select_memory_push_note
from grove.memory.store import MemoryStore
from tools.flywheel_review_tool import approve_proposal, review_proposals

_TS = "2026-06-01T00:00:00+00:00"


def _stage_memory(content="Operator prefers the CLI for deploys.", confidence=0.9):
    proposal = {
        "action": "create", "target_id": None, "dock_goal_ref": None,
        "proposed_record": {"entity_type": "OperatorPreference", "content": content,
                            "confidence": confidence, "justification": "j"},
    }
    rec = {"session_id": "s", "status": "pending", "timestamp": _TS, "proposal": proposal}
    path = Path(get_hermes_home()) / "memory_proposals.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return memory_proposal_short_id(proposal)


# ── Fix 1: memory push note conformance ───────────────────────────────────

def test_memory_push_note_has_no_cli_syntax(tmp_path):
    proposal = {
        "action": "create", "target_id": None, "dock_goal_ref": None,
        "proposed_record": {"entity_type": "DomainFact", "content": "A fact.",
                            "confidence": 0.9, "justification": "j"},
    }
    rec = {"session_id": "s", "status": "pending", "timestamp": _TS, "proposal": proposal}
    (tmp_path / "memory_proposals.jsonl").write_text(json.dumps(rec) + "\n")

    short_id, note = select_memory_push_note(shown_ids=set(), base_dir=tmp_path)
    assert "`" not in note                 # no backtick command syntax
    assert "flywheel" not in note          # no CLI command
    assert short_id not in note            # no id/SHA in operator text
    assert "approve" in note.lower()       # conversational affirmation
    assert "dismiss" in note.lower()


# ── Fix 3: routing push note conformance ──────────────────────────────────

def test_routing_push_note_has_no_cli_syntax():
    from grove import flywheel_cli
    payload = {"rule": "ratchet_promoted_t1", "add_intents": ["conversation"]}
    p = RoutingProposal(
        proposal_id=compute_proposal_id(
            type="routing_adjustment", payload=payload, evidence=("t1",)),
        type="routing_adjustment", payload=payload, evidence=("t1",),
        eval_hash="", created_at=datetime.now(timezone.utc).isoformat(),
        source_patterns=("c1",),
    )
    push = flywheel_cli.compose_offering(p, is_push=True)
    assert "`" not in push
    assert "flywheel" not in push
    assert "Reply 'approve'" in push
    # pull form stays bare/technical (unchanged surface)
    pull = flywheel_cli.compose_offering(p, is_push=False)
    assert pull == flywheel_cli._summary_routing_adjustment(p)


# ── Fix 2: inline-approval tool guidance ──────────────────────────────────

def test_tool_guidance_includes_inline_approval_when_tool_present():
    from grove.prompt.composer import _tool_guidance_provider
    result = _tool_guidance_provider(
        {"valid_tool_names": {"approve_proposal", "review_proposals", "memory"}}
    )
    assert result is not None
    text = result.text
    assert "review_proposals" in text
    assert "approve_proposal" in text
    assert "CLI" in text                       # the "never a CLI command" rule
    assert "inline" in text.lower()


def test_tool_guidance_omits_approval_when_tool_absent():
    from grove.prompt.composer import _tool_guidance_provider
    result = _tool_guidance_provider({"valid_tool_names": {"memory"}})
    # memory guidance present, but no proposal-approval guidance leaked in
    text = result.text if result else ""
    assert "review_proposals" not in text


# ── Fix 4: the inline loop still commits (review -> approve) ───────────────

def test_review_then_approve_commits_memory_inline():
    short = _stage_memory(content="Take Flight Advisors uses Notion.")

    listing = json.loads(review_proposals())
    blob = " ".join(listing["proposals"])
    assert short in blob and "memory_context" in blob   # discoverable via tool

    out = json.loads(approve_proposal(short))
    assert out["success"] is True

    store = MemoryStore(base_dir=Path(get_hermes_home()))
    active = [r for r in store.projected_records().values() if r.status == "active"]
    assert any(r.content == "Take Flight Advisors uses Notion." for r in active)

"""Phase 3.1 — proactive conversational push of memory proposals.

Two layers: select_memory_push_note (the pure picker/renderer, reuses the
CLI reader + short-id + the Phase 3 summary_renderer) and the thin agent
method AIAgent._append_memory_offer (shown-set dedup + concatenation).
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from grove.memory.cli import memory_proposal_short_id
from grove.memory.push import select_memory_push_note

_TS = "2026-06-01T00:00:00+00:00"


def _stage(base: Path, status: str, content: str, *, confidence=0.9,
           action="create", target_id=None):
    proposal = {
        "action": action, "target_id": target_id, "dock_goal_ref": None,
        "proposed_record": {"entity_type": "DomainFact", "content": content,
                            "confidence": confidence, "justification": "j"},
    }
    rec = {"session_id": "s", "status": status, "timestamp": _TS,
           "proposal": proposal}
    with open(base / "memory_proposals.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return proposal


# ── select_memory_push_note (the picker/renderer) ─────────────────────────

def test_no_pending_returns_none(tmp_path):
    assert select_memory_push_note(shown_ids=set(), base_dir=tmp_path) is None


def test_one_pending_renders_note(tmp_path):
    proposal = _stage(tmp_path, "pending", "Operator prefers the CLI.", confidence=0.9)
    short_id = memory_proposal_short_id(proposal)

    result = select_memory_push_note(shown_ids=set(), base_dir=tmp_path)
    assert result is not None
    returned_id, note = result
    assert returned_id == short_id
    assert "Shop floor note" in note
    assert "Operator prefers the CLI." in note            # summary_renderer output
    assert f"flywheel memory approve {short_id}" in note
    assert f"flywheel memory reject {short_id}" in note


def test_already_shown_excluded(tmp_path):
    proposal = _stage(tmp_path, "pending", "Already seen.", confidence=0.9)
    short_id = memory_proposal_short_id(proposal)
    assert select_memory_push_note(shown_ids={short_id}, base_dir=tmp_path) is None


def test_highest_confidence_first(tmp_path):
    _stage(tmp_path, "pending", "Low value.", confidence=0.55)
    high = _stage(tmp_path, "pending", "High value.", confidence=0.95)
    result = select_memory_push_note(shown_ids=set(), base_dir=tmp_path)
    assert result is not None
    _id, note = result
    assert "High value." in note
    assert "Low value." not in note


def test_only_pending_status_surfaces(tmp_path):
    _stage(tmp_path, "rejected", "Rejected one.", confidence=0.99)
    _stage(tmp_path, "processing", "Processing lock.", confidence=0.99)
    pending = _stage(tmp_path, "pending", "The real pending.", confidence=0.6)

    result = select_memory_push_note(shown_ids=set(), base_dir=tmp_path)
    assert result is not None
    _id, note = result
    assert "The real pending." in note
    assert "Rejected one." not in note
    assert "Processing lock." not in note
    assert _id == memory_proposal_short_id(pending)


# ── AIAgent._append_memory_offer (the thin wrapper) ───────────────────────

def _home(tmp_path_factory=None):
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _agent():
    return types.SimpleNamespace(_surfaced_proposal_ids=set())


def test_method_appends_and_marks_shown():
    from run_agent import AIAgent
    base = _home()
    proposal = _stage(base, "pending", "Take Flight uses Notion.", confidence=0.9)
    short_id = memory_proposal_short_id(proposal)
    agent = _agent()

    out = AIAgent._append_memory_offer(agent, "Here is the answer.")
    assert "Here is the answer." in out
    assert "Shop floor note" in out
    assert f"flywheel memory approve {short_id}" in out
    assert short_id in agent._surfaced_proposal_ids


def test_method_idempotent_within_session():
    from run_agent import AIAgent
    base = _home()
    _stage(base, "pending", "Only proposal.", confidence=0.9)
    agent = _agent()

    first = AIAgent._append_memory_offer(agent, "Answer.")
    assert "Shop floor note" in first
    # second call same session: already shown → unchanged
    second = AIAgent._append_memory_offer(agent, "Answer 2.")
    assert second == "Answer 2."


def test_method_empty_response_unchanged():
    from run_agent import AIAgent
    base = _home()
    _stage(base, "pending", "X.", confidence=0.9)
    assert AIAgent._append_memory_offer(_agent(), "") == ""


def test_method_composes_with_prior_offer():
    """Memory push appends after a prior (routing) offer — independent systems
    compose, neither clobbers the other (SPEC test 5)."""
    from run_agent import AIAgent
    base = _home()
    _stage(base, "pending", "Memory fact.", confidence=0.9)
    agent = _agent()

    # simulate the response already carrying a routing push note
    prior = "Here is the answer.\n\nShop floor note — I noticed I could tune routing."
    out = AIAgent._append_memory_offer(agent, prior)
    assert "tune routing" in out          # prior routing offer preserved
    assert "Memory fact." in out          # memory offer appended

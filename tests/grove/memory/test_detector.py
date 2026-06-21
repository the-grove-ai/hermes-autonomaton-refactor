"""Phase 2 tests — transcript pre-filter + Context Persistence Detector.

The detector's T1 Haiku call is the one external dependency; tests mock it
by monkeypatching ``ContextPersistenceDetector._call_detector`` so no
credentials or network are required (the call surface itself is exercised
live only in Phase 3 integration).
"""

from __future__ import annotations

import json

import pytest

from grove.memory.detector import ContextPersistenceDetector
from grove.memory.store import MemoryStore
from grove.memory.transcript_filter import filter_transcript_for_extraction


@pytest.fixture()
def store(tmp_path):
    return MemoryStore(base_dir=tmp_path)


@pytest.fixture()
def detector(store, tmp_path):
    return ContextPersistenceDetector(store=store, base_dir=tmp_path)


def _proposal_records(detector):
    path = detector.proposals_path
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _mock_t1(detector, payload):
    """Patch the T1 call to return ``payload`` as the raw assistant text."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    detector._call_detector = lambda *a, **k: raw  # type: ignore[assignment]


# A transcript that clears the Fix 3 minimum-complexity gate (>=3 user turns)
# so these detector-flow tests exercise the T1 path, not the early-exit.
_GATE_OK = [
    {"role": "user", "content": "First message about the work."},
    {"role": "user", "content": "Second message with detail."},
    {"role": "user", "content": "Third message confirming something."},
]


# 1. Transcript filter

def test_filter_strips_and_preserves():
    messages = [
        {"role": "system", "content": "you are an autonomaton"},
        {"role": "user", "content": "Remember: TFA uses Notion."},
        {
            "role": "assistant",
            "content": "Got it.",
            "reasoning": "private chain of thought",
            "reasoning_content": "more reasoning",
            "reasoning_details": {"steps": 3},
            "codex_reasoning_items": [{"x": 1}],
            "codex_message_items": [{"y": 2}],
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "save_memory", "arguments": '{"k":"v"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "tool_name": "save_memory",
         "content": "saved ok — internal tool output"},
        {"role": "user", "content": "\x00json:" + "QUJD" * 200},  # base64 blob
    ]
    out = filter_transcript_for_extraction(messages)
    roles = [m["role"] for m in out]

    # system + tool stripped entirely
    assert "system" not in roles
    assert "tool" not in roles

    # user text preserved; base64 user blob dropped
    users = [m for m in out if m["role"] == "user"]
    assert len(users) == 1
    assert users[0]["content"] == "Remember: TFA uses Notion."

    # assistant text preserved; reasoning/codex stripped
    asst = next(m for m in out if m["role"] == "assistant")
    assert asst["content"] == "Got it."
    for stripped in (
        "reasoning", "reasoning_content", "reasoning_details",
        "codex_reasoning_items", "codex_message_items",
    ):
        assert stripped not in asst

    # tool_calls keep name+arguments only (id/type stripped, output gone)
    tc = asst["tool_calls"][0]
    assert tc["function"]["name"] == "save_memory"
    assert tc["function"]["arguments"] == '{"k":"v"}'
    assert "id" not in tc and "type" not in tc

    # input not mutated
    assert messages[2]["reasoning"] == "private chain of thought"
    assert messages[0]["role"] == "system"


# 2. Idempotency

def test_idempotency_second_call_returns_zero(detector):
    _mock_t1(detector, {"proposals": [
        {"action": "create", "target_id": None, "dock_goal_ref": None,
         "proposed_record": {"entity_type": "DomainFact", "content": "X.",
                             "confidence": 0.9, "justification": "why"}},
    ]})
    first = detector.detect_and_stage("sess-1", _GATE_OK, [])
    assert first == 1

    second = detector.detect_and_stage("sess-1", _GATE_OK, [])
    assert second == 0


# 3. Processing lock written before T1 call

def test_processing_lock_written_before_t1(detector):
    seen = {}

    def spy(*_a, **_k):
        seen["records_at_call"] = _proposal_records(detector)
        return json.dumps({"proposals": []})

    detector._call_detector = spy  # type: ignore[assignment]
    detector.detect_and_stage("sess-lock", _GATE_OK, [])

    statuses = [r["status"] for r in seen["records_at_call"]
                if r.get("session_id") == "sess-lock"]
    assert "processing" in statuses


# 4. Proposal count enforcement — 5 returned, 3 staged

def test_proposal_count_truncated_to_three(detector):
    five = [
        {"action": "create", "target_id": None, "dock_goal_ref": None,
         "proposed_record": {"entity_type": "DomainFact", "content": f"Fact {i}.",
                             "confidence": 0.8, "justification": "j"}}
        for i in range(5)
    ]
    _mock_t1(detector, {"proposals": five})
    staged = detector.detect_and_stage("sess-5", _GATE_OK, [])
    assert staged == 3

    pending = [r for r in _proposal_records(detector)
               if r.get("status") == "pending"]
    assert len(pending) == 3


# 5. Malformed JSON — 0 proposals, no crash

def test_malformed_json_stages_zero(detector):
    _mock_t1(detector, "this is not json at all {{{")
    staged = detector.detect_and_stage("sess-bad", _GATE_OK, [])
    assert staged == 0
    pending = [r for r in _proposal_records(detector)
               if r.get("status") == "pending"]
    assert pending == []


def test_markdown_fenced_json_parsed(detector):
    fenced = "```json\n" + json.dumps({"proposals": [
        {"action": "create", "target_id": None, "dock_goal_ref": None,
         "proposed_record": {"entity_type": "DomainFact", "content": "Fenced.",
                             "confidence": 0.9, "justification": "j"}},
    ]}) + "\n```"
    _mock_t1(detector, fenced)
    staged = detector.detect_and_stage("sess-fence", _GATE_OK, [])
    assert staged == 1


# 6. Empty session — {"proposals": []} → 0 staged

def test_empty_proposals_stages_zero(detector):
    _mock_t1(detector, {"proposals": []})
    staged = detector.detect_and_stage("sess-empty", _GATE_OK, [])
    assert staged == 0


# 7. Supersession proposal links target_id

def test_supersede_proposal_links_target(detector):
    _mock_t1(detector, {"proposals": [
        {"action": "supersede", "target_id": "mem_old123", "dock_goal_ref": None,
         "proposed_record": {"entity_type": "DomainFact",
                             "content": "Updated fact.", "confidence": 0.92,
                             "justification": "contradicts old"}},
    ]})
    detector.detect_and_stage("sess-sup", _GATE_OK, [])
    pending = [r for r in _proposal_records(detector) if r.get("status") == "pending"]
    assert len(pending) == 1
    prop = pending[0]["proposal"]
    assert prop["action"] == "supersede"
    assert prop["target_id"] == "mem_old123"


# 8. Dock goal ref carried through

def test_dock_goal_ref_carried(detector):
    _mock_t1(detector, {"proposals": [
        {"action": "create", "target_id": None, "dock_goal_ref": "content-pipeline",
         "proposed_record": {"entity_type": "ProjectState",
                             "content": "Pipeline at stage 2.", "confidence": 0.85,
                             "justification": "active goal"}},
    ]})
    dock_goals = [{"slug": "content-pipeline", "name": "Content Pipeline",
                   "status": "active", "vector": "ship"}]
    detector.detect_and_stage("sess-dock", _GATE_OK, dock_goals)
    pending = [r for r in _proposal_records(detector) if r.get("status") == "pending"]
    assert pending[0]["proposal"]["dock_goal_ref"] == "content-pipeline"

"""silent-degradation-sweep-v1 — fail-loud filings at the swallow sites.

Phase 2 (site a): the kaizen-push outer catch files ONE ``andon_halt``
(source=kaizen_push) into the session's Kaizen ledger and never disturbs
turn delivery; the ever-pushed memory mark moves AFTER compose_offering so
a failed compose leaves the proposal push-eligible.
"""

from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_constants import get_hermes_home

from grove.eval.proposal_queue import (
    RoutingProposal,
    append as queue_append,
    compute_proposal_id,
)
from grove.memory.cli import memory_proposal_short_id

_TS = "2026-06-01T00:00:00+00:00"


def _stage_routing(intent="code_generation"):
    payload = {"rule": "ratchet_promoted_t1", "add_intents": [intent]}
    p = RoutingProposal(
        proposal_id=compute_proposal_id(
            type="routing_adjustment", payload=payload, evidence=("t1",)),
        type="routing_adjustment", payload=payload, evidence=("t1",),
        eval_hash="", created_at=datetime.now(timezone.utc).isoformat(),
        source_patterns=("c1",), proposer="tier_ratchet",
    )
    queue_append(p)
    return p


def _stage_memory(content="Operator prefers the CLI."):
    proposal = {
        "action": "create", "target_id": None, "dock_goal_ref": None,
        "proposed_record": {
            "entity_type": "OperatorPreference", "content": content,
            "confidence": 0.9, "justification": "j",
        },
    }
    rec = {"session_id": "s", "status": "pending", "timestamp": _TS,
           "proposal": proposal}
    path = Path(get_hermes_home()) / "memory_proposals.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return memory_proposal_short_id(proposal)


def _agent(session_id="sds-push-test", turn=3):
    import run_agent
    a = object.__new__(run_agent.AIAgent)
    a.session_start = datetime.now() - timedelta(hours=1)
    a.session_id = session_id
    a._user_turn_count = turn
    a._surfaced_proposal_ids = set()
    # portal-link-reliability-v1 seam — resident config snapshot stub (the
    # test_flywheel_offerings idiom).
    a._config_load_or = lambda: {}
    return a


def _ledger_events(session_id):
    path = Path(get_hermes_home()) / ".kaizen_ledger" / f"{session_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


@pytest.fixture(autouse=True)
def _relevant_intent(monkeypatch):
    # Memory pushes are relevance-gated by intent_class; the staged
    # OperatorPreference memories need a relevant turn classification.
    from grove import providers
    monkeypatch.setattr(
        providers, "_last_classification",
        types.SimpleNamespace(intent_class="conversation"), raising=False,
    )


# ── Phase 2 (site a): push-pipeline failure filing ───────────────────────


def test_push_failure_files_one_ledger_event(monkeypatch):
    """A compose failure files exactly one andon_halt (source=kaizen_push,
    check=push_pipeline) into THIS session's ledger; the answer is returned
    untouched — turn delivery is never blocked by push telemetry."""
    from run_agent import AIAgent
    from grove import flywheel_cli

    _stage_routing()

    def _boom(*a, **kw):
        raise ValueError("compose exploded")

    monkeypatch.setattr(flywheel_cli, "compose_offering", _boom)
    agent = _agent(session_id="sds-file-once", turn=7)

    out = AIAgent._append_pending_offer(agent, "Answer.")
    assert out == "Answer."  # delivery unharmed

    events = _ledger_events("sds-file-once")
    halts = [e for e in events if e["event_type"] == "andon_halt"]
    assert len(halts) == 1
    halt = halts[0]
    assert halt["source"] == "kaizen_push"
    assert halt["check"] == "push_pipeline"
    assert "compose exploded" in halt["detail"]
    assert halt["turn"] == 7
    assert halt["session_id"] == "sds-file-once"


def test_compose_failure_leaves_memory_unmarked_and_repushable(monkeypatch):
    """The ever-pushed mark is written AFTER a successful compose: a failed
    compose leaves the memory proposal unmarked, so a later turn pushes it."""
    from run_agent import AIAgent
    from grove import flywheel_cli
    from tools.flywheel_review_tool import _read_pushed_memory_ids

    short_id = _stage_memory(content="Mark me only on display.")

    def _boom(*a, **kw):
        raise ValueError("compose exploded")

    monkeypatch.setattr(flywheel_cli, "compose_offering", _boom)
    out = AIAgent._append_pending_offer(_agent(session_id="sds-mark"), "A.")
    assert out == "A."
    assert short_id not in _read_pushed_memory_ids()  # mark never written

    # Compose recovers → the SAME proposal is still push-eligible and the
    # mark lands only now, with the successful display.
    monkeypatch.setattr(
        flywheel_cli, "compose_offering", lambda *a, **kw: "OFFER-NOTE",
    )
    out2 = AIAgent._append_pending_offer(_agent(session_id="sds-mark"), "B.")
    assert "OFFER-NOTE" in out2
    assert short_id in _read_pushed_memory_ids()

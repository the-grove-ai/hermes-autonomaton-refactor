"""Phase 5 — end-to-end verification of the memory substrate.

Exercises the full loop: dormant transcript -> detector (T1 mocked) ->
staged proposals -> Kaizen approval via run_digest -> store events ->
projected index -> provider -> /context composition. Also asserts the R4
invariant and kaizen_disposition recording end to end.

The base dir is the conftest-isolated GROVE_HOME so the default composer's
accumulated_domain_memory provider (which builds a store from the hermes
home) reads the very records this test approves.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_constants import get_hermes_home

from grove.memory.detector import ContextPersistenceDetector
from grove.memory.digest import run_digest
from grove.memory.events import MemoryAccessed, MemoryCreated
from grove.memory.provider import create_memory_provider
from grove.memory.store import MemoryStore


def _proposal(entity_type, content, confidence, *, dock_goal_ref=None):
    return {
        "action": "create",
        "target_id": None,
        "dock_goal_ref": dock_goal_ref,
        "proposed_record": {
            "entity_type": entity_type,
            "content": content,
            "confidence": confidence,
            "justification": "from session",
        },
    }


def _proposal_records(path: Path):
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _dispositions(ledger_dir: Path):
    out = []
    for p in ledger_dir.glob("*.jsonl"):
        for ln in p.read_text().splitlines():
            if ln.strip() and json.loads(ln).get("event_type") == "kaizen_disposition":
                out.append(json.loads(ln))
    return out


def test_memory_substrate_end_to_end():
    # 1. MemoryStore on the isolated hermes home (shared with default composer).
    base = Path(get_hermes_home())
    store = MemoryStore(base_dir=base)
    detector = ContextPersistenceDetector(store=store, base_dir=base)

    # 2. Synthetic 5-turn transcript: a domain fact, a preference, a goal.
    transcript = [
        {"role": "user",
         "content": "Take Flight Advisors uses Notion for project tracking."},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": "Always deploy via the CLI, not the dashboard."},
        {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": "Let's push the content pipeline goal forward."},
    ]

    # 3. One active Dock goal.
    dock_goals = [{"slug": "content-pipeline", "name": "Content Pipeline",
                   "status": "accelerating"}]

    # T1 mocked — returns the three observations the detector would crystallize.
    proposals = [
        _proposal("DomainFact",
                  "Take Flight Advisors uses Notion for project tracking.", 0.95),
        _proposal("OperatorPreference", "Operator always deploys via the CLI.", 0.9),
        _proposal("ProjectState", "Content pipeline goal is in active focus.",
                  0.6, dock_goal_ref="content-pipeline"),
    ]
    detector._call_detector = lambda *a, **k: json.dumps({"proposals": proposals})

    # 4. Run the detector.
    staged = detector.detect_and_stage("sess-e2e", transcript, dock_goals)

    # 5. Three staged; processing lock resolved (session is one-shot).
    assert staged == 3
    ppath = detector.proposals_path
    pending = [r for r in _proposal_records(ppath)
               if r.get("status") == "pending" and r.get("proposal")]
    assert len(pending) == 3
    assert detector.detect_and_stage("sess-e2e", transcript, dock_goals) == 0

    # 6. Kaizen approval: approve the fact + preference, reject the project state.
    ledger_dir = base / ".kaizen_ledger"

    def decide(_summary, proposal):
        et = proposal["proposed_record"]["entity_type"]
        return "reject" if et == "ProjectState" else "approve"

    counts = run_digest(store=store, proposals_path=ppath, decide=decide,
                        ledger_dir=ledger_dir)
    # crystallization-cadence-v1 added a "dismissed" disposition to the engine.
    assert counts == {"approved": 2, "rejected": 1, "deferred": 0, "dismissed": 0}

    # 7. Two MemoryCreated events in the log; one proposal rejected.
    created = [e for e in store.read_events() if isinstance(e, MemoryCreated)]
    assert len(created) == 2
    rejected = [r for r in _proposal_records(ppath) if r.get("status") == "rejected"]
    assert len(rejected) == 1

    # 8. Rebuild → two active records.
    store.rebuild_index()
    active = [r for r in store.projected_records().values() if r.status == "active"]
    assert len(active) == 2

    # 9. Query by a keyword from an approved record → match.
    hits = store.query(keywords=["notion"])
    assert any("Notion" in r.content for r in hits)

    # 10. Provider returns the approved records.
    provider = create_memory_provider(
        store=store, dock_goals_loader=lambda: dock_goals, token_budget=2000,
    )
    result = provider({"session_id": "sess-e2e", "intent_class": "conversation"})
    assert result is not None
    assert "Notion" in result.text
    assert "deploys via the CLI" in result.text

    # 11. /context integration: the section appears in composed output.
    from grove.prompt.composer import build_default_composer
    composed = build_default_composer().compose(
        session_id="sess-e2e", intent_class="conversation",
    )
    assert "accumulated_domain_memory" in composed.sections
    assert "Accumulated Domain Memory" in composed.text

    # 12. R4 invariant: delete the index, rebuild from the log, identical state.
    store.rebuild_index()
    before = dict(store.projected_records())
    store.index_path.unlink()
    store.rebuild_index()
    assert store.projected_records() == before

    # 13. kaizen_disposition recorded for all three dispositions.
    disp = _dispositions(ledger_dir)
    assert sorted(d["disposition"] for d in disp) == ["applied", "applied", "rejected"]
    assert all(d["proposal_type"] == "memory_context" for d in disp)

    # Post-conditions #1-3: the three persistence files exist on disk.
    assert (base / "memory_records.jsonl").exists()   # append-only event log
    assert (base / "memory_index.json").exists()      # projected active index
    assert (base / "memory_proposals.jsonl").exists()  # staged proposal queue


def test_e2e_provider_records_access_events():
    """Served records leave a debounced MemoryAccessed trail: nothing during
    the turn, one batched event per record on the session flush (Fix 1)."""
    base = Path(get_hermes_home())
    store = MemoryStore(base_dir=base)
    store.append_event(MemoryCreated(
        event_id="evt_1", timestamp="2026-06-01T00:00:00+00:00",
        record_id="mem_a", entity_type="DomainFact", content="A served fact.",
        confidence=0.9, dock_goal_ref=None, sources=[], supersedes=None,
    ))
    store.rebuild_index()

    provider = create_memory_provider(store=store, dock_goals_loader=lambda: [])
    provider({"session_id": "s", "intent_class": "conversation"})

    # Debounced: no event during the live turn.
    assert not [e for e in store.read_events() if isinstance(e, MemoryAccessed)]
    # One event per served record on flush.
    assert store.flush_access_events("s") == 1
    accesses = [e for e in store.read_events() if isinstance(e, MemoryAccessed)]
    assert len(accesses) == 1
    assert accesses[0].record_id == "mem_a"

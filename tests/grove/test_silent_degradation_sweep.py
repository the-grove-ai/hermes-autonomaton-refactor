"""silent-degradation-sweep-v1 — fail-loud filings at the swallow sites.

Phase 2 (site a): the kaizen-push outer catch files ONE ``andon_halt``
(source=kaizen_push) into the session's Kaizen ledger and never disturbs
turn delivery; the ever-pushed memory mark moves AFTER compose_offering so
a failed compose leaves the proposal push-eligible.

Phase 3 (site b): tier-ratchet memory enrichment stays a sanctioned
degradation (returns "") but degrades LOUDLY — WARN + one ``andon_halt``
(source=tier_ratchet) under the cli-<utc-timestamp> sentinel session.
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


# ── Phase 3 (site b): tier-ratchet enrichment degradation ────────────────


def _intent_records(intent_class="conversation", n=5):
    from grove.intent_store import IntentRecord
    return [
        IntentRecord(
            timestamp=datetime(2026, 5, 1, tzinfo=timezone.utc).isoformat(),
            session_id="s_t",
            turn_id=f"t_{intent_class}_{i}",
            user_message_stem="probe",
            pattern_hash="f" * 64,
            intent_class=intent_class,
            register_class="casual",
            complexity_signal="simple",
            confidence=0.92,
            outcome="success",
            tier_selected="T2",
        )
        for i in range(n)
    ]


class _ExplodingStore:
    def query(self, **kw):
        raise RuntimeError("index corrupt")


def _all_ledger_halts():
    ledger_dir = Path(get_hermes_home()) / ".kaizen_ledger"
    halts = []
    if not ledger_dir.is_dir():
        return halts
    for path in ledger_dir.glob("*.jsonl"):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event["event_type"] == "andon_halt":
                halts.append(event)
    return halts


def test_enrichment_failure_returns_empty_and_files_once():
    """Query failure → '' (sanctioned degradation KEPT), plus exactly one
    andon_halt (source=tier_ratchet, check=memory_enrichment) under a
    cli-<utc-timestamp> sentinel session; generation completes."""
    from grove.eval.tier_ratchet import propose_routing_adjustments

    proposals = propose_routing_adjustments(
        _intent_records(), memory_store=_ExplodingStore(),
    )
    assert len(proposals) == 1  # downward proposal still generates
    assert proposals[0].semantic_justification == ""

    halts = [h for h in _all_ledger_halts() if h["source"] == "tier_ratchet"]
    assert len(halts) == 1
    halt = halts[0]
    assert halt["check"] == "memory_enrichment"
    assert "index corrupt" in halt["detail"]
    assert halt["intent_class"] == "conversation"
    assert halt["session_id"].startswith("cli-")


def test_enrichment_absent_store_files_nothing():
    """memory_store=None is the handled-upstream branch — no filing, no
    warning storm; the proposal generates un-enriched exactly as before."""
    from grove.eval.tier_ratchet import propose_routing_adjustments

    proposals = propose_routing_adjustments(_intent_records(), memory_store=None)
    assert len(proposals) == 1
    assert proposals[0].semantic_justification == ""
    assert [h for h in _all_ledger_halts() if h["source"] == "tier_ratchet"] == []


# ── Phase 4 (site c): proposal-queue drops fail loud + quarantine ────────


def _queue_proposal(intent="code_generation", evidence=("t1",)):
    payload = {"rule": "ratchet_promoted_t1", "add_intents": [intent]}
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type="routing_adjustment", payload=payload, evidence=evidence),
        type="routing_adjustment", payload=payload, evidence=evidence,
        eval_hash="", created_at=datetime.now(timezone.utc).isoformat(),
        source_patterns=("c1",), proposer="tier_ratchet",
    )


_GARBAGE_LINE = "{this is not json"
_MISMATCH_LINE = json.dumps({"type": "fault_triage", "bogus_field": 1})


def _damaged_queue(tmp_path, good):
    """One queue file: parseable rows in *good* + one undecodable line +
    one schema-mismatch line."""
    qp = tmp_path / "proposals.jsonl"
    with open(qp, "w", encoding="utf-8") as fh:
        for p in good:
            fh.write(json.dumps(p.to_dict(), sort_keys=True, default=str) + "\n")
        fh.write(_GARBAGE_LINE + "\n")
        fh.write(_MISMATCH_LINE + "\n")
    return qp


def _pq_halts():
    return [h for h in _all_ledger_halts() if h["source"] == "proposal_queue"]


def test_damaged_read_warns_and_files_once(tmp_path, caplog):
    """A damaged file read WARNs (single aggregate line, counts + line
    numbers) and files one andon_halt per check class; repeated reads of
    the SAME damage do not re-file (in-process memo)."""
    import logging
    from grove.eval import proposal_queue as pq

    good = _queue_proposal()
    qp = _damaged_queue(tmp_path, [good])

    with caplog.at_level(logging.WARNING, logger="grove.eval.proposal_queue"):
        records = pq.read_all(path=qp)
    assert [p.proposal_id for p in records] == [good.proposal_id]

    warns = [r for r in caplog.records
             if "preserved in file until quarantined" in r.getMessage()]
    assert len(warns) == 1  # single aggregate WARNING per read
    assert "2 unparseable" in warns[0].getMessage()
    assert "lines: 2, 3" in warns[0].getMessage()

    halts = _pq_halts()
    assert {h["check"] for h in halts} == {
        "json_decode", "schema_mismatch:fault_triage",
    }
    assert len(halts) == 2

    pq.read_all(path=qp)  # same damage, same file → memo suppresses
    assert len(_pq_halts()) == 2


def test_first_mutation_quarantines_verbatim(tmp_path):
    """remove() quarantines the unparseable lines VERBATIM to the sidecar
    and rewrites the main file with ONLY parseable rows — the surviving
    parseable row is unaffected."""
    from grove.eval import proposal_queue as pq

    keep_me = _queue_proposal(intent="conversation", evidence=("t9",))
    remove_me = _queue_proposal()
    qp = _damaged_queue(tmp_path, [remove_me, keep_me])

    assert pq.remove(remove_me.proposal_id, path=qp) is True

    quarantine = qp.parent / (qp.name + ".quarantine")
    assert quarantine.read_text(encoding="utf-8") == (
        _GARBAGE_LINE + "\n" + _MISMATCH_LINE + "\n"
    )  # verbatim, append-only sidecar

    survivors = [json.loads(l) for l in qp.read_text().splitlines() if l.strip()]
    assert [s["proposal_id"] for s in survivors] == [keep_me.proposal_id]
    assert pq.read_all(path=qp)[0].payload == keep_me.payload  # unaffected


def test_no_refiling_after_schema_fix(tmp_path):
    """Repairing the file ends the noise: a read of the repaired file
    neither WARNs nor files — and a read after quarantine is clean too."""
    from grove.eval import proposal_queue as pq

    good = _queue_proposal()
    qp = _damaged_queue(tmp_path, [good])
    pq.read_all(path=qp)
    baseline = len(_pq_halts())

    # Simulated schema fix: rewrite with only the parseable row.
    with open(qp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(good.to_dict(), sort_keys=True, default=str) + "\n")

    records = pq.read_all(path=qp)
    assert [p.proposal_id for p in records] == [good.proposal_id]
    assert len(_pq_halts()) == baseline  # nothing new filed


def test_append_dedup_blindness_documented_unchanged(tmp_path):
    """DOCUMENTED-UNCHANGED: append's dedup scan sees only parseable rows,
    so a proposal whose only queue row is damaged re-appends. The drop is
    surfaced (filing) but the dedup contract is untouched by this sprint."""
    from grove.eval import proposal_queue as pq

    p = _queue_proposal()
    corrupted = dict(p.to_dict())
    corrupted["bogus_field"] = 1  # same identity, unparseable row
    qp = tmp_path / "proposals.jsonl"
    qp.write_text(json.dumps(corrupted, sort_keys=True, default=str) + "\n",
                  encoding="utf-8")

    assert pq.append(p, path=qp) is True  # dedup blind to the damaged row
    lines = [l for l in qp.read_text().splitlines() if l.strip()]
    assert len(lines) == 2

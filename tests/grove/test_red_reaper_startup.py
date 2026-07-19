"""kaizen-queue-hygiene-v1 K-2a — startup RED-orphan reaper.

Proves:
  * ORPHAN → reaped via the sanctioned ``finalize_proposal_state`` writer, leaving a
    ``kaizen_disposition`` ledger row (provenance per reap). No raw store rewrite.
  * NO ORPHANS → zero-count return (the caller logs the loud line), queue untouched.
  * LIVE payload (a real pending RED) is NEVER swept — ``store.has()`` guards it.
  * A non-``governance_env_pending`` proposal is NEVER swept, even payload-less.
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_FAULT_TRIAGE,
    RoutingProposal,
    append,
    read_all,
)
from grove.red_pending_store import (
    RED_PENDING_PROPOSAL_TYPE,
    PendingRedProposal,
    RedPendingStore,
    reap_orphaned_red_pending,
)


def _rid(bare: str) -> str:
    """The PREFIXED queue id the dispatcher writes for a RED bridge row."""
    return f"{RED_PENDING_PROPOSAL_TYPE}:{bare}"


def _bridge(bare: str, ptype: str = RED_PENDING_PROPOSAL_TYPE) -> RoutingProposal:
    """A bridge row as the dispatcher appends it: a RED row carries the PREFIXED
    queue id (``governance_env_pending:<bare>``) while the store keys on the bare
    ``<bare>``; a non-RED row uses its own id unprefixed."""
    pid = _rid(bare) if ptype == RED_PENDING_PROPOSAL_TYPE else bare
    return RoutingProposal(
        proposal_id=pid,
        type=ptype,
        payload={"tool": "propose_governance_change"},
        evidence=(pid,),
        eval_hash="",
        created_at="2026-07-19T00:00:00+00:00",
        proposer="governance",
    )


def _payload(pid: str) -> PendingRedProposal:
    return PendingRedProposal(
        proposal_id=pid,
        tool_name="terminal",
        arguments={"command": "echo hi"},
        effect_signature="sig-" + pid,
        description="d",
        rationale="",
        created_at="2026-07-19T00:00:00+00:00",
    )


def _ledger_events(ledger_dir: Path):
    events = []
    for f in glob.glob(str(ledger_dir / "*.jsonl")):
        for line in Path(f).read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


def test_orphan_reaped_with_ledger_provenance(tmp_path):
    q = tmp_path / "proposals.jsonl"
    ledger = tmp_path / "ledger"
    store = RedPendingStore(db_path=tmp_path / "red.db")
    append(_bridge("orphan1"), path=q)  # bridge row, NO payload in store == orphan

    reaped = reap_orphaned_red_pending(store=store, queue_path=q, ledger_dir=ledger)

    assert reaped == [_rid("orphan1")]
    assert [p.proposal_id for p in read_all(path=q)] == []  # queue row gone
    ev = [e for e in _ledger_events(ledger) if e.get("event_type") == "kaizen_disposition"]
    assert len(ev) == 1
    assert ev[0]["proposal_id"] == _rid("orphan1")
    assert ev[0]["disposition"] == "reaped"
    assert ev[0]["proposal_type"] == RED_PENDING_PROPOSAL_TYPE


def test_live_payload_never_swept(tmp_path):
    q = tmp_path / "proposals.jsonl"
    store = RedPendingStore(db_path=tmp_path / "red.db")
    store.put(_payload("live1"))          # a REAL pending RED — payload keyed on BARE id
    append(_bridge("live1"), path=q)      # queue row is PREFIXED — exercises the strip

    reaped = reap_orphaned_red_pending(store=store, queue_path=q, ledger_dir=tmp_path / "l")

    assert reaped == []
    assert [p.proposal_id for p in read_all(path=q)] == [_rid("live1")]  # untouched


def test_non_red_proposal_never_swept(tmp_path):
    q = tmp_path / "proposals.jsonl"
    store = RedPendingStore(db_path=tmp_path / "red.db")
    # a payload-less fault_triage row — NOT a RED bridge, must be ignored entirely
    append(_bridge("ft1", ptype=PROPOSAL_TYPE_FAULT_TRIAGE), path=q)

    reaped = reap_orphaned_red_pending(store=store, queue_path=q, ledger_dir=tmp_path / "l")

    assert reaped == []
    assert [p.proposal_id for p in read_all(path=q)] == ["ft1"]  # untouched


def test_zero_orphans_returns_empty_and_leaves_queue(tmp_path):
    q = tmp_path / "proposals.jsonl"
    store = RedPendingStore(db_path=tmp_path / "red.db")
    store.put(_payload("live1"))
    append(_bridge("live1"), path=q)

    reaped = reap_orphaned_red_pending(store=store, queue_path=q, ledger_dir=tmp_path / "l")

    assert reaped == []  # caller logs the loud zero-count line
    assert len(read_all(path=q)) == 1


def test_mixed_batch_reaps_only_orphans(tmp_path):
    q = tmp_path / "proposals.jsonl"
    ledger = tmp_path / "ledger"
    store = RedPendingStore(db_path=tmp_path / "red.db")
    store.put(_payload("live1"))
    append(_bridge("live1"), path=q)                               # live RED — keep
    append(_bridge("orphanA"), path=q)                             # orphan — reap
    append(_bridge("orphanB"), path=q)                             # orphan — reap
    append(_bridge("ft1", ptype=PROPOSAL_TYPE_FAULT_TRIAGE), path=q)  # non-RED — keep

    reaped = reap_orphaned_red_pending(store=store, queue_path=q, ledger_dir=ledger)

    assert sorted(reaped) == [_rid("orphanA"), _rid("orphanB")]
    survivors = sorted(p.proposal_id for p in read_all(path=q))
    assert survivors == ["ft1", _rid("live1")]

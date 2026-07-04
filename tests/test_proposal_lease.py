"""fleet-pipeline-v1 P1 — lease primitive + finalize helper (safety-critical).

Covers: set_lease CAS (double-tap → already-held), lease id-exclusion + present-key
serialization, clear_lease, the startup-only stuck-lease sweep, and
finalize_proposal_state as the SINGLE disposition path for both verbs. All queue
mutations run under the one shared proposal_queue._lock; the queue path + ledger
dir are passed explicitly so the test is fully isolated.
"""

from __future__ import annotations

import glob
import json

import pytest

from grove.eval import proposal_queue as pq
from grove.eval.proposal_queue import (
    LEASE_ACQUIRED,
    LEASE_ALREADY_HELD,
    LEASE_NOT_FOUND,
    RoutingProposal,
    compute_proposal_id,
)


def _mk(seed: str) -> RoutingProposal:
    payload = {"rule": "downward", "k": seed}
    pid = compute_proposal_id(type="routing_adjustment", payload=payload, evidence=("e1",))
    return RoutingProposal(
        proposal_id=pid,
        type="routing_adjustment",
        payload=payload,
        evidence=("e1",),
        eval_hash="h",
        created_at="2026-07-04T00:00:00+00:00",
    )


@pytest.fixture
def queue(tmp_path):
    return tmp_path / "proposals.jsonl"


# ── lease field: id-excluded + present-key serialization ─────────────────────


def test_lease_defaults_none_and_is_present_key():
    p = _mk("a")
    assert p.lease is None
    assert "lease" not in p.to_dict()  # unheld -> no key (byte-identical to old)
    held = pq.replace(p, lease={"held_by": "x", "held_at": "t"})
    assert held.to_dict()["lease"] == {"held_by": "x", "held_at": "t"}


def test_lease_does_not_change_identity():
    p = _mk("a")
    held = pq.replace(p, lease={"held_by": "x", "held_at": "t"})
    # id is content-addressable on type|payload|evidence only — lease excluded
    assert held.proposal_id == p.proposal_id


def test_old_record_without_lease_deserializes(queue):
    p = _mk("a")
    pq.append(p, path=queue)
    # the serialized line has no lease key; re-read must default lease=None
    got = pq.read(p.proposal_id, path=queue)
    assert got is not None and got.lease is None
    assert "lease" not in queue.read_text()


# ── set_lease CAS (double-tap → 409) ─────────────────────────────────────────


def test_double_tap_second_is_already_held(queue):
    p = _mk("a")
    pq.append(p, path=queue)
    assert pq.set_lease(p.proposal_id, holder="tap1", path=queue) == LEASE_ACQUIRED
    assert pq.set_lease(p.proposal_id, holder="tap2", path=queue) == LEASE_ALREADY_HELD
    # the held lease persists on disk with the FIRST holder
    assert pq.read(p.proposal_id, path=queue).lease["held_by"] == "tap1"


def test_set_lease_not_found(queue):
    assert pq.set_lease("sha256:missing", path=queue) == LEASE_NOT_FOUND


def test_clear_lease_reverts_to_actionable(queue):
    p = _mk("a")
    pq.append(p, path=queue)
    pq.set_lease(p.proposal_id, holder="t", path=queue)
    assert pq.clear_lease(p.proposal_id, path=queue) is True
    assert pq.read(p.proposal_id, path=queue).lease is None
    # re-tap now succeeds (completed-failure path)
    assert pq.set_lease(p.proposal_id, path=queue) == LEASE_ACQUIRED
    assert pq.clear_lease(p.proposal_id, path=queue) is True
    assert pq.clear_lease(p.proposal_id, path=queue) is False  # already clear


# ── startup-only stuck-lease sweep ───────────────────────────────────────────


def test_sweep_reverts_held_leases_and_returns_them(queue):
    held, free = _mk("held"), _mk("free")
    pq.append(held, path=queue)
    pq.append(free, path=queue)
    pq.set_lease(held.proposal_id, holder="crashed_tap", path=queue)

    reverted = pq.sweep_stuck_leases(path=queue)
    assert [r.proposal_id for r in reverted] == [held.proposal_id]
    # the returned record carries the ORIGINAL lease (for the Andon)
    assert reverted[0].lease["held_by"] == "crashed_tap"
    # on disk both are now lease-free and STILL PRESENT (reverted to pending)
    assert pq.read(held.proposal_id, path=queue).lease is None
    assert pq.read(free.proposal_id, path=queue) is not None


def test_sweep_noop_when_no_leases(queue):
    pq.append(_mk("a"), path=queue)
    assert pq.sweep_stuck_leases(path=queue) == []


# ── finalize: the single disposition path for BOTH verbs ─────────────────────


def _ledger_events(ledger_dir):
    events = []
    for f in glob.glob(str(ledger_dir / "*.jsonl")):
        for line in open(f):
            events.append(json.loads(line))
    return events


def test_finalize_applied_removes_and_records(queue, tmp_path):
    ledger = tmp_path / "ledger"
    p = _mk("a")
    pq.append(p, path=queue)
    ok = pq.finalize_proposal_state(
        p.proposal_id, "applied", {"folder_link": "drive://x"},
        path=queue, ledger_dir=ledger,
    )
    assert ok is True
    assert pq.read(p.proposal_id, path=queue) is None  # removed from queue
    ev = _ledger_events(ledger)
    assert len(ev) == 1
    assert ev[0]["event_type"] == "kaizen_disposition"
    assert ev[0]["disposition"] == "applied"
    assert ev[0]["applied_result"] == {"folder_link": "drive://x"}


def test_finalize_rejected_uses_same_path(queue, tmp_path):
    ledger = tmp_path / "ledger"
    p = _mk("b")
    pq.append(p, path=queue)
    ok = pq.finalize_proposal_state(
        p.proposal_id, "rejected", {"archive_path": "~/.grove/forge/.archive/x"},
        reason="operator dismissed", path=queue, ledger_dir=ledger,
    )
    assert ok is True
    assert pq.read(p.proposal_id, path=queue) is None
    ev = _ledger_events(ledger)
    assert ev[0]["disposition"] == "rejected"
    assert ev[0]["reason"] == "operator dismissed"
    assert ev[0]["applied_result"] == {"archive_path": "~/.grove/forge/.archive/x"}


def test_finalize_idempotent_on_missing(queue, tmp_path):
    # a double-finalize (or finalize of an already-disposed proposal) is a no-op
    assert pq.finalize_proposal_state(
        "sha256:gone", "applied", path=queue, ledger_dir=tmp_path / "l"
    ) is False


def test_lease_and_finalize_share_one_lock():
    # structural: every queue mutation takes proposal_queue._lock (one scope).
    import inspect
    for fn in (pq.set_lease, pq.clear_lease, pq.finalize_proposal_state, pq.sweep_stuck_leases):
        src = inspect.getsource(fn)
        assert "with _lock:" in src, f"{fn.__name__} must mutate under _lock"

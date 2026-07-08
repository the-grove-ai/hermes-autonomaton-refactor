"""propose-approve-deadlock-v1 Phase 1b-i — store singleton + concurrency proof.

Proves the CORE CORRECTION:
  * SURVIVAL — the pending-RED store is a PROCESS singleton: two separate
    Dispatcher constructions (two turns/requests) share ONE store, so an entry
    stored via the first is reachable via the second and via the getter the
    portal approve handler uses. (1a's per-Dispatcher instance was GC'd at turn
    end — the blocker this fixes.)
  * ORPHAN-CHECK — `has()` distinguishes a live payload from an orphan (durable
    queue row, payload gone) for the render side.
  * CONCURRENT-APPROVE — two approves of the same proposal race on the atomic
    claim (pop); exactly ONE writes, the other fails clean; the .env is written
    exactly once and the entry is consumed.
"""

from __future__ import annotations

import threading

import pytest

from grove.dispatcher import Dispatcher
from grove.effect_signature import canonical_effect_signature
from grove.red_pending_store import (
    PendingRedProposal,
    action_proposal_id,
    get_red_pending_store,
    prepare_execute_arguments,
)


@pytest.fixture(autouse=True)
def _redirect_grove_home(tmp_path, monkeypatch):
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    yield tmp_path


@pytest.fixture(autouse=True)
def _fresh_red_store(monkeypatch):
    """Reset the process singleton per test for deterministic isolation."""
    import grove.red_pending_store as rps

    monkeypatch.setattr(rps, "_STORE", None)
    yield


def _entry(env_path, content="A=1\n", rationale="r"):
    args = prepare_execute_arguments(
        "propose_governance_change",
        {"target_file": str(env_path), "content": content, "rationale": rationale},
    )
    sig = canonical_effect_signature("propose_governance_change", args)
    pid = action_proposal_id(sig)
    return pid, PendingRedProposal(
        proposal_id=pid, tool_name="propose_governance_change",
        arguments=args, effect_signature=sig, description="d",
        rationale=rationale, created_at="2026-07-08T00:00:00+00:00",
    )


class TestSurvival:
    def test_two_dispatchers_share_one_store(self, tmp_path):
        d1 = Dispatcher()
        d2 = Dispatcher()
        # SAME instance — the blocker fix (was a fresh per-Dispatcher store in 1a).
        assert d1._red_pending_store is d2._red_pending_store
        assert d1._red_pending_store is get_red_pending_store()

        pid, entry = _entry(tmp_path / ".env")
        d1._red_pending_store.put(entry)
        # visible via the SECOND Dispatcher and via the getter the portal uses.
        assert d2._red_pending_store.get(pid) is entry
        assert get_red_pending_store().get(pid) is entry

    def test_explicit_store_override_isolates(self, tmp_path):
        from grove.red_pending_store import RedPendingStore

        iso = RedPendingStore()
        d = Dispatcher(red_pending_store=iso)
        assert d._red_pending_store is iso
        assert iso is not get_red_pending_store()  # override is NOT the singleton


class TestOrphanCheck:
    def test_has_reflects_payload_presence(self, tmp_path):
        store = get_red_pending_store()
        pid, entry = _entry(tmp_path / ".env")
        assert store.has(pid) is False          # nothing yet
        store.put(entry)
        assert store.has(pid) is True           # live payload
        store.pop(pid)
        assert store.has(pid) is False          # orphan: payload gone


class TestConcurrentApprove:
    def test_two_approves_single_writer(self, tmp_path):
        d = Dispatcher()
        env = tmp_path / ".env"
        content = "HF_TOKEN=hf_race\n"
        pid, entry = _entry(env, content=content)
        d._red_pending_store.put(entry)

        results: list = []
        barrier = threading.Barrier(2)

        def _approve():
            barrier.wait()  # release both threads into approve simultaneously
            results.append(d.approve_pending_red_proposal(pid))

        t1 = threading.Thread(target=_approve)
        t2 = threading.Thread(target=_approve)
        t1.start(); t2.start(); t1.join(); t2.join()

        successes = [r for r in results if r.get("success")]
        failures = [r for r in results if not r.get("success")]
        assert len(successes) == 1, results            # exactly one writer
        assert len(failures) == 1
        assert (
            "already approved" in failures[0]["error"].lower()
            or "no pending proposal" in failures[0]["error"].lower()
        )
        assert env.read_text() == content              # written EXACTLY once
        assert d._red_pending_store.has(pid) is False  # consumed (single-use)

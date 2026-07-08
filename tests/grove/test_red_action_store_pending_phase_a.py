"""red-action-store-pending-v1 Phase A — the RED store-pending arm proof.

Generalizes the 1a ``.env``-only store-pending to ANY RED action:
  * reachable legible RED (a ``sudo`` terminal command) → STORE_PENDING (not cancel);
  * reachable opaque RED (command substitution) → STORE_PENDING + is_opaque set;
  * unreachable RED (non_interactive_deny handler / fleet) → cancel;
  * ``.env`` propose regression → STORE_PENDING (propose is one instance);
  * generic execute: a stored ToolIntent → mint → registry.dispatch runs the
    handler once, gate consumed (proven via a SAFE propose-to-tempfile);
  * claim-then-execute concurrency: two approvers, single winner.
"""
from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from grove.dispatcher import AndonResolutionHalt, Dispatcher
from grove.governance_halt import TerminalGovernanceHalt
from grove.effect_signature import canonical_effect_signature
from grove.intents import ToolIntent
from grove.red_pending_store import (
    PendingRedProposal,
    action_proposal_id,
    approve_red_proposal,
    prepare_execute_arguments,
)
from grove.sovereign_prompt_handlers import non_interactive_deny_handler
from tests.grove.test_kaizen_voice_red_fork_b1 import _bare_agent


@pytest.fixture(autouse=True)
def _redirect_grove_home(tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    yield tmp_path


@pytest.fixture(autouse=True)
def _fresh_red_store(monkeypatch):
    import grove.red_pending_store as rps
    monkeypatch.setattr(rps, "_STORE", None)
    yield


@pytest.fixture(autouse=True)
def _capture_queue_writes(monkeypatch):
    from grove.eval import proposal_queue as pq
    cap: list = []
    monkeypatch.setattr(pq, "append", lambda p: cap.append(p))
    return cap


class _FakeGen:
    def __init__(self) -> None:
        self.sent: Any = None

    def send(self, obs: Any) -> Any:
        self.sent = obs
        return obs


def _drive(d: Dispatcher, intent: ToolIntent):
    """Classify a RED intent + resolve. Returns the halt; STORE_PENDING resumes
    (no raise), Cancel raises TerminalGovernanceHalt."""
    try:
        d._classify_intents_batch_and_halt_or_raise([intent])
    except AndonResolutionHalt as halt:
        d._resolve_red_halt(_bare_agent([]), _FakeGen(), halt)
        return halt
    raise AssertionError("expected AndonResolutionHalt for a RED intent")


def _term(cmd: str) -> ToolIntent:
    return ToolIntent(tool_name="terminal", arguments={"command": cmd}, call_id="c1")


# ── STORE routing (reachability + generalization) ─────────────────────────────
class TestStoreRouting:
    def test_reachable_legible_sudo_stores_not_cancels(self, tmp_path):
        d = Dispatcher()  # default: platform!=fleet, interactive handler → reachable
        _drive(d, _term("sudo apt-get install ffmpeg"))
        assert len(d._red_pending_store) == 1
        (entry,) = list(d._red_pending_store._by_id.values())
        assert entry.tool_name == "terminal"
        assert entry.arguments == {"command": "sudo apt-get install ffmpeg"}
        assert entry.is_opaque is False
        assert "sudo apt-get install ffmpeg" in entry.description

    def test_reachable_opaque_stores_with_flag(self, tmp_path):
        d = Dispatcher()
        _drive(d, _term("echo $(whoami)"))  # command substitution → opacity:*
        assert len(d._red_pending_store) == 1
        (entry,) = list(d._red_pending_store._by_id.values())
        assert entry.is_opaque is True
        assert "not statically resolved" in entry.description

    def test_env_propose_regression_stores(self, tmp_path, _capture_queue_writes):
        d = Dispatcher()
        env = tmp_path / ".env"
        _drive(d, ToolIntent(
            tool_name="propose_governance_change",
            arguments={"target_file": str(env), "content": "HF_TOKEN=hf_x\n", "rationale": "r"},
            call_id="c1",
        ))
        assert len(d._red_pending_store) == 1
        (entry,) = list(d._red_pending_store._by_id.values())
        assert entry.tool_name == "propose_governance_change"
        # propose's TOCTOU anchor folded into the execute args (byte-identical .env path)
        assert "approved_content_sha256" in entry.arguments
        assert not env.exists()  # nothing written at propose time
        assert _capture_queue_writes and _capture_queue_writes[0].payload == {"zone": "red"}

    def test_unreachable_handler_cancels(self, tmp_path):
        d = Dispatcher(sovereign_prompt_handler=non_interactive_deny_handler)
        assert d._is_operator_reachable() is False
        with pytest.raises(TerminalGovernanceHalt):
            _drive(d, _term("sudo apt-get install ffmpeg"))
        assert len(d._red_pending_store) == 0

    def test_fleet_platform_unreachable(self):
        d = Dispatcher()
        d._platform = "fleet"
        assert d._is_operator_reachable() is False


# ── generic execute (mint → registry.dispatch → handler once, gate consumed) ──
class TestGenericExecute:
    def _entry_for_propose(self, env_path, content="TOK=v1\n"):
        args = prepare_execute_arguments(
            "propose_governance_change",
            {"target_file": str(env_path), "content": content, "rationale": "r"},
        )
        sig = canonical_effect_signature("propose_governance_change", args)
        return PendingRedProposal(
            proposal_id=action_proposal_id(sig),
            tool_name="propose_governance_change",
            arguments=args,
            effect_signature=sig,
            description="d",
            rationale="r",
            created_at="2026-07-08T00:00:00+00:00",
        )

    def test_stored_intent_redispatches_once(self, tmp_path):
        env = tmp_path / ".env"
        from grove.red_pending_store import get_red_pending_store
        store = get_red_pending_store()
        entry = self._entry_for_propose(env, "TOK=real\n")
        store.put(entry)
        res = approve_red_proposal(entry.proposal_id, store)
        assert res["success"] is True and res["reason"] == "written"
        assert env.read_text() == "TOK=real\n"          # handler ran (write landed)
        assert store.get(entry.proposal_id) is None       # single-use, popped

    def test_integrity_mismatch_aborts(self, tmp_path):
        env = tmp_path / ".env"
        from grove.red_pending_store import get_red_pending_store
        store = get_red_pending_store()
        entry = self._entry_for_propose(env)
        entry.effect_signature = "TAMPERED"               # anchor no longer matches args
        store.put(entry)
        res = approve_red_proposal(entry.proposal_id, store)
        assert res["success"] is False and res["reason"] == "integrity"
        assert not env.exists()

    def test_unknown_tool_loud(self, tmp_path):
        from grove.red_pending_store import get_red_pending_store
        store = get_red_pending_store()
        args = {"x": 1}
        sig = canonical_effect_signature("no_such_tool", args)
        entry = PendingRedProposal(
            proposal_id=action_proposal_id(sig), tool_name="no_such_tool",
            arguments=args, effect_signature=sig, description="d", rationale="r",
            created_at="2026-07-08T00:00:00+00:00",
        )
        store.put(entry)
        res = approve_red_proposal(entry.proposal_id, store)
        assert res["success"] is False and res["reason"] == "unknown_tool"


# ── claim-then-execute concurrency (atomic pop, single winner) ────────────────
class TestConcurrency:
    def test_two_approvers_single_winner(self, tmp_path):
        env = tmp_path / ".env"
        from grove.red_pending_store import get_red_pending_store
        store = get_red_pending_store()
        args = prepare_execute_arguments(
            "propose_governance_change",
            {"target_file": str(env), "content": "TOK=once\n", "rationale": "r"},
        )
        sig = canonical_effect_signature("propose_governance_change", args)
        entry = PendingRedProposal(
            proposal_id=action_proposal_id(sig), tool_name="propose_governance_change",
            arguments=args, effect_signature=sig, description="d", rationale="r",
            created_at="2026-07-08T00:00:00+00:00",
        )
        store.put(entry)
        results: list = []
        barrier = threading.Barrier(2)

        def _approve():
            barrier.wait()
            results.append(approve_red_proposal(entry.proposal_id, store))

        ts = [threading.Thread(target=_approve) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        wins = [r for r in results if r["success"]]
        misses = [r for r in results if not r["success"] and r["reason"] == "not_found"]
        assert len(wins) == 1 and len(misses) == 1        # exactly one winner
        assert env.read_text() == "TOK=once\n"            # written exactly once

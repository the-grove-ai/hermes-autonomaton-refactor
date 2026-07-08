"""propose-approve-deadlock-v1 Phase 1a — GOVERNANCE CORE unit proof.

Proves the RED ``.env`` store-pending-approval mechanics (Step 9):

  * a RED ``.env`` ``propose_governance_change`` resolves to STORE_PENDING (it is
    STORED), NOT the ``TerminalGovernanceHalt`` Cancel;
  * the store holds the payload IN-MEMORY only, emits a feed-worthy NON-TERMINAL
    event, and writes ONLY opaque metadata to the bridge queue (no secret);
  * the mint-on-approve callback mints + writes + consumes + clears;
  * TOCTOU: a payload mutated after propose ABORTS the callback with NO write;
  * the non-generator RPC path HARD-DENIES with a legible portal error and never
    stores off-generator;
  * the pure helpers (content id, key extraction, masking) behave.
"""

from __future__ import annotations

from typing import Any

import pytest

from grove.dispatcher import AndonResolutionHalt, Dispatcher
from grove.effect_signature import canonical_effect_signature
from grove.halt_event import HaltTrigger, is_feed_worthy
from grove.intents import ToolIntent
from grove.red_pending_store import (
    RED_PENDING_PROPOSAL_TYPE,
    PendingRedProposal,
    action_proposal_id,
    content_proposal_id,
    describe_red_action,
    extract_env_keys,
    masked_env_description,
    prepare_execute_arguments,
)
from grove.sovereign_prompt_handlers import non_interactive_deny_handler
from tests.grove.test_kaizen_voice_red_fork_b1 import _bare_agent, _force_zone


@pytest.fixture(autouse=True)
def _redirect_grove_home(tmp_path, monkeypatch):
    """No test may touch the operator's ~/.grove."""
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    yield tmp_path


@pytest.fixture(autouse=True)
def _fresh_red_store(monkeypatch):
    """propose-approve-deadlock-v1 Phase 1b-i — the pending-RED store is now a
    PROCESS singleton, so entries would leak across tests (worker-order flake).
    Reset it to a fresh instance per test for deterministic isolation."""
    import grove.red_pending_store as rps

    monkeypatch.setattr(rps, "_STORE", None)
    yield


@pytest.fixture(autouse=True)
def _capture_queue_writes(monkeypatch):
    """The opaque bridge write is best-effort; capture it instead of hitting disk."""
    from grove.eval import proposal_queue as pq

    captured: list = []
    monkeypatch.setattr(pq, "append", lambda prop: captured.append(prop))
    return captured


class _FakeGen:
    def __init__(self) -> None:
        self.sent: Any = None

    def send(self, obs: Any) -> Any:
        self.sent = obs
        return obs


def _propose_intent(env_path, content="HF_TOKEN=hf_x\n", rationale="persist HF token"):
    return ToolIntent(
        tool_name="propose_governance_change",
        arguments={"target_file": str(env_path), "content": content, "rationale": rationale},
        call_id="c1",
    )


def _propose_pid(env_path, content="HF_TOKEN=hf_x\n", rationale="persist HF token"):
    """The generic pending-RED id the dispatcher stores for a .env propose —
    ``action_proposal_id(canonical_effect_signature(tool, prepare_execute_arguments(...)))``.
    Mirrors ``_store_pending_red_proposal`` so tests key on the same anchor."""
    args = prepare_execute_arguments(
        "propose_governance_change",
        {"target_file": str(env_path), "content": content, "rationale": rationale},
    )
    sig = canonical_effect_signature("propose_governance_change", args)
    return action_proposal_id(sig)


def _stash(d: Dispatcher, env_path, content):
    """Drive a RED .env propose to the STORE_PENDING store; return the halt."""
    intent = _propose_intent(env_path, content=content)
    try:
        d._classify_intents_batch_and_halt_or_raise([intent])
    except AndonResolutionHalt as halt:
        d._resolve_red_halt(_bare_agent([]), _FakeGen(), halt)
        return halt
    raise AssertionError("expected AndonResolutionHalt for a .env RED propose")


class TestStorePendingRouting:
    def test_red_env_propose_stores_not_cancels(self, tmp_path, _capture_queue_writes):
        d = Dispatcher()
        env = tmp_path / ".env"
        intent = _propose_intent(env)
        try:
            d._classify_intents_batch_and_halt_or_raise([intent])
        except AndonResolutionHalt as halt:
            gen = _FakeGen()
            # MUST NOT raise TerminalGovernanceHalt (Cancel) — it stores + resumes.
            d._resolve_red_halt(_bare_agent([]), gen, halt)
        else:
            raise AssertionError("expected AndonResolutionHalt for a .env RED propose")

        # red-action-store-pending-v1 Phase A — the stored id is now the generic
        # action anchor (action_proposal_id(effect_signature)), not content_proposal_id.
        assert len(d._red_pending_store) == 1
        (entry,) = list(d._red_pending_store._by_id.values())
        assert entry.tool_name == "propose_governance_change"
        # feed-worthy NON-TERMINAL store event
        assert d._last_store_pending_event.trigger is HaltTrigger.OPERATOR_STORED_PENDING
        assert is_feed_worthy(d._last_store_pending_event) is True
        # opaque bridge metadata only (no key/diff/path)
        assert _capture_queue_writes, "expected an opaque queue bridge write"
        bridge = _capture_queue_writes[0]
        assert bridge.type == RED_PENDING_PROPOSAL_TYPE
        assert bridge.payload == {"zone": "red"}
        # the .env was NOT written at propose time
        assert not env.exists()
        # resume observation (success, relay-not-replan) — not a re-plan/cancel
        assert gen.sent and gen.sent[0].success is True

    def test_reachable_non_propose_red_stores_pending(self, tmp_path, monkeypatch):
        """red-action-store-pending-v1 Phase A — STORE_PENDING now covers ALL RED
        on an operator-REACHABLE surface, not just propose_governance_change. A
        forced-RED non-propose action STORES (no terminal Cancel)."""
        _force_zone(monkeypatch, "red")  # force a NON-propose tool to RED
        d = Dispatcher()  # default cli platform + handler → REACHABLE
        assert d._is_operator_reachable() is True
        intent = ToolIntent(
            tool_name="write_file", arguments={"path": str(tmp_path / "x")}, call_id="c1"
        )
        try:
            d._classify_intents_batch_and_halt_or_raise([intent])
        except AndonResolutionHalt as halt:
            # Stores + resumes — MUST NOT raise TerminalGovernanceHalt.
            d._resolve_red_halt(_bare_agent([]), _FakeGen(), halt)
        else:
            raise AssertionError("expected AndonResolutionHalt")
        assert len(d._red_pending_store) == 1
        (entry,) = list(d._red_pending_store._by_id.values())
        assert entry.tool_name == "write_file"

    def test_unreachable_red_action_still_cancels(self, tmp_path, monkeypatch):
        """On an UNREACHABLE surface (non_interactive_deny_handler → no operator
        can approve) RED keeps the terminal Cancel and stores NOTHING."""
        from grove.governance_halt import TerminalGovernanceHalt

        _force_zone(monkeypatch, "red")  # force a NON-propose tool to RED
        d = Dispatcher(sovereign_prompt_handler=non_interactive_deny_handler)
        assert d._is_operator_reachable() is False
        intent = ToolIntent(
            tool_name="write_file", arguments={"path": str(tmp_path / "x")}, call_id="c1"
        )
        try:
            d._classify_intents_batch_and_halt_or_raise([intent])
        except AndonResolutionHalt as halt:
            with pytest.raises(TerminalGovernanceHalt):
                d._resolve_red_halt(_bare_agent([]), _FakeGen(), halt)
        else:
            raise AssertionError("expected AndonResolutionHalt")
        assert len(d._red_pending_store) == 0


class TestApproveCallback:
    def test_valid_approval_mints_writes_consumes_clears(self, tmp_path):
        d = Dispatcher()
        env = tmp_path / ".env"
        _stash(d, env, "HF_TOKEN=hf_real\n")
        pid = _propose_pid(env, content="HF_TOKEN=hf_real\n")  # generic action anchor
        assert not env.exists()  # nothing written until approval

        res = d.approve_pending_red_proposal(pid)
        assert res["success"] is True
        assert env.read_text() == "HF_TOKEN=hf_real\n"      # written POST-approval
        assert d._red_pending_store.get(pid) is None         # cleared (single-use)

    def test_toctou_mutation_aborts_no_write(self, tmp_path):
        d = Dispatcher()
        env = tmp_path / ".env"
        content = "HF_TOKEN=hf_good\n"
        # Build the generic pending-RED record the same way the dispatcher does.
        args = prepare_execute_arguments(
            "propose_governance_change",
            {"target_file": str(env), "content": content, "rationale": "r"},
        )
        sig = canonical_effect_signature("propose_governance_change", args)
        pid = action_proposal_id(sig)
        desc, is_opaque = describe_red_action("propose_governance_change", args)
        entry = PendingRedProposal(
            proposal_id=pid,
            tool_name="propose_governance_change",
            arguments=args,
            effect_signature=sig,
            description=desc,
            rationale="r",
            created_at="2026-07-08T00:00:00+00:00",
            is_opaque=is_opaque,
        )
        d._red_pending_store.put(entry)
        # Tamper AFTER storing — the stored anchor no longer matches the recomputed
        # effect signature (there is no content field to tamper; the signature is it).
        entry.effect_signature = "TAMPERED"

        res = d.approve_pending_red_proposal(pid)
        assert res["success"] is False
        assert res["reason"] == "integrity"
        assert "integrity" in res["error"].lower()
        assert not env.exists()                              # NO write
        assert d._red_pending_store.get(pid) is None         # tampered entry dropped

    def test_unknown_id_fails_clean(self):
        d = Dispatcher()
        res = d.approve_pending_red_proposal("deadbeef")
        assert res["success"] is False
        assert "no pending proposal" in res["error"].lower()


class TestRpcHardDeny:
    def test_rpc_propose_env_hard_denies_with_portal_error(self, tmp_path):
        d = Dispatcher()
        env = tmp_path / ".env"
        ok, reason = d.classify_and_mint(
            "propose_governance_change",
            {"target_file": str(env), "content": "X=1\n", "rationale": "r"},
        )
        assert ok is False
        assert "portal" in reason.lower() and "approval" in reason.lower()
        assert len(d._red_pending_store) == 0  # never stored off-generator
        assert not env.exists()


class TestPureHelpers:
    def test_content_id_stable_and_distinct(self):
        assert content_proposal_id("a") == content_proposal_id("a")
        assert content_proposal_id("a") != content_proposal_id("b")

    def test_extract_env_keys_names_only(self):
        keys = extract_env_keys("HF_TOKEN=x\nexport GOOGLE_T=y\n# comment\nbad-lower=z\n")
        assert keys == ["HF_TOKEN", "GOOGLE_T"]

    def test_masking_names_key_hides_value(self):
        desc = masked_env_description("~/.grove/.env", ["HF_TOKEN"])
        assert "HF_TOKEN" in desc and "hidden" in desc

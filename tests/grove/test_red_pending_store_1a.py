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
from grove.halt_event import HaltTrigger, is_feed_worthy
from grove.intents import ToolIntent
from grove.red_pending_store import (
    RED_PENDING_PROPOSAL_TYPE,
    PendingRedProposal,
    content_proposal_id,
    extract_env_keys,
    masked_env_description,
)
from tests.grove.test_kaizen_voice_red_fork_b1 import _bare_agent, _force_zone


@pytest.fixture(autouse=True)
def _redirect_grove_home(tmp_path, monkeypatch):
    """No test may touch the operator's ~/.grove."""
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    yield tmp_path


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

        pid = content_proposal_id("HF_TOKEN=hf_x\n")
        assert len(d._red_pending_store) == 1
        assert d._red_pending_store.get(pid) is not None
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

    def test_other_red_action_still_cancels(self, tmp_path, monkeypatch):
        """Non-propose RED actions keep the terminal Cancel — STORE_PENDING is
        scoped to propose_governance_change ONLY."""
        from grove.governance_halt import TerminalGovernanceHalt

        _force_zone(monkeypatch, "red")  # force a NON-propose tool to RED
        d = Dispatcher()  # default headless handler → cancel
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
        pid = content_proposal_id("HF_TOKEN=hf_real\n")
        assert not env.exists()  # nothing written until approval

        res = d.approve_pending_red_proposal(pid)
        assert res["success"] is True
        assert env.read_text() == "HF_TOKEN=hf_real\n"      # written POST-approval
        assert d._red_pending_store.get(pid) is None         # cleared (single-use)

    def test_toctou_mutation_aborts_no_write(self, tmp_path):
        d = Dispatcher()
        env = tmp_path / ".env"
        content = "HF_TOKEN=hf_good\n"
        pid = content_proposal_id(content)
        entry = PendingRedProposal(
            proposal_id=pid, target_file=str(env), content=content,
            content_sha256=pid, effect_signature="propose\x1f\x1f{}",
            rationale="r", description="d", created_at="2026-07-07T00:00:00+00:00",
        )
        d._red_pending_store.put(entry)
        # Tamper AFTER storing — the payload no longer hashes to the anchor.
        entry.content = "HF_TOKEN=EVIL\n"

        res = d.approve_pending_red_proposal(pid)
        assert res["success"] is False
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

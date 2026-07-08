"""operator-red-correctness-v1 — resolution routing + confirm-card correctness.

Proves:
  (a) a REACHABLE operator priv:* RED routes to Operator-Runs-It AT RESOLUTION —
      NON-TERMINAL, and NO store row is created (no claim, no post-claim "expired").
  (b) a FLEET / UNREACHABLE priv:* RED still routes to the headless handler (cancel),
      NOT the new operator-runs-it branch (the nesting is load-bearing).
  (c) a gateway-safe (non-priv) operator RED still store-pends.
  (d) the confirm success card reflects the ACTUAL executed effect (governance-write
      path / terminal executed / priv:* handback), not the hardcoded ".env" mislabel.
"""
from __future__ import annotations

import pytest

from grove.dispatcher import AndonResolutionHalt, Dispatcher
from grove.effect_signature import canonical_effect_signature
from grove.governance_halt import TerminalGovernanceHalt
from grove.intents import ToolIntent
from grove.sovereign_prompt_handlers import non_interactive_deny_handler
from tests.grove.test_kaizen_voice_red_fork_b1 import _bare_agent

from grove.api.actions import handle_red_proposal_confirm
from grove.api.red_nonce import red_nonce
from grove.red_pending_store import (
    RED_PENDING_PROPOSAL_TYPE,
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
    import grove.red_pending_store as rps

    monkeypatch.setattr(rps, "_STORE", None)
    yield


@pytest.fixture(autouse=True)
def _silence_queue(monkeypatch):
    from grove.eval import proposal_queue as pq

    monkeypatch.setattr(pq, "append", lambda p: None)
    yield


class _CapGen:
    """A generator stand-in that captures what _resolve_red_halt sends."""

    def __init__(self):
        self.sent = None

    def send(self, obs):
        self.sent = obs
        return obs


def _term(cmd: str) -> ToolIntent:
    return ToolIntent(tool_name="terminal", arguments={"command": cmd}, call_id="c1")


def _classify(d: Dispatcher, intent: ToolIntent) -> "AndonResolutionHalt":
    try:
        d._classify_intents_batch_and_halt_or_raise([intent])
    except AndonResolutionHalt as halt:
        return halt
    raise AssertionError("expected AndonResolutionHalt for a RED intent")


# ── (a) operator priv:* → operator-runs-it, NO store ──────────────────────────
class TestOperatorRunsIt:
    def test_operator_priv_sudo_operator_runs_it_no_store(self, tmp_path):
        d = Dispatcher()  # platform != fleet, interactive handler → reachable
        assert d._is_operator_reachable() is True
        halt = _classify(d, _term("sudo apt-get install ffmpeg"))  # priv:sudo
        gen = _CapGen()
        d._resolve_red_halt(_bare_agent([]), gen, halt)  # NON-TERMINAL — must not raise
        assert len(d._red_pending_store) == 0            # NO store, NO claim
        assert gen.sent is not None                       # observation surfaced
        obs0 = gen.sent[0]
        low = str(obs0.value).lower()
        assert ("run it in your terminal" in low) or ("stay with you" in low)
        assert obs0.metadata.get("disposition") == "operator_identity_required"


# ── (b) fleet / unreachable priv:* → headless handler (cancel), NOT the new branch ─
class TestUnreachablePrivUnchanged:
    def test_fleet_priv_sudo_still_headless_handler(self, tmp_path):
        d = Dispatcher()
        d._platform = "fleet"
        assert d._is_operator_reachable() is False
        halt = _classify(d, _term("sudo apt-get install ffmpeg"))
        with pytest.raises(TerminalGovernanceHalt):
            d._resolve_red_halt(_bare_agent([]), _CapGen(), halt)
        assert len(d._red_pending_store) == 0

    def test_deny_handler_priv_sudo_still_cancels(self, tmp_path):
        d = Dispatcher(sovereign_prompt_handler=non_interactive_deny_handler)
        assert d._is_operator_reachable() is False
        halt = _classify(d, _term("sudo apt-get install ffmpeg"))
        with pytest.raises(TerminalGovernanceHalt):
            d._resolve_red_halt(_bare_agent([]), _CapGen(), halt)
        assert len(d._red_pending_store) == 0


# ── (c) gateway-safe (non-priv) operator RED still store-pends ────────────────
class TestGatewaySafeStillStores:
    def test_operator_opaque_red_still_store_pends(self, tmp_path):
        d = Dispatcher()
        halt = _classify(d, _term("echo $(whoami)"))  # opacity:substitution — NOT priv
        d._resolve_red_halt(_bare_agent([]), _CapGen(), halt)
        assert len(d._red_pending_store) == 1          # store-pending unchanged


# ── (d) confirm card reflects the actual effect ───────────────────────────────
class _StubAdapter:
    def __init__(self, key: str):
        self._api_key = key


class _StubReq:
    def __init__(self, full_pid, store, nonce, key="testkey"):
        self.match_info = {"proposal_id": full_pid}
        self.app = {"red_pending_store": store, "api_server_adapter": _StubAdapter(key)}
        self._nonce = nonce

    async def post(self):
        return {"nonce": self._nonce}


async def _confirm(store, bare):
    full_pid = f"{RED_PENDING_PROPOSAL_TYPE}:{bare}"
    nonce = red_nonce(full_pid, "confirm", b"testkey")
    resp = await handle_red_proposal_confirm(_StubReq(full_pid, store, nonce))
    return resp.text


class TestConfirmCard:
    async def test_governance_write_card_shows_target_path_not_env_hardcode(self, tmp_path):
        env = tmp_path / ".env"
        args = prepare_execute_arguments(
            "propose_governance_change",
            {"target_file": str(env), "content": "HF_TOKEN=SECRETVALUE\n", "rationale": "r"},
        )
        sig = canonical_effect_signature("propose_governance_change", args)
        bare = action_proposal_id(sig)
        get_red_pending_store().put(PendingRedProposal(
            proposal_id=bare, tool_name="propose_governance_change", arguments=args,
            effect_signature=sig, description="Persist credential — values hidden.",
            rationale="r", created_at="2026-07-08T00:00:00+00:00",
        ))
        body = await _confirm(get_red_pending_store(), bare)
        assert "governance write" in body
        assert str(env) in body                                   # actual PATH reflected
        assert "the credential was saved to .env." not in body    # old hardcode gone
        assert "SECRETVALUE" not in body                          # value NEVER rendered

    async def test_terminal_card_shows_executed_not_env(self, tmp_path):
        args = {"command": "echo probe-card-test"}  # green, benign, executes
        sig = canonical_effect_signature("terminal", args)
        bare = action_proposal_id(sig)
        get_red_pending_store().put(PendingRedProposal(
            proposal_id=bare, tool_name="terminal", arguments=args, effect_signature=sig,
            description="Run command: echo probe-card-test", rationale="r",
            created_at="2026-07-08T00:00:00+00:00",
        ))
        body = await _confirm(get_red_pending_store(), bare)
        low = body.lower()
        assert "executed" in low
        assert "saved to .env" not in low
        assert "credential" not in low

    async def test_priv_card_defensive_handback_not_executed(self, tmp_path, monkeypatch):
        # Defensive: Move 1 keeps priv:* out of the store, but a legacy row that
        # dispatched 'successfully' must NOT be mislabeled as executed — it hands back.
        import grove.api.actions as A

        bare = "legacyprivrow"
        full_pid = f"{RED_PENDING_PROPOSAL_TYPE}:{bare}"
        monkeypatch.setattr(A, "approve_red_proposal", lambda b, s: {
            "success": True, "reason": "written", "proposal_id": b, "result": "{}",
            "tool_name": "terminal", "pattern_key": "priv:sudo", "target_path": None,
        })
        nonce = red_nonce(full_pid, "confirm", b"testkey")
        resp = await handle_red_proposal_confirm(
            _StubReq(full_pid, get_red_pending_store(), nonce)
        )
        low = resp.text.lower()
        assert ("stays with you" in low) or ("run it in your terminal" in low)
        assert "executed" not in low
        assert "saved to" not in low

"""red-action-store-pending-v1 Phase B — guardrails + UX proof.

  * DENIED_BY_POLICY: catastrophic rm → denied (never store-pending/executed) on
    ANY surface; non-catastrophic sudo → store-pending (unaffected); an
    operator-config deny pattern → denied; the config reader parses the list.
  * OPAQUE_DYNAMIC_EFFECT: an opaque proposal card renders the warning; a legible
    one does not; the secret value never leaks; masking/escaping unchanged.
  * ExecutionIdentity: OPERATOR_IDENTITY_REQUIRED is reserved-empty (no action
    routes to it; default agent-execute unchanged).
  * RPC-path message reflects hard-deny (not "approve via portal").
  * Store-pending operator copy reads "operator holds this".
"""
from __future__ import annotations

from typing import Any

import pytest

from grove.dispatcher import (
    AndonResolutionHalt,
    Dispatcher,
    RED_RESOLUTIONS,
    RED_RESOLUTION_OPERATOR_IDENTITY,
)
from grove.governance_halt import TerminalGovernanceHalt
from grove.intents import ToolIntent
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
    monkeypatch.setattr(pq, "append", lambda p: None)
    yield


class _FakeGen:
    def __init__(self) -> None:
        self.sent: Any = None

    def send(self, obs: Any) -> Any:
        self.sent = obs
        return obs


def _term(cmd: str) -> ToolIntent:
    return ToolIntent(tool_name="terminal", arguments={"command": cmd}, call_id="c1")


def _resolve(d: Dispatcher, intent: ToolIntent):
    """Classify + resolve a RED intent. STORE_PENDING resumes (no raise);
    DENIED_BY_POLICY / Cancel raise TerminalGovernanceHalt."""
    try:
        d._classify_intents_batch_and_halt_or_raise([intent])
    except AndonResolutionHalt as halt:
        return d._resolve_red_halt(_bare_agent([]), _FakeGen(), halt)
    raise AssertionError("expected AndonResolutionHalt")


# ── STEP 1 — DENIED_BY_POLICY ─────────────────────────────────────────────────
class TestDeniedByPolicy:
    def test_catastrophic_rm_denied_not_stored(self):
        d = Dispatcher()  # reachable — but deny pre-empts reachability
        with pytest.raises(TerminalGovernanceHalt) as exc:
            _resolve(d, _term("rm -rf /"))
        assert exc.value.context.trigger == "red_denied_by_policy"
        detail = (exc.value.context.detail or "").lower()
        # Option B floor: HARD structural boundary + operator-runs-it, NO false
        # "remove the pattern" promise (the floor is not toggleable).
        assert "hard structural boundary" in detail
        assert "run it yourself outside the agent" in detail
        assert "remove" not in detail
        assert len(d._red_pending_store) == 0        # NEVER stored

    def test_catastrophic_rm_denied_even_unreachable(self):
        from grove.sovereign_prompt_handlers import non_interactive_deny_handler
        d = Dispatcher(sovereign_prompt_handler=non_interactive_deny_handler)
        with pytest.raises(TerminalGovernanceHalt) as exc:
            _resolve(d, _term("rm -rf /"))
        assert exc.value.context.trigger == "red_denied_by_policy"  # policy, not workflow_cancel

    def test_non_catastrophic_sudo_still_store_pends(self):
        d = Dispatcher()
        _resolve(d, _term("sudo apt-get install ffmpeg"))
        assert len(d._red_pending_store) == 1        # unaffected by the deny-list

    def test_operator_config_deny_pattern_denies(self, monkeypatch):
        import grove.red_policy as rp
        # operator extends the deny-list to the whole privilege family
        monkeypatch.setattr(
            rp, "denied_patterns", lambda: frozenset({"rm:catastrophic", "priv:"})
        )
        d = Dispatcher()
        with pytest.raises(TerminalGovernanceHalt) as exc:
            _resolve(d, _term("sudo apt-get install ffmpeg"))  # priv:sudo → matched by "priv:"
        assert exc.value.context.trigger == "red_denied_by_policy"
        assert len(d._red_pending_store) == 0

    def test_config_reader_parses_list(self, tmp_path):
        import grove.red_policy as rp
        cfg = tmp_path / "z.yaml"
        cfg.write_text("red_denied_by_policy:\n  - 'priv:'\n  - custom:effect\n")
        assert rp._read_deny_list(cfg) == ["priv:", "custom:effect"]
        # hardcoded floor is always present in the active set
        assert "rm:catastrophic" in rp.denied_patterns()

    def test_floor_message_is_hard_boundary_no_false_lever(self):
        # Option B — a FLOOR pattern is a hard structural boundary: honest, no
        # "remove from config" promise (a config edit cannot remove the floor).
        import grove.red_policy as rp
        msg = rp.denial_message("rm:catastrophic")
        assert rp.is_floor_denial("rm:catastrophic") is True
        assert "hard structural boundary" in msg.lower()
        assert "run it yourself outside the agent" in msg.lower()
        assert "red_denied_by_policy" not in msg      # no false config lever
        assert "remove" not in msg.lower()

    def test_operator_config_message_names_the_lever(self, monkeypatch):
        # An OPERATOR-config pattern IS removable — the message names the lever.
        import grove.red_policy as rp
        monkeypatch.setattr(
            rp, "denied_patterns", lambda: frozenset({"rm:catastrophic", "priv:"})
        )
        assert rp.is_floor_denial("priv:sudo") is False
        msg = rp.denial_message("priv:sudo")
        assert "denied by your policy" in msg.lower()
        assert "red_denied_by_policy" in msg          # names the config surface
        assert "remove" in msg.lower()                # accurate removal lever
        assert "governed change" in msg.lower()


# ── STEP 2 — OPAQUE_DYNAMIC_EFFECT render ─────────────────────────────────────
class _StubAdapter:
    def __init__(self, key: str) -> None:
        self._api_key = key


class _FakeReq:
    def __init__(self, store) -> None:
        self.app = {"red_pending_store": store, "api_server_adapter": _StubAdapter("k")}


def _put(store, tool_name, arguments, is_opaque, description, pattern_key=None):
    from grove.red_pending_store import PendingRedProposal, action_proposal_id
    from grove.effect_signature import canonical_effect_signature
    sig = canonical_effect_signature(tool_name, arguments)
    pid = action_proposal_id(sig)
    store.put(PendingRedProposal(
        proposal_id=pid, tool_name=tool_name, arguments=arguments, effect_signature=sig,
        description=description, rationale="r", created_at="2026-07-08T00:00:00+00:00",
        is_opaque=is_opaque, pattern_key=pattern_key,
    ))
    return pid


class TestOpaqueRender:
    def _render(self, store, pid):
        from grove.api.fragments import _render_red_proposal_card, RED_PENDING_PROPOSAL_TYPE
        full_pid = f"{RED_PENDING_PROPOSAL_TYPE}:{pid}"
        return _render_red_proposal_card(_FakeReq(store), full_pid, pid[:8])

    def test_opaque_proposal_shows_warning(self):
        from grove.red_pending_store import get_red_pending_store
        store = get_red_pending_store()
        pid = _put(store, "terminal", {"command": "echo $(whoami)"}, True,
                   "Opaque dynamic command — effect not statically resolved.")
        html = self._render(store, pid)
        assert "OPAQUE dynamic command" in html
        assert "not a guaranteed outcome" in html

    def test_legible_proposal_no_warning(self):
        from grove.red_pending_store import get_red_pending_store
        store = get_red_pending_store()
        pid = _put(store, "terminal", {"command": "sudo apt-get install ffmpeg"}, False,
                   "Run command: sudo apt-get install ffmpeg")
        html = self._render(store, pid)
        assert "OPAQUE dynamic command" not in html

    def test_secret_value_never_in_card(self):
        from grove.red_pending_store import get_red_pending_store
        store = get_red_pending_store()
        pid = _put(store, "propose_governance_change",
                   {"target_file": "~/.grove/.env", "content": "HF_TOKEN=SUPERSECRET\n", "rationale": "r"},
                   False, "Persist credential(s) to ~/.grove/.env: HF_TOKEN — values hidden.")
        html = self._render(store, pid)
        assert "SUPERSECRET" not in html
        assert "•••• (masked)" in html


# ── STEP 2 (title) — per-action-type portal card title ────────────────────────
class TestCardTitle:
    def test_title_derivation(self):
        from grove.red_pending_store import red_action_title
        assert red_action_title("propose_governance_change", None, False) == "RED — governance write"
        assert red_action_title("terminal", "priv:sudo", False) == "RED — privileged shell"
        assert red_action_title("terminal", "secret:operand", False) == "RED — secret access"
        assert red_action_title("terminal", "opacity:substitution", True) == "RED — opaque command"
        assert red_action_title("terminal", None, False) == "RED — shell command"

    def test_rendered_titles_reflect_action_type(self):
        from grove.red_pending_store import get_red_pending_store
        from grove.api.fragments import _render_red_proposal_card, RED_PENDING_PROPOSAL_TYPE
        store = get_red_pending_store()
        sudo_pid = _put(store, "terminal", {"command": "sudo apt-get install ffmpeg"},
                        False, "Run command: sudo apt-get install ffmpeg", pattern_key="priv:sudo")
        env_pid = _put(store, "propose_governance_change",
                       {"target_file": "~/.grove/.env", "content": "K=v\n", "rationale": "r"},
                       False, "Persist credential(s) to ~/.grove/.env: K — values hidden.")
        sudo_html = _render_red_proposal_card(
            _FakeReq(store), f"{RED_PENDING_PROPOSAL_TYPE}:{sudo_pid}", sudo_pid[:8])
        env_html = _render_red_proposal_card(
            _FakeReq(store), f"{RED_PENDING_PROPOSAL_TYPE}:{env_pid}", env_pid[:8])
        assert "RED — privileged shell" in sudo_html
        assert "RED — governance write" in env_html    # 1b-ii pin preserved for .env


# ── STEP 3 — ExecutionIdentity reserved-empty ─────────────────────────────────
class TestReservedIdentity:
    def test_operator_identity_reserved_member(self):
        assert RED_RESOLUTION_OPERATOR_IDENTITY == "operator_identity_required"
        assert RED_RESOLUTION_OPERATOR_IDENTITY in RED_RESOLUTIONS

    def test_no_current_action_routes_to_operator_identity(self):
        # The three live outcomes (denied / store-pending / cancel) never yield the
        # reserved resolution — default agent-execute is unchanged.
        from grove.sovereign_prompt_handlers import non_interactive_deny_handler
        d_denied = Dispatcher()
        with pytest.raises(TerminalGovernanceHalt) as e1:
            _resolve(d_denied, _term("rm -rf /"))
        assert e1.value.context.trigger != "operator_identity_required"
        d_store = Dispatcher()
        _resolve(d_store, _term("sudo apt-get install ffmpeg"))  # store-pending, no raise
        d_cancel = Dispatcher(sovereign_prompt_handler=non_interactive_deny_handler)
        with pytest.raises(TerminalGovernanceHalt) as e2:
            _resolve(d_cancel, _term("sudo apt-get install ffmpeg"))
        assert e2.value.context.trigger == "red_workflow_cancel"


# ── STEP 4 — RPC-path hard-deny message ───────────────────────────────────────
class TestRpcMessage:
    def test_non_generator_red_reports_hard_deny(self, tmp_path):
        d = Dispatcher()
        ok, msg = d.classify_and_mint(
            "propose_governance_change",
            {"target_file": str(tmp_path / ".env"), "content": "K=v\n", "rationale": "r"},
        )
        assert ok is False
        assert "DENIED here" in msg and "nothing was stored" in msg
        assert "Approve the pending proposal via the portal" not in msg  # old misleading copy gone


# ── STEP 5 — store-pending operator copy ──────────────────────────────────────
class TestStorePendingCopy:
    def test_copy_reads_operator_holds_this(self):
        from grove.halt_renderer import _render_red_pending_approval
        text = _render_red_pending_approval("Run command: sudo apt-get install ffmpeg",
                                            "http://x/portal#fragments/proposals/pending")
        assert "queued a RED action" in text
        assert "Approve it in the portal" in text
        assert "Nothing runs until you approve" in text
        # not the hard-boundary "can't/blocked" register
        assert "can't" not in text.lower() and "blocked" not in text.lower()

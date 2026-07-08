"""red-action-store-pending-v1 Phase C — mint-aware terminal execution proof.

The store-pending → operator-approve → EXECUTE path for hermes-executable RED
shell, gated so it cannot unblock catastrophic:

  * mint-aware guard: RED shell + matching approved-effect ContextVar → approved;
  * un-approved / different-command → blocked (content-binding, hash-what-you-execute);
  * SAFETY-CRITICAL: catastrophic → hardline blocks even if the ContextVar is set
    to its exact signature (the mint can NEVER unblock catastrophic);
  * ContextVar isolation: reset in finally → None → blocked (fail-safe);
  * end-to-end: approve_red_proposal executes a previously-blocked RED command and
    resets the ContextVar after.
"""
from __future__ import annotations

import json

import pytest

from grove.effect_signature import canonical_effect_signature
from grove.red_execution_context import approved_effect_var
from grove.red_pending_store import (
    PendingRedProposal,
    action_proposal_id,
    approve_red_proposal,
    get_red_pending_store,
    prepare_execute_arguments,
)
from tools.approval import check_all_command_guards as _guard


@pytest.fixture(autouse=True)
def _fresh_red_store(monkeypatch):
    import grove.red_pending_store as rps
    monkeypatch.setattr(rps, "_STORE", None)
    yield


def _sig(cmd: str) -> str:
    return canonical_effect_signature("terminal", {"command": cmd})


# ── mint-aware guard (unit) ───────────────────────────────────────────────────
class TestMintAwareGuard:
    def test_unapproved_red_blocked(self):
        assert _guard("sudo apt-get install -y ffmpeg", "local").get("approved") is False

    def test_approved_effect_executes(self):
        cmd = "sudo apt-get install -y ffmpeg"
        tok = approved_effect_var.set(_sig(cmd))
        try:
            r = _guard(cmd, "local")
        finally:
            approved_effect_var.reset(tok)
        assert r.get("approved") is True and r.get("approved_via_mint") is True

    def test_content_binding_different_command_blocked(self):
        # ContextVar approves command A; a DIFFERENT command B must still block.
        tok = approved_effect_var.set(_sig("sudo apt-get install -y ffmpeg"))
        try:
            assert _guard("sudo rm -rf /home/victim", "local").get("approved") is False
        finally:
            approved_effect_var.reset(tok)

    def test_isolation_after_reset_blocked(self):
        cmd = "sudo apt-get install -y ffmpeg"
        tok = approved_effect_var.set(_sig(cmd))
        approved_effect_var.reset(tok)
        assert approved_effect_var.get() is None
        assert _guard(cmd, "local").get("approved") is False


# ── SAFETY-CRITICAL: the mint can NEVER unblock catastrophic ──────────────────
class TestCatastrophicCannotBeUnblocked:
    def test_hardline_blocks_even_with_matching_contextvar(self):
        cat = "rm -rf /"
        tok = approved_effect_var.set(_sig(cat))  # maliciously set to catastrophic's sig
        try:
            r = _guard(cat, "local")
        finally:
            approved_effect_var.reset(tok)
        assert r.get("approved") is False
        assert r.get("hardline") is True             # hardline floor fired FIRST
        assert r.get("approved_via_mint") is not True

    def test_approve_red_proposal_cannot_execute_catastrophic(self):
        # Even if a catastrophic entry reached approve (it can't — the dispatcher
        # DENIED_BY_POLICY denies before store — this is defense-in-depth), the
        # terminal hardline stops it: execute_error, nothing runs.
        store = get_red_pending_store()
        cat = "rm -rf /"
        args = prepare_execute_arguments("terminal", {"command": cat})
        sig = canonical_effect_signature("terminal", args)
        store.put(PendingRedProposal(
            proposal_id=action_proposal_id(sig), tool_name="terminal", arguments=args,
            effect_signature=sig, description="d", rationale="r",
            created_at="2026-07-08T00:00:00+00:00", pattern_key="rm:catastrophic",
        ))
        res = approve_red_proposal(action_proposal_id(sig), store)
        assert res["success"] is False and res["reason"] == "execute_error"
        assert "hardline" in str(res.get("result", "")).lower() or "blocked" in str(res.get("result", "")).lower()


# ── Gemini ordering constraint: catastrophic returns BEFORE the ContextVar read ─
class _SpyVar:
    def __init__(self, val):
        self._v = val
        self.reads = 0

    def get(self, *a):
        self.reads += 1
        return self._v


class TestOrderingConstraint:
    def test_catastrophic_returns_before_contextvar_read(self, monkeypatch):
        # Spy the approved-effect ContextVar. A catastrophic command must be denied
        # by the early-return WITHOUT the ContextVar ever being read (reads == 0);
        # a non-catastrophic RED command DOES reach the mint check (reads >= 1).
        import grove.red_execution_context as rec
        cat_spy = _SpyVar(_sig("rm -rf /"))
        monkeypatch.setattr(rec, "approved_effect_var", cat_spy)
        r = _guard("rm -rf /", "local")
        assert r.get("hardline") is True
        assert cat_spy.reads == 0                     # ContextVar NEVER read for catastrophic

        ok_spy = _SpyVar(None)
        monkeypatch.setattr(rec, "approved_effect_var", ok_spy)
        _guard("sudo apt-get install -y ffmpeg", "local")
        assert ok_spy.reads >= 1                        # non-catastrophic reaches the mint check


# ── end-to-end: approve_red_proposal EXECUTES a previously-blocked RED command ─
class TestApproveExecutes:
    def test_opaque_red_executes_under_approval(self):
        # echo $(whoami) is RED (opacity) — blocked WITHOUT the mint. Through
        # approve_red_proposal (which sets the ContextVar around dispatch) it
        # executes and returns output. Proves store-pending → approve → EXECUTE.
        store = get_red_pending_store()
        cmd = "echo $(whoami)"
        assert _guard(cmd, "local").get("approved") is False   # blocked un-approved
        args = prepare_execute_arguments("terminal", {"command": cmd})
        sig = canonical_effect_signature("terminal", args)
        store.put(PendingRedProposal(
            proposal_id=action_proposal_id(sig), tool_name="terminal", arguments=args,
            effect_signature=sig, description="d", rationale="r",
            created_at="2026-07-08T00:00:00+00:00", pattern_key="opacity:substitution",
        ))
        res = approve_red_proposal(action_proposal_id(sig), store)
        assert res["success"] is True and res["reason"] == "written"
        payload = json.loads(res["result"])
        assert payload.get("exit_code") == 0 and payload.get("output")  # actually ran
        # hash-what-you-execute: the ContextVar is cleared after dispatch (fail-safe)
        assert approved_effect_var.get() is None
        # single-use: the entry was popped
        assert store.get(action_proposal_id(sig)) is None

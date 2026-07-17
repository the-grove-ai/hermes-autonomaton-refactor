"""unresolved-writer-execution-path-v1 Fix 1 — unified-signature approval honoring.

The live defect: a stored RED shell UNRESOLVED_WRITER whose args carried a
``workdir`` could not be executed on approval — the terminal guard re-hashed only
``{"command": ...}`` while the gate consumed the full-args signature, so they never
matched and the approval was refused with the priv copy. Fix: the terminal handler
threads the EXACT dispatched args (command + workdir + everything) into the guard,
which recomputes ``canonical_effect_signature("terminal", dispatched_args)`` and
honors iff it equals the gate-CONSUMED signature published on
``consumed_signature_var`` by ``registry.dispatch`` — byte-for-byte the same
computation the gate ran. Full-args equality: a different command OR a different
workdir yields a different signature and is refused. No fragment containment, no
arg tolerance.
"""
from __future__ import annotations

from typing import Any

import pytest

from grove.dispatcher import AndonResolutionHalt, Dispatcher
from grove.effect_signature import canonical_effect_signature
from grove.intents import ToolIntent
from grove.red_execution_context import consumed_signature_var
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


@pytest.fixture(autouse=True)
def _clear_consumed_ctx():
    # ensure no leakage between tests
    tok = consumed_signature_var.set(None)
    yield
    consumed_signature_var.reset(tok)


class _FakeGen:
    def send(self, obs: Any) -> Any:
        return obs


def _term(cmd: str, **extra) -> ToolIntent:
    return ToolIntent(tool_name="terminal", arguments={"command": cmd, **extra}, call_id="c1")


# ── the live-defect case: honor an approval whose args carried workdir ────────

class TestUnifiedSignatureHonoring:
    def test_guard_honors_approval_with_workdir(self):
        # Full-args EQUALITY is what honors: the gate consumed the signature over
        # {command, workdir}; the terminal handler threads those SAME args into the
        # guard, which recomputes the identical signature and matches. The presence
        # of workdir (the live-defect arg) no longer breaks the match.
        from tools.approval import check_all_command_guards
        cmd = "git log --oneline -3"
        dispatched = {"command": cmd, "workdir": "/tmp"}
        # Exactly what registry.dispatch consumes and publishes on the ContextVar.
        full_sig = canonical_effect_signature("terminal", dispatched)
        tok = consumed_signature_var.set(full_sig)
        try:
            res = check_all_command_guards(cmd, env_type="local",
                                           dispatched_args=dispatched)
        finally:
            consumed_signature_var.reset(tok)
        assert res["approved"] is True
        assert res.get("approved_via_mint") is True
        # Prove it was EQUALITY, not tolerance: the consumed sig is byte-identical
        # to the signature recomputed over the dispatched args.
        assert canonical_effect_signature("terminal", dispatched) == full_sig

    def test_guard_refuses_different_workdir(self):
        # Same command, DIFFERENT workdir than the one stored/consumed → the
        # recomputed full-args signature differs → refused with
        # MISSING_APPROVAL_CONTEXT (no arg tolerance).
        from tools.approval import check_all_command_guards
        cmd = "git log --oneline -3"
        approved_sig = canonical_effect_signature(
            "terminal", {"command": cmd, "workdir": "/tmp"})
        tok = consumed_signature_var.set(approved_sig)
        try:
            res = check_all_command_guards(
                cmd, env_type="local",
                dispatched_args={"command": cmd, "workdir": "/private/var"})
        finally:
            consumed_signature_var.reset(tok)
        assert res["approved"] is False
        assert res.get("failure_class") == "missing_approval_context"
        assert "MISSING_APPROVAL_CONTEXT" in res["message"]
        assert "privileges that stay with you" not in (res["message"] or "")

    def test_guard_refuses_mismatched_effect(self):
        # Nested/mismatched: the consumed context is for a DIFFERENT command
        # → MISSING_APPROVAL_CONTEXT, never the sovereignty surface.
        from tools.approval import check_all_command_guards
        other_sig = canonical_effect_signature("terminal", {"command": "git status"})
        tok = consumed_signature_var.set(other_sig)
        try:
            res = check_all_command_guards(
                "git log --oneline -3", env_type="local",
                dispatched_args={"command": "git log --oneline -3"})
        finally:
            consumed_signature_var.reset(tok)
        assert res["approved"] is False
        assert res.get("failure_class") == "missing_approval_context"
        assert "MISSING_APPROVAL_CONTEXT" in res["message"]
        # never the priv/sovereignty copy on this gate-required path
        assert "privileges that stay with you" not in (res["message"] or "")

    def test_ungated_red_falls_through_to_legacy(self):
        # No consumed context (ungated call) → NOT MISSING_APPROVAL_CONTEXT; the
        # existing sovereign/descope path still handles it (unchanged).
        from tools.approval import check_all_command_guards
        res = check_all_command_guards("git log --oneline -3", env_type="local")
        assert res["approved"] is False
        assert res.get("failure_class") != "missing_approval_context"


# ── registry publishes the consumed signature on a gated dispatch ────────────

class TestRegistryPublishesConsumedSignature:
    def test_dispatch_sets_and_resets_contextvar(self):
        from tools.registry import ToolRegistry
        from grove.effect_signature import ApprovalGate

        seen = {}

        reg = ToolRegistry()

        def _probe(args, **kw):
            seen["ctx"] = consumed_signature_var.get()
            return "ok"

        reg.register(name="probe_tool", toolset="t", schema={}, handler=_probe)
        gate = ApprovalGate()
        gate.activate()
        reg._approval_gate = gate
        sig = canonical_effect_signature("probe_tool", {"x": 1})
        gate.mint(sig)
        reg.dispatch("probe_tool", {"x": 1})
        assert seen["ctx"] == sig                      # set during the handler
        assert consumed_signature_var.get() is None    # reset after


# ── Fix 2: render_red_surface picks the trigger (hence copy) from classification ─

from types import SimpleNamespace

# The governance-implementation vocab the operator-facing surface must never leak
# (mirrors tests/grove/test_gateway_noise.py::_assert_no_governance_vocab).
_BANNED_VOCAB = [
    "andon", "sovereignty", "sovereign zone", "red-zone", "red zone",
    "yellow zone", "zone violation", "dispatcher halted",
]


def _assert_clean_surface(text: str) -> None:
    low = text.lower()
    hits = [t for t in _BANNED_VOCAB if t in low]
    assert not hits, f"surface leaks governance vocab {hits}: {text!r}"
    assert "access denied" not in low
    assert "forbidden" not in low


class TestFix2RedSurfaceTriggerFromClassification:
    def test_trigger_priv_selects_privilege_required(self):
        from grove.dispatch import _red_surface_trigger
        from grove.halt_event import HaltTrigger
        zr = SimpleNamespace(zone="red", pattern_key="priv:sudo")
        assert _red_surface_trigger("sudo apt install foo", zr) is HaltTrigger.PRIVILEGE_REQUIRED

    def test_trigger_unresolved_writer_selects_named_abnormality(self):
        from grove.dispatch import _red_surface_trigger
        from grove.halt_event import HaltTrigger
        zr = SimpleNamespace(zone="red", pattern_key="UNRESOLVED_WRITER:git:abc123")
        assert _red_surface_trigger("git commit -am x", zr) is HaltTrigger.RED_UNRESOLVED_WRITER

    def test_trigger_other_red_selects_neutral_sovereign_boundary(self):
        from grove.dispatch import _red_surface_trigger
        from grove.halt_event import HaltTrigger
        zr = SimpleNamespace(zone="red", pattern_key="opacity:substitution")
        assert _red_surface_trigger("echo $(whoami)", zr) is HaltTrigger.RED_SOVEREIGN_BOUNDARY

    def test_trigger_priv_dominates_compound_with_unresolved_writer(self):
        # A chain carrying BOTH a priv node and an UNRESOLVED_WRITER node → priv wins
        # (the operator's hands are genuinely required).
        from grove.dispatch import _red_surface_trigger
        from grove.halt_event import HaltTrigger
        zr = SimpleNamespace(zone="red", pattern_key="UNRESOLVED_WRITER:git:abc||priv:sudo")
        assert _red_surface_trigger("git commit && sudo x", zr) is HaltTrigger.PRIVILEGE_REQUIRED

    def test_trigger_absent_pattern_key_falls_back_to_classifying_command(self):
        # zone_result with no pattern_key (None / legacy) → classify the command
        # itself so a real sudo command still gets the privilege surface.
        from grove.dispatch import _red_surface_trigger
        from grove.halt_event import HaltTrigger
        assert _red_surface_trigger("sudo systemctl restart grove", None) is HaltTrigger.PRIVILEGE_REQUIRED

    def test_surface_unresolved_writer_names_abnormality_not_priv(self):
        from grove.dispatch import render_red_surface
        zr = SimpleNamespace(zone="red", pattern_key="UNRESOLVED_WRITER:git:abc123")
        surface = render_red_surface("git commit -am wip", zr)
        assert "git commit -am wip" in surface
        # NOT the privilege copy — that was the live-defect mis-render.
        assert "privileges that stay with you" not in surface
        assert "sudo / su / doas" not in surface
        # names the abnormality (unresolvable write target) + the honest go-forward.
        assert "pin down" in surface
        # ungated/strict BLOCK path — no proposal stored, so NO portal promise.
        assert "portal" not in surface
        assert "Run it yourself and tell me the result" in surface
        assert "give me a different approach" in surface
        _assert_clean_surface(surface)

    def test_surface_other_red_is_neutral_not_priv(self):
        from grove.dispatch import render_red_surface
        zr = SimpleNamespace(zone="red", pattern_key="opacity:substitution")
        surface = render_red_surface("echo $(whoami)", zr)
        assert "echo $(whoami)" in surface
        assert "privileges that stay with you" not in surface
        assert "hold for your decision" in surface
        # ungated/strict BLOCK path — no proposal stored, so NO portal promise.
        assert "portal" not in surface
        assert "give me a different approach" in surface
        _assert_clean_surface(surface)

    def test_surface_priv_still_privilege_copy(self):
        from grove.dispatch import render_red_surface
        zr = SimpleNamespace(zone="red", pattern_key="priv:sudo")
        surface = render_red_surface("sudo apt install foo", zr)
        assert "needs privileges that stay with you" in surface
        assert "sudo / su / doas, never with me" in surface
        _assert_clean_surface(surface)

    def test_surface_unresolved_writer_truncates_long_command(self):
        from grove.dispatch import render_red_surface
        long_cmd = "git commit -m " + "x" * 200
        zr = SimpleNamespace(zone="red", pattern_key="UNRESOLVED_WRITER:git:abc123")
        assert "…" in render_red_surface(long_cmd, zr)


# ── e2e: store → approve → re-dispatch → EXECUTE, with workdir in the args ────

class TestE2EStoreApproveExecute:
    def _to_halt(self, d: Dispatcher, intent: ToolIntent) -> AndonResolutionHalt:
        try:
            d._classify_intents_batch_and_halt_or_raise([intent])
        except AndonResolutionHalt as halt:
            return halt
        raise AssertionError("expected AndonResolutionHalt")

    def test_store_approve_execute_shell_unresolved_writer_with_workdir(self):
        from grove.red_pending_store import approve_red_proposal
        d = Dispatcher()  # reachable → STORE_PENDING
        # `git --version` is a harmless, real, bucket-3 UNRESOLVED_WRITER command;
        # workdir is present in the stored args (the live-defect shape).
        from grove.red_pending_store import action_proposal_id, prepare_execute_arguments
        halt = self._to_halt(d, _term("git --version", workdir="/tmp"))
        d._resolve_red_halt(_bare_agent([]), _FakeGen(), halt)
        assert len(d._red_pending_store) == 1
        # deterministic content-addressed id over the exact stored (tool, args)
        _exec_args = prepare_execute_arguments("terminal", {"command": "git --version", "workdir": "/tmp"})
        pid = action_proposal_id(canonical_effect_signature("terminal", _exec_args))
        result = approve_red_proposal(pid, d._red_pending_store)
        # The guard HONORED the approval (workdir no longer breaks the match) and the
        # command ran — success, NOT the "execute_error" the defect produced.
        assert result.get("success") is True, result
        assert result.get("reason") == "written"

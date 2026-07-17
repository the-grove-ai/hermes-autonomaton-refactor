"""GRV-010 C1c-i — in-process classifier-skip containment.

Verifies the dispatch-primitive crypto lock and that each of the three Gemini
unforgeability vectors is closed:

* reflection  — the approved set stores HMAC digests, not raw signatures, so an
  attacker who reaches the set (but not the per-turn secret) cannot forge a
  consumable token;
* TOCTOU/replay — tokens are single-use (consumed on match) and turn-scoped
  (flushed), so a stale token cannot be replayed;
* symlink     — the effect signature is realpath-canonical and re-resolved at
  consume time, so a target swapped after approval yields a different signature
  and fails the check.
"""

from __future__ import annotations

import json
import os

import pytest

from grove.effect_signature import ApprovalGate, canonical_effect_signature
from grove.errors import GovernanceError


# ── ApprovalGate primitive ────────────────────────────────────────────────


class TestApprovalGate:
    def test_inactive_consume_is_false(self):
        g = ApprovalGate()
        assert g.consume("sig") is False  # not armed → nothing to consume

    def test_mint_then_single_use_consume(self):
        g = ApprovalGate(); g.activate()
        g.mint("sig")
        assert g.consume("sig") is True
        assert g.consume("sig") is False  # single-use — second consume fails

    def test_flush_is_turn_scoped(self):
        g = ApprovalGate(); g.activate()
        g.mint("sig")
        g.flush()
        assert g.active is False
        assert g.consume("sig") is False  # flushed → no cross-turn replay

    def test_secret_rotates_on_activate(self):
        # A token minted under one turn's secret cannot be consumed after a
        # re-activate (new secret) — defends cross-turn replay even without flush.
        g = ApprovalGate(); g.activate()
        g.mint("sig")
        g.activate()  # new turn, rotated secret + cleared set
        assert g.consume("sig") is False


class TestGeminiVectors:
    def test_reflection_raw_signature_injection_does_not_forge(self):
        # Attacker reaches the internal approved set (reflection) and inserts the
        # RAW signature — but consume checks HMAC(secret, sig), not the raw sig,
        # so the injection is not a valid token. Only mint() (which holds the
        # secret) produces a consumable entry.
        g = ApprovalGate(); g.activate()
        sig = canonical_effect_signature("terminal", {"command": "echo x"})
        g._approved[sig] += 1               # forged raw-sig injection
        assert g.consume(sig) is False      # rejected — not an HMAC token
        g.mint(sig)                          # the only legitimate path
        assert g.consume(sig) is True

    def test_toctou_replay_blocked_by_single_use_and_flush(self):
        g = ApprovalGate(); g.activate()
        sig = canonical_effect_signature("write_file", {"path": "/tmp/x"})
        g.mint(sig)
        assert g.consume(sig) is True       # the one legitimate dispatch
        assert g.consume(sig) is False      # replay within turn → blocked
        g.mint(sig); g.flush()
        assert g.consume(sig) is False      # replay across turn → blocked

    def test_symlink_swap_changes_signature(self, tmp_path):
        real = tmp_path / "real"; real.write_text("x")
        other = tmp_path / "other"; other.write_text("y")
        link = tmp_path / "link"; link.symlink_to(real)
        sig_before = canonical_effect_signature("write_file", {"path": str(link)})
        # Approve against the link-resolved target, then swap the symlink.
        link.unlink(); link.symlink_to(other)
        sig_after = canonical_effect_signature("write_file", {"path": str(link)})
        assert sig_before != sig_after      # re-resolution catches the swap
        # And the swap means a token minted before does not consume after.
        g = ApprovalGate(); g.activate()
        g.mint(sig_before)
        assert g.consume(sig_after) is False


class TestCanonicalSignature:
    def test_deterministic(self):
        a = canonical_effect_signature("terminal", {"command": "git status"})
        b = canonical_effect_signature("terminal", {"command": "git status"})
        assert a == b

    def test_symlink_resolves_to_real_target(self, tmp_path):
        real = tmp_path / "f"; real.write_text("x")
        link = tmp_path / "l"; link.symlink_to(real)
        assert (
            canonical_effect_signature("write_file", {"path": str(link)})
            == canonical_effect_signature("write_file", {"path": str(real)})
        )

    def test_distinct_effects_distinct_signatures(self):
        assert (
            canonical_effect_signature("terminal", {"command": "git status"})
            != canonical_effect_signature("terminal", {"command": "git push"})
        )


# ── Dispatch-primitive fail-closed (ToolRegistry.dispatch) ─────────────────


class TestDispatchPrimitiveLock:
    def _registry(self):
        from tools.registry import ToolRegistry
        reg = ToolRegistry()
        reg.register(
            name="demo_tool", toolset="x", schema={"name": "demo_tool"},
            handler=lambda args, **kw: "ran",
        )
        return reg

    def test_unminted_dispatch_in_armed_window_is_refused(self):
        reg = self._registry()
        gate = ApprovalGate(); gate.activate()
        reg._approval_gate = gate
        with pytest.raises(GovernanceError):
            reg.dispatch("demo_tool", {})

    def test_minted_dispatch_consumes_and_runs(self):
        reg = self._registry()
        gate = ApprovalGate(); gate.activate()
        reg._approval_gate = gate
        gate.mint(canonical_effect_signature("demo_tool", {}))
        assert reg.dispatch("demo_tool", {}) == "ran"
        # token was single-use — a second dispatch is refused.
        with pytest.raises(GovernanceError):
            reg.dispatch("demo_tool", {})

    def test_inactive_gate_does_not_enforce(self):
        reg = self._registry()
        reg._approval_gate = ApprovalGate()  # not activated
        assert reg.dispatch("demo_tool", {}) == "ran"

    def test_no_gate_installed_proceeds(self):
        reg = self._registry()  # no _approval_gate → non-governed context
        assert reg.dispatch("demo_tool", {}) == "ran"


# ── classify_and_mint (sandbox/plugin per-site closure) ────────────────────


class TestClassifyAndMint:
    @pytest.fixture
    def dispatcher(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GROVE_HOME", str(tmp_path))
        from grove.dispatcher import Dispatcher
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")
        d._approval_gate.activate()
        return d

    def test_green_mints_and_allows(self, dispatcher):
        # read_file is green in the schema → minted, no prompt, consumable.
        ok, _ = dispatcher.classify_and_mint("read_file", {"path": "/tmp/x"})
        assert ok is True
        assert dispatcher._approval_gate.consume(
            canonical_effect_signature("read_file", {"path": "/tmp/x"})
        ) is True

    def test_yellow_covered_mints_without_prompt(self, dispatcher):
        # write_file is yellow; yellow_covered (sandbox) auto-mints — the deny
        # handler is NOT consulted (no prompt), proving coverage by the grant.
        ok, _ = dispatcher.classify_and_mint(
            "write_file", {"path": "/tmp/x"}, yellow_covered=True,
        )
        assert ok is True

    def test_yellow_not_covered_goes_through_disposition_and_denies(self, hermetic_grove_home, dispatcher):
        # Plugin path (yellow_covered=False): the deny handler runs → not allowed,
        # nothing minted → the primitive would refuse the dispatch.
        ok, why = dispatcher.classify_and_mint("write_file", {"path": "/tmp/x"})
        assert ok is False
        assert dispatcher._approval_gate.consume(
            canonical_effect_signature("write_file", {"path": "/tmp/x"})
        ) is False

"""Kaizen voice unification — Sprint A (GATE-C2).

Tests the fresh HaltEvent struct, the boundary adapter from the
C2a-terminal-coupled GovernanceHaltContext, the base unified renderer, the
infallible fallback, and the renderer-derived Feed Invariant.

GATE-C2 builds the struct + adapter + renderer + fallback ONLY — the RAW/ANDON
bypasses are NOT yet rewired (that is GATE-C3). The renderer is proven to
reproduce the CURRENT wording byte-for-byte (wiring, not copy) by comparing
against the live ``TerminalGovernanceHalt.surface_text`` and
``render_red_surface``.
"""

from __future__ import annotations

import dataclasses

import pytest

from grove.capability import FailureFallback
from grove.dispatch import render_red_surface
from grove.governance_halt import (
    TERMINAL_TRIGGERS,
    GovernanceHaltContext,
    TerminalGovernanceHalt,
)
from grove.halt_event import (
    STEERING_CAPABILITY_FLAGS,
    HaltCapabilities,
    HaltDetail,
    HaltEvent,
    HaltRatchet,
    HaltSeverity,
    HaltTrigger,
    OriginatingLayer,
    WhatHalted,
    halt_event_from_governance_context,
    is_feed_worthy,
)
from grove.halt_renderer import _CRITICAL_FALLBACK, render_halt_event
import grove.halt_renderer as halt_renderer


def _event(**overrides) -> HaltEvent:
    base = dict(
        trigger=HaltTrigger.RED_SOVEREIGN,
        what_halted=WhatHalted(tool_name="shell"),
        zone="red",
        severity=HaltSeverity.TERMINAL,
        originating_layer=OriginatingLayer.C2A_GATE,
    )
    base.update(overrides)
    return HaltEvent(**base)


# ── Struct round-trip ────────────────────────────────────────────────────────


class TestStructRoundTrip:
    def test_asdict_serializes_without_throwing(self):
        event = _event(
            detail=HaltDetail(matched_rule="rm -rf", note="strike 3"),
            capabilities=HaltCapabilities(can_cancel=True, can_operator_run=True),
            ratchet=HaltRatchet(skill_name="s", skill_path="/p"),
        )
        d = dataclasses.asdict(event)
        assert d["trigger"] == "red_sovereign"
        assert d["severity"] == "terminal"
        assert d["capabilities"]["can_operator_run"] is True
        assert d["detail"]["matched_rule"] == "rm -rf"
        assert d["ratchet"]["skill_path"] == "/p"

    def test_equality_and_hashability(self):
        # Frozen dataclasses → value equality and hashability give a faithful
        # round-trip identity.
        a = _event()
        b = _event()
        assert a == b
        assert hash(a) == hash(b)

    def test_no_feed_criterion_field(self):
        # Feed-worthiness is renderer-derived; the struct must carry no hint.
        names = {f.name for f in dataclasses.fields(HaltEvent)}
        assert "feed_criterion" not in names

    def test_capabilities_pin_at_six_flags(self):
        names = {f.name for f in dataclasses.fields(HaltCapabilities)}
        assert names == {
            "can_cancel",
            "can_operator_run",
            "can_descope",
            "can_promote",
            "can_retry",
            "can_configure_fallback",
        }
        assert "can_redirect" not in names


# ── Boundary adapter ─────────────────────────────────────────────────────────


class TestAdapter:
    def test_severity_pinned_terminal_for_every_context_trigger(self):
        # GovernanceHaltContext is C2a-terminal-coupled; the boundary pins it.
        for trigger in TERMINAL_TRIGGERS:
            ctx = GovernanceHaltContext(trigger=trigger, tool_name="shell")
            event = halt_event_from_governance_context(ctx)
            assert event.severity is HaltSeverity.TERMINAL
            assert event.originating_layer is OriginatingLayer.C2A_GATE
            assert event.trigger.value == trigger

    def test_field_mapping(self):
        ctx = GovernanceHaltContext(
            trigger="red_sovereign",
            tool_name="shell",
            zone="red",
            matched_rule="sudo",
            reason="declined",
            detail="extra detail",
            skill_name="sk",
            skill_path="/x.andon",
        )
        event = halt_event_from_governance_context(ctx)
        assert event.what_halted.tool_name == "shell"
        assert event.zone == "red"  # raw str, not lossy-converted
        assert event.detail.matched_rule == "sudo"
        assert event.detail.note == "extra detail"  # kept distinct from matched_rule
        assert event.reason == "declined"
        assert event.fallback is FailureFallback.HALT_AND_SURFACE
        assert event.ratchet.skill_name == "sk"
        assert event.ratchet.skill_path == "/x.andon"

    def test_deny_hard_has_operator_run_path(self):
        # A red-zone strike-limit hard deny is a sovereign action the operator
        # holds; the current surface offers "handle the action yourself".
        ctx = GovernanceHaltContext(trigger="deny_hard", tool_name="shell")
        caps = halt_event_from_governance_context(ctx).capabilities
        assert caps.can_operator_run is True
        assert caps.can_cancel is True

    def test_tier_unavailable_has_no_operator_run(self):
        # Model-availability failure — no tool to run yourself.
        ctx = GovernanceHaltContext(trigger="tier_unavailable")
        caps = halt_event_from_governance_context(ctx).capabilities
        assert caps.can_operator_run is False
        assert caps.can_retry is True
        assert caps.can_configure_fallback is True

    def test_quarantine_promote_only_when_skill_named(self):
        with_skill = halt_event_from_governance_context(
            GovernanceHaltContext(trigger="quarantine", skill_name="s")
        ).capabilities
        without = halt_event_from_governance_context(
            GovernanceHaltContext(trigger="quarantine")
        ).capabilities
        assert with_skill.can_promote is True
        assert without.can_promote is False

    def test_unknown_trigger_fails_loud(self):
        with pytest.raises(ValueError):
            halt_event_from_governance_context(
                GovernanceHaltContext(trigger="not_a_real_trigger")
            )


# ── Renderer: wiring, not copy (byte-for-byte vs. live surfaces) ─────────────


class TestRendererMatchesLiveSurfaces:
    @pytest.mark.parametrize("trigger", list(TERMINAL_TRIGGERS))
    def test_c2a_matches_surface_text(self, trigger):
        ctx = GovernanceHaltContext(
            trigger=trigger,
            tool_name="shell",
            skill_name="my-skill" if trigger == "quarantine" else None,
        )
        live = TerminalGovernanceHalt(ctx).surface_text()
        rendered = render_halt_event(halt_event_from_governance_context(ctx))
        assert rendered == live

    def test_c2a_quarantine_without_skill_matches(self):
        ctx = GovernanceHaltContext(trigger="quarantine", tool_name="shell")
        live = TerminalGovernanceHalt(ctx).surface_text()
        rendered = render_halt_event(halt_event_from_governance_context(ctx))
        assert rendered == live

    def test_privilege_required_matches_render_red_surface(self):
        command = "sudo systemctl restart grove"
        live = render_red_surface(command, zone_result=None)
        event = _event(
            trigger=HaltTrigger.PRIVILEGE_REQUIRED,
            what_halted=WhatHalted(summary=command),
            originating_layer=OriginatingLayer.TOOL_BOUNDARY,
            severity=HaltSeverity.NON_TERMINAL,
        )
        assert render_halt_event(event) == live

    def test_privilege_required_truncates_long_command(self):
        command = "sudo " + "x" * 200
        live = render_red_surface(command, zone_result=None)
        event = _event(
            trigger=HaltTrigger.PRIVILEGE_REQUIRED,
            what_halted=WhatHalted(summary=command),
            originating_layer=OriginatingLayer.TOOL_BOUNDARY,
            severity=HaltSeverity.NON_TERMINAL,
        )
        assert render_halt_event(event) == live
        assert "…" in render_halt_event(event)

    def test_tool_boundary_hard_deny_text(self):
        event = _event(
            trigger=HaltTrigger.DENY_HARD,
            what_halted=WhatHalted(tool_name="shell"),
            originating_layer=OriginatingLayer.TOOL_BOUNDARY,
        )
        assert render_halt_event(event) == (
            "HARD DENIAL: This action is prohibited. "
            "Do not attempt this tool with these arguments again. "
            "(tool: shell)"
        )

    def test_tool_boundary_soft_decline_text(self):
        event = _event(
            trigger=HaltTrigger.OPERATOR_DECLINE,
            what_halted=WhatHalted(tool_name="shell"),
            originating_layer=OriginatingLayer.TOOL_BOUNDARY,
            severity=HaltSeverity.NON_TERMINAL,
        )
        assert render_halt_event(event) == (
            "This action was paused and the operator declined to run "
            "it ('shell'). It did not execute. Continue "
            "with an alternative approach."
        )


# ── Infallible fallback ──────────────────────────────────────────────────────


class TestInfallibleFallback:
    def test_fallback_on_render_throw(self, monkeypatch):
        def boom(_event):
            raise RuntimeError("renderer exploded")

        monkeypatch.setattr(halt_renderer, "_render", boom)
        assert render_halt_event(_event()) == _CRITICAL_FALLBACK

    def test_fallback_on_empty_render(self, monkeypatch):
        # An empty surface is a silent-swallow; the loud literal must fire.
        monkeypatch.setattr(halt_renderer, "_render", lambda _e: "")
        assert render_halt_event(_event()) == _CRITICAL_FALLBACK

    def test_fallback_on_unhandled_layer(self):
        # ROUTER has no renderer branch yet → _render raises → literal, never
        # a silent empty surface or an uncaught exception reaching the operator.
        event = _event(originating_layer=OriginatingLayer.ROUTER)
        assert render_halt_event(event) == _CRITICAL_FALLBACK

    def test_fallback_does_not_trust_str_of_event(self, monkeypatch):
        # str(event) can itself throw; the fallback is a pure literal.
        monkeypatch.setattr(halt_renderer, "_render", lambda _e: 1 / 0)
        out = render_halt_event(_event())
        assert out == _CRITICAL_FALLBACK
        assert "blocked" in out.lower()


# ── Feed Invariant (renderer-derived) ────────────────────────────────────────


class TestFeedInvariant:
    def test_terminal_is_feed_worthy(self):
        assert is_feed_worthy(_event(severity=HaltSeverity.TERMINAL)) is True

    def test_operator_decline_soft_is_not_feed_worthy(self):
        # NON_TERMINAL, no steering flag → Orchestration Bus telemetry, not feed.
        event = _event(
            trigger=HaltTrigger.OPERATOR_DECLINE,
            what_halted=WhatHalted(tool_name="shell"),
            originating_layer=OriginatingLayer.TOOL_BOUNDARY,
            severity=HaltSeverity.NON_TERMINAL,
            capabilities=HaltCapabilities(),
        )
        assert is_feed_worthy(event) is False

    def test_non_terminal_with_steering_flag_is_feed_worthy(self):
        for flag in STEERING_CAPABILITY_FLAGS:
            event = _event(
                severity=HaltSeverity.NON_TERMINAL,
                capabilities=HaltCapabilities(**{flag: True}),
            )
            assert is_feed_worthy(event) is True, flag

    def test_can_cancel_alone_is_not_steering(self):
        # cancel is the null action, present everywhere; it does not earn a feed
        # slot on its own.
        event = _event(
            severity=HaltSeverity.NON_TERMINAL,
            capabilities=HaltCapabilities(can_cancel=True),
        )
        assert is_feed_worthy(event) is False
        assert "can_cancel" not in STEERING_CAPABILITY_FLAGS


# ── GATE-C3 rewire: frozen golden oracle (byte-for-byte preservation) ────────
#
# After the rewire the live surfaces delegate to the renderer, so comparing
# them to the renderer is tautological. These FROZEN literals are the captured
# pre-rewire operator output — the real regression oracle that the rewire did
# not drift a single character (ANDON-copy-drift guard).

_GOLDEN_RED_SOVEREIGN = (
    "This action (shell) requires your approval and was declined. It did not "
    "execute. I've stopped here rather than work around it. Your options: "
    "cancel this request, handle the action yourself, or tell me a different "
    "approach to take."
)
_GOLDEN_QUARANTINE = (
    "This action would run an unapproved (quarantined) skill 'my-skill'. It was "
    "not executed. I've stopped here rather than work around it. Your options: "
    "promote 'my-skill' to your live skills, cancel this request, handle it "
    "yourself, or tell me a different approach to take."
)
_GOLDEN_TIER_UNAVAILABLE = (
    "I couldn't reach the model for this work, and no backup is configured to "
    "take over. I've stopped here rather than guess. Your options: try again in "
    "a moment, cancel this request, or configure a fallback model and retry."
)
_GOLDEN_RED_SURFACE = (
    "That's in your direct control — here's how.\n"
    "\n"
    "The command `sudo systemctl restart grove` needs privileges I deliberately "
    "don't hold — sudo / su / doas stay with you, never with me. Run it "
    "yourself in a terminal that has your credentials, then paste back anything "
    "I need to keep going.\n"
    "\n"
    "To move this line, edit `~/.grove/zones.schema.yaml` (the `red.sovereign` "
    "list) and restart me."
)


class TestC3RewireGolden:
    def test_surface_text_red_sovereign_unchanged(self):
        ctx = GovernanceHaltContext(trigger="red_sovereign", tool_name="shell")
        assert TerminalGovernanceHalt(ctx).surface_text() == _GOLDEN_RED_SOVEREIGN

    def test_surface_text_quarantine_unchanged(self):
        ctx = GovernanceHaltContext(
            trigger="quarantine", tool_name="shell", skill_name="my-skill"
        )
        assert TerminalGovernanceHalt(ctx).surface_text() == _GOLDEN_QUARANTINE

    def test_surface_text_tier_unavailable_unchanged(self):
        ctx = GovernanceHaltContext(trigger="tier_unavailable")
        assert TerminalGovernanceHalt(ctx).surface_text() == _GOLDEN_TIER_UNAVAILABLE

    def test_render_red_surface_unchanged(self):
        assert (
            render_red_surface("sudo systemctl restart grove", None)
            == _GOLDEN_RED_SURFACE
        )

    def test_terminal_halt_result_still_uses_surface_text(self):
        # terminal_halt_result.final_response @156 is produced by surface_text;
        # the rewire must not perturb that contract.
        from grove.governance_halt import terminal_halt_result

        ctx = GovernanceHaltContext(trigger="red_sovereign", tool_name="shell")
        halt = TerminalGovernanceHalt(ctx)
        assert terminal_halt_result(halt)["final_response"] == halt.surface_text()


class TestC3ModelSignalPreserved:
    """The soft OPERATOR_DECLINE is routed OFF the operator feed (non-feed-worthy)
    but MUST still reach the model. Proves the dispatcher's non-interactive deny
    builder still appends the observation to the model's message list."""

    def test_soft_decline_observation_reaches_model_messages(self):
        from grove.dispatcher import Dispatcher
        from grove.intents import ToolIntent

        d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")

        class _Agent:
            _current_messages: list = []

        agent = _Agent()
        agent._current_messages = []
        intent = ToolIntent(tool_name="shell", arguments={}, call_id="c1")

        obs = d._build_skip_observations(agent, [intent], hard=False)

        # Model receipt: the denial is appended to the model's message list.
        assert len(agent._current_messages) == 1
        msg = agent._current_messages[0]
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "c1"
        assert "declined to run" in msg["content"]
        # The Observation carries the same text and the unchanged disposition.
        assert obs[0].value == msg["content"]
        assert obs[0].metadata["disposition"] == "deny"

    def test_hard_deny_observation_reaches_model_messages(self):
        from grove.dispatcher import Dispatcher
        from grove.intents import ToolIntent

        d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")

        class _Agent:
            _current_messages: list = []

        agent = _Agent()
        agent._current_messages = []
        intent = ToolIntent(tool_name="shell", arguments={}, call_id="c2")

        obs = d._build_skip_observations(agent, [intent], hard=True)

        assert len(agent._current_messages) == 1
        assert agent._current_messages[0]["content"].startswith("HARD DENIAL:")
        assert obs[0].metadata["disposition"] == "deny_hard"
        assert obs[0].metadata["is_hard_denial"] is True

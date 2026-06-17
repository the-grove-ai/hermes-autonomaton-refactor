"""The base unified halt renderer (Kaizen voice, Sprint A).

One function turns a :class:`grove.halt_event.HaltEvent` into operator-facing
text, and it is the **sole Feed-Commit Enforcement Point** for the Sprint-A
rewired surfaces: a halt that fails the Feed Invariant
(:func:`grove.halt_event.is_feed_worthy`) is routed to Orchestration Bus
telemetry, never the permanent feed.

Sprint A is WIRING, NOT COPY. Each branch reproduces the CURRENT wording of the
surface it will replace at GATE-C3 — the renderer relocates the text, it does
not rewrite it. Capability flags feed the struct contract and feed-worthiness
derivation ONLY; they do NOT compose new visible option menus here (that is
Sprint B). Adding rendered options for these surfaces would be ANDON-copy-drift.

The public entrypoint :func:`render_halt_event` is INFALLIBLE: the renderer is
attempted inside a guard, and ANY exception (a lazy property that throws, an
un-stringifiable enum, recursion) falls through to a hardcoded string literal —
``str(event)`` is NOT trusted as the fallback because it can itself throw. The
operator is never left with a silent empty surface (no silent-swallow).
"""

from __future__ import annotations

from grove.halt_event import HaltEvent, HaltTrigger, OriginatingLayer

# The final defense. A pure literal — no struct access, so it cannot itself
# throw. Loud by construction: the operator sees that the action was blocked.
_CRITICAL_FALLBACK = (
    "CRITICAL GOVERNANCE HALT: renderer + serialization failed. Action blocked."
)


def _render_c2a(event: HaltEvent) -> str:
    """Reproduce ``TerminalGovernanceHalt.surface_text`` (governance_halt.py
    :89-137) for a context-sourced halt. Byte-for-byte; wiring, not copy."""
    tool = f" ({event.what_halted.tool_name})" if event.what_halted.tool_name else ""
    trigger = event.trigger
    if trigger is HaltTrigger.QUARANTINE:
        skill_name = event.ratchet.skill_name
        named = f" '{skill_name}'" if skill_name else ""
        what = (
            f"This action would run an unapproved (quarantined) skill{named}. "
            "It was not executed."
        )
        if skill_name:
            return (
                f"{what} I've stopped here rather than work around it. "
                f"Your options: promote '{skill_name}' to your live "
                "skills, cancel this request, handle it yourself, or tell me "
                "a different approach to take."
            )
    elif trigger is HaltTrigger.GOVERNANCE_ERROR:
        what = (
            f"This action{tool} could not be verified as governed and was "
            "refused before it could run."
        )
    elif trigger is HaltTrigger.TIER_UNAVAILABLE:
        return (
            "I couldn't reach the model for this work, and no backup is "
            "configured to take over. I've stopped here rather than guess. "
            "Your options: try again in a moment, cancel this request, or "
            "configure a fallback model and retry."
        )
    else:  # red_sovereign / deny_hard
        what = (
            f"This action{tool} requires your approval and was declined. "
            "It did not execute."
        )
    return (
        f"{what} I've stopped here rather than work around it. "
        "Your options: cancel this request, handle the action yourself, or "
        "tell me a different approach to take."
    )


def _render_tool_boundary(event: HaltEvent) -> str:
    """Reproduce the RAW build-time surfaces: the dispatcher's non-interactive
    deny observations (dispatcher.py:4700-4720) and the red-zone privilege
    surface (dispatch.py:render_red_surface, text 246-256). Wiring, not copy."""
    trigger = event.trigger
    tool_name = event.what_halted.tool_name
    if trigger is HaltTrigger.DENY_HARD:
        return (
            f"HARD DENIAL: This action is prohibited. "
            f"Do not attempt this tool with these arguments again. "
            f"(tool: {tool_name})"
        )
    if trigger is HaltTrigger.OPERATOR_DECLINE:
        return (
            f"This action was paused and the operator declined to run "
            f"it ('{tool_name}'). It did not execute. Continue "
            f"with an alternative approach."
        )
    if trigger is HaltTrigger.PRIVILEGE_REQUIRED:
        command = event.what_halted.summary or ""
        snippet = command if len(command) <= 120 else command[:117] + "…"
        return (
            "That's in your direct control — here's how.\n"
            "\n"
            f"The command `{snippet}` needs privileges I deliberately don't "
            f"hold — sudo / su / doas stay with you, never with me. Run it "
            f"yourself in a terminal that has your credentials, then paste back "
            f"anything I need to keep going.\n"
            "\n"
            "To move this line, edit `~/.grove/zones.schema.yaml` (the "
            "`red.sovereign` list) and restart me."
        )
    raise ValueError(
        f"unhandled tool-boundary trigger: {trigger!r}"
    )  # fail-loud, never a silent default surface


def _render(event: HaltEvent) -> str:
    """Dispatch on originating layer (which disambiguates shared triggers such
    as ``DENY_HARD``). May raise; the public entrypoint guards it."""
    layer = event.originating_layer
    if layer is OriginatingLayer.C2A_GATE:
        return _render_c2a(event)
    if layer is OriginatingLayer.TOOL_BOUNDARY:
        return _render_tool_boundary(event)
    raise ValueError(f"unhandled originating layer: {layer!r}")


def render_halt_event(event: HaltEvent) -> str:
    """Infallible operator-facing render of a halt event.

    Attempts the real renderer; on ANY failure — or an empty/non-string result —
    returns :data:`_CRITICAL_FALLBACK`. Never returns a silent empty surface,
    never trusts ``str(event)`` (which can throw).
    """
    try:
        text = _render(event)
        if not isinstance(text, str) or not text:
            raise ValueError("renderer produced an empty or non-string surface")
        return text
    except Exception:
        return _CRITICAL_FALLBACK

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

from typing import Optional

from grove.action_facts import describe_action_kaizen
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
    elif trigger is HaltTrigger.RED_WORKFLOW_CANCEL:
        # §VI (kaizen-voice Sprint B2) — the operator chose Cancel at the RED
        # workflow-resolution menu: abort the structurally-blocked workflow.
        # Distinct from the red_sovereign/deny_hard "requires your approval"
        # copy below — RED is a hard structural boundary, not a grantable
        # action. Factual RED register; no alternatives footer.
        #
        # governance-gateway-parity-v1 (Strike 1) — VERBOSE CANCEL: the bare
        # "structurally prohibited / cancelled" surface left a gateway operator
        # with no remediation. Name WHAT was blocked (threaded via ctx.detail ->
        # HaltDetail.note at the _resolve_red_halt raise site) and the lever to
        # change it, so Cancel is actionable rather than a dead end. Honest only:
        # no interactive De-scope/resume promise — that bridge ships in Strike 2.
        return _render_red_cancel(event.detail.note)
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


def _render_red_cancel(action_summary: Optional[str]) -> str:
    """Verbose-Cancel surface for a structurally-blocked RED workflow that the
    operator (or the headless fail-safe) resolved to Cancel — governance-gateway-
    parity-v1 (Strike 1).

    Names WHAT was blocked (``action_summary`` is the shared
    :func:`grove.action_facts.describe_action_kaizen` phrase, threaded through
    ``GovernanceHaltContext.detail`` at the raise site) and the operator's lever
    to change it: edit the governing config and restart, or ask for a within-
    bounds approach. It deliberately makes NO interactive De-scope/resume
    promise — the resumable bridge that would let the operator pick an
    alternative from this surface ships in Strike 2; promising it here would be
    the dishonest pause-then-terminate button the sprint forbids. Standards
    register; carries no governance implementation terms (Andon / zone /
    sovereignty) the agent would parrot back.
    """
    action = (action_summary or "").strip()
    if action:
        lead = f"I can't {action} on my own authority"
    else:
        lead = "I can't complete that step on my own authority"
    return (
        f"{lead} — that crosses a hard boundary, so it didn't run. "
        "Tell me a different approach within my authority and I'll pursue it."
    )


def _render_red_pending_approval(
    description: Optional[str], portal_url: Optional[str] = None
) -> str:
    """Operator-facing copy for a RED proposal STORED for approval —
    propose-approve-deadlock-v1 Phase 1a (STORE_PENDING).

    The action was NOT cancelled: it is proposed and queued for the operator's
    approval. Proposal register (not the hard-boundary interrupt): honest about
    what happens next and where to act. NO internals (no member name / hash /
    map). ``description`` may name the key (the operator authored it) but never a
    value — the caller supplies a value-masked description.
    """
    what = (description or "").strip() or "The change"
    tail = (
        f" Review it in the portal: {portal_url}"
        if portal_url
        else " Review it in the portal."
    )
    return (
        f"Proposed — it's waiting for your approval. {what}"
        f"{tail} Nothing was written until you approve."
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
            f"The command `{snippet}` needs privileges that stay with you "
            "— sudo / su / doas, never with me. Run it in your terminal, "
            "then tell me the result so I can keep going."
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


def render_yellow_sovereign_prompt(tool_name: str, arguments: dict) -> str:
    """The YELLOW four-choice Sovereign Prompt TEXT (kaizen-voice Sprint B1 fold).

    GRV-005 §VI: after the RED hard fork, the four-choice disposition menu
    (``once`` / ``session`` / ``always`` / ``deny``) is a YELLOW-only surface —
    a permission grant. Sprint 32 inlined this text inside
    ``sovereign_prompt_handlers.tty_sovereign_prompt``; Sprint B1 relocates the
    TEXT here (the ``input()`` loop is I/O and stays at the call site) so the
    renderer owns operator-facing copy. This is RELOCATION, NOT REWRITE — the
    returned block is byte-identical to the prior inline ``print`` sequence
    (Sprint 32 vocabulary, Sprint 60 concierge refresh): a leading blank line,
    the first-person header carrying :func:`describe_action_kaizen`, a blank
    line, the four numbered choices, and a trailing newline. The caller emits it
    with a single ``print(block, file=out)``, which appends the final blank line
    the prior eighth ``print()`` produced.

    The fact (WHAT the agent wants to do) comes from the shared
    :mod:`grove.action_facts` layer; the TONE (this four-choice permission
    framing) is owned here and shared with no other surface (§VI ANDON-tone
    isolation). No RED branch exists on this surface — RED never reaches it.
    """
    description = describe_action_kaizen(tool_name, arguments or {})
    lines = (
        "",
        f"I'd like to {description}. This one's your call before I go ahead.",
        "",
        "  [1] Just this once",
        "  [2] For the rest of this session",
        "  [3] Always — I'll remember it",
        "  [4] Not this time",
    )
    return "\n".join(lines) + "\n"


def render_red_resolution_prompt(tool_name: str, arguments: dict) -> str:
    """The §VI RED workflow-resolution menu TEXT (kaizen-voice Sprint B2).

    GRV-005 §VI: RED is a STRUCTURAL block, not a permission grant. Where the
    YELLOW four-choice prompt (:func:`render_yellow_sovereign_prompt`) asks the
    operator to GRANT a disposition, this surface asks the operator to RESOLVE a
    structurally-blocked workflow — Cancel (abort) or De-scope (drop the blocked
    action and let the autonomaton re-plan within authority). These are the only
    two RED resolutions; there is no once/session/always/deny here, and no
    Operator-Runs-It (the resumable bridge is a deferred track — GATE-A leg-b
    re-yield NOT-PROVEN).

    Mirrors the YELLOW fold's SHAPE: a leading blank line, a first-person header
    carrying :func:`describe_action_kaizen`, a blank line, the numbered choices,
    and a trailing newline. The caller emits it with one ``print(block, file=out)``.

    The fact (WHAT the agent wanted to do) comes from the shared
    :mod:`grove.action_facts` layer — byte-identical to every other surface. The
    TONE (this RED hard-boundary-interrupt framing) is owned here and shared with
    no other surface (§VI ANDON-tone isolation). This is the INTERRUPT register,
    NOT the proposal register: the action is blocked and will not proceed without
    the operator's resolution.
    """
    description = describe_action_kaizen(tool_name, arguments or {})
    lines = (
        "",
        f"This crosses a hard boundary and won't run on my authority: "
        f"{description}. Your call on how we proceed.",
        "",
        "  [1] Cancel — stop here",
        "  [2] De-scope — drop it and let me re-plan within bounds",
    )
    return "\n".join(lines) + "\n"

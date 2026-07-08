"""Terminal governance-halt control-flow signal (GRV-010 C2a — B15 fail-loud core).

When a **structural** governed denial fires, the agent's autonomous turn must
END and surface to the operator — not improvise a shell/manual workaround. The
four structural triggers:

  * ``red_sovereign``   — an operator declines a RED (sovereign-approval) action.
  * ``deny_hard``       — the red-zone strike limit forces a hard denial
                          (``grove.dispatcher`` strike counter).
  * ``quarantine``      — a quarantined ``.andon`` skill invocation is declined.
  * ``governance_error``— the dispatch primitive's ``GovernanceError`` (a
                          classifier-skip reached the crypto-lock with no token).

Contrast with an ordinary **Yellow** operator decline ("not now"), which stays
collaborative: the soft observation is returned and the agent may try a
different, governed approach. C2a terminalizes only the structural set.

:class:`TerminalGovernanceHalt` subclasses **BaseException** — like
:class:`grove.operator_input.OperatorInputRequired` — so it propagates UNCAUGHT
past the ~dozen ``except Exception`` catch-alls between the raise sites
(``grove.dispatcher`` deny fork, ``grove.tool_executor``) and the surface's
terminal catch. Unlike ``OperatorInputRequired`` it is **terminal, not
resumable**: there is no ``PendingOperatorRequest`` and no store-and-resume.
Reusing ``OperatorInputRequired`` would make the gateway RESUME the turn on the
operator's next message — re-opening B15 — so this is a distinct terminal type.

The surface outer loops (gateway / CLI / api_server / …) recognize the halt and
end-turn → flush → surface the Kaizen disposition (cancel / operator-handles /
descoped-alternative). The behavior honors the declarative
``FailureFallback.HALT_AND_SURFACE`` policy (``grove.capability``) — the default
failure fallback on every capability record; ``ESCALATE_TIER`` /
``DEGRADE_TO_PULL`` are the tier-unavailable concern of a later sprint (C2d).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from grove.capability import FailureFallback


# The structural triggers that terminalize a turn. Ordinary Yellow declines are
# NOT in this set — they remain collaborative.
TERMINAL_TRIGGERS = (
    "red_sovereign",
    "deny_hard",
    "quarantine",
    "governance_error",
    # GRV-010 C2d — the current cognitive tier's model is unreachable and no
    # governed fallback tier is declared/available; the turn fails loud rather
    # than silently swapping to an undeclared model.
    "tier_unavailable",
    # GRV-005 §VI (kaizen-voice Sprint B1) — the operator aborted a structurally
    # RED-blocked workflow (Cancel). Reuses the terminal mechanism with distinct
    # provenance from ``red_sovereign``: "operator aborted a structurally-blocked
    # workflow", not "a sovereign-approval action was declined". The RED operator
    # menu that surfaces this choice ships in Sprint B2; B1 wires the trigger so
    # the resolution logic + C2a boundary adapter (HaltTrigger(ctx.trigger))
    # resolve it.
    "red_workflow_cancel",
    # red-action-store-pending-v1 Phase B — a deny-listed catastrophic RED effect
    # (grove.red_policy) is terminated on ANY surface, never store-pending/executed.
    # Distinct provenance from red_workflow_cancel: "on the deny-list", not
    # "operator aborted". Legible: the detail names how to change the policy.
    "red_denied_by_policy",
)


@dataclass
class GovernanceHaltContext:
    """Diagnostic context for a terminal governance halt, surfaced to the operator.

    ``trigger`` is one of :data:`TERMINAL_TRIGGERS`. ``fallback`` records the
    declarative policy being honored (always ``HALT_AND_SURFACE`` for C2a).
    """

    trigger: str
    tool_name: Optional[str] = None
    zone: Optional[str] = None
    matched_rule: Optional[str] = None
    reason: Optional[str] = None
    detail: Optional[str] = None
    fallback: FailureFallback = FailureFallback.HALT_AND_SURFACE
    # GRV-010 C2b §V ratchet — the promote target for a ``quarantine`` halt: the
    # quarantined skill's name (capability id) and its .andon path. Populated at
    # the Level-1 quarantine raise site so the surface can offer the operator-only
    # 1-tap promote. None for non-quarantine triggers.
    skill_name: Optional[str] = None
    skill_path: Optional[str] = None


class TerminalGovernanceHalt(BaseException):
    """Raised inside the agent thread to TERMINATE the autonomous turn on a
    structural governed denial. Terminal, not resumable (cf.
    :class:`grove.operator_input.OperatorInputRequired`)."""

    def __init__(self, context: GovernanceHaltContext) -> None:
        self.context = context
        # governance-representation-v1 (cascade coverage): when a governed
        # write_file was blocked EARLIER this turn and the model then cascaded
        # into a shell write that triggers THIS halt, the halt exit bypasses
        # the normal _apply_mutation_verifier seam. The dispatcher attaches the
        # ⛔ governance-block text here so terminal_halt_result surfaces it
        # above the halt message. Representation only — None unless a governed
        # write was recorded.
        self.pending_governed_block: Optional[str] = None
        super().__init__(self.surface_text())

    def surface_text(self) -> str:
        """Operator-facing message in the standards/butler register.

        Per the editorial rule, this MUST NOT carry governance implementation
        terms (Andon / zone / sovereignty) the agent would parrot back — it
        states what happened and the operator's options, no judgment.
        """
        # Sprint A (kaizen-voice) — this surface is now produced by the unified
        # HaltEvent renderer. The wording is preserved byte-for-byte (wiring,
        # not copy): the renderer reproduces exactly the per-trigger text this
        # method used to inline. Imports are function-local because
        # ``grove.halt_event`` imports THIS module for its boundary adapter — a
        # module-level import here would be a cycle.
        from grove.halt_event import halt_event_from_governance_context
        from grove.halt_renderer import render_halt_event

        return render_halt_event(halt_event_from_governance_context(self.context))


def terminal_halt_result(
    halt: "TerminalGovernanceHalt",
    *,
    messages: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the canonical end-of-turn result dict for a terminal governance halt.

    The shape matches ``run_conversation``'s contract so every surface renders it
    uniformly. The turn is marked ``failed`` (the agent's autonomous goal did not
    complete) and ``governance_terminated`` (the recognition flag surfaces use to
    render the Kaizen disposition). There is NO ``awaiting_operator`` field — this
    is terminal, not resumable; the store-and-resume path is
    ``OperatorInputRequired`` alone.
    """
    ctx = halt.context
    # governance-representation-v1 (cascade coverage): if a governed write was
    # blocked earlier in this (now halt-terminated) turn, surface the ⛔ block
    # ABOVE the halt message so the operator always sees it regardless of which
    # exit path the turn took. None unless the dispatcher attached it.
    _surface = halt.surface_text()
    _pending = getattr(halt, "pending_governed_block", None)
    if _pending:
        _surface = _pending + "\n\n— — —\n\n" + _surface
    result: Dict[str, Any] = {
        "final_response": _surface,
        "messages": messages if messages is not None else [],
        "api_calls": 0,
        "completed": True,
        "partial": False,
        "failed": True,
        "governance_terminated": True,
        "governance_trigger": ctx.trigger,
        # The declarative policy honored at this halt path.
        "failure_fallback": ctx.fallback.value,
        "error": f"governed turn terminated ({ctx.trigger}) — fail-loud, no improvisation",
    }
    # §V ratchet — surface the operator-only 1-tap promote target for a
    # quarantine halt. The surface renders this as an actionable option; the tap
    # invokes :func:`operator_promote_quarantined` (operator action, never agent).
    if ctx.trigger == "quarantine" and ctx.skill_name:
        result["governance_promote_target"] = ctx.skill_name
    return result


def operator_promote_quarantined(skill_name: str, *, replace: bool = False) -> Dict[str, Any]:
    """OPERATOR-ONLY §V ratchet — promote a quarantined skill to the live tree.

    This is the action behind the 1-tap offered at a ``quarantine`` halt. It is
    a thin wrapper over the existing operator-approved, ledgered
    :func:`grove.sovereignty.promote` (which writes a ``sovereignty_decision``
    provenance record). §V invariant: the system cannot promote itself — only an
    operator, by tapping/typing the promotion at the surface, may invoke this.
    The agent's autonomous turn has already TERMINATED at the halt and has no
    path here; the tap IS the Stage-04 operator act.
    """
    from grove.sovereignty import promote
    return promote(skill_name, replace=replace)

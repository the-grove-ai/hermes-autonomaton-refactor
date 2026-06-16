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
TERMINAL_TRIGGERS = ("red_sovereign", "deny_hard", "quarantine", "governance_error")


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


class TerminalGovernanceHalt(BaseException):
    """Raised inside the agent thread to TERMINATE the autonomous turn on a
    structural governed denial. Terminal, not resumable (cf.
    :class:`grove.operator_input.OperatorInputRequired`)."""

    def __init__(self, context: GovernanceHaltContext) -> None:
        self.context = context
        super().__init__(self.surface_text())

    def surface_text(self) -> str:
        """Operator-facing message in the standards/butler register.

        Per the editorial rule, this MUST NOT carry governance implementation
        terms (Andon / zone / sovereignty) the agent would parrot back — it
        states what happened and the operator's options, no judgment.
        """
        ctx = self.context
        tool = f" ({ctx.tool_name})" if ctx.tool_name else ""
        if ctx.trigger == "quarantine":
            what = (
                f"This action would run an unapproved (quarantined) skill{tool}. "
                "It was not executed."
            )
        elif ctx.trigger == "governance_error":
            what = (
                f"This action{tool} could not be verified as governed and was "
                "refused before it could run."
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
    result: Dict[str, Any] = {
        "final_response": halt.surface_text(),
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
    return result

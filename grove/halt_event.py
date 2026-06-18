"""Unified halt event — the fresh struct behind the Kaizen voice (Sprint A).

GRV-010 C2a gave us :class:`grove.governance_halt.GovernanceHaltContext`, but
that struct is **C2a-terminal-coupled**: every instance terminalizes a turn
(``terminal_halt_result`` assumes the turn ended). Widening it to carry a
recoverable, non-terminal halt would bleed terminal semantics into the
collaborative path (the Q1 risk).

:class:`HaltEvent` is therefore a FRESH struct, not a generalization.
``GovernanceHaltContext`` maps INTO it via the single boundary adapter
:func:`halt_event_from_governance_context` — the one place where terminal
semantics attach to context-sourced halts (``severity = TERMINAL``, pinned at
the boundary). The two RAW build-time surfaces (the dispatcher's
non-interactive deny observations and the red-zone privilege surface)
construct ``HaltEvent`` directly at their sites with their own structural
facts.

Feed-worthiness is **renderer-derived**, never carried on the struct: there is
deliberately NO ``feed_criterion`` field. Producers report structural facts
only (severity + capability flags + zone); the renderer is the sole
Feed-Commit Enforcement Point that decides feed vs. Orchestration Bus
telemetry. See :func:`is_feed_worthy`.

This shape is the contract Sprint 77.3 binds to — the field names are pinned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from grove.capability import FailureFallback
from grove.governance_halt import GovernanceHaltContext


class HaltTrigger(str, Enum):
    """What caused the halt. The first five mirror
    :data:`grove.governance_halt.TERMINAL_TRIGGERS` 1:1; the last two name the
    RAW build-time surfaces rewired in Sprint A."""

    RED_SOVEREIGN = "red_sovereign"
    DENY_HARD = "deny_hard"
    QUARANTINE = "quarantine"
    GOVERNANCE_ERROR = "governance_error"
    TIER_UNAVAILABLE = "tier_unavailable"
    # The non-interactive SOFT operator decline (dispatcher skip-observation
    # else-branch). Recoverable — the agent re-plans on an alternative. The
    # canonical NON_TERMINAL, non-feed-worthy halt.
    OPERATOR_DECLINE = "operator_decline"
    # A command needs privileges the agent deliberately does not hold
    # (sudo / su / doas) — the red-zone "you run it yourself" surface.
    PRIVILEGE_REQUIRED = "privilege_required"
    # ── GRV-005 §VI RED workflow-resolution triggers (kaizen-voice Sprint B1) ──
    # A RED halt post-§VI is resolved (not disposed): the operator's structurally
    # blocked workflow is either aborted or de-scoped. These two name the RED
    # resolution provenance; they are NOT dispositions and never mint a token.
    #
    # Cancel — the operator aborts a structurally-blocked workflow. Distinct from
    # RED_SOVEREIGN (a declined sovereign-approval action): same terminal
    # mechanism, different ledger provenance. Listed in
    # ``grove.governance_halt.TERMINAL_TRIGGERS`` so the C2a boundary adapter
    # resolves it.
    RED_WORKFLOW_CANCEL = "red_workflow_cancel"
    # De-scoped — the operator drops the privileged action and the agent re-plans
    # on a within-authority alternative. FEED-WORTHY (a genuine steering
    # decision, surfaced via ``can_descope``); distinct from the non-feed-worthy
    # OPERATOR_DECLINE soft auto-decline. NON_TERMINAL — the turn continues.
    OPERATOR_DESCOPED = "operator_descoped"


class HaltSeverity(str, Enum):
    """Whether the halt ends the autonomous turn. This is the axis
    ``GovernanceHaltContext`` lacks — and the reason ``HaltEvent`` is a fresh
    struct rather than a widening of it."""

    TERMINAL = "terminal"
    NON_TERMINAL = "non_terminal"


class OriginatingLayer(str, Enum):
    """Where the halt was produced. Disambiguates surfaces that share a trigger
    (e.g. ``DENY_HARD`` renders differently from ``C2A_GATE`` vs.
    ``TOOL_BOUNDARY``)."""

    TOOL_BOUNDARY = "tool_boundary"
    ROUTER = "router"
    C2A_GATE = "c2a_gate"


@dataclass(frozen=True)
class WhatHalted:
    """The action that was stopped. ``effect_signature`` is populated only where
    the producer has the canonical signature (the tool boundary); the C2a
    adapter leaves it ``None`` (``GovernanceHaltContext`` carries no signature).
    ``summary`` is a short human label of the halted action (e.g. the command
    string for a privilege halt)."""

    tool_name: Optional[str] = None
    effect_signature: Optional[str] = None
    summary: Optional[str] = None


@dataclass(frozen=True)
class HaltDetail:
    """Diagnostic detail. ``matched_rule`` and ``note`` keep
    ``GovernanceHaltContext``'s two distinct free fields (``matched_rule`` and
    ``detail``) separate rather than collapsing them."""

    matched_rule: Optional[str] = None
    note: Optional[str] = None


@dataclass(frozen=True)
class HaltCapabilities:
    """Capability FLAGS — structural facts about what the operator *could* do,
    NOT composed option menus. The renderer reads these (with zone + severity)
    to derive feed-worthiness; composing them into visible operator menus is
    Sprint B. ``can_cancel`` is the null action present everywhere and is
    deliberately NOT a steering flag (see :data:`STEERING_CAPABILITY_FLAGS`)."""

    can_cancel: bool = False
    can_operator_run: bool = False
    can_descope: bool = False
    can_promote: bool = False
    can_retry: bool = False
    can_configure_fallback: bool = False


@dataclass(frozen=True)
class HaltRatchet:
    """GRV-010 C2b §V promote target — the quarantined skill's id and ``.andon``
    path. Populated only for a ``QUARANTINE`` halt; ``None`` otherwise."""

    skill_name: Optional[str] = None
    skill_path: Optional[str] = None


@dataclass(frozen=True)
class HaltEvent:
    """A unified, layer-agnostic halt. Pinned shape (Sprint 77.3 contract).

    Note the absence of any feed-criterion field: feed-worthiness is derived by
    the renderer from ``severity`` + ``capabilities`` + ``zone``.
    """

    trigger: HaltTrigger
    what_halted: WhatHalted
    zone: Optional[str]  # raw str ("green"/"yellow"/"red") to match ZoneResult
    severity: HaltSeverity
    originating_layer: OriginatingLayer
    reason: Optional[str] = None
    detail: HaltDetail = field(default_factory=HaltDetail)
    capabilities: HaltCapabilities = field(default_factory=HaltCapabilities)
    fallback: FailureFallback = FailureFallback.HALT_AND_SURFACE
    ratchet: HaltRatchet = field(default_factory=HaltRatchet)


# The capability flags that represent a genuine operator STEERING decision (the
# operator must choose between meaningful paths). ``can_cancel`` is excluded: it
# is the always-available null action, not a decision that earns a feed slot.
STEERING_CAPABILITY_FLAGS = (
    "can_operator_run",
    "can_descope",
    "can_promote",
    "can_retry",
    "can_configure_fallback",
)


def is_feed_worthy(event: HaltEvent) -> bool:
    """The Feed Invariant, in code. A halt earns the permanent feed iff it is a
    Terminal milestone (``severity == TERMINAL``) OR carries a steering
    decision (any flag in :data:`STEERING_CAPABILITY_FLAGS`). Everything else —
    a recoverable decline the agent simply re-plans around — is Orchestration
    Bus telemetry, never the feed.
    """
    if event.severity is HaltSeverity.TERMINAL:
        return True
    caps = event.capabilities
    return any(getattr(caps, flag) for flag in STEERING_CAPABILITY_FLAGS)


def _capabilities_for_c2a(
    trigger: HaltTrigger, skill_name: Optional[str]
) -> HaltCapabilities:
    """Derive capability flags for a context-sourced (C2a) halt from the
    structural facts its current operator surface offers (see
    ``grove.governance_halt.TerminalGovernanceHalt.surface_text``).

    * ``tier_unavailable`` — a model-availability failure; there is no tool the
      operator can run. Offers retry + configure-a-fallback, NOT operator-run.
    * ``quarantine`` — offers operator-run + (iff a skill is named) the §V
      1-tap promote.
    * ``red_sovereign`` / ``deny_hard`` / ``governance_error`` — the action is
      the operator's to perform; the surface offers "handle the action
      yourself", so ``can_operator_run`` is accurate.
    """
    if trigger is HaltTrigger.TIER_UNAVAILABLE:
        return HaltCapabilities(
            can_cancel=True, can_retry=True, can_configure_fallback=True
        )
    if trigger is HaltTrigger.QUARANTINE:
        return HaltCapabilities(
            can_cancel=True, can_operator_run=True, can_promote=bool(skill_name)
        )
    return HaltCapabilities(can_cancel=True, can_operator_run=True)


def halt_event_from_governance_context(ctx: GovernanceHaltContext) -> HaltEvent:
    """Boundary adapter — the SOLE bridge from the C2a-terminal-coupled
    ``GovernanceHaltContext`` into the layer-agnostic ``HaltEvent``.

    ``severity`` is pinned to ``TERMINAL`` here: this is exactly the boundary
    where C2a's terminal coupling attaches, and pinning it in one place keeps
    that semantic out of the recoverable paths. ``HaltTrigger(ctx.trigger)``
    raises (fail-loud) if the trigger string is not a known member rather than
    silently coercing.
    """
    trigger = HaltTrigger(ctx.trigger)
    return HaltEvent(
        trigger=trigger,
        what_halted=WhatHalted(tool_name=ctx.tool_name),
        zone=ctx.zone,
        severity=HaltSeverity.TERMINAL,
        originating_layer=OriginatingLayer.C2A_GATE,
        reason=ctx.reason,
        detail=HaltDetail(matched_rule=ctx.matched_rule, note=ctx.detail),
        capabilities=_capabilities_for_c2a(trigger, ctx.skill_name),
        fallback=ctx.fallback,
        ratchet=HaltRatchet(skill_name=ctx.skill_name, skill_path=ctx.skill_path),
    )

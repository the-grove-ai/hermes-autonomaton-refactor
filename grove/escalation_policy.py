"""Grove Escalation Policy — Sprint 30 escalation-signal-v1.

The Dispatcher's deterministic auto-decide policy for
:class:`grove.intents.EscalationRequest`. Locked at GATE-A: no
synchronous operator prompt, no Kaizen-mediated halt. Auto-grant
within budget/ceiling. Auto-deny above. Both log to the Kaizen
Ledger; both surface in the intent feed.

Configuration lives under ``routing:escalation:`` in
``config/routing.config.yaml`` (operator runtime copy at
``~/.grove/routing.config.yaml``). Missing block → defaults to
``enabled: false`` and every request auto-denies. Vanilla installs
preserve pre-Sprint-30 behavior.

The Agent declares WHAT it needs (reasoning_depth,
context_size, blocker); the policy maps WHAT to HOW (target tier).
GRV-005 § III preserved: the Agent never dictates infrastructure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


__all__ = [
    "EscalationPolicy",
    "EscalationDecision",
    "PreRoutePolicy",
    "load_escalation_policy",
    "evaluate_escalation",
    "pre_route_check",
]


_DEFAULT_MAPPING: Dict[str, str] = {
    "shallow": "T1",
    "moderate": "T2",
    "deep": "T3",
    "apex": "T3",
}


_DEFAULT_PRE_ROUTE_TRIGGERS = frozenset({"complex", "novel"})


@dataclass(frozen=True)
class PreRoutePolicy:
    """Classifier-driven pre-routing for hard turns.

    Sprint 30.1 (post-completion patch). Distinct from Sprint 12's
    ``routing_rules.escalation`` step_up:

    * ``routing_rules.step_up``: bumps one tier when classifier
      confidence is low, regardless of complexity. Handles the
      "classifier isn't sure" case.
    * ``pre_route``: jumps to the policy's mapped tier for
      ``target_depth`` when complexity_signal is in
      ``complexity_triggers`` AND confidence is below
      ``confidence_threshold``. Handles the "this is genuinely hard"
      case.

    The two are complementary. When both would trigger on the same
    turn, pre_route wins — it's the stronger signal (precedence
    enforced in ``grove.router.CognitiveRouter.route``).
    """

    enabled: bool = False
    complexity_triggers: frozenset = None  # type: ignore[assignment]
    confidence_threshold: float = 0.6
    target_depth: str = "deep"

    def __post_init__(self) -> None:
        if self.complexity_triggers is None:
            object.__setattr__(
                self, "complexity_triggers", _DEFAULT_PRE_ROUTE_TRIGGERS,
            )


@dataclass(frozen=True)
class EscalationPolicy:
    """The Dispatcher's escalation decision rules.

    All fields are loaded from ``routing.escalation`` in the routing
    config. Missing fields fall to their default value — vanilla
    installs see ``enabled=False`` and every request auto-denies.
    """

    enabled: bool = False
    max_escalations_per_turn: int = 1
    max_escalations_per_session: int = 5
    ceiling_tier: str = "T3"
    auto_grant_above_tier: str = "T3"
    mapping: Dict[str, str] = None  # type: ignore[assignment]
    pre_route: PreRoutePolicy = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # Frozen dataclass: assign via object.__setattr__ when post-init
        # needs to fix a default-collection field.
        if self.mapping is None:
            object.__setattr__(self, "mapping", dict(_DEFAULT_MAPPING))
        if self.pre_route is None:
            object.__setattr__(self, "pre_route", PreRoutePolicy())

    def resolved_tier(self, reasoning_depth: Optional[str]) -> Optional[str]:
        """Map a declarative ``reasoning_depth`` to a configured tier.

        Returns ``None`` for unknown depth values — the caller
        interprets ``None`` as "policy can't satisfy this request"
        and auto-denies with that reason.
        """
        if not reasoning_depth:
            return None
        return self.mapping.get(reasoning_depth)


@dataclass(frozen=True)
class EscalationDecision:
    """The result of policy evaluation.

    The Dispatcher reads ``granted`` to branch into hot-swap (True)
    or denial-injection (False). ``target_tier`` is populated only
    when granted; ``reason`` always carries the operator-facing
    explanation that lands in the Kaizen Ledger and the denial
    tool-response.
    """

    granted: bool
    reason: str
    target_tier: Optional[str] = None
    current_tier: Optional[str] = None


def _tier_index(tier: str) -> int:
    """Order tiers by capability for ceiling comparisons.

    T0 < T1 < T2 < T3 < anything else. Unknown tier strings sort
    at the high end so a misconfigured policy fails closed.
    """
    order = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}
    return order.get(tier, 99)


def load_escalation_policy(routing_config: Dict[str, Any]) -> EscalationPolicy:
    """Build an :class:`EscalationPolicy` from a routing config dict.

    Accepts the loaded ``routing.config.yaml`` mapping. Reads
    ``routing.escalation`` and applies defaults for missing fields.
    Schema validation is intentionally permissive — a missing or
    malformed escalation block produces a disabled policy, NOT an
    exception. The Dispatcher must boot even when escalation config
    is absent.
    """
    routing = routing_config.get("routing") if isinstance(routing_config, dict) else None
    if not isinstance(routing, dict):
        return EscalationPolicy()
    # Sprint 30 lives under routing.escalation_policy to avoid colliding
    # with the existing routing.routing_rules.escalation block (Sprint 12
    # confidence-driven step-up; different concept). Permissive lookup:
    # missing or malformed block → disabled policy, NOT exception.
    esc = routing.get("escalation_policy")
    if not isinstance(esc, dict):
        return EscalationPolicy()

    mapping = esc.get("mapping")
    if not isinstance(mapping, dict):
        mapping = dict(_DEFAULT_MAPPING)
    else:
        # Coerce to str-to-str. Unknown depth keys carried through so
        # tests/operators can extend the taxonomy in config; only
        # validates types.
        mapping = {
            str(k): str(v) for k, v in mapping.items()
            if isinstance(k, str) and isinstance(v, str)
        }

    escalation_enabled = bool(esc.get("enabled", False))

    # pre_route sub-block: defaults pre_route.enabled to escalation.enabled
    # so flipping escalation on also enables classifier-driven pre-routing
    # by default. Operators can disable pre-routing independently by
    # setting routing.escalation_policy.pre_route.enabled to false.
    pre_route_cfg = esc.get("pre_route")
    if not isinstance(pre_route_cfg, dict):
        pre_route_cfg = {}
    triggers_raw = pre_route_cfg.get("complexity_triggers")
    if isinstance(triggers_raw, list):
        triggers = frozenset(str(t) for t in triggers_raw if isinstance(t, str))
    else:
        triggers = _DEFAULT_PRE_ROUTE_TRIGGERS
    pre_route = PreRoutePolicy(
        enabled=bool(pre_route_cfg.get("enabled", escalation_enabled)),
        complexity_triggers=triggers,
        confidence_threshold=float(pre_route_cfg.get("confidence_threshold", 0.6)),
        target_depth=str(pre_route_cfg.get("target_depth", "deep")),
    )

    return EscalationPolicy(
        enabled=escalation_enabled,
        max_escalations_per_turn=int(esc.get("max_escalations_per_turn", 1)),
        max_escalations_per_session=int(esc.get("max_escalations_per_session", 5)),
        ceiling_tier=str(esc.get("ceiling_tier", "T3")),
        auto_grant_above_tier=str(esc.get("auto_grant_above_tier", "T3")),
        mapping=mapping,
        pre_route=pre_route,
    )


def evaluate_escalation(
    *,
    policy: EscalationPolicy,
    current_tier: Optional[str],
    requested_depth: Optional[str],
    requested_context: Optional[str],
    turn_escalations_so_far: int,
    session_escalations_so_far: int,
) -> EscalationDecision:
    """Apply the policy to one EscalationRequest. Pure function.

    Order of evaluation matters — the first denial reason wins so the
    operator-facing ``reason`` always points at the binding
    constraint, not a downstream one.
    """
    if not policy.enabled:
        return EscalationDecision(
            granted=False,
            reason="escalation disabled in routing config",
            current_tier=current_tier,
        )

    if turn_escalations_so_far >= policy.max_escalations_per_turn:
        return EscalationDecision(
            granted=False,
            reason=(
                f"per-turn ceiling reached "
                f"({turn_escalations_so_far}/{policy.max_escalations_per_turn})"
            ),
            current_tier=current_tier,
        )

    if session_escalations_so_far >= policy.max_escalations_per_session:
        return EscalationDecision(
            granted=False,
            reason=(
                f"per-session budget exhausted "
                f"({session_escalations_so_far}/{policy.max_escalations_per_session})"
            ),
            current_tier=current_tier,
        )

    target_tier = policy.resolved_tier(requested_depth)
    if target_tier is None:
        return EscalationDecision(
            granted=False,
            reason=(
                f"reasoning_depth={requested_depth!r} not in policy mapping "
                f"({sorted(policy.mapping.keys())})"
            ),
            current_tier=current_tier,
        )

    if _tier_index(target_tier) > _tier_index(policy.ceiling_tier):
        return EscalationDecision(
            granted=False,
            reason=(
                f"target tier {target_tier!r} exceeds policy ceiling "
                f"{policy.ceiling_tier!r}"
            ),
            current_tier=current_tier,
        )

    # If the agent is already at-or-above target, no escalation is
    # required — grant a no-op so the Agent gets a clean "you're
    # already there" signal in the ledger.
    if current_tier and _tier_index(current_tier) >= _tier_index(target_tier):
        return EscalationDecision(
            granted=False,
            reason=(
                f"already at-or-above target tier {target_tier!r} "
                f"(current={current_tier!r}); no escalation needed"
            ),
            current_tier=current_tier,
            target_tier=target_tier,
        )

    return EscalationDecision(
        granted=True,
        reason=(
            f"granted {requested_depth} → {target_tier} "
            f"(context_size={requested_context!r})"
        ),
        target_tier=target_tier,
        current_tier=current_tier,
    )


def pre_route_check(
    *,
    policy: EscalationPolicy,
    complexity_signal: Optional[str],
    confidence: Optional[float],
    current_tier: Optional[str] = None,
) -> Optional[str]:
    """Classifier-driven pre-routing — return target tier or None.

    Pure function. Runs at routing time on the T-telemetry classifier's
    read of the request, before the LLM call. No budget / per-turn
    counter checks (those apply only to Agent-yielded EscalationRequest
    via ``evaluate_escalation``).

    Returns ``None`` when:
      * the parent ``policy.enabled`` is False,
      * ``policy.pre_route.enabled`` is False,
      * ``complexity_signal`` is not in ``policy.pre_route.complexity_triggers``,
      * ``confidence`` is None or at-or-above ``confidence_threshold``,
      * the mapped target tier is unknown or exceeds ``ceiling_tier``,
      * ``current_tier`` is already at-or-above the target.

    Returns the resolved target tier string when all conditions hold.
    The router (``grove.router.CognitiveRouter.route``) consumes this
    return value to build a ``RoutingDecision`` with
    ``reason="pre_route_escalation"``. The Dispatcher then emits a
    Kaizen Ledger ``escalation_decision`` event with ``source="pre_route"``
    so analytics can distinguish pre-routing from Agent-yielded
    escalations (``source="agent_request"``).
    """
    if not policy.enabled:
        return None
    if not policy.pre_route.enabled:
        return None
    if complexity_signal not in policy.pre_route.complexity_triggers:
        return None
    if confidence is None or confidence >= policy.pre_route.confidence_threshold:
        return None
    target_tier = policy.resolved_tier(policy.pre_route.target_depth)
    if target_tier is None:
        return None
    if _tier_index(target_tier) > _tier_index(policy.ceiling_tier):
        return None
    if current_tier and _tier_index(current_tier) >= _tier_index(target_tier):
        return None
    return target_tier

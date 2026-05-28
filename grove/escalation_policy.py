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
    "load_escalation_policy",
    "evaluate_escalation",
]


_DEFAULT_MAPPING: Dict[str, str] = {
    "shallow": "T1",
    "moderate": "T2",
    "deep": "T3",
    "apex": "T3",
}


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

    def __post_init__(self) -> None:
        # Frozen dataclass: assign via object.__setattr__ when post-init
        # needs to fix a default-collection field.
        if self.mapping is None:
            object.__setattr__(self, "mapping", dict(_DEFAULT_MAPPING))

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

    return EscalationPolicy(
        enabled=bool(esc.get("enabled", False)),
        max_escalations_per_turn=int(esc.get("max_escalations_per_turn", 1)),
        max_escalations_per_session=int(esc.get("max_escalations_per_session", 5)),
        ceiling_tier=str(esc.get("ceiling_tier", "T3")),
        auto_grant_above_tier=str(esc.get("auto_grant_above_tier", "T3")),
        mapping=mapping,
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

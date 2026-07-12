"""R5 (browser-read-surface-v1) — per-skill model-binding tier resolution.

Pure precedence, Design B (stateless): operator routing.config.yaml override >
skill ``model_binding`` > turn default. Operator config is inviolate at the top.
The resolver reads only its inputs and returns the tier to bind for THIS skill —
nothing is persisted, so two skills in one turn resolve independently and skill
A's binding cannot leak into skill B's resolution (no-bleed by construction).

Application (a rebind of ``agent.model`` via ``_bind_agent_to_tier``) is the
Dispatcher's job; this module only decides which tier wins. The rebind fires on
EVERY invoke_skill — a bindingless skill resolves to the turn default and rebinds
off any prior skill's tier — which is what makes the coarse in-agent form safe.

aux-model-bindings-v1 / binding-governance-surfaces-v1 P4 — ``type=model``
(the exact-slug pin) is FLEET-ONLY. On the Mylo path this resolver REFUSES the
rebind (the skill proceeds at the turn default) and never raises. The refusal
is EXPECTED behavior now that the governance surfaces exist: it logs at
WARNING (no andon_halt — a by-design plane boundary is not a fault; the
per-invocation filing was retired in P4) and the Dispatcher surfaces a
once-per-session-per-skill conversational notice instead. Cadence note
(supersedes the earlier fires-on-failed-attempts claim): since
skill-invocation-path-integrity-v1 P5, ``_apply_skill_tier_binding``
success-gates the rebind, so this resolver runs only for SUCCESSFUL
invoke_skill executions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from grove.capability import ModelBinding

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillTierResolution:
    tier: str
    # operator_override | skill_tier_override | skill_specialty_noop |
    # model_binding_mylo_refusal | turn_default
    reason: str


# binding-governance-surfaces-v1 P4 — the per-invocation andon_halt filer
# (_file_mylo_model_binding_refusal) was RETIRED here: with the binding
# governance surfaces shipped, a Mylo-plane refusal of a fleet-only pin is
# by-design behavior, not a fault to triage. The WARNING log in
# resolve_skill_tier and the Dispatcher's once-per-session-per-skill
# conversational notice are the remaining surfaces (GATE-A D10/FLAG-11:
# the un-deduplicated filing was a steady noise stream into the ledger).


def resolve_skill_tier(
    *,
    operator_active: bool,
    model_binding: Optional[ModelBinding],
    turn_tier: str,
    skill_name: Optional[str] = None,
) -> SkillTierResolution:
    """Resolve the tier a skill's reasoning should run at.

    ``operator_active`` is True when the operator has pinned a tier/model
    (GROVE_TIER / GROVE_INFERENCE_MODEL / --tier / --model); the turn was already
    routed at that tier, so the skill binding does not apply. ``turn_tier`` is the
    turn's routed tier (the default baseline). ``skill_name`` is diagnostic-only
    (names the skill in the type=model refusal filing); it never affects the
    resolved tier. A malformed/unknown binding type is a fail-loud programming
    error (Capability.validate() rejects it at load).
    """
    if operator_active:
        return SkillTierResolution(turn_tier, "operator_override")
    if model_binding is not None:
        if model_binding.type == "tier_override":
            return SkillTierResolution(model_binding.tier, "skill_tier_override")
        if model_binding.type == "specialty":
            # Validated-but-honored-no-op (reserved) — fall through to turn default.
            return SkillTierResolution(turn_tier, "skill_specialty_noop")
        if model_binding.type == "model":
            # binding-governance-surfaces-v1 P4 — exact-model pins are
            # fleet-only by design. REFUSE the rebind (never raise), WARN,
            # and let the turn continue at its default. No andon filing —
            # the plane boundary is expected behavior, not a fault; the
            # Dispatcher surfaces the once-per-session conversational notice.
            logger.warning(
                "[skill_binding] skill %r is pinned to %r for fleet runs; "
                "exact-model pins do not apply on the interactive path — "
                "continuing at turn default %s",
                skill_name,
                model_binding.model,
                turn_tier,
            )
            return SkillTierResolution(turn_tier, "model_binding_mylo_refusal")
        raise ValueError(f"unknown model_binding.type: {model_binding.type!r}")
    return SkillTierResolution(turn_tier, "turn_default")

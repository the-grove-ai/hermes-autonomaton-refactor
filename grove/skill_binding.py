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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from grove.capability import ModelBinding


@dataclass(frozen=True)
class SkillTierResolution:
    tier: str
    # operator_override | skill_tier_override | skill_specialty_noop | turn_default
    reason: str


def resolve_skill_tier(
    *,
    operator_active: bool,
    model_binding: Optional[ModelBinding],
    turn_tier: str,
) -> SkillTierResolution:
    """Resolve the tier a skill's reasoning should run at.

    ``operator_active`` is True when the operator has pinned a tier/model
    (GROVE_TIER / GROVE_INFERENCE_MODEL / --tier / --model); the turn was already
    routed at that tier, so the skill binding does not apply. ``turn_tier`` is the
    turn's routed tier (the default baseline). A malformed/unknown binding type
    is a fail-loud programming error (Capability.validate() rejects it at load).
    """
    if operator_active:
        return SkillTierResolution(turn_tier, "operator_override")
    if model_binding is not None:
        if model_binding.type == "tier_override":
            return SkillTierResolution(model_binding.tier, "skill_tier_override")
        if model_binding.type == "specialty":
            # Validated-but-honored-no-op (reserved) — fall through to turn default.
            return SkillTierResolution(turn_tier, "skill_specialty_noop")
        raise ValueError(f"unknown model_binding.type: {model_binding.type!r}")
    return SkillTierResolution(turn_tier, "turn_default")

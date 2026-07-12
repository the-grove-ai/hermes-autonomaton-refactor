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

aux-model-bindings-v1 — ``type=model`` (the exact-slug pin) is FLEET-ONLY until
the governance sprint. On the Mylo path this resolver REFUSES the rebind (the
skill proceeds at the turn default), files an ``andon_halt`` under the
``skill_binding`` component source so fault triage aggregates recurrences, and
never raises: ``_apply_skill_tier_binding`` fires on FAILED invoke_skill
attempts too (dispatcher extracts the name from intent arguments with no
success check), so a model's exploratory call must not detonate the turn.
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


def _file_mylo_model_binding_refusal(
    skill_name: Optional[str], model: Optional[str]
) -> None:
    """File the Mylo-path exact-model refusal as FACTS (aux-model-bindings-v1).

    Component-filer pattern (the tier_ratchet memory-enrichment precedent):
    the resolver runs with no CLI session of its own, so the filing lands
    under the dedicated ``cli-<utc-timestamp>`` sentinel session. Best-effort
    with an error-log floor — filing must never break resolution (the turn
    continues at its default either way).
    """
    try:
        from datetime import datetime, timezone

        from grove.kaizen_ledger import KaizenLedger

        session_id = "cli-" + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        KaizenLedger(session_id=session_id).record(
            "andon_halt",
            source="skill_binding",
            check="model_binding_mylo_refusal",
            detail=(
                f"skill {skill_name!r} declares model_binding.type=model "
                f"(pinned {model!r}) — exact-model bindings are fleet-only "
                f"until the governance sprint; rebind refused, skill ran at "
                f"the turn default"
            ),
        )
    except Exception as file_exc:  # noqa: BLE001 — filing leg, log floor stands
        logger.error("[skill_binding] kaizen filing leg failed: %r", file_exc)


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
            # aux-model-bindings-v1 — exact-model pins are fleet-only until the
            # governance sprint. REFUSE the rebind (never raise: the caller
            # fires on failed invoke_skill attempts too), file the refusal as
            # FACTS, warn, and let the turn continue at its default.
            logger.warning(
                "[skill_binding] refusing exact-model rebind for skill %r "
                "(pinned %r) — model_binding.type=model is fleet-only until "
                "the governance sprint; continuing at turn default %s",
                skill_name,
                model_binding.model,
                turn_tier,
            )
            _file_mylo_model_binding_refusal(skill_name, model_binding.model)
            return SkillTierResolution(turn_tier, "model_binding_mylo_refusal")
        raise ValueError(f"unknown model_binding.type: {model_binding.type!r}")
    return SkillTierResolution(turn_tier, "turn_default")

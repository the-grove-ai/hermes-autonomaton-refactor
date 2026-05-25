"""Provider-resolution bridge for the Cognitive Router.

Sprint 11 (cognitive-router-tiering-v1). Two thin functions connect the
router's tier selection to the agent's existing model-selection path:

  route_for_agent()         — consult the router; returns a RoutingDecision
                              or None when there is no routing config to
                              load (a vanilla install -> legacy chain).
  resolve_tier_to_runtime() — turn the selected tier's TierConfig into the
                              runtime dict the agent constructor needs,
                              reusing the existing resolve_runtime_provider()
                              credential chain.

The router selects *which* (provider, model) pair serves a request; the
existing runtime layer resolves *how* to call it. This module is the seam
between the two and holds no provider-specific logic. Every routing
decision is logged here, between route() and the caller.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from typing import Optional

from grove import router as _router
from grove.classify import ClassificationResult
from grove.router import CognitiveRouter, RoutingDecision, TierConfig
from grove.telemetry import log_ratchet_candidate, log_routing_decision

logger = logging.getLogger(__name__)

# The tier the most recent route_for_agent() call selected. Best-effort
# runtime metadata, read by skill-proposal stamping to record which
# cognitive tier authored a skill. None until the first routing call.
_last_routed_tier: Optional[str] = None

# The T-telemetry classification from the most recent route_for_agent()
# call. Best-effort runtime metadata, read by the CLI tier-UX surface
# (the /why command, the transition line). None until the first call.
_last_classification: Optional[ClassificationResult] = None


def route_for_agent(
    *,
    message: Optional[str] = None,
    explicit_tier: Optional[str] = None,
    explicit_model: Optional[str] = None,
    tier_source: Optional[str] = None,
) -> Optional[RoutingDecision]:
    """Consult the Cognitive Router for the tier an agent should run on.

    Returns None when there is no ``routing.config.yaml`` to load — a
    vanilla install — signalling the caller to fall back to the legacy
    model-selection chain unchanged. A routing config that exists but is
    malformed raises loudly; absence is the only fallback trigger.

    ``message`` is the operator's request; it is classified (T-telemetry)
    so route() can escalate on low confidence. ``explicit_tier`` /
    ``explicit_model`` are the ``--tier`` / ``--model`` flag values; when
    unset, ``GROVE_TIER`` / ``GROVE_INFERENCE_MODEL`` are consulted.
    ``tier_source="session"`` marks an ``explicit_tier`` that came from
    the in-session ``/tier`` override, so telemetry records it as
    ``operator_session_override`` rather than ``operator_override``.
    """
    router = _ensure_router()
    if router is None:
        return None
    # T-telemetry: classify the request so route() can escalate on low
    # confidence. None on any failure — route() then falls back cleanly.
    from grove.classify import classify_for_routing  # local: avoid circular

    classification = classify_for_routing(message)
    decision = router.route(
        operator_tier=_resolve_operator_tier(explicit_tier),
        operator_model=_resolve_operator_model(explicit_model),
        intent=classification.intent_class if classification else None,
        confidence=classification.confidence if classification else None,
        complexity_signal=(
            classification.complexity_signal if classification else None
        ),
    )
    # tier-ux: a /tier session override and a --tier flag both reach
    # route() as operator_tier and yield reason="operator_override".
    # Re-stamp the session case so telemetry — and the v0.2 Ratchet —
    # can tell "operator corrected the router mid-session" apart from
    # "operator forced a tier from the command line".
    if tier_source == "session" and decision.reason == "operator_override":
        decision = replace(decision, reason="operator_session_override")
    global _last_routed_tier, _last_classification
    _last_routed_tier = decision.tier
    _last_classification = classification
    _log_routing(decision, classification)
    return decision


def current_tier() -> Optional[str]:
    """The cognitive tier the most recent route_for_agent() call selected.

    Best-effort runtime metadata: None before the first routing call, or
    on a vanilla install with no routing config. Skill-proposal stamping
    reads this to record which tier authored a skill — the system's own
    record of the routing decision, never the model's self-report.
    """
    return _last_routed_tier


def current_classification() -> Optional[ClassificationResult]:
    """The T-telemetry classification the most recent route_for_agent()
    call produced.

    Best-effort runtime metadata: None before the first routing call, or
    on a vanilla install with no routing config. The CLI tier-UX surface
    reads this to explain a routing decision (/why) and to label the
    transition line — without spending a second classification call.
    """
    return _last_classification


def resolve_tier_to_runtime(tier_config: TierConfig) -> dict:
    """Map a selected tier's TierConfig to an agent-ready runtime dict.

    Reuses the existing ``resolve_runtime_provider()`` chain to turn the
    tier's ``(provider, model)`` into credentials. Returns the keys the
    agent constructor consumes: ``model``, ``provider``, ``api_key``,
    ``base_url``, ``api_mode``, ``credential_pool``, and (S22.1)
    ``auth_type``.

    ``auth_type`` is the credential's authentication scheme as recorded
    by the credential pool: ``"api_key"`` for ``sk-ant-api*`` keys,
    ``"oauth"`` for OAuth bearer tokens (Claude Code subscriptions,
    setup-tokens, JWTs). The classifier branch in ``grove.classify``
    uses this to construct the Anthropic SDK client correctly —
    OAuth tokens sent via ``x-api-key`` return 401, OAuth tokens sent
    via ``auth_token`` (Bearer + the oauth-2025-04-20 beta) succeed.
    Threaded explicitly through the result dict so any consumer can
    audit the auth path the classifier took, even though the
    downstream ``build_anthropic_client`` also detects the token
    shape directly.

    Raises ValueError for a handler-backed tier (e.g. T0 pattern_cache) —
    those resolve no inference provider and must not reach this bridge.
    """
    if tier_config.handler:
        raise ValueError(
            f"tier {tier_config.tier} is handler-backed "
            f"({tier_config.handler!r}); resolve_tier_to_runtime is for "
            f"provider-backed inference tiers only"
        )
    # Local import: keeps grove.providers free of an import-time
    # dependency on hermes_cli, matching the agent paths' own pattern.
    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider(
        requested=tier_config.provider,
        target_model=tier_config.model,
    )
    return {
        "model": tier_config.model,
        "provider": runtime.get("provider"),
        "api_key": runtime.get("api_key"),
        "base_url": runtime.get("base_url"),
        "api_mode": runtime.get("api_mode"),
        "credential_pool": runtime.get("credential_pool"),
        "auth_type": runtime.get("auth_type"),
    }


# ----- internals --------------------------------------------------------------


def _log_routing(
    decision: RoutingDecision,
    classification: Optional[ClassificationResult] = None,
) -> None:
    """Emit routing telemetry: a routing_decision event for every call,
    enriched with the T-telemetry classification when one is available,
    plus a ratchet_candidate when the decision lands on a premium tier."""
    log_routing_decision(
        tier=decision.tier,
        reason=decision.reason,
        model=decision.tier_config.model,
        confidence=decision.confidence,
        pattern_cache_hit=decision.pattern_cache_hit,
        intent_class=classification.intent_class if classification else None,
        pattern_hash=classification.pattern_hash if classification else None,
        register_class=classification.register_class if classification else None,
        complexity_signal=(
            classification.complexity_signal if classification else None
        ),
    )
    if decision.tier in ("T2", "T3"):
        log_ratchet_candidate(
            tier=decision.tier,
            model=decision.tier_config.model,
            reason=decision.reason,
        )


def _ensure_router() -> Optional[CognitiveRouter]:
    """Return the module router, initializing it on first use.

    Returns None only when no routing config exists (FileNotFoundError) —
    the vanilla-install path. A present-but-malformed config raises from
    CognitiveRouter and is NOT swallowed here: absence falls back,
    breakage fails loud.
    """
    if _router._default_router is not None:
        return _router._default_router
    try:
        return _router.initialize()
    except FileNotFoundError:
        logger.debug("[router] no routing.config.yaml found; legacy model chain in use")
        return None


def _resolve_operator_tier(explicit_tier: Optional[str]) -> Optional[str]:
    """The --tier flag wins; otherwise the GROVE_TIER env var."""
    tier = (explicit_tier or "").strip() or os.getenv("GROVE_TIER", "").strip()
    return tier or None


def _resolve_operator_model(explicit_model: Optional[str]) -> Optional[str]:
    """The --model flag wins; otherwise the GROVE_INFERENCE_MODEL env var."""
    model = (explicit_model or "").strip() or os.getenv(
        "GROVE_INFERENCE_MODEL", ""
    ).strip()
    return model or None

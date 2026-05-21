"""Cognitive Router for the Grove Autonomaton.

Reads ``~/.grove/routing.config.yaml`` (or the repo default at
``config/routing.config.yaml``) and exposes ``route()`` — tier selection
from config rules, zone classification, and operator overrides — plus a
read-only view of the four cognitive tiers and their model bindings.

No inference happens here. The router selects *which* model serves a
request; the existing runtime layer resolves *how* to call it. The
loader is provider-agnostic: a tier's ``provider`` and ``model`` are
opaque strings, so swapping a binding from Anthropic to a local model is
a config edit, never a code change (the Principle 7 contract).

``reload()`` is the one graceful-degradation path, mirroring
``grove.zones``: on parse or validation failure the router retains the
last known good config and logs the error loudly.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TierConfig:
    """Resolved configuration for one cognitive tier.

    ``handler`` is set for non-inference tiers (``"pattern_cache"`` for T0)
    and ``None`` for provider-backed tiers; ``provider``/``model`` are the
    reverse. The loader does not interpret any of these — they are opaque
    config values.
    """

    tier: str
    handler: Optional[str]
    provider: Optional[str]
    model: Optional[str]
    max_tokens: Optional[int]
    max_latency_ms: Optional[int]
    description: str


@dataclass(frozen=True)
class RoutingDecision:
    """The outcome of a route() call.

    ``reason`` is one of ``"operator_override"``,
    ``"operator_model_preference"``, ``"operator_model_untiered"``,
    ``"zone_override"``, ``"default"``, ``"escalation"``. ``confidence``
    and ``pattern_cache_hit`` are wired for Sprint 12 / Sprint 13 and
    inert in v0.1.
    """

    tier: str
    tier_config: TierConfig
    reason: str
    confidence: Optional[float]
    pattern_cache_hit: bool


class CognitiveRouter:
    """Loads, queries, and routes against a routing.config.yaml file."""

    def __init__(self, config_path: Path):
        self._config_path = Path(config_path)
        self._tiers: dict[str, TierConfig] = {}
        self._zone_overrides: dict[str, str] = {}
        self._default_tier: str = ""
        self._escalation_threshold: float = 0.0
        self._telemetry_tier: str = ""
        self._load_into_self()

    # ----- public query API ---------------------------------------------------

    def get_tier_config(self, tier: str) -> TierConfig:
        """Return the TierConfig for ``tier`` (e.g. ``"T2"``).

        Raises KeyError if the tier is not declared in the config.
        """
        if tier not in self._tiers:
            raise KeyError(
                f"unknown tier {tier!r}; declared tiers: {sorted(self._tiers)}"
            )
        return self._tiers[tier]

    def get_default_tier(self) -> str:
        return self._default_tier

    def get_escalation_threshold(self) -> float:
        return self._escalation_threshold

    def get_telemetry_tier(self) -> str:
        return self._telemetry_tier

    def model_to_tier(self, model: str) -> Optional[str]:
        """Return the tier whose binding is ``model``, or None if no tier
        declares it. Exact match; first declared tier wins on a tie."""
        for tier_name, cfg in self._tiers.items():
            if cfg.model == model:
                return tier_name
        return None

    def route(
        self,
        *,
        action: Optional[str] = None,
        intent: Optional[str] = None,
        confidence: Optional[float] = None,
        zone: Optional[str] = None,
        operator_tier: Optional[str] = None,
        operator_model: Optional[str] = None,
    ) -> RoutingDecision:
        """Select a cognitive tier for an interaction.

        Every interaction runs through this method when a routing config
        is loaded — there is no bypass. Precedence:

        1. operator_tier (--tier / GROVE_TIER) — forces a tier.
        2. operator_model (--model / GROVE_INFERENCE_MODEL) — resolved to
           a tier: the tier that binds the model if one does
           (operator_model_preference), else the default tier's provider
           carrying the operator's model as-is (operator_model_untiered).
        3. Rule-based: T0 pattern cache (always a miss in v0.1 — no cellar
           until Sprint 13), then zone_overrides, then default_tier.

        Escalation applies only to the rule-based path and is inert until
        Sprint 12 supplies ``confidence``. ``action`` and ``intent`` are
        accepted for the Sprint 12 signature and the telemetry record;
        v0.1 routing does not read them.
        """
        # 1. Operator tier override — forces a tier.
        if operator_tier:
            return RoutingDecision(
                tier=operator_tier,
                tier_config=self.get_tier_config(operator_tier),
                reason="operator_override",
                confidence=confidence,
                pattern_cache_hit=False,
            )

        # 2. Operator model preference — resolved to a tier, never routed
        #    around the pipeline.
        if operator_model:
            tier = self.model_to_tier(operator_model)
            if tier is not None:
                return RoutingDecision(
                    tier=tier,
                    tier_config=self.get_tier_config(tier),
                    reason="operator_model_preference",
                    confidence=confidence,
                    pattern_cache_hit=False,
                )
            # No tier binds this model: keep the operator's model, borrow
            # the default tier's provider and limits.
            default_config = self.get_tier_config(self._default_tier)
            return RoutingDecision(
                tier=self._default_tier,
                tier_config=replace(default_config, model=operator_model),
                reason="operator_model_untiered",
                confidence=confidence,
                pattern_cache_hit=False,
            )

        # 3. Rule-based selection: T0 pattern cache (always a miss in
        #    v0.1), then zone_overrides, then default_tier.
        pattern_cache_hit = False
        if zone is not None and zone in self._zone_overrides:
            tier = self._zone_overrides[zone]
            reason = "zone_override"
        else:
            tier = self._default_tier
            reason = "default"

        # Escalation: one step up on low classification confidence. Inert
        # in v0.1 (confidence is None until Sprint 12). Disabled when the
        # threshold is 0.0 — the circuit breaker.
        if (
            confidence is not None
            and self._escalation_threshold > 0.0
            and confidence < self._escalation_threshold
        ):
            escalated = _escalate_one(tier)
            if escalated != tier:
                tier = escalated
                reason = "escalation"

        return RoutingDecision(
            tier=tier,
            tier_config=self.get_tier_config(tier),
            reason=reason,
            confidence=confidence,
            pattern_cache_hit=pattern_cache_hit,
        )

    def reload(self) -> None:
        """Reload config from disk; on failure, keep last known good and log loudly."""
        snapshot = (
            dict(self._tiers),
            dict(self._zone_overrides),
            self._default_tier,
            self._escalation_threshold,
            self._telemetry_tier,
        )
        try:
            self._load_into_self()
        except Exception as exc:
            logger.error(
                "[router] reload failed; keeping last known good config: %r", exc
            )
            (
                self._tiers,
                self._zone_overrides,
                self._default_tier,
                self._escalation_threshold,
                self._telemetry_tier,
            ) = snapshot

    # ----- internals ----------------------------------------------------------

    def _load_into_self(self) -> None:
        """Read, parse, validate; mutate self atomically on success."""
        with open(self._config_path) as fh:
            raw = yaml.safe_load(fh)

        if not isinstance(raw, dict):
            raise ValueError(
                f"routing config at {self._config_path} did not parse to a mapping"
            )

        routing = raw.get("routing")
        if not isinstance(routing, dict):
            raise ValueError(
                f"routing config at {self._config_path} has no 'routing' mapping"
            )

        version = routing.get("schema_version")
        if version != 1:
            raise ValueError(
                f"unsupported schema_version {version!r} in {self._config_path}"
                f" (expected 1)"
            )

        default_tier = routing.get("default_tier")
        if not isinstance(default_tier, str) or not default_tier:
            raise ValueError(
                f"routing config at {self._config_path} missing a string 'default_tier'"
            )

        tier_prefs = routing.get("tier_preferences")
        if not isinstance(tier_prefs, dict) or not tier_prefs:
            raise ValueError(
                f"routing config at {self._config_path} has no 'tier_preferences'"
            )

        tiers: dict[str, TierConfig] = {}
        for name, spec in tier_prefs.items():
            spec = spec or {}
            if not isinstance(spec, dict):
                raise ValueError(f"tier {name!r} is not a mapping")
            tiers[name] = TierConfig(
                tier=name,
                handler=spec.get("handler"),
                provider=spec.get("provider"),
                model=spec.get("model"),
                max_tokens=spec.get("max_tokens"),
                max_latency_ms=spec.get("max_latency_ms"),
                description=str(spec.get("description") or "").strip(),
            )

        zone_overrides = routing.get("zone_overrides") or {}
        if not isinstance(zone_overrides, dict):
            raise ValueError(
                f"routing config at {self._config_path} has a non-mapping"
                f" 'zone_overrides'"
            )

        escalation = routing.get("escalation") or {}
        threshold = escalation.get("threshold")
        if not isinstance(threshold, (int, float)):
            raise ValueError(
                f"routing config at {self._config_path} missing numeric"
                f" 'escalation.threshold'"
            )

        telemetry = routing.get("telemetry") or {}
        telemetry_tier = telemetry.get("tier")
        if not isinstance(telemetry_tier, str) or not telemetry_tier:
            raise ValueError(
                f"routing config at {self._config_path} missing 'telemetry.tier'"
            )

        # All-or-nothing swap (mutation only after validation succeeds).
        self._tiers = tiers
        self._zone_overrides = dict(zone_overrides)
        self._default_tier = default_tier
        self._escalation_threshold = float(threshold)
        self._telemetry_tier = telemetry_tier


# ----- module-level singleton + helpers ---------------------------------------

_default_router: Optional[CognitiveRouter] = None

# Tier ordering, lowest to highest — the escalation ladder.
_TIER_LADDER = ("T0", "T1", "T2", "T3")


def _escalate_one(tier: str) -> str:
    """Return the next tier up the ladder; T3 is the ceiling."""
    try:
        idx = _TIER_LADDER.index(tier)
    except ValueError:
        return tier
    return _TIER_LADDER[min(idx + 1, len(_TIER_LADDER) - 1)]


def initialize(config_path: Optional[Path] = None) -> CognitiveRouter:
    """Initialize (or re-initialize) the module-level router.

    Resolution order for ``config_path``:
        1. Explicit argument, if given.
        2. ``~/.grove/routing.config.yaml`` (operator copy).
        3. Repo default at ``<grove-package-parent>/config/routing.config.yaml``,
           copied to the operator location on first run.

    Raises FileNotFoundError if neither the operator copy nor the repo
    default exists.
    """
    global _default_router
    _default_router = CognitiveRouter(_resolve_config_path(config_path))
    return _default_router


def get_tier_config(tier: str) -> TierConfig:
    """Module-level convenience that delegates to the initialized router."""
    if _default_router is None:
        raise RuntimeError(
            "grove.router is not initialized; call grove.router.initialize() first."
        )
    return _default_router.get_tier_config(tier)


def _resolve_config_path(explicit: Optional[Path]) -> Path:
    if explicit is not None:
        return Path(explicit)

    operator_copy = Path.home() / ".grove" / "routing.config.yaml"
    if operator_copy.exists():
        return operator_copy

    repo_default = (
        Path(__file__).resolve().parent.parent / "config" / "routing.config.yaml"
    )
    if not repo_default.exists():
        raise FileNotFoundError(
            f"no routing config found at {operator_copy} or {repo_default}"
        )

    operator_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo_default, operator_copy)
    return operator_copy

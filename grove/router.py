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

from grove.escalation_policy import (
    EscalationPolicy,
    load_escalation_policy,
    pre_route_check,
)

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
    ``"downward"``, ``"upward"``, ``"escalation"``, ``"zone_override"``,
    or ``"default"`` — the rule or fallback that selected the tier.
    """

    tier: str
    tier_config: TierConfig
    reason: str
    confidence: Optional[float]
    pattern_cache_hit: bool


@dataclass(frozen=True)
class RoutingRule:
    """One declarative routing rule loaded from the ``routing_rules`` block.

    ``downward`` and ``upward`` carry a ``target_tier``; ``escalation``
    carries ``action="step_up"``. The match criteria are AND-ed — a rule
    fires only when every criterion it declares holds against the
    telemetry classification. A criterion left empty (an empty frozenset,
    or ``None``) is simply not tested.
    """

    name: str
    enabled: bool
    target_tier: Optional[str]
    action: Optional[str]
    complexity: frozenset
    intents: frozenset
    min_confidence: Optional[float]
    max_confidence: Optional[float]


class CognitiveRouter:
    """Loads, queries, and routes against a routing.config.yaml file."""

    def __init__(self, config_path: Path):
        self._config_path = Path(config_path)
        self._tiers: dict[str, TierConfig] = {}
        self._zone_overrides: dict[str, str] = {}
        self._routing_rules: list[RoutingRule] = []
        self._default_tier: str = ""
        self._escalation_threshold: float = 0.0
        self._telemetry_tier: str = ""
        # Sprint 30.1 (post-completion patch): classifier-driven pre-routing.
        # Default disabled — vanilla installs see the legacy step_up path
        # only. Parsed from routing.escalation_policy in _load_into_self.
        self._escalation_policy: EscalationPolicy = EscalationPolicy()
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
        complexity_signal: Optional[str] = None,
        zone: Optional[str] = None,
        operator_tier: Optional[str] = None,
        operator_model: Optional[str] = None,
    ) -> RoutingDecision:
        """Select a cognitive tier for an interaction.

        Every interaction runs through this method when a routing config
        is loaded — there is no bypass. Precedence, first match wins:

        1. operator_tier (--tier / GROVE_TIER) — forces a tier.
        2. operator_model (--model / GROVE_INFERENCE_MODEL) — resolved to
           a tier, or carried as-is on the default tier's provider.
        3. Declarative routing_rules — downward, upward, escalation, in
           that order. The first enabled rule whose match criteria all
           hold against the classification decides the tier.
        4. zone_overrides — a tier pinned for a classified zone.
        5. default_tier.

        ``intent``, ``confidence`` and ``complexity_signal`` are the
        telemetry classifier's read of the request; the routing_rules
        match against them. ``action`` is accepted for the telemetry
        record and is not matched on.
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

        # 2.5. Classifier-driven pre-routing (Sprint 30.1 post-completion
        #      patch). When complexity_signal is in the policy's triggers
        #      AND classifier confidence is below the threshold, jump
        #      straight to the policy's mapped tier for ``target_depth``
        #      (defaults: complex|novel triggers, 0.6 threshold, "deep"
        #      depth → T3).
        #
        #      Precedence:
        #        - Operator overrides above always win (steps 1-2).
        #        - Pre-route fires BEFORE routing_rules.step_up. If both
        #          would trigger on the same turn, pre-route wins as the
        #          stronger signal: "this is genuinely hard" beats
        #          "classifier isn't sure". The step_up path still
        #          handles low-confidence-on-any-complexity turns when
        #          pre-route doesn't fire.
        pre_route_tier = pre_route_check(
            policy=self._escalation_policy,
            complexity_signal=complexity_signal,
            confidence=confidence,
            current_tier=self._default_tier,
        )
        if pre_route_tier is not None:
            return RoutingDecision(
                tier=pre_route_tier,
                tier_config=self.get_tier_config(pre_route_tier),
                reason="pre_route_escalation",
                confidence=confidence,
                pattern_cache_hit=False,
            )

        # 3. Declarative routing rules — downward, upward, escalation in
        #    config order. The first enabled rule that matches wins.
        for rule in self._routing_rules:
            if not rule.enabled:
                continue
            if not _rule_matches(
                rule,
                intent=intent,
                confidence=confidence,
                complexity=complexity_signal,
            ):
                continue
            if rule.action == "step_up":
                # Escalation steps the default tier up one rung of the
                # ladder; T3 is the ceiling. Inlined — the old
                # _escalate_one helper is gone.
                try:
                    _idx = _TIER_LADDER.index(self._default_tier)
                    tier = _TIER_LADDER[min(_idx + 1, len(_TIER_LADDER) - 1)]
                except ValueError:
                    tier = self._default_tier
                reason = "escalation"
            else:
                tier = rule.target_tier
                reason = rule.name
            return RoutingDecision(
                tier=tier,
                tier_config=self.get_tier_config(tier),
                reason=reason,
                confidence=confidence,
                pattern_cache_hit=False,
            )

        # 4. Zone override — a tier pinned for a classified zone.
        if zone is not None and zone in self._zone_overrides:
            tier = self._zone_overrides[zone]
            return RoutingDecision(
                tier=tier,
                tier_config=self.get_tier_config(tier),
                reason="zone_override",
                confidence=confidence,
                pattern_cache_hit=False,
            )

        # 5. Default tier. When no classifier input was available
        #    (intent / confidence / complexity_signal all None — the
        #    T-telemetry classifier failed or was suppressed), the
        #    decision is degraded: the pipeline still routes (to the
        #    default tier) so the turn is governed, but the reason
        #    distinguishes this from a normal default-tier landing.
        #    Telemetry consumers and the v0.2 Ratchet can filter on
        #    reason="classifier_unavailable" to see classifier outages.
        _classifier_unavailable = (
            intent is None and confidence is None and complexity_signal is None
        )
        return RoutingDecision(
            tier=self._default_tier,
            tier_config=self.get_tier_config(self._default_tier),
            reason="classifier_unavailable" if _classifier_unavailable else "default",
            confidence=0.0 if _classifier_unavailable else confidence,
            pattern_cache_hit=False,
        )

    def reload(self) -> None:
        """Reload config from disk; on failure, keep last known good and log loudly."""
        snapshot = (
            dict(self._tiers),
            dict(self._zone_overrides),
            list(self._routing_rules),
            self._default_tier,
            self._escalation_threshold,
            self._telemetry_tier,
            self._escalation_policy,
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
                self._routing_rules,
                self._default_tier,
                self._escalation_threshold,
                self._telemetry_tier,
                self._escalation_policy,
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

        # Declarative routing rules. Optional: a config predating this
        # section yields downward/upward absent and a synthesized
        # escalation rule carrying the top-level escalation.threshold.
        routing_rules = _parse_routing_rules(routing, float(threshold))
        for rule in routing_rules:
            if rule.target_tier is not None and rule.target_tier not in tiers:
                raise ValueError(
                    f"routing config at {self._config_path}: routing_rules."
                    f"{rule.name}.target_tier {rule.target_tier!r} is not a"
                    f" declared tier"
                )

        # Sprint 30.1 (post-completion patch): load the escalation policy
        # for classifier-driven pre-routing. Load is permissive — a
        # missing or malformed routing.escalation_policy block yields a
        # disabled policy, never an exception. Parent escalation must be
        # enabled for pre_route to fire (load_escalation_policy honors
        # the parent flag when defaulting pre_route.enabled).
        escalation_policy = load_escalation_policy(raw)

        # All-or-nothing swap (mutation only after validation succeeds).
        self._tiers = tiers
        self._zone_overrides = dict(zone_overrides)
        self._routing_rules = routing_rules
        self._default_tier = default_tier
        self._escalation_threshold = float(threshold)
        self._telemetry_tier = telemetry_tier
        self._escalation_policy = escalation_policy


# ----- module-level singleton + helpers ---------------------------------------

_default_router: Optional[CognitiveRouter] = None

# Tier ordering, lowest to highest — the ladder the escalation rule
# steps a request up.
_TIER_LADDER = ("T0", "T1", "T2", "T3")


def _rule_matches(
    rule: RoutingRule,
    *,
    intent: Optional[str],
    confidence: Optional[float],
    complexity: Optional[str],
) -> bool:
    """True when every match criterion the rule declares is satisfied.

    A criterion the rule does not declare is not tested. A criterion the
    rule declares but the classification cannot supply (a ``None``
    signal) counts as unsatisfied — an unclassifiable request never
    matches a rule that needs the classification.
    """
    if rule.complexity and complexity not in rule.complexity:
        return False
    if rule.intents and intent not in rule.intents:
        return False
    if rule.min_confidence is not None:
        if confidence is None or confidence < rule.min_confidence:
            return False
    if rule.max_confidence is not None:
        if confidence is None or confidence >= rule.max_confidence:
            return False
    return True


def _as_frozenset(value) -> frozenset:
    """Normalize a YAML scalar, list, or absent value into a frozenset."""
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, (list, tuple)):
        return frozenset(value)
    raise ValueError(f"expected a string or list of strings, got {value!r}")


def _as_float(value, label: str) -> Optional[float]:
    """Coerce a numeric YAML value to float; ``None`` passes through."""
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number, got {value!r}")
    return float(value)


def _parse_routing_rules(routing: dict, default_threshold: float) -> list:
    """Build the ordered [downward, upward, escalation] routing-rule list.

    ``downward`` and ``upward`` exist only when ``routing_rules`` declares
    them. ``escalation`` is always present: when the config omits it (a
    config predating routing_rules), it is synthesized enabled with the
    top-level ``escalation.threshold`` as its ``max_confidence`` — the
    backward-compatible migration path.
    """
    raw = routing.get("routing_rules") or {}
    if not isinstance(raw, dict):
        raise ValueError("'routing_rules' must be a mapping")

    rules: list = []

    for name in ("downward", "upward"):
        spec = raw.get(name)
        if spec is None:
            continue
        if not isinstance(spec, dict):
            raise ValueError(f"routing_rules.{name} must be a mapping")
        target = spec.get("target_tier")
        if not isinstance(target, str) or not target:
            raise ValueError(
                f"routing_rules.{name} needs a string 'target_tier'"
            )
        match = spec.get("match") or {}
        if not isinstance(match, dict):
            raise ValueError(f"routing_rules.{name}.match must be a mapping")
        rules.append(
            RoutingRule(
                name=name,
                enabled=bool(spec.get("enabled", False)),
                target_tier=target,
                action=None,
                complexity=_as_frozenset(match.get("complexity")),
                intents=_as_frozenset(match.get("intents")),
                min_confidence=_as_float(
                    match.get("min_confidence"),
                    f"routing_rules.{name}.match.min_confidence",
                ),
                max_confidence=_as_float(
                    match.get("max_confidence"),
                    f"routing_rules.{name}.match.max_confidence",
                ),
            )
        )

    esc = raw.get("escalation")
    if esc is None:
        rules.append(
            RoutingRule(
                name="escalation",
                enabled=True,
                target_tier=None,
                action="step_up",
                complexity=frozenset(),
                intents=frozenset(),
                min_confidence=None,
                max_confidence=default_threshold,
            )
        )
    else:
        if not isinstance(esc, dict):
            raise ValueError("routing_rules.escalation must be a mapping")
        esc_action = esc.get("action") or "step_up"
        if esc_action != "step_up":
            raise ValueError(
                f"routing_rules.escalation.action must be 'step_up', got"
                f" {esc_action!r}"
            )
        esc_match = esc.get("match") or {}
        if not isinstance(esc_match, dict):
            raise ValueError("routing_rules.escalation.match must be a mapping")
        raw_max = esc_match.get("max_confidence")
        max_confidence = (
            default_threshold
            if raw_max is None
            else _as_float(
                raw_max, "routing_rules.escalation.match.max_confidence"
            )
        )
        rules.append(
            RoutingRule(
                name="escalation",
                enabled=bool(esc.get("enabled", True)),
                target_tier=None,
                action="step_up",
                complexity=frozenset(),
                intents=frozenset(),
                min_confidence=None,
                max_confidence=max_confidence,
            )
        )

    return rules


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

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

import hashlib
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
from grove.router_merge import load_merged_routing_config
from grove.telemetry import log_routing_config_load

logger = logging.getLogger(__name__)

# Fault-attribution sentinel: the machine hash carries this fixed value
# when no machine routing file exists, so a load can be attributed to the
# operator file alone without conflating "absent" with any real digest.
_MACHINE_ABSENT_SENTINEL = "machine-config-absent"


@dataclass(frozen=True)
class TierConfig:
    """Resolved configuration for one cognitive tier.

    ``handler`` is set for non-inference tiers (``"pattern_cache"`` for T0)
    and ``None`` for provider-backed tiers; ``provider``/``model`` are the
    reverse. The loader does not interpret any of these — they are opaque
    config values.

    ``cost_per_mtok_input`` and ``cost_per_mtok_output`` are USD-per-
    million-tokens list prices the operator declares per tier. Optional
    so legacy ``routing.config.yaml`` files without the field round-trip
    cleanly; consumers that compute cost telemetry (the T-telemetry
    classifier's spend tracker) treat None as "operator has not
    declared cost; emit a one-shot warning and skip accumulation"
    rather than silently defaulting to zero.
    """

    tier: str
    handler: Optional[str]
    provider: Optional[str]
    model: Optional[str]
    max_tokens: Optional[int]
    max_latency_ms: Optional[int]
    description: str
    cost_per_mtok_input: Optional[float] = None
    cost_per_mtok_output: Optional[float] = None
    # GRV-010 C2d — optional governed downshift target. When this tier's bound
    # model becomes unavailable (connection/timeout/429/exhausted pool), the
    # Dispatcher re-routes the turn through the Cognitive Router at this tier
    # instead of the ungoverned silent fallback_model swap. Absent (None) =
    # legacy behavior (the old in-loop fallback chain handles failures).
    fallback_tier: Optional[str] = None


# ── model_facts — slug-keyed physics, dispatch-readable, operator-declared ──
# binding-opacity-v1 P4a. A model's physics (context window, native tool-call
# schema, message-role convention, reasoning support, cache style) is a fact
# about the MODEL, true regardless of which tier binds it. Declared ONCE per
# opaque slug in ``routing.model_facts`` and resolved tier -> model -> facts, so
# a model bound to two tiers carries ONE declaration — no per-tier drift. The
# slug is used only as a DICT KEY (R-2 permits equality and keys; never parsed),
# and it lives in the SAME file the dispatch path already reads — no catalog
# read, so the G-1b dispatch-isolation invariant is untouched.
_CONTEXT_WINDOW_FLOOR = 8192  # conservative floor when context_window is undeclared


@dataclass(frozen=True)
class ModelFacts:
    """Operator-declared physics for one model slug. Consumed by the adapter and
    runtime, NEVER by prompt composition (assertion 3 in the opacity guard makes
    that structural).

    Absence is safe and legible, never silent-wrong:
      * ``context_window`` absent   -> ``_CONTEXT_WINDOW_FLOOR`` + one-shot warning
      * ``native_tool_schema`` None -> caller falls back to provider/base-url detect
      * ``reasoning_support`` False -> no reasoning params sent
      * ``system_message_role``     -> ``"system"`` (universal default)
      * ``prompt_cache_style``      -> ``"none"``
    ``declared`` is False when no ``model_facts`` entry named the slug.
    """

    context_window: int = _CONTEXT_WINDOW_FLOOR
    reasoning_support: bool = False
    native_tool_schema: Optional[str] = None
    system_message_role: str = "system"
    prompt_cache_style: str = "none"
    declared: bool = False


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

    def __init__(self, config_path: Path, machine_path: Optional[Path] = None):
        self._config_path = Path(config_path)
        # The machine routing file (Skill Flywheel additions) merges on top
        # of the operator root. When unspecified, resolve the live hermes_home
        # path via the same resolver the flywheel CLI uses — imported lazily
        # to avoid pulling the CLI import chain into router load order.
        if machine_path is None:
            from grove.flywheel_cli import _machine_config_path

            machine_path = _machine_config_path()
        self._machine_path = Path(machine_path)
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
        # openrouter-zero-retention-routing-v1: operator-owned OpenRouter request
        # shaping (provider order / data_collection / fallbacks), passed through
        # verbatim at the call sites. Empty mapping ⇒ feature off.
        self._provider_routing: dict = {}
        # binding-opacity-v1 P4a: slug-keyed operator-declared model physics,
        # parsed from routing.model_facts. Resolved on demand via
        # model_facts_for(slug); absence is safe-defaulted with a one-shot warn.
        self._model_facts: dict = {}
        self._warned_missing_facts: set = set()
        # Fault attribution (router-merge-wiring-v1): sha256 of the source
        # files at the last successful load. Empty until the first load;
        # used by reload() to attribute a kept-last-known-good outcome.
        self._last_operator_hash: str = ""
        self._last_machine_hash: str = ""
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

    def get_provider_routing(self) -> dict:
        """The ``routing.provider_routing`` mapping (or ``{}`` when unset).

        Operator-owned, provider-specific request shaping (e.g. the OpenRouter
        ``provider`` object). Returned verbatim — the router never interprets it;
        the call sites attach it to the matching provider's request."""
        return self._provider_routing

    def model_facts_for(self, slug: Optional[str]) -> "ModelFacts":
        """Resolve a bound model slug to its declared physics (``ModelFacts``).

        The slug is an opaque dict key — never parsed (R-2). An undeclared slug,
        or one missing ``context_window``, gets safe defaults plus a ONE-SHOT
        loud warning naming the slug: never a ``KeyError``, never silent-wrong.
        Mirrors the ``TierConfig`` cost one-shot-warning discipline.
        """
        entry = self._model_facts.get(slug or "")
        if not isinstance(entry, dict):
            self._warn_missing_facts(slug, "no model_facts entry")
            return ModelFacts(declared=False)
        cw_raw = entry.get("context_window")
        if cw_raw is None:
            self._warn_missing_facts(slug, "no context_window declared")
            context_window = _CONTEXT_WINDOW_FLOOR
        else:
            context_window = int(cw_raw)
        return ModelFacts(
            context_window=context_window,
            reasoning_support=bool(entry.get("reasoning_support", False)),
            native_tool_schema=entry.get("native_tool_schema"),
            system_message_role=str(entry.get("system_message_role") or "system"),
            prompt_cache_style=str(entry.get("prompt_cache_style") or "none"),
            declared=True,
        )

    def _warn_missing_facts(self, slug: Optional[str], reason: str) -> None:
        if slug in self._warned_missing_facts:
            return
        self._warned_missing_facts.add(slug)
        logger.warning(
            "[grove.router] routing.model_facts: %s for bound model %r — using "
            "safe defaults (context_window=%d floor). Declare it under "
            "routing.model_facts in routing.config.yaml to silence this.",
            reason, slug, _CONTEXT_WINDOW_FLOOR,
        )

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
        3. Declarative routing_rules — every rule in config-key
           (insertion) order. The first enabled rule whose match criteria
           all hold against the classification decides the tier.
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

        # 3. Declarative routing rules — every enabled rule in config-key
        #    (insertion) order, first match wins. No rule name is special.
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
            # Fault attribution (router-merge-wiring-v1): the retained hashes
            # are unchanged (we kept last known good); attribute the failed
            # load to whichever source file on disk diverged from them.
            changed_file = self._classify_changed(
                self._hash_file(self._config_path),
                self._hash_file(self._machine_path),
            )
            log_routing_config_load(
                outcome="kept_last_known_good",
                operator_hash=self._last_operator_hash,
                machine_hash=self._last_machine_hash,
                changed_file=changed_file,
                error=repr(exc),
            )

    # ----- internals ----------------------------------------------------------

    @staticmethod
    def _hash_file(path: Optional[Path]) -> str:
        """sha256 hexdigest of ``path``'s bytes, or the absent sentinel."""
        if path is None or not Path(path).exists():
            return _MACHINE_ABSENT_SENTINEL
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def _classify_changed(self, cur_operator: str, cur_machine: str) -> str:
        """Attribute a load to the file(s) diverging from the retained hash."""
        operator_changed = cur_operator != self._last_operator_hash
        machine_changed = cur_machine != self._last_machine_hash
        if operator_changed and machine_changed:
            return "both"
        if operator_changed:
            return "operator"
        if machine_changed:
            return "machine"
        return "none"

    def _load_into_self(self) -> None:
        """Read, parse, validate; mutate self atomically on success."""
        raw = load_merged_routing_config(self._config_path, self._machine_path)

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
            cost_input_raw = spec.get("cost_per_mtok_input")
            cost_output_raw = spec.get("cost_per_mtok_output")
            tiers[name] = TierConfig(
                tier=name,
                handler=spec.get("handler"),
                provider=spec.get("provider"),
                model=spec.get("model"),
                max_tokens=spec.get("max_tokens"),
                max_latency_ms=spec.get("max_latency_ms"),
                description=str(spec.get("description") or "").strip(),
                cost_per_mtok_input=(
                    float(cost_input_raw) if cost_input_raw is not None else None
                ),
                cost_per_mtok_output=(
                    float(cost_output_raw) if cost_output_raw is not None else None
                ),
                fallback_tier=(
                    str(spec["fallback_tier"]).strip()
                    if spec.get("fallback_tier")
                    else None
                ),
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

        # openrouter-zero-retention-routing-v1: optional, operator-owned. Absent
        # ⇒ {} (feature off). Present ⇒ must be a mapping; the inner objects
        # (e.g. provider_routing.openrouter) are passed through verbatim, so we
        # validate only the container shape, not provider field names.
        provider_routing = routing.get("provider_routing") or {}
        if not isinstance(provider_routing, dict):
            raise ValueError(
                f"routing config at {self._config_path}: 'provider_routing' must"
                f" be a mapping (got {type(provider_routing).__name__})"
            )

        # binding-opacity-v1 P4a — slug-keyed operator-declared model physics.
        # Optional: absent ⇒ empty mapping (every bound model safe-defaults). The
        # router never parses the slug; it is a dict key (R-2).
        model_facts = routing.get("model_facts") or {}
        if not isinstance(model_facts, dict):
            raise ValueError(
                f"routing config at {self._config_path}: 'model_facts' must be a "
                f"mapping of slug -> facts (got {type(model_facts).__name__})"
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
        self._provider_routing = provider_routing
        self._model_facts = model_facts

        # Fault attribution (router-merge-wiring-v1): record the source-file
        # hashes that produced this merged state and emit a loaded event,
        # attributing the load to whichever file diverged from the prior
        # retained hashes (both, on the first load). Emitted only after the
        # swap succeeds, so a loaded event always reflects live state.
        current_operator_hash = self._hash_file(self._config_path)
        current_machine_hash = self._hash_file(self._machine_path)
        changed_file = self._classify_changed(
            current_operator_hash, current_machine_hash
        )
        self._last_operator_hash = current_operator_hash
        self._last_machine_hash = current_machine_hash
        log_routing_config_load(
            outcome="loaded",
            operator_hash=current_operator_hash,
            machine_hash=current_machine_hash,
            changed_file=changed_file,
        )


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


# declarative-routing-rules-v1 (GRV-001 Invariant I) — the allowed key sets
# per rule kind. A key outside these sets is a malformed rule: raise loud
# naming the offender rather than silently ignore it. The rule NAME is NOT
# locked here — any name parses; only the SHAPE is constrained.
_SET_TIER_RULE_KEYS = frozenset({"enabled", "target_tier", "match"})
_SET_TIER_MATCH_KEYS = frozenset(
    {"complexity", "intents", "min_confidence", "max_confidence"}
)
_ESCALATION_RULE_KEYS = frozenset({"enabled", "action", "match"})
_ESCALATION_MATCH_KEYS = frozenset({"intents", "max_confidence"})


def _reject_unknown_keys(present, allowed: frozenset, label: str) -> None:
    """Raise naming the unknown key(s) when *present* is not a subset of
    *allowed*. The loud half of Invariant I: an unknown key is a malformed
    rule, never a silent no-op (the silent-ignore gap this closes)."""
    unknown = sorted(set(present) - allowed)
    if unknown:
        raise ValueError(
            f"{label} has unknown key(s) {unknown}; allowed keys are "
            f"{sorted(allowed)}"
        )


def _parse_routing_rules(routing: dict, default_threshold: float) -> list:
    """Build the ordered routing-rule list — one rule per ``routing_rules``
    key, in config (insertion) order, first-enabled-match-wins.

    declarative-routing-rules-v1 (GRV-001 Invariant I): EVERY key is parsed
    as a rule; the rule NAME is not locked in code. A well-formed novel name
    (e.g. ``premium_coding``) parses and evaluates at its config-key
    position. A malformed rule — or an unknown rule-level / match-level key —
    fails loud naming the offender (no silent drop, no silent ignore). Eval
    order equals config-key order by construction, not by incident.

    Rule kinds:
      * any key != ``escalation`` → a ``target_tier`` (set-tier) rule:
        requires a string ``target_tier``; matches on
        complexity / intents / min_confidence / max_confidence. (The legacy
        ``downward`` / ``upward_moderate`` / ``upward`` names are now just
        ordinary set-tier rules — no special-casing.)
      * ``escalation`` → the low-confidence ``step_up`` rule, parsed AND
        evaluated at its config-key position (fully positional — no
        evaluate-last pinning). Its match accepts ``intents`` +
        ``max_confidence``; ``action`` defaults to and must be ``step_up``.

    Backward-compat: a config with NO ``escalation`` key still gets a
    step_up synthesized from the top-level ``escalation.threshold``,
    appended LAST (it has no declared position).
    """
    raw = routing.get("routing_rules") or {}
    if not isinstance(raw, dict):
        raise ValueError("'routing_rules' must be a mapping")

    rules: list = []
    saw_escalation = False

    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise ValueError(f"routing_rules.{name} must be a mapping")

        if name == "escalation":
            saw_escalation = True
            _reject_unknown_keys(
                spec.keys(), _ESCALATION_RULE_KEYS, f"routing_rules.{name}"
            )
            esc_action = spec.get("action") or "step_up"
            if esc_action != "step_up":
                raise ValueError(
                    f"routing_rules.escalation.action must be 'step_up', got"
                    f" {esc_action!r}"
                )
            esc_match = spec.get("match") or {}
            if not isinstance(esc_match, dict):
                raise ValueError(
                    "routing_rules.escalation.match must be a mapping"
                )
            _reject_unknown_keys(
                esc_match.keys(), _ESCALATION_MATCH_KEYS,
                f"routing_rules.{name}.match",
            )
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
                    enabled=bool(spec.get("enabled", True)),
                    target_tier=None,
                    action="step_up",
                    # Sprint 54 — accept an ``intents:`` filter so the
                    # operator can narrow step_up to specific intent
                    # classes. Omitted/empty → fires on any intent
                    # (Sprint 12 behaviour).
                    complexity=frozenset(),
                    intents=_as_frozenset(esc_match.get("intents")),
                    min_confidence=None,
                    max_confidence=max_confidence,
                )
            )
            continue

        # set-tier rule (any non-escalation name, including the legacy
        # downward / upward_moderate / upward).
        _reject_unknown_keys(
            spec.keys(), _SET_TIER_RULE_KEYS, f"routing_rules.{name}"
        )
        target = spec.get("target_tier")
        if not isinstance(target, str) or not target:
            raise ValueError(
                f"routing_rules.{name} needs a string 'target_tier'"
            )
        match = spec.get("match") or {}
        if not isinstance(match, dict):
            raise ValueError(f"routing_rules.{name}.match must be a mapping")
        _reject_unknown_keys(
            match.keys(), _SET_TIER_MATCH_KEYS, f"routing_rules.{name}.match"
        )
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

    # Backward-compat: no DECLARED escalation key → synthesize the step_up
    # from the top-level escalation.threshold and append LAST (it has no
    # config position to evaluate at).
    if not saw_escalation:
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


def get_provider_routing() -> dict:
    """Module-level: the ``routing.provider_routing`` mapping, or ``{}``.

    Unlike :func:`get_tier_config`, this does NOT raise when the router is
    uninitialized — provider routing is an OPTIONAL feature, so absence (or an
    uninitialized router) means simply "no routing configured", never an error.
    """
    if _default_router is None:
        return {}
    return _default_router.get_provider_routing()


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

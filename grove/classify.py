"""T-telemetry classifier for the Grove Autonomaton.

Sprint 12 (haiku-telemetry-normalization-v1). One Haiku call per operator
request produces a structured classification — intent, register,
complexity, confidence — plus a deterministic pattern hash. The
classification feeds two consumers: route() (confidence drives tier
escalation) and the telemetry log (the enrichment Kaizen's Ratchet mines).

The classifier runs on the T1 tier declared in routing.config.yaml
(telemetry.tier). The intent taxonomy is code, not operator-editable
config: it is the system's own model of what work looks like.

Failure is the one commanded graceful degradation (Sprint 12 D4): on any
API error, timeout, or malformed response, classify_for_routing() logs
loudly and returns None. route() then falls back to default-tier
behaviour — the agent always runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The intent taxonomy — the system's model of what operator work looks like.
INTENT_CLASSES = (
    "code_generation",
    "debugging",
    "analysis",
    "planning",
    "factual_retrieval",
    "creative_writing",
    "system_admin",
    "conversation",
)
REGISTER_CLASSES = ("technical", "strategic", "casual", "formal")
COMPLEXITY_SIGNALS = ("simple", "moderate", "complex", "novel")

# Sprint 28 Phase 2 — goal-alignment taxonomy. The Haiku classifier scores
# each request against the operator's current goals.md content. The closed
# set protects downstream consumers (Skill Flywheel, future Cognitive
# Router learning) from arbitrary string values accumulating in the feed.
GOAL_ALIGNMENT_VALUES = (
    "direct",         # directly advances a stated goal
    "indirect",       # supports something that helps a goal
    "orthogonal",     # neither helping nor blocking
    "distracting",    # pulls focus away from goals
    "no_goals_set",   # goals.md empty or absent (graceful tier)
)


def _build_classification_system_prompt(goals_content: str) -> str:
    """Compose the classifier system prompt with the two-envelope schema.

    Sprint 28 Phase 2 GATE-A directive: structural separation between
    the routing-critical fields and the learning-layer goal_alignment.
    The two-envelope JSON output protects routing accuracy from any
    semantic noise the goal_alignment reasoning introduces — the model
    treats them as independent answers within one response.

    Empty/missing goals: ``goals_content`` is the empty string. The
    prompt names this case explicitly so the model returns
    ``goal_alignment: "no_goals_set"`` without hallucinating goals.
    """
    goals_block = goals_content.strip() if goals_content else (
        "(no goals set; return goal_alignment: no_goals_set)"
    )
    return f"""\
You are the telemetry classifier for a cognitive-routing system. You read
one operator request and return exactly one JSON object describing it —
no prose, no markdown, no explanation.

Return ONE JSON object with TWO envelopes:

"routing_envelope" — drives tier selection. Keep these reliable; routing
accuracy depends on them.

  intent_class — the kind of work the operator is asking for:
    code_generation    writing or extending code
    debugging          diagnosing an error or a failing test
    analysis           examining data, code, or a situation for conclusions
    planning           strategy, architecture, breaking work into steps
    factual_retrieval  answering a knowledge question or looking something up
    creative_writing   drafting prose, narrative, or expressive content
    system_admin       file, config, shell, or environment operations
    conversation       casual exchange, clarification, or meta-discussion

  register_class — the communication register: technical, strategic,
    casual, or formal.

  complexity_signal — how demanding the request is: simple, moderate,
    complex, or novel.

  confidence — your confidence in the intent_class, a number 0.0 to 1.0.

"learning_envelope" — drives the feed-first learning layer. Routing
ignores these; this is interpretive signal for cross-session pattern
recognition.

  goal_alignment — how the request aligns with the operator's current
    goals (listed below). One of:
    direct        — the request directly advances a stated goal
    indirect      — the request supports something that helps a goal
    orthogonal    — neither helping nor blocking
    distracting   — pulls focus away from goals
    no_goals_set  — goals.md empty or absent (graceful tier)

OPERATOR GOALS (the alignment target):
{goals_block}

Pick the single best value for each field. Return ONE JSON object with
both envelopes; no prose, no markdown, no explanation.\
"""


def _goals_path() -> Path:
    """Resolve the runtime goals.md path.

    Reads from the operator runtime copy at ``$GROVE_HOME/goals.md``
    (default ``~/.grove/goals.md``) per Sprint 28 GATE-A disposition
    A-x1. The repo template at ``config/identity/goals.md`` is a
    starting point, not the authority — the active runtime state is
    the sovereign truth of the Autonomaton.
    """
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "goals.md"


def _read_goals_content() -> str:
    """Read the operator's runtime goals.md; return "" if missing/empty.

    Graceful-tier per the goals.md template comment: a missing file is
    fine, the classifier composes without it (the prompt names the
    empty case explicitly so the model returns ``no_goals_set``).
    Any read error degrades the same way — log debug, return empty.
    """
    try:
        text = _goals_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        logger.debug("[classify] could not read goals.md: %r", exc)
        return ""
    return text


_MAX_OUTPUT_TOKENS = 280  # Sprint 28 Phase 2: room for two envelopes
_STEM_CHARS = 100  # message-stem length for the pattern hash (D9)

# Haiku list pricing, May 2026 (USD per million tokens) — cost tracking (D5).
_HAIKU_INPUT_USD_PER_MTOK = 1.0
_HAIKU_OUTPUT_USD_PER_MTOK = 5.0
_DEFAULT_BUDGET_WARN_USD = 20.0

# Cumulative T-telemetry spend this process — a runaway-loop guard, not an
# accounting ledger. A fresh process starts at zero.
_cumulative_cost_usd = 0.0
_budget_warned = False


@dataclass(frozen=True)
class ClassificationResult:
    """A structured classification of one operator request.

    ``pattern_hash`` is computed code-side (not asked of the model): a
    SHA-256 of the intent and a normalized message stem, so identical
    requests hash identically. It is the key Sprint 13's T0 pattern
    cache will match against.

    ``goal_alignment`` is Sprint 28 Phase 2's learning-envelope field.
    Optional so consumers that predate Phase 2 (or responses where the
    learning envelope is missing/malformed) round-trip cleanly with
    ``None``. The router and tier-UX surfaces ignore this field; the
    Skill Flywheel reads it from the intent record store.
    """

    intent_class: str
    pattern_hash: str
    confidence: float
    register_class: str
    complexity_signal: str
    goal_alignment: Optional[str] = None


def classify_for_routing(message: str) -> Optional[ClassificationResult]:
    """Classify one operator request via a single Haiku call.

    Returns a ClassificationResult, or None on any failure — an empty
    message, an uninitialized router, an API error, or a malformed
    response. None is the commanded graceful-degradation signal (D4):
    the caller routes on default-tier behaviour and the agent still runs.
    """
    if not isinstance(message, str) or not message.strip():
        logger.debug("[classify] no text message; skipping classification")
        return None

    try:
        runtime = _telemetry_tier_runtime()
        raw = _call_classifier(runtime, message)
        fields = _parse_classification(raw)
        return ClassificationResult(
            intent_class=fields["intent_class"],
            pattern_hash=_pattern_hash(fields["intent_class"], message),
            confidence=fields["confidence"],
            register_class=fields["register_class"],
            complexity_signal=fields["complexity_signal"],
            goal_alignment=fields.get("goal_alignment"),
        )
    except Exception as exc:
        logger.error(
            "[classify] classification failed; routing without it: %r", exc
        )
        return None


# ----- internals --------------------------------------------------------------


def _telemetry_tier_runtime() -> dict:
    """Resolve runtime (api_key, base_url, model) for the T-telemetry tier.

    Lazy imports of grove.providers avoid a circular import — providers
    imports this module for the route-time classification.
    """
    from grove.providers import _ensure_router, resolve_tier_to_runtime

    router = _ensure_router()
    if router is None:
        raise RuntimeError("no Cognitive Router; cannot resolve the telemetry tier")
    tier_config = router.get_tier_config(router.get_telemetry_tier())
    runtime = resolve_tier_to_runtime(tier_config)
    if runtime.get("api_mode") != "anthropic_messages":
        raise RuntimeError(
            f"telemetry tier resolves api_mode {runtime.get('api_mode')!r}; "
            f"the v0.1 classifier requires an Anthropic-native tier"
        )
    return runtime


def _call_classifier(runtime: dict, message: str) -> str:
    """Make the Haiku classification call; return the raw JSON text.

    S22.1 — credential-aware client construction. The bare
    ``anthropic.Anthropic(api_key=...)`` constructor always sends the
    token in the ``x-api-key`` header, which is correct for
    ``sk-ant-api*`` keys but produces 401 ``invalid x-api-key`` for
    OAuth bearer tokens (Claude Code subscriptions, setup-tokens,
    JWTs). The canonical agent-side client builder
    ``agent.anthropic_adapter.build_anthropic_client`` already
    auto-detects token shape and routes OAuth tokens through
    ``auth_token=`` (Bearer + the oauth-2025-04-20 beta + Claude
    Code identity headers). Reuse it here so the classifier has
    parity with every other agent call site and so any future auth
    scheme added to ``build_anthropic_client`` is picked up
    automatically.

    ``auth_type`` from the runtime dict (S22.1 — threaded through
    ``resolve_tier_to_runtime``) is logged at debug level so an
    operator inspecting routing telemetry can see which path the
    classifier took without inspecting the token itself.
    """
    from agent.anthropic_adapter import build_anthropic_client

    api_key = runtime.get("api_key") or ""
    auth_type = runtime.get("auth_type") or "unspecified"
    logger.debug(
        "[classify] T-telemetry runtime: model=%r base_url=%r auth_type=%r",
        runtime.get("model"),
        runtime.get("base_url"),
        auth_type,
    )
    client = build_anthropic_client(
        api_key=api_key,
        base_url=runtime.get("base_url") or None,
    )
    # Sprint 28 Phase 2: build the system prompt per-call so a goals.md
    # edit takes effect on the next classify without restarting the
    # process. The file is small and the cost is trivial against a
    # Haiku call's existing baseline.
    system_prompt = _build_classification_system_prompt(_read_goals_content())
    response = client.messages.create(
        model=runtime["model"],
        max_tokens=_MAX_OUTPUT_TOKENS,
        system=system_prompt,
        messages=[
            {"role": "user", "content": message},
            {"role": "assistant", "content": "{"},  # prefill — force JSON
        ],
    )
    _track_cost(response.usage)
    # Prefill: the model continues from "{"; rejoin for a full object.
    return "{" + response.content[0].text


def _parse_classification(raw: str) -> dict:
    """Parse the classifier's JSON and validate the required fields.

    Accepts both shapes:

    * **Two-envelope (Sprint 28 Phase 2 contract).** The current prompt
      produces ``{"routing_envelope": {...}, "learning_envelope": {...}}``.
      Routing fields read from ``routing_envelope``; ``goal_alignment``
      reads from ``learning_envelope`` (defaults to ``None`` if absent).

    * **Flat (legacy / Sprint 12 contract).** A response where the
      routing fields sit at the top level. Defensive support for tests
      that mock the older shape and for any in-flight response that
      arrives mid-prompt-transition. Treated as routing-only —
      ``goal_alignment`` is ``None``.

    Raises ValueError if the response is not usable as routing
    classification — caught by ``classify_for_routing`` as a commanded
    fall-through (no classification → default tier; the agent still
    runs per Sprint 12 D4).
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        end = raw.rfind("}")  # tolerate trailing prose after the object
        if end == -1:
            raise ValueError(f"classifier response is not JSON: {raw!r}")
        data = json.loads(raw[: end + 1])

    if not isinstance(data, dict):
        raise ValueError(f"classifier response is not a JSON object: {raw!r}")

    # Two-envelope shape takes priority; otherwise treat the object as
    # the legacy flat routing-only response.
    if isinstance(data.get("routing_envelope"), dict):
        routing = data["routing_envelope"]
        learning = data.get("learning_envelope") if isinstance(
            data.get("learning_envelope"), dict
        ) else {}
    else:
        routing = data
        learning = {}

    for key in ("intent_class", "register_class", "complexity_signal"):
        value = routing.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"classifier response missing string {key!r}: {data!r}"
            )

    try:
        confidence = float(routing.get("confidence"))
    except (TypeError, ValueError):
        raise ValueError(
            f"classifier response missing numeric confidence: {data!r}"
        )

    # goal_alignment is optional in the learning envelope. Validate
    # against the closed set when present; drop (to None) on anything
    # unknown rather than failing the whole classification — the
    # routing path doesn't care about goal_alignment and we don't want
    # a bad learning value to take routing down with it.
    goal_alignment_raw = learning.get("goal_alignment")
    if isinstance(goal_alignment_raw, str):
        candidate = goal_alignment_raw.strip()
        if candidate in GOAL_ALIGNMENT_VALUES:
            goal_alignment: Optional[str] = candidate
        else:
            logger.debug(
                "[classify] learning_envelope.goal_alignment=%r not in "
                "the closed set %s; dropping to None",
                candidate, GOAL_ALIGNMENT_VALUES,
            )
            goal_alignment = None
    else:
        goal_alignment = None

    return {
        "intent_class": routing["intent_class"].strip(),
        "register_class": routing["register_class"].strip(),
        "complexity_signal": routing["complexity_signal"].strip(),
        "confidence": max(0.0, min(1.0, confidence)),  # clamp to 0.0-1.0
        "goal_alignment": goal_alignment,
    }


def _pattern_hash(intent_class: str, message: str) -> str:
    """SHA-256 of the intent and a normalized message stem (D9).

    The stem is the first 100 characters of the message, lowercased and
    whitespace-collapsed — a stable key for Sprint 13's T0 pattern cache
    that never stores the full message.
    """
    stem = " ".join(message[:_STEM_CHARS].lower().split())
    return hashlib.sha256(f"{intent_class}:{stem}".encode("utf-8")).hexdigest()


def _budget_warn_threshold() -> float:
    """The cost ceiling that trips the Jidoka warning. $20 default;
    overridable via the GROVE_TELEMETRY_BUDGET_WARN env var."""
    raw = os.getenv("GROVE_TELEMETRY_BUDGET_WARN", "").strip()
    if not raw:
        return _DEFAULT_BUDGET_WARN_USD
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "[classify] GROVE_TELEMETRY_BUDGET_WARN=%r is not a number; "
            "using the $%.0f default",
            raw,
            _DEFAULT_BUDGET_WARN_USD,
        )
        return _DEFAULT_BUDGET_WARN_USD


def _track_cost(usage) -> None:
    """Accumulate T-telemetry spend; warn once past the budget (D5).

    Cost discipline, not a hard block — classification continues, the
    operator decides. The Jidoka pattern: surface the signal loudly,
    never silently stop.
    """
    global _cumulative_cost_usd, _budget_warned
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    _cumulative_cost_usd += (
        input_tokens / 1_000_000 * _HAIKU_INPUT_USD_PER_MTOK
        + output_tokens / 1_000_000 * _HAIKU_OUTPUT_USD_PER_MTOK
    )
    threshold = _budget_warn_threshold()
    if not _budget_warned and _cumulative_cost_usd > threshold:
        _budget_warned = True
        logger.warning(
            "[classify] T-telemetry spend has passed $%.2f this run "
            "(cumulative $%.4f); classification continues. Adjust the "
            "ceiling with GROVE_TELEMETRY_BUDGET_WARN.",
            threshold,
            _cumulative_cost_usd,
        )


def cumulative_cost_usd() -> float:
    """The cumulative T-telemetry (classification) spend this process.

    A fresh process starts at zero. The CLI session summary reads this to
    report classification cost alongside the per-tier turn costs.
    """
    return _cumulative_cost_usd

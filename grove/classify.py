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

_CLASSIFICATION_SYSTEM_PROMPT = """\
You are the telemetry classifier for a cognitive-routing system. You read
one operator request and return exactly one JSON object describing it —
no prose, no markdown, no explanation.

Return these four fields:

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

Pick the single best value for each field.\
"""

_MAX_OUTPUT_TOKENS = 200
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
    """

    intent_class: str
    pattern_hash: str
    confidence: float
    register_class: str
    complexity_signal: str


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
        )
    except Exception as exc:
        logger.error(
            "[classify] classification failed; routing without it: %r", exc
        )
        return None


def log_turn_classification(message: str) -> None:
    """Classify-to-learn: classify one turn and log it, no routing.

    The per-turn telemetry hook for the interactive loop (Sprint 12). It
    classifies the operator's message and emits a ``classification``
    event so the Ratchet sees every interaction — not only the opening
    turn that drove routing. It never re-routes; the session's tier is
    fixed at construction. A failed or skipped classification logs
    nothing here (classify_for_routing already logged loudly).
    """
    result = classify_for_routing(message)
    if result is None:
        return
    from grove.telemetry import log_classification

    log_classification(
        intent_class=result.intent_class,
        pattern_hash=result.pattern_hash,
        confidence=result.confidence,
        register_class=result.register_class,
        complexity_signal=result.complexity_signal,
    )


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
    """Make the Haiku classification call; return the raw JSON text."""
    import anthropic

    client = anthropic.Anthropic(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url") or None,
    )
    response = client.messages.create(
        model=runtime["model"],
        max_tokens=_MAX_OUTPUT_TOKENS,
        system=_CLASSIFICATION_SYSTEM_PROMPT,
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

    Raises ValueError if the response is not a usable classification —
    caught by classify_for_routing() as a commanded fall-through.
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

    for key in ("intent_class", "register_class", "complexity_signal"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"classifier response missing string {key!r}: {data!r}")

    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        raise ValueError(f"classifier response missing numeric confidence: {data!r}")

    return {
        "intent_class": data["intent_class"].strip(),
        "register_class": data["register_class"].strip(),
        "complexity_signal": data["complexity_signal"].strip(),
        "confidence": max(0.0, min(1.0, confidence)),  # clamp to 0.0-1.0
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

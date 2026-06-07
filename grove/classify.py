"""T-telemetry classifier for the Grove Autonomaton.

Sprint 12 (telemetry-normalization-v1). One T-telemetry classification
call per operator request produces a structured classification — intent,
register, complexity, confidence — plus a deterministic pattern hash.
The classification feeds two consumers: route() (confidence drives tier
escalation) and the telemetry log (the enrichment Kaizen's Ratchet mines).

The classifier runs on whichever tier ``routing.config.yaml`` binds to
``telemetry.tier`` — by default T1, but operators may rebind. The
intent taxonomy is code, not operator-editable config: it is the
system's own model of what work looks like.

Failure is the one commanded graceful degradation (Sprint 12 D4): on any
API error, timeout, or malformed response, classify_for_routing() logs
loudly and returns None. route() then falls back to default-tier
behaviour — the agent always runs.

Cost telemetry reads ``cost_per_mtok_input`` / ``cost_per_mtok_output``
off the T-telemetry tier's ``TierConfig`` (loaded from
``routing.config.yaml``). No model-specific constants live in this
module — when the operator rebinds the telemetry tier, the spend
tracker follows the binding automatically.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The intent taxonomy — the system's model of what operator work looks like.
# Sprint 54 (intent-taxonomy-v2): 8 → 15 labels.  Carves the daily-driver
# work the original Sprint 12 taxonomy collapsed under "conversation" /
# "factual_retrieval" / "system_admin" out into its own first-class
# intents.  T1 is the floor under the inverted routing config, so the
# taxonomy must distinguish work that is naturally cheap (memory recall,
# scheduling, messaging, summarization, translation, retrieval of cached
# T3 artifacts) from work that needs Premium Cognition (code, analysis,
# research) and Apex (planning).
#
# Knowledge-cascade discipline (the economic engine): RETRIEVING a past
# architectural plan or complex analysis from memory is ``retrieval`` or
# ``memory_operation`` — a T1 task — even though CREATING the artifact
# the first time was ``planning`` / T3 work.  T3 pays once; every later
# fetch is cheap.
INTENT_CLASSES = (
    # Daily driver — T1-native under the v2 routing config.
    "memory_operation",
    "scheduling",
    "messaging",
    "retrieval",
    "summarization",
    "translation",
    "conversation",
    "factual_lookup",
    # Knowledge work — T2-native; escalates to T3 on complex/novel.
    "code_generation",
    "debugging",
    "analysis",
    "research",
    "creative_writing",
    "system_admin",
    # Architect work — T2 floor (via upward_moderate), T3-native on
    # complex/novel.
    "planning",
)
REGISTER_CLASSES = ("technical", "strategic", "casual", "formal")
COMPLEXITY_SIGNALS = ("simple", "moderate", "complex", "novel")

# Sprint 28 Phase 2 — goal-alignment taxonomy. The T-telemetry classifier
# scores each request against the operator's current goals.md content. The
# closed set protects downstream consumers (Skill Flywheel, future
# Cognitive Router learning) from arbitrary string values accumulating in
# the feed.
GOAL_ALIGNMENT_VALUES = (
    "direct",         # directly advances a stated goal
    "indirect",       # supports something that helps a goal
    "orthogonal",     # neither helping nor blocking
    "distracting",    # pulls focus away from goals
    "no_goals_set",   # goals.md empty or absent (graceful tier)
)


# Sprint 65 (classifier-tool-use-refactor-v1): the classification tool.
# tool_choice forces the telemetry model to CALL this tool, so the API
# contract — not a prompt instruction — enforces the schema. The model
# physically cannot return {follow_ups: [...]}; it can only emit a
# classify_intent call carrying these parameters. Enums are sourced from
# the module tuples above so the tool schema and the downstream validators
# can never drift.
_CLASSIFY_TOOL = {
    "name": "classify_intent",
    "description": (
        "Record the classification of one operator request. Fill every "
        "field with your single best judgment — the routing_envelope "
        "drives tier selection, the learning_envelope feeds the learning "
        "layer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "routing_envelope": {
                "type": "object",
                "description": (
                    "Drives tier selection. Keep these reliable; routing "
                    "accuracy depends on them."
                ),
                "properties": {
                    "intent_class": {
                        "type": "string",
                        "enum": list(INTENT_CLASSES),
                        "description": "The kind of work the operator is asking for.",
                    },
                    "register_class": {
                        "type": "string",
                        "enum": list(REGISTER_CLASSES),
                        "description": "The communication register.",
                    },
                    "complexity_signal": {
                        "type": "string",
                        "enum": list(COMPLEXITY_SIGNALS),
                        "description": "How demanding the request is.",
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "description": "Confidence in intent_class, 0.0 to 1.0.",
                    },
                },
                "required": [
                    "intent_class",
                    "register_class",
                    "complexity_signal",
                    "confidence",
                ],
            },
            "learning_envelope": {
                "type": "object",
                "description": (
                    "Feeds the feed-first learning layer. Routing ignores "
                    "these."
                ),
                "properties": {
                    "goal_alignment": {
                        "type": "string",
                        "enum": list(GOAL_ALIGNMENT_VALUES),
                        "description": (
                            "How the request aligns with the operator's "
                            "current goals."
                        ),
                    },
                    "is_correction": {
                        "type": "boolean",
                        "description": (
                            "True if the message indicates the system's "
                            "previous response was wrong or misunderstood."
                        ),
                    },
                },
                "required": ["is_correction"],
            },
        },
        "required": ["routing_envelope", "learning_envelope"],
    },
}


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
one operator request and judge what kind of work it is. Record your
judgment by calling the classify_intent tool. Every routing field is
required; fill each with your single best value.

routing_envelope — drives tier selection. Keep these reliable; routing
accuracy depends on them.

  intent_class — the kind of work the operator is asking for. Pick the
    single best label.

    Daily driver:
      memory_operation   storing, recalling, updating, or forgetting
                         operator-owned facts AND retrieving past
                         architectural plans, complex analyses, or any
                         earlier system-generated artifact from memory
      scheduling         calendar, reminders, time-of-day management
      messaging          drafting, sending, or replying to short
                         interpersonal messages (email, chat, iMessage)
      retrieval          fetching a known item from the operator's
                         cellar, recent web context, project docs, or
                         memory — INCLUDING past plans and analyses
                         already created by the system. Retrieving a
                         cached complex artifact is RETRIEVAL, not
                         PLANNING, even when the original creation was
                         expensive
      summarization      condensing or extracting key points from
                         supplied text
      translation        converting text between human languages
      conversation       chit-chat, greetings, clarification of the
                         system's own state, small talk — no external
                         knowledge or tool call
      factual_lookup     simple factual questions with known short
                         answers — no synthesis, no multi-source

    Knowledge work:
      code_generation    writing or modifying code
      debugging          diagnosing an error or a failing test
      analysis           examining supplied data, code, or a situation
                         to reach a conclusion (synthesis on what is
                         already in front of you)
      research           multi-source investigation requiring tool-
                         mediated gathering across web, repos, or docs
      creative_writing   long-form or strategic drafting of prose,
                         narrative, or expressive content
      system_admin       file, config, shell, or environment operations
                         that change system state (creating dotfiles,
                         launchd plists, systemd units, cron entries,
                         shell scripts that wire the OS — NOT
                         application code)

    Architect work:
      planning           novel synthesis — multi-step strategy or
                         system architecture that CREATES something new

  register_class — the communication register: technical, strategic,
    casual, or formal.

  complexity_signal — how demanding the request is: simple, moderate,
    complex, or novel.

  confidence — your confidence in the intent_class, a number 0.0 to 1.0.

EXAMPLES — boundary cases the definitions alone can blur:

  "Pull up the payment system architecture we designed yesterday"
    → retrieval, simple        (fetch a cached artifact; NOT planning)
  "Design a payment system architecture"
    → planning, complex        (novel synthesis; CREATE the artifact)

  "What's the capital of France"
    → factual_lookup, simple   (short known answer; no source needed)
  "Find the cellar entry about Postgres backups"
    → retrieval, simple        (a known item from a specific source)

  "How are you"
    → conversation, simple     (small talk; no knowledge question)
  "What is 2 + 2"
    → factual_lookup, simple   (a short knowledge question)

  "Compare React Server Components vs Solid signals"
    → research, moderate       (multi-source investigation required)
  "Review this PR for risks"
    → analysis, moderate       (synthesis on what is in front of you)

learning_envelope — drives the feed-first learning layer. Routing
ignores these; this is interpretive signal for cross-session pattern
recognition.

  goal_alignment — how the request aligns with the operator's current
    goals (listed below). One of:
    direct        — the request directly advances a stated goal
    indirect      — the request supports something that helps a goal
    orthogonal    — neither helping nor blocking
    distracting   — pulls focus away from goals
    no_goals_set  — goals.md empty or absent (graceful tier)

  is_correction — true if the user's message indicates the system's
    previous response was incorrect, misunderstood, or needs
    adjustment; false otherwise. Default false.
    Corrections (true): "actually, that's wrong", "you misunderstood",
      "I meant X not Y", "no, that's not right", "scratch that".
    NOT corrections (false): "thanks, now do X" (acknowledgment +
      new task), "actually, can you also do X" (extension),
      "no, the meeting is Tuesday" (factual answer), the operator's
      first message in a session.

OPERATOR GOALS (the alignment target):
{goals_block}

Pick the single best value for each field and call classify_intent.\
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

# Cost telemetry: USD-per-million-tokens values are read from the
# T-telemetry ``TierConfig`` per call (``cost_per_mtok_input`` /
# ``cost_per_mtok_output``). No model-specific constants are declared
# here — the operator rebinds the telemetry tier and the spend tracker
# follows automatically.
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

    ``is_correction`` is Sprint 38's learning-envelope addition. The
    Dispatcher reads it at ``_finalize_previous_turn_pending`` time
    to branch the previous turn's outcome between ``success`` and
    ``correction``. ``None`` when absent or unparseable — the
    finalizer treats None as False, biasing toward success.
    """

    intent_class: str
    pattern_hash: str
    confidence: float
    register_class: str
    complexity_signal: str
    goal_alignment: Optional[str] = None
    is_correction: Optional[bool] = None


def classify_for_routing(message: str) -> Optional[ClassificationResult]:
    """Classify one operator request via a single T-telemetry call.

    Returns a ClassificationResult, or None on any failure — an empty
    message, an uninitialized router, an API error, or a malformed
    response. None is the commanded graceful-degradation signal (D4):
    the caller routes on default-tier behaviour and the agent still runs.
    """
    if not isinstance(message, str) or not message.strip():
        logger.debug("[classify] no text message; skipping classification")
        return None

    try:
        runtime, tier_config = _telemetry_tier_runtime()
        tool_input = _call_classifier(runtime, message, tier_config=tier_config)
        fields = _parse_classification(tool_input)
        return ClassificationResult(
            intent_class=fields["intent_class"],
            pattern_hash=_pattern_hash(fields["intent_class"], message),
            confidence=fields["confidence"],
            register_class=fields["register_class"],
            complexity_signal=fields["complexity_signal"],
            goal_alignment=fields.get("goal_alignment"),
            is_correction=fields.get("is_correction"),
        )
    except Exception as exc:
        logger.error(
            "[classify] classification failed; routing without it: %r", exc
        )
        return None


# ----- internals --------------------------------------------------------------


def _telemetry_tier_runtime():
    """Resolve runtime + TierConfig for the T-telemetry tier.

    Returns a ``(runtime_dict, tier_config)`` pair. The runtime dict
    carries the agent-ready call surface (api_key, base_url, model,
    api_mode, auth_type, credential_pool); the tier_config carries the
    declarative policy fields the call site reads — currently
    ``cost_per_mtok_input`` / ``cost_per_mtok_output`` for spend
    tracking. Returning both avoids a second router lookup at the
    caller and keeps the cost-telemetry plumbing model-binding-agnostic.

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
    return runtime, tier_config


def _call_classifier(
    runtime: dict,
    message: str,
    *,
    tier_config: "Optional[Any]" = None,
) -> dict:
    """Make the T-telemetry classification call; return the tool input dict.

    Sprint 65 (classifier-tool-use-refactor-v1): the call forces the
    ``classify_intent`` tool via ``tool_choice``, so the schema is enforced
    by the API contract rather than by prompt instruction. The model
    returns a ``tool_use`` block whose ``input`` is the structured
    classification — no prefill, no freeform JSON, no bracket hunting.

    ``tier_config`` is the T-telemetry ``TierConfig`` resolved by
    ``_telemetry_tier_runtime``; its ``cost_per_mtok_input`` /
    ``cost_per_mtok_output`` fields drive the spend tracker.

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
    # process. The file is small and the cost is trivial against the
    # T-telemetry classifier's existing baseline.
    system_prompt = _build_classification_system_prompt(_read_goals_content())
    response = client.messages.create(
        model=runtime["model"],
        max_tokens=_MAX_OUTPUT_TOKENS,
        system=system_prompt,
        tools=[_CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "classify_intent"},
        messages=[{"role": "user", "content": message}],
    )
    _track_cost(response.usage, tier_config=tier_config)
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and \
                getattr(block, "name", None) == "classify_intent":
            return block.input
    # tool_choice forces the call, so this should never fire. If it does,
    # raise loudly — classify_for_routing's except logs it at ERROR and
    # routes on default-tier behaviour (the commanded Sprint 12 D4
    # degradation). A silent None would discard the diagnostic.
    raise ValueError(
        "classifier returned no classify_intent tool_use block: "
        f"{getattr(response, 'content', None)!r}"
    )


def _parse_classification(data: dict) -> dict:
    """Validate the classifier tool input and map it to result fields.

    Sprint 65 (classifier-tool-use-refactor-v1): ``data`` arrives as the
    ``classify_intent`` tool's structured ``input`` dict — the API
    contract already guarantees the envelope shape and the enums. This
    function still validates and normalizes defensively (a model can omit
    an optional field; confidence is clamped, ``goal_alignment`` is
    closed-set-checked, ``is_correction`` is lenient-parsed) and maps the
    two envelopes onto the ClassificationResult fields.

    Accepts both shapes:

    * **Two-envelope.** ``{"routing_envelope": {...},
      "learning_envelope": {...}}`` — the tool's contract. Routing fields
      read from ``routing_envelope``; ``goal_alignment`` /
      ``is_correction`` read from ``learning_envelope``.

    * **Flat.** Routing fields at the top level — defensive support for
      tests and any caller passing a flat dict. Treated as routing-only.

    Raises ValueError if the input is not usable as routing
    classification — caught by ``classify_for_routing`` as a commanded
    fall-through (no classification → default tier; the agent still
    runs per Sprint 12 D4).
    """
    if not isinstance(data, dict):
        raise ValueError(f"classifier tool input is not an object: {data!r}")

    # Two-envelope shape takes priority; otherwise treat the object as
    # a flat routing-only input.
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

    # Sprint 38 — is_correction is the learning-envelope bool the
    # Dispatcher reads at finalization time to branch the previous
    # turn's outcome between success and correction. Accept the JSON
    # bool literal; accept the strings "true"/"false" as a
    # lenient-parse aid; everything else degrades to None and the
    # finalizer treats None as False.
    is_correction_raw = learning.get("is_correction")
    if isinstance(is_correction_raw, bool):
        is_correction: Optional[bool] = is_correction_raw
    elif isinstance(is_correction_raw, str):
        normalized = is_correction_raw.strip().lower()
        if normalized == "true":
            is_correction = True
        elif normalized == "false":
            is_correction = False
        else:
            logger.debug(
                "[classify] learning_envelope.is_correction=%r is not a "
                "bool literal; dropping to None", is_correction_raw,
            )
            is_correction = None
    else:
        is_correction = None

    return {
        "intent_class": routing["intent_class"].strip(),
        "register_class": routing["register_class"].strip(),
        "complexity_signal": routing["complexity_signal"].strip(),
        "confidence": max(0.0, min(1.0, confidence)),  # clamp to 0.0-1.0
        "goal_alignment": goal_alignment,
        "is_correction": is_correction,
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


_missing_cost_warned = False


def _track_cost(usage, *, tier_config) -> None:
    """Accumulate T-telemetry spend; warn once past the budget (D5).

    USD-per-million-tokens values come from the T-telemetry tier's
    ``TierConfig`` (``cost_per_mtok_input`` / ``cost_per_mtok_output``).
    When either is ``None`` — operator has not declared cost for the
    bound tier — accumulate nothing and emit one loud warning per
    process (Jidoka pattern: surface the gap, do not silently default
    to zero). The classification call itself continues unaffected.

    Cost discipline, not a hard block — classification continues, the
    operator decides. The Jidoka pattern: surface the signal loudly,
    never silently stop.
    """
    global _cumulative_cost_usd, _budget_warned, _missing_cost_warned
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0

    cost_in = getattr(tier_config, "cost_per_mtok_input", None)
    cost_out = getattr(tier_config, "cost_per_mtok_output", None)
    if cost_in is None or cost_out is None:
        if not _missing_cost_warned:
            _missing_cost_warned = True
            tier_name = getattr(tier_config, "tier", "?")
            logger.warning(
                "[classify] T-telemetry tier %r declares no "
                "cost_per_mtok_input/output in routing.config.yaml; "
                "skipping spend accumulation for this process. "
                "Classification continues. Declare the values under the "
                "tier's block to restore cost tracking.",
                tier_name,
            )
        return

    _cumulative_cost_usd += (
        input_tokens / 1_000_000 * float(cost_in)
        + output_tokens / 1_000_000 * float(cost_out)
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

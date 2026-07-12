"""grove/t1_call.py — the shared T1 (Cheap Cognition) call primitive.

Sprint K1 (living-cellar-v1) Phase 1. One entry point, :func:`call_t1`, for
code paths that need a single structured T1 Haiku call without reaching into
``grove.classify`` internals.

The T1 tier is resolved BY NAME through the router's PUBLIC tier API
(:func:`grove.router.get_tier_config` + :func:`grove.providers.resolve_tier_to_runtime`)
— deliberately NOT ``grove.classify._telemetry_tier_runtime`` (private, and
pinned to the *telemetry* tier rather than T1). The model binding therefore
follows ``routing.config.yaml`` ``tier_preferences.T1`` (Haiku by default);
rebinding the tier moves this primitive with no code change.

The client is built with :func:`agent.anthropic_adapter.build_anthropic_client`,
the credential-aware builder shared by every other agent call site (handles
both ``sk-ant-api*`` keys and OAuth bearer tokens).

Two modes:

* ``tool`` given — a forced tool call (``tool_choice`` pins that tool),
  returning the validated structured ``input`` dict. Use for the Evaluator's
  verdict.
* ``tool`` absent — a plain-text completion, returning the concatenated text.
  Use for the Writer / Editor prose.

Provider-agnostic (wiki-pipeline-provider-agnostic-v1): the call branches on the
T1 tier's resolved ``api_mode``. ``anthropic_messages`` uses the Messages API
(preserved byte-for-byte); ``chat_completions`` uses the OpenAI-compatible Chat
Completions API (OpenRouter, Ollama, vLLM, …) — the SAME tier the telemetry
classifier already runs on. A passed Anthropic-style ``tool`` is reshaped to the
OpenAI function shape generically, so no consumer maintains a parallel constant.
Either surface forces the tool, so the structured contract holds on both.

Fail loud (Digital Jidoka): an unrecognized api_mode, a missing tool call, or an
empty text response each raise — never a silent default. Cost is
tracked by replicating the ``cost_per_mtok_*`` field read from the resolved
``TierConfig`` (no import of classify's private spend tracker); an undeclared
cost surfaces one loud warning per process and skips accumulation rather than
defaulting to zero.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)

# The tier this primitive targets, by name (routing.config.yaml key).
_T1_TIER = "T1"


class T1TruncationError(ValueError):
    """A forced-tool T1 call was cut at the output-token cap.

    wiki-writer-structured-output-v1 P2, carrying the P0 wire findings:
    OpenRouter rewrites a provider cap-hit to top-level
    ``finish_reason: "tool_calls"`` — the truth is the router's
    ``native_finish_reason: "length"`` extra field. Raised when a
    chat_completions forced-tool response is cap-cut, whether the truncated
    JSON fails to parse (the common case — unterminated string) OR happens to
    parse valid-but-shorter (the silent-damage class this exception exists to
    kill). Subclasses ValueError so existing callers' error handling is
    unchanged; callers that can retry catch THIS type and re-call at a raised
    cap (P0: identical-at-cap retry is deterministic 0/6; raised-cap 2/2).
    """

# Default output ceiling when the caller does not specify one. Callers size
# this per call (a Writer wants more than an Evaluator).
_DEFAULT_MAX_TOKENS = 4096

# Cumulative T1 spend this process — a runaway-loop signal, not an accounting
# ledger. A fresh process starts at zero.
_cumulative_cost_usd = 0.0
_missing_cost_warned = False


def call_t1(
    prompt: str,
    *,
    system: Optional[str] = None,
    tool: Optional[Dict[str, Any]] = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    tier: Optional[str] = None,
) -> Union[str, Dict[str, Any]]:
    """Make one structured tier call and return its result.

    ``tool`` present → forced tool call; returns the tool's structured
    ``input`` dict. ``tool`` absent → plain-text completion; returns the
    concatenated text. Branches on the resolved tier's ``api_mode``
    (``anthropic_messages`` | ``chat_completions``); any other value raises.
    Raises loudly on a malformed response.

    ``tier`` (drafter-quality-checks-v1 P2, additive): resolve THIS tier by
    name instead of the historic ``"T1"`` default — the quality-gate evaluator
    resolves its record-declared ``evaluator_tier`` through the same by-name
    primitive (R-A5: evaluator tier independent of the producer pin). ``None``
    → ``"T1"``, so every pre-existing call site is behavior-identical. An
    unknown tier name raises the router's loud KeyError — never a fallback.
    """
    runtime, tier_config = _resolve_t1_runtime(tier or _T1_TIER)
    api_mode = runtime.get("api_mode")

    # wiki-pipeline-provider-agnostic-v1: branch on the wire protocol the T1
    # tier resolves to. The anthropic_messages path is preserved byte-for-byte
    # (I1); chat_completions drives any OpenAI-compatible provider (the same
    # tier the telemetry classifier already runs on). Both honour a plain-text
    # OR a forced-tool call, so the Writer/Editor/Evaluator are transport-blind.
    if api_mode == "anthropic_messages":
        from agent.anthropic_adapter import build_anthropic_client

        client = build_anthropic_client(
            api_key=runtime.get("api_key") or "",
            base_url=runtime.get("base_url") or None,
        )

        kwargs: Dict[str, Any] = {
            "model": runtime["model"],
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        if tool is not None:
            kwargs["tools"] = [tool]
            kwargs["tool_choice"] = {"type": "tool", "name": tool["name"]}

        response = client.messages.create(**kwargs)
        _track_cost(getattr(response, "usage", None), tier_config)

        if tool is not None:
            return _extract_tool_input(response, tool["name"])
        return _extract_text(response)

    if api_mode == "chat_completions":
        # Lazy import keeps the ~800ms openai/pydantic load off the module-import
        # path, matching the Anthropic branch's local import.
        from openai import OpenAI

        client = OpenAI(
            api_key=runtime.get("api_key") or "",
            base_url=runtime.get("base_url") or None,
        )

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        kwargs = {
            "model": runtime["model"],
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if tool is not None:
            kwargs["tools"] = [_to_openai_tool(tool)]
            kwargs["tool_choice"] = {
                "type": "function",
                "function": {"name": tool["name"]},
            }

        # openrouter-zero-retention-routing-v1: attach the operator's OpenRouter
        # provider routing (order/data_collection/fallbacks) verbatim, when this
        # is an OpenRouter call and routing is configured. No-op otherwise.
        from grove.providers import openrouter_provider_pref

        _pp = openrouter_provider_pref(runtime)
        if _pp:
            kwargs["extra_body"] = {"provider": _pp}

        response = client.chat.completions.create(**kwargs)
        _track_cost(getattr(response, "usage", None), tier_config)

        if tool is not None:
            return _extract_openai_tool_input(response, tool["name"])
        return _extract_openai_text(response)  # text mode: byte-preserved

    # Any other api_mode (bedrock_converse, codex_responses, …) is not a surface
    # this primitive speaks. Fail loud rather than issue a mis-shaped call.
    raise RuntimeError(
        f"call_t1: unsupported T1 api_mode {api_mode!r} "
        f"(model={runtime.get('model')!r}); bind T1 to an anthropic_messages "
        f"or chat_completions provider in routing.config.yaml."
    )


# ── tier resolution (public API, by name) ──────────────────────────────


def _resolve_t1_runtime(tier_name: str = _T1_TIER):
    """Resolve a tier's ``(runtime, tier_config)`` via the public router API.

    drafter-quality-checks-v1 P2: parameterized by tier NAME (default the
    historic ``"T1"``) so :func:`call_t1` consumers can target the tier a
    capability record declares. Resolution stays by-name through the public
    router — no classification, no private imports.

    A1: no import of the private ``_telemetry_tier_runtime``. A fresh CLI
    process may not have initialized the module router yet — initialize it
    via the public :func:`grove.router.initialize` and retry once.

    wiki-pipeline-provider-agnostic-v1: the pre-call ``anthropic_messages``
    guard is retired. :func:`call_t1` now speaks both anthropic_messages and
    chat_completions, branching on the resolved ``api_mode`` and failing loud
    POST-call on a structural failure (missing tool call / empty text) or on an
    unrecognized api_mode — never on what the provider IS.
    """
    from grove import router as grove_router
    from grove.providers import resolve_tier_to_runtime

    try:
        tier_config = grove_router.get_tier_config(tier_name)
    except RuntimeError:
        # Router not initialized (fresh process). Initialize and retry — a
        # required dependency made ready, not an error swallowed.
        grove_router.initialize()
        tier_config = grove_router.get_tier_config(tier_name)

    return resolve_tier_to_runtime(tier_config), tier_config


# ── response extraction (fail loud) ────────────────────────────────────


def _extract_tool_input(response, tool_name: str) -> Dict[str, Any]:
    for block in getattr(response, "content", None) or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == tool_name
        ):
            return block.input
    raise ValueError(
        f"T1 returned no {tool_name!r} tool_use block: "
        f"{getattr(response, 'content', None)!r}"
    )


def _extract_text(response) -> str:
    parts = [
        block.text
        for block in getattr(response, "content", None) or []
        if getattr(block, "type", None) == "text"
    ]
    if not parts:
        raise ValueError(
            f"T1 returned no text content: {getattr(response, 'content', None)!r}"
        )
    return "".join(parts)


# ── chat_completions extraction (OpenAI-compatible, fail loud) ──────────


def _to_openai_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
    """Reshape an Anthropic-style tool (``{name, description, input_schema}``)
    into the OpenAI function-tool shape. Generic — :func:`call_t1` reshapes
    whatever tool it is passed, so no consumer maintains a parallel constant."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool["input_schema"],
        },
    }


def _extract_openai_tool_input(response, tool_name: str) -> Dict[str, Any]:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise ValueError(
            f"T1 (chat_completions) returned no choices: {response!r}"
        )
    # P2 truncation guard (P0 finding 1, wire-byte confirmed): the router's
    # native_finish_reason is the truthful cap-hit signal — the top-level
    # finish_reason reads "tool_calls" even when the provider cut mid-argument.
    # Either field saying "length" marks the response truncated. The pydantic
    # extra field is absent on non-OpenRouter providers → None → no effect.
    _truncated = "length" in (
        getattr(choices[0], "finish_reason", None),
        getattr(choices[0], "native_finish_reason", None),
    )
    message = choices[0].message
    tool_calls = getattr(message, "tool_calls", None) or []
    if not tool_calls or not getattr(tool_calls[0], "function", None):
        if _truncated:
            raise T1TruncationError(
                f"T1 (chat_completions) forced-tool response cap-cut before any "
                f"tool call materialized (max_tokens too low): {message!r}"
            )
        raise ValueError(
            f"T1 (chat_completions) returned no tool_calls; forced tool_choice "
            f"failed — the model may not support function calling: {message!r}"
        )
    fn = tool_calls[0].function
    if fn.name != tool_name:
        raise ValueError(
            f"T1 (chat_completions) returned tool {fn.name!r}, expected "
            f"{tool_name!r}"
        )
    try:
        args = json.loads(fn.arguments)
    except json.JSONDecodeError:
        if _truncated:
            raise T1TruncationError(
                f"T1 (chat_completions) tool arguments cap-cut mid-JSON "
                f"({len(fn.arguments or '')} chars; unterminated) — retry at a "
                f"raised max_tokens"
            ) from None
        raise  # non-truncated malformed JSON: byte-equivalent to pre-P2
    if _truncated:
        # Parsed BUT cap-cut: a valid-but-shorter body is the silent damage
        # class P0 proved possible — fail loud, never return short data.
        raise T1TruncationError(
            f"T1 (chat_completions) response hit the output cap yet parsed "
            f"cleanly — refusing possibly-shortened tool args (tool "
            f"{tool_name!r})"
        )
    return args


def _extract_openai_text(response) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise ValueError(
            f"T1 (chat_completions) returned no choices: {response!r}"
        )
    content = choices[0].message.content
    if not content:
        raise ValueError(
            f"T1 (chat_completions) returned empty content: {choices[0].message!r}"
        )
    return content


# ── cost telemetry (replicated field read; Jidoka on missing cost) ──────


def _track_cost(usage, tier_config) -> None:
    """Accumulate T1 spend from the resolved tier's declared per-Mtok cost.

    When either ``cost_per_mtok_*`` is undeclared, surface one loud warning
    per process and skip accumulation — never silently default to zero. The
    call itself is unaffected (cost discipline, not a hard block).
    """
    global _cumulative_cost_usd, _missing_cost_warned
    if usage is None:
        return
    # Field-tolerant: Anthropic usage exposes input_tokens/output_tokens;
    # OpenAI-compatible usage exposes prompt_tokens/completion_tokens. Read the
    # Anthropic names first, fall back to the OpenAI names — so the same tracker
    # accounts spend on either transport instead of silently counting zero.
    input_tokens = getattr(usage, "input_tokens", None)
    if input_tokens is None:
        input_tokens = getattr(usage, "prompt_tokens", 0)
    output_tokens = getattr(usage, "output_tokens", None)
    if output_tokens is None:
        output_tokens = getattr(usage, "completion_tokens", 0)
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0

    cost_in = getattr(tier_config, "cost_per_mtok_input", None)
    cost_out = getattr(tier_config, "cost_per_mtok_output", None)
    if cost_in is None or cost_out is None:
        if not _missing_cost_warned:
            _missing_cost_warned = True
            logger.warning(
                "[t1_call] tier %r declares no cost_per_mtok_input/output in "
                "routing.config.yaml; skipping spend accumulation for this "
                "process. Calls continue. Declare the values to restore "
                "cost tracking.",
                getattr(tier_config, "tier", "?"),
            )
        return

    _cumulative_cost_usd += (
        input_tokens / 1_000_000 * float(cost_in)
        + output_tokens / 1_000_000 * float(cost_out)
    )


def cumulative_cost_usd() -> float:
    """T1 spend accumulated this process (USD)."""
    return _cumulative_cost_usd

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

* ``tool`` given — a forced ``tool_use`` call (``tool_choice`` pins that tool),
  returning the validated structured ``input`` dict. Use for the Evaluator's
  verdict.
* ``tool`` absent — a plain-text completion, returning the concatenated text.
  Use for the Writer / Editor prose.

Fail loud (Digital Jidoka): a non-anthropic_messages tier, a missing tool_use
block, or an empty text response each raise — never a silent default. Cost is
tracked by replicating the ``cost_per_mtok_*`` field read from the resolved
``TierConfig`` (no import of classify's private spend tracker); an undeclared
cost surfaces one loud warning per process and skips accumulation rather than
defaulting to zero.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)

# The tier this primitive targets, by name (routing.config.yaml key).
_T1_TIER = "T1"

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
) -> Union[str, Dict[str, Any]]:
    """Make one T1 call and return its result.

    ``tool`` present → forced ``tool_use``; returns the tool's structured
    ``input`` dict. ``tool`` absent → plain-text completion; returns the
    concatenated text. Raises loudly on a malformed response or a
    non-anthropic_messages tier.
    """
    from agent.anthropic_adapter import build_anthropic_client

    runtime, tier_config = _resolve_t1_runtime()
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


# ── tier resolution (public API, by name) ──────────────────────────────


def _resolve_t1_runtime():
    """Resolve the T1 ``(runtime, tier_config)`` via the public router API.

    A1: no import of the private ``_telemetry_tier_runtime``. A fresh CLI
    process may not have initialized the module router yet — initialize it
    via the public :func:`grove.router.initialize` and retry once. Asserts
    the resolved tier speaks anthropic_messages; raises otherwise.
    """
    from grove import router as grove_router
    from grove.providers import resolve_tier_to_runtime

    try:
        tier_config = grove_router.get_tier_config(_T1_TIER)
    except RuntimeError:
        # Router not initialized (fresh process). Initialize and retry — a
        # required dependency made ready, not an error swallowed.
        grove_router.initialize()
        tier_config = grove_router.get_tier_config(_T1_TIER)

    runtime = resolve_tier_to_runtime(tier_config)
    if runtime.get("api_mode") != "anthropic_messages":
        raise RuntimeError(
            f"T1 tier resolves api_mode {runtime.get('api_mode')!r}; "
            f"the wiki pipeline requires an Anthropic-native (anthropic_messages) "
            f"tier. Rebind T1 in routing.config.yaml."
        )
    return runtime, tier_config


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
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0

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

"""Oneshot (-z) mode: send a prompt, get the final content block, exit.

Bypasses cli.py entirely.  No banner, no spinner, no session_id line.
Just the agent's final text to stdout; a one-line tier + cost summary
goes to stderr, so stdout stays pure for piping.

Toolsets = explicit --toolsets when provided, otherwise whatever the user has
configured for "cli" in `hermes tools`.
Rules / memory / AGENTS.md / preloaded skills = same as a normal chat turn.
Approvals = auto-bypassed (GROVE_YOLO_MODE=1 is set for the call).
Working directory = the user's CWD (AGENTS.md etc. resolve from there as usual).

Model / provider selection mirrors `hermes chat`:
    - Both optional. If omitted, use the user's configured default.
    - If both given, pair them exactly as given.
    - If only --model given, auto-detect the provider that serves it.
    - If only --provider given, error out (ambiguous — caller must pick a model).

Env var fallbacks (used when the corresponding arg is not passed):
    - GROVE_INFERENCE_MODEL
    - GROVE_INFERENCE_PROVIDER  (already read by resolve_runtime_provider)
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from typing import Optional

from grove.dispatcher import Dispatcher



# Sprint 53 — ad-hoc Dispatcher-style ToolRegistry built once per
# process for read-only CLI introspection paths.
_CLI_REGISTRY = None
def _cli_registry():
    global _CLI_REGISTRY
    if _CLI_REGISTRY is None:
        from tools.registry import ToolRegistry, register_builtin_tools
        _CLI_REGISTRY = ToolRegistry()
        register_builtin_tools(_CLI_REGISTRY)
        try:
            from hermes_cli.plugins import discover_plugins as _dp
            _dp(registry=_CLI_REGISTRY)
        except Exception:
            pass
    return _CLI_REGISTRY

class ModelConfigError(RuntimeError):
    """No model could be resolved for a oneshot (-z) run — not from --model,
    GROVE_INFERENCE_MODEL, config, or the Cognitive Router. Fail loud: the
    agent must not run with an empty model string (PL-1)."""


# Human-readable model names for the oneshot tier/cost summary. Mirrors
# cli.py's _MODEL_DISPLAY_NAMES — oneshot deliberately does not import the
# 14k-line cli module (the -z fast path must stay cheap). Unmapped models
# display as their raw API string.
_MODEL_DISPLAY_NAMES = {
    "claude-haiku-4-5-20251001": "Haiku",
    "claude-sonnet-4-6": "Sonnet",
    "claude-opus-4-6": "Opus",
    "gemma4": "Gemma 4",
}


def _tier_cost_summary(routed, agent) -> Optional[str]:
    """Build the one-line tier + cost summary for oneshot's stderr.

    Returns None when there is no routing decision (a vanilla install with
    no routing config) — nothing to report. The summary goes to stderr so
    stdout stays the pure, pipeable response.
    """
    if routed is None:
        return None
    model = routed.tier_config.model
    provider = (routed.tier_config.provider or "").strip().lower()
    name = _MODEL_DISPLAY_NAMES.get(model, model) or model
    in_tok = getattr(agent, "session_input_tokens", 0) or 0
    out_tok = getattr(agent, "session_output_tokens", 0) or 0
    total = in_tok + out_tok
    if provider in ("ollama", "mlx"):
        cost_str = "local ($0)"
    else:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
        cost = estimate_usage_cost(
            model,
            CanonicalUsage(input_tokens=in_tok, output_tokens=out_tok),
            provider=provider or None,
        )
        cost_str = cost.label or "n/a"
    return f"  ↳ {routed.tier} {name} · {total:,} tokens · {cost_str}"


def _normalize_toolsets(toolsets: object = None) -> list[str] | None:
    if not toolsets:
        return None

    raw_items = [toolsets] if isinstance(toolsets, str) else toolsets
    if not isinstance(raw_items, (list, tuple)):
        raw_items = [raw_items]

    normalized: list[str] = []
    for item in raw_items:
        if isinstance(item, str):
            normalized.extend(part.strip() for part in item.split(","))
        else:
            normalized.append(str(item).strip())

    return [item for item in normalized if item] or None


def _validate_explicit_toolsets(toolsets: object = None) -> tuple[list[str] | None, str | None]:
    normalized = _normalize_toolsets(toolsets)
    if normalized is None:
        return None, None

    try:
        from toolsets import validate_toolset
    except Exception as exc:
        return None, f"autonomaton -z: failed to validate --toolsets: {exc}\n"

    built_in = [name for name in normalized if validate_toolset(name, _cli_registry())]
    unresolved = [name for name in normalized if name not in built_in]

    if unresolved:
        try:
            from hermes_cli.plugins import discover_plugins

            discover_plugins()
            plugin_valid = [name for name in unresolved if validate_toolset(name, _cli_registry())]
        except Exception:
            plugin_valid = []

        if plugin_valid:
            built_in.extend(plugin_valid)
            unresolved = [name for name in unresolved if name not in plugin_valid]

    if any(name in {"all", "*"} for name in built_in):
        ignored = [name for name in normalized if name not in {"all", "*"}]
        if ignored:
            sys.stderr.write(
                "autonomaton -z: --toolsets all enables every toolset; "
                f"ignoring additional entries: {', '.join(ignored)}\n"
            )
        return None, None

    mcp_names: set[str] = set()
    mcp_disabled: set[str] = set()
    if unresolved:
        try:
            from hermes_cli.config import read_raw_config
            from hermes_cli.tools_config import _parse_enabled_flag

            cfg = read_raw_config()
            mcp_servers = cfg.get("mcp_servers") if isinstance(cfg.get("mcp_servers"), dict) else {}
            for name, server_cfg in mcp_servers.items():
                if not isinstance(server_cfg, dict):
                    continue
                if _parse_enabled_flag(server_cfg.get("enabled", True), default=True):
                    mcp_names.add(str(name))
                else:
                    mcp_disabled.add(str(name))
        except Exception:
            mcp_names = set()
            mcp_disabled = set()

    mcp_valid = [name for name in unresolved if name in mcp_names]
    disabled = [name for name in unresolved if name in mcp_disabled]
    unknown = [name for name in unresolved if name not in mcp_names and name not in mcp_disabled]
    valid = built_in + mcp_valid

    if unknown:
        sys.stderr.write(f"autonomaton -z: ignoring unknown --toolsets entries: {', '.join(unknown)}\n")
    if disabled:
        sys.stderr.write(
            "autonomaton -z: ignoring disabled MCP servers (set enabled: true in config.yaml to use): "
            f"{', '.join(disabled)}\n"
        )

    if not valid:
        return None, "autonomaton -z: --toolsets did not contain any valid toolsets.\n"

    return valid, None


def run_oneshot(
    prompt: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    toolsets: object = None,
) -> int:
    """Execute a single prompt and print only the final content block.

    Args:
        prompt: The user message to send.
        model: Optional model override. Falls back to GROVE_INFERENCE_MODEL
            env var, then config.yaml's model.default / model.model.
        provider: Optional provider override. Falls back to
            GROVE_INFERENCE_PROVIDER env var, then config.yaml's model.provider,
            then "auto".
        toolsets: Optional comma-separated string or iterable of toolsets.

    Returns the exit code.  Caller should sys.exit() with the return.
    """
    # Silence every stdlib logger for the duration.  AIAgent, tools, and
    # provider adapters all log to stderr through the root logger; file
    # handlers added by setup_logging() keep working (they're attached to
    # the root logger's handler list, not affected by level), but no
    # bytes reach the terminal.
    logging.disable(logging.CRITICAL)

    # --provider without --model is ambiguous: carrying the user's configured
    # model across to a different provider is usually wrong (that provider may
    # not host it), and silently picking the provider's catalog default hides
    # the mismatch.  Require the caller to be explicit.  Validate BEFORE the
    # stderr redirect so the message actually reaches the terminal.
    env_model_early = os.getenv("GROVE_INFERENCE_MODEL", "").strip()
    if provider and not ((model or "").strip() or env_model_early):
        sys.stderr.write(
            "autonomaton -z: --provider requires --model (or GROVE_INFERENCE_MODEL). "
            "Pass both explicitly, or neither to use your configured defaults.\n"
        )
        return 2

    explicit_toolsets, toolsets_error = _validate_explicit_toolsets(toolsets)
    if toolsets_error:
        sys.stderr.write(toolsets_error)
        return 2
    use_config_toolsets = _normalize_toolsets(toolsets) is None

    # Auto-approve any shell / tool approvals.  Non-interactive by
    # definition — a prompt would hang forever.
    os.environ["GROVE_YOLO_MODE"] = "1"
    os.environ["GROVE_ACCEPT_HOOKS"] = "1"

    # Redirect stderr AND stdout to devnull for the entire call tree.
    # We'll print the final response to the real stdout at the end.
    real_stdout = sys.stdout
    real_stderr = sys.stderr
    devnull = open(os.devnull, "w", encoding="utf-8")

    try:
        with redirect_stdout(devnull), redirect_stderr(devnull):
            response, tier_summary = _run_agent(
                prompt,
                model=model,
                provider=provider,
                toolsets=explicit_toolsets,
                use_config_toolsets=use_config_toolsets,
            )
    except ModelConfigError as exc:
        # PL-1: a fatal config error must survive the devnull redirect.
        # The redirect context managers restore the real streams as the
        # exception unwinds; write the message plainly and exit non-zero
        # instead of returning 0 with no output.
        real_stderr.write(f"autonomaton -z: {exc}\n")
        return 2
    finally:
        try:
            devnull.close()
        except Exception:
            pass

    if response:
        real_stdout.write(response)
        if not response.endswith("\n"):
            real_stdout.write("\n")
        real_stdout.flush()
    # Tier + cost to stderr (Option A): stdout stays the pure, pipeable
    # response; the routing summary is informational, on the operator's
    # terminal. Plain text — oneshot's stderr style carries no ANSI.
    if tier_summary:
        real_stderr.write(tier_summary + "\n")
        real_stderr.flush()
    return 0


def _create_session_db_for_oneshot():
    """Best-effort SessionDB for ``hermes -z`` / oneshot mode.

    Oneshot bypasses ``HermesCLI._init_agent()``, so it must wire the SQLite
    session store itself. Without this, the ``session_search``/recall tool is
    advertised but every call returns "Session database not available.".
    """
    try:
        from hermes_state import SessionDB

        return SessionDB()
    except Exception as exc:
        logging.debug("SQLite session store not available for oneshot mode: %s", exc)
        return None


def _run_agent(
    prompt: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    toolsets: object = None,
    use_config_toolsets: bool = True,
) -> tuple[str, Optional[str]]:
    """Build an AIAgent exactly like a normal CLI chat turn would, then
    run a single conversation.

    Returns ``(response, tier_summary)`` — the final response string and a
    one-line tier + cost summary for stderr (None on a vanilla install with
    no routing config). stdout carries only the response."""
    # Imports are local so they don't run when hermes is invoked for
    # other commands (keeps top-level CLI startup cheap).
    from hermes_cli.config import load_config
    from hermes_cli.models import detect_provider_for_model
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_cli.tools_config import _get_platform_tools
    from run_agent import AIAgent
    from grove.providers import route_for_agent

    cfg = load_config()

    # Resolve effective model: explicit arg → env var → config.
    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, str):
        cfg_model = model_cfg
    else:
        cfg_model = model_cfg.get("default") or model_cfg.get("model") or ""

    env_model = os.getenv("GROVE_INFERENCE_MODEL", "").strip()
    effective_model = (model or "").strip() or env_model or cfg_model

    # Resolve effective provider: explicit arg → (auto-detect from model if
    # model was explicit) → env / config (handled inside resolve_runtime_provider).
    #
    # When --model is given without --provider, auto-detect the provider that
    # serves that model — same semantic as `/model <name>` in an interactive
    # session.  Without this, resolve_runtime_provider() would fall back to
    # the user's configured default provider, which may not host the model
    # the caller just asked for.
    effective_provider = (provider or "").strip() or None
    explicit_base_url_from_alias: Optional[str] = None
    if effective_provider is None and (model or env_model):
        # Only auto-detect when the model was explicitly requested via arg or
        # env var (not when it came from config — that's the "use my defaults"
        # path and the configured provider is already correct).
        explicit_model = (model or "").strip() or env_model
        if explicit_model:
            # First check DIRECT_ALIASES populated from config.yaml `model_aliases:`.
            # These map a user-defined alias to (model, provider, base_url) for
            # endpoints not in any catalog (local servers, custom proxies, etc.).
            try:
                from hermes_cli import model_switch as _ms
                _ms._ensure_direct_aliases()
                direct = _ms.DIRECT_ALIASES.get(explicit_model.strip().lower())
            except Exception:
                direct = None
            if direct is not None:
                effective_model = direct.model
                effective_provider = direct.provider
                if direct.base_url:
                    explicit_base_url_from_alias = direct.base_url.rstrip("/")
            else:
                cfg_provider = ""
                if isinstance(model_cfg, dict):
                    cfg_provider = str(model_cfg.get("provider") or "").strip().lower()
                current_provider = (
                    cfg_provider
                    or os.getenv("GROVE_INFERENCE_PROVIDER", "").strip().lower()
                    or "auto"
                )
                detected = detect_provider_for_model(explicit_model, current_provider)
                if detected:
                    effective_provider, effective_model = detected

    # Sprint 49 — T0 Pattern Cache short-circuit. If the message resolves to
    # an active compiled pattern, the classifier (an LLM call) MUST NOT fire —
    # that is the whole point of T0. dispatch_turn serves the cached response
    # model-free. We still route with ``classify=False`` so the agent SHELL
    # gets a non-empty default-tier model (router-only installs have no legacy
    # default), WITHOUT paying for or logging a classification.
    from grove.pattern_cache import pattern_cache_enabled, PatternCacheStore
    _t0_hit = bool(
        pattern_cache_enabled()
        and PatternCacheStore().get_active_for_message(prompt) is not None
    )

    # Cognitive Router: --model feeds INTO the router, which resolves it
    # to a tier. The router runs whenever routing.config.yaml is present;
    # the legacy chain above is the vanilla-install fallback.
    _routed = route_for_agent(
        message=prompt, explicit_model=model, classify=not _t0_hit,
    )
    if _routed is not None:
        effective_model = _routed.tier_config.model
        effective_provider = _routed.tier_config.provider
        explicit_base_url_from_alias = None

    # PL-1: fail loud on an empty model. If none of --model, the env var,
    # config, or the Cognitive Router resolved a model, the agent would run
    # with model="" and fail deep inside the provider call — where the
    # oneshot devnull redirect swallows the error and run_oneshot returns 0
    # with no output. Raise here, before the agent runs: the symmetric
    # partner of the AuthError raised for a missing API key.
    if not (effective_model or "").strip():
        raise ModelConfigError(
            "No model configured. Run `autonomaton model` to set one, "
            "or pass --model."
        )

    runtime = resolve_runtime_provider(
        requested=effective_provider,
        target_model=effective_model or None,
        explicit_base_url=explicit_base_url_from_alias,
    )

    # Pull in explicit toolsets when provided; otherwise use whatever the user
    # has enabled for "cli". sorted() gives stable ordering for config-derived
    # sets; explicit values preserve user order.
    toolsets_list = _normalize_toolsets(toolsets)
    if toolsets_list is None and use_config_toolsets:
        toolsets_list = sorted(_get_platform_tools(cfg, "cli"))

    session_db = _create_session_db_for_oneshot()

    # Cognitive Router RAG (Sprint 13): retrieve cellar context for this
    # one-shot request. ephemeral_system_prompt is injected at API-call
    # time only — never cached, never saved to trajectories.
    from grove.cellar import retrieve_cellar_context
    _cellar_context = retrieve_cellar_context(prompt)

    agent = Dispatcher(session_db=session_db, agent_kwargs=dict(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        model=effective_model,
        enabled_toolsets=toolsets_list,
        quiet_mode=True,
        platform="cli",
        credential_pool=runtime.get("credential_pool"),
        # Interactive callbacks are intentionally NOT wired beyond this
        # one.  In oneshot mode there's no user sitting at a terminal:
        #   - clarify  → returns a synthetic "pick a default" instruction
        #                so the agent continues instead of stalling on
        #                the tool's built-in "not available" error
        #   - sudo password prompt → terminal_tool gates on
        #                GROVE_INTERACTIVE which we never set
        #   - shell-hook approval → auto-approved via GROVE_ACCEPT_HOOKS=1
        #                (set above); also falls back to deny on non-tty
        #   - dangerous-command approval → bypassed via GROVE_YOLO_MODE=1
        #   - skill secret capture → returns gracefully when no callback set
        clarify_callback=_oneshot_clarify_callback,
        ephemeral_system_prompt=_cellar_context or None,
    )).agent

    # Belt-and-braces: make sure AIAgent doesn't invoke any streaming
    # display callbacks that would bypass our stdout capture.
    agent.suppress_status_output = True
    agent.stream_delta_callback = None
    agent.tool_gen_callback = None

    # _run_agent has already called route_for_agent above (the _routed
    # variable) and constructed AIAgent with the routed runtime. Skip
    # run_conversation's self-route so T-telemetry fires exactly once.
    response = agent.chat(prompt, already_routed=True) or ""
    # On a T0 hit no inference happened — suppress the tier/cost footer so the
    # stderr summary reflects reality (a cache hit, not a tier-N model turn).
    summary = None if _t0_hit else _tier_cost_summary(_routed, agent)
    return response, summary


def _oneshot_clarify_callback(question: str, choices=None) -> str:
    """Clarify is disabled in oneshot mode — tell the agent to pick a
    default and proceed instead of stalling or erroring."""
    if choices:
        return (
            f"[oneshot mode: no user available. Pick the best option from "
            f"{choices} using your own judgment and continue.]"
        )
    return (
        "[oneshot mode: no user available. Make the most reasonable "
        "assumption you can and continue.]"
    )

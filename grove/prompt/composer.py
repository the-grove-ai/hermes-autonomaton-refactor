"""GRV-007 Declarative Prompt Composition — composer + v0.1 providers.

Sprint 36 — extracts ``AIAgent._build_system_prompt_parts`` (run_agent.py)
into a declarative composer with one named provider per section. The
Dispatcher constructs the composer, calls ``compose(**context)`` with
the per-turn + per-Agent state, and injects the resulting
``ComposedPrompt`` into the Agent.

Per GRV-007 § III, providers are pure functions of the ``context``
dict — no Agent-instance reach-back. Per § VI, the composer does
NOT cache across turns; per-turn composition is sub-50ms.

The 17 v0.1 section providers below mirror the pre-Sprint-36
``_build_system_prompt_parts`` output byte-for-byte. The regression
test at ``tests/grove/test_composer_regression.py`` is the source of
truth for that property.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── Public dataclasses ────────────────────────────────────────────────


@dataclass(frozen=True)
class SectionResult:
    """What a provider returns from ``render(context)``.

    Empty / whitespace-only ``text`` is treated as a skip — the
    composer drops the section before joining tiers.
    """

    label: str
    text: str


SectionProvider = Callable[[Dict[str, Any]], Optional[SectionResult]]


@dataclass(frozen=True)
class SectionRegistration:
    """One section's declarative registration.

    Tier order is fixed (``stable → context → volatile``); ``order``
    sequences sections within a tier. ``enabled=False`` from config
    disables the section globally; the provider is never called.
    """

    name: str
    provider: SectionProvider
    order: int
    tier: str = "stable"
    enabled: bool = True


@dataclass(frozen=True)
class ComposedPrompt:
    """What ``compose()`` returns.

    * ``text`` — the joined system prompt the Agent receives.
    * ``sections`` — per-section content keyed by label
      (consumed by ``grove.context_report``).
    * ``tiers`` — three-tier split (``stable`` / ``context`` /
      ``volatile``) matching the pre-Sprint-36
      ``_build_system_prompt_parts`` return shape, kept for
      backward compatibility with consumers that expected the
      tiered dict.
    """

    text: str
    sections: Dict[str, str]
    tiers: Dict[str, str]


# ── The composer ──────────────────────────────────────────────────────


_TIER_ORDER: Tuple[str, ...] = ("stable", "context", "volatile")


class PromptComposer:
    """Declarative prompt composition (GRV-007).

    Sections register once at composer setup via
    ``register_section``; ``compose(**context)`` walks registered
    sections in (tier, order) sequence and joins the results.

    Multiple concurrent ``compose()`` calls on the same composer
    instance are safe (§ IX.3 reentrancy clause): the composer holds
    only registration state; per-call state lives on the local stack.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        # name → SectionRegistration. Re-registration overwrites
        # (deliberate: Sprint 37 can swap a default provider for a
        # contextual-preamble-aware one via a second register call).
        self._sections: Dict[str, SectionRegistration] = {}
        self._config: Dict[str, Any] = config or {}

    def register_section(
        self,
        name: str,
        provider: SectionProvider,
        *,
        order: int,
        tier: str = "stable",
    ) -> None:
        """Register ``provider`` under ``name`` at the given tier+order.

        The config layer (``prompt.config.yaml`` section entry's
        ``enabled`` flag) is consulted at ``compose()`` time. The
        ``order`` argument is the default; config can override it via
        a per-section ``order`` entry.
        """
        if tier not in _TIER_ORDER:
            raise ValueError(
                f"PromptComposer.register_section: unknown tier {tier!r}; "
                f"expected one of {_TIER_ORDER}"
            )
        cfg = self._section_config(name)
        self._sections[name] = SectionRegistration(
            name=name,
            provider=provider,
            order=int(cfg.get("order", order)) if cfg else order,
            tier=str(cfg.get("tier", tier)) if cfg else tier,
            enabled=bool(cfg.get("enabled", True)) if cfg else True,
        )

    def compose(self, **context: Any) -> ComposedPrompt:
        """Build the system prompt from registered, enabled sections.

        ``context`` carries the turn-specific and per-Agent state
        providers need (see ``build_default_composer``'s providers for
        the field set). Providers MUST NOT reach back into the Agent
        instance — all state flows through this dict.
        """
        per_tier: Dict[str, List[Tuple[int, str, str]]] = {
            t: [] for t in _TIER_ORDER
        }
        for reg in self._sections.values():
            if not reg.enabled:
                continue
            try:
                result = reg.provider(context)
            except Exception:
                # Provider failure must not crash the turn. Sprint 36
                # discipline: degrade by dropping the section, do not
                # raise. Operator sees a missing section, not a
                # broken turn.
                continue
            if result is None:
                continue
            if not result.text or not result.text.strip():
                continue
            per_tier[reg.tier].append(
                (reg.order, result.label, result.text.strip())
            )

        tiers: Dict[str, str] = {}
        sections: Dict[str, str] = {}
        joined_tiers: List[str] = []
        for tier in _TIER_ORDER:
            parts = sorted(per_tier[tier], key=lambda x: x[0])
            tier_text = "\n\n".join(text for _, _, text in parts if text)
            tiers[tier] = tier_text
            for _, label, text in parts:
                sections[label] = text
            if tier_text:
                joined_tiers.append(tier_text)
        full_text = "\n\n".join(joined_tiers)
        return ComposedPrompt(text=full_text, sections=sections, tiers=tiers)

    def _section_config(self, name: str) -> Dict[str, Any]:
        """Return the ``prompt.sections.<name>`` config block, or {}."""
        sections_cfg = self._config.get("sections")
        if not isinstance(sections_cfg, dict):
            return {}
        entry = sections_cfg.get(name)
        if not isinstance(entry, dict):
            return {}
        return entry


# ── v0.1 section providers (mirror _build_system_prompt_parts) ────────
#
# Each provider takes the ``context`` dict and returns a
# ``SectionResult`` or ``None``. Providers are pure functions of the
# context — they do not import from the Agent module.


def _identity_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    """Sprint 07 identity composition (constitution → soul → operator →
    goals). Falls back to ``DEFAULT_AGENT_IDENTITY`` in batch /
    trajectory mode (``skip_context_files=True`` AND
    ``load_soul_identity=False``).
    """
    from agent.prompt_builder import DEFAULT_AGENT_IDENTITY
    load_soul_identity = bool(ctx.get("load_soul_identity", False))
    skip_context_files = bool(ctx.get("skip_context_files", False))
    if load_soul_identity or not skip_context_files:
        from grove.identity import load_identity
        composed = load_identity(
            session_register=ctx.get("session_register"),
        ).compose_stable()
        if composed:
            return SectionResult(label="identity", text=composed)
    return SectionResult(label="identity", text=DEFAULT_AGENT_IDENTITY)


def _grove_agent_help_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    from agent.prompt_builder import GROVE_AGENT_HELP_GUIDANCE
    return SectionResult(label="grove_agent_help", text=GROVE_AGENT_HELP_GUIDANCE)


def _tool_affordances_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    """Sprint 53 — turn-0 capability summary.

    Emits a names+one-line-descriptions list of every tool currently in
    the Dispatcher's registry, gated by the agent's ``valid_tool_names``.
    The agent reads this at turn 0 and learns what it has — addressing
    the Sprint 54 confabulation gap where the model invents tool calls
    for capabilities it doesn't actually possess.

    Per the Sprint 53 architectural-review addendum, this preamble is
    self-awareness only: names + first line of each description. Full
    JSON schemas flow separately through the API tool-list channel
    (``get_authorized_tools()``) — duplicating them here would balloon
    the system prompt without adding information.
    """
    valid = ctx.get("valid_tool_names") or set()
    registry = ctx.get("registry")
    if registry is None or not valid:
        return None

    lines: List[Tuple[str, str, str]] = []  # (toolset, name, one_line_desc)
    for name in sorted(valid):
        entry = registry.get_entry(name)
        if entry is None:
            continue
        raw = (entry.description or "").strip()
        if not raw:
            continue
        # First sentence-ish: stop at newline or 160 chars, whichever first.
        first_line = raw.splitlines()[0].strip()
        if len(first_line) > 160:
            first_line = first_line[:157].rstrip() + "..."
        lines.append((entry.toolset or "", name, first_line))

    if not lines:
        return None

    body = "\n".join(f"- {name}: {desc}" for _, name, desc in lines)
    text = (
        "## Available tools (turn-0 affordances)\n"
        "\n"
        "You have access to the tools listed below. Full JSON schemas are "
        "delivered separately by the API; this list is your self-awareness "
        "summary so you can decline tasks that require tools NOT in this "
        "list rather than confabulating a tool call.\n"
        "\n"
        f"{body}"
    )
    return SectionResult(label="tool_affordances", text=text)


def _tool_guidance_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    """Joined tool guidance for memory / session_search / skill_manage /
    escalate / kanban tools, gated per-tool on the live valid_tool_names.
    """
    from agent.prompt_builder import (
        MEMORY_GUIDANCE,
        SESSION_SEARCH_GUIDANCE,
        SKILLS_GUIDANCE,
        ESCALATION_GUIDANCE,
        KANBAN_GUIDANCE,
    )
    valid = ctx.get("valid_tool_names") or set()
    parts: List[str] = []
    if "memory" in valid:
        parts.append(MEMORY_GUIDANCE)
    if "session_search" in valid:
        parts.append(SESSION_SEARCH_GUIDANCE)
    if "skill_manage" in valid:
        parts.append(SKILLS_GUIDANCE)
    if "escalate" in valid:
        parts.append(ESCALATION_GUIDANCE)
    if "kanban_show" in valid:
        parts.append(KANBAN_GUIDANCE)
    if not parts:
        return None
    return SectionResult(label="tool_guidance", text=" ".join(parts))


def _computer_use_guidance_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    valid = ctx.get("valid_tool_names") or set()
    if "computer_use" not in valid:
        return None
    from agent.prompt_builder import COMPUTER_USE_GUIDANCE
    return SectionResult(label="computer_use_guidance", text=COMPUTER_USE_GUIDANCE)


def _nous_subscription_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    from agent.prompt_builder import build_nous_subscription_prompt
    valid = ctx.get("valid_tool_names") or set()
    text = build_nous_subscription_prompt(valid)
    if not text:
        return None
    return SectionResult(label="nous_subscription", text=text)


def _tool_use_enforcement_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    from agent.prompt_builder import (
        TOOL_USE_ENFORCEMENT_GUIDANCE,
        TOOL_USE_ENFORCEMENT_MODELS,
    )
    valid = ctx.get("valid_tool_names") or set()
    if not valid:
        return None
    enforce = ctx.get("tool_use_enforcement")
    model_lower = (ctx.get("model") or "").lower()
    inject = False
    if enforce is True or (
        isinstance(enforce, str) and enforce.lower() in {"true", "always", "yes", "on"}
    ):
        inject = True
    elif enforce is False or (
        isinstance(enforce, str) and enforce.lower() in {"false", "never", "no", "off"}
    ):
        inject = False
    elif isinstance(enforce, list):
        inject = any(
            p.lower() in model_lower for p in enforce if isinstance(p, str)
        )
    else:
        inject = any(p in model_lower for p in TOOL_USE_ENFORCEMENT_MODELS)
    if not inject:
        return None
    return SectionResult(
        label="tool_use_enforcement", text=TOOL_USE_ENFORCEMENT_GUIDANCE,
    )


def _model_operational_guidance_provider(
    ctx: Dict[str, Any],
) -> Optional[SectionResult]:
    """Google or OpenAI model-family operational guidance, gated on
    model-name substring AND tool_use_enforcement having injected.
    Mirrors the pre-Sprint-36 nested gate.
    """
    from agent.prompt_builder import (
        TOOL_USE_ENFORCEMENT_MODELS,
        GOOGLE_MODEL_OPERATIONAL_GUIDANCE,
        OPENAI_MODEL_EXECUTION_GUIDANCE,
    )
    valid = ctx.get("valid_tool_names") or set()
    if not valid:
        return None
    enforce = ctx.get("tool_use_enforcement")
    model_lower = (ctx.get("model") or "").lower()
    inject = False
    if enforce is True or (
        isinstance(enforce, str) and enforce.lower() in {"true", "always", "yes", "on"}
    ):
        inject = True
    elif enforce is False or (
        isinstance(enforce, str) and enforce.lower() in {"false", "never", "no", "off"}
    ):
        inject = False
    elif isinstance(enforce, list):
        inject = any(
            p.lower() in model_lower for p in enforce if isinstance(p, str)
        )
    else:
        inject = any(p in model_lower for p in TOOL_USE_ENFORCEMENT_MODELS)
    if not inject:
        return None
    if "gemini" in model_lower or "gemma" in model_lower:
        return SectionResult(
            label="model_operational_guidance",
            text=GOOGLE_MODEL_OPERATIONAL_GUIDANCE,
        )
    if "gpt" in model_lower or "codex" in model_lower:
        return SectionResult(
            label="model_operational_guidance",
            text=OPENAI_MODEL_EXECUTION_GUIDANCE,
        )
    return None


def _skills_index_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    valid = ctx.get("valid_tool_names") or set()
    has_skills_tools = any(
        name in valid for name in ("skills_list", "skill_view", "skill_manage")
    )
    if not has_skills_tools:
        return None
    from agent.prompt_builder import build_skills_system_prompt
    from model_tools import get_toolset_for_tool
    # Sprint 53 — composer providers receive the Dispatcher-owned
    # registry via the ctx dict. ``ctx["registry"]`` is populated by
    # Dispatcher._compose_and_inject_system_prompt.
    registry = ctx.get("registry")
    if registry is None:
        return None
    avail_toolsets = {
        toolset for toolset in (
            get_toolset_for_tool(registry, tool_name) for tool_name in valid
        )
        if toolset
    }
    text = build_skills_system_prompt(
        available_tools=valid,
        available_toolsets=avail_toolsets,
    )
    if not text:
        return None
    return SectionResult(label="skills_index", text=text)


def _alibaba_model_override_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    if ctx.get("provider") != "alibaba":
        return None
    model = ctx.get("model") or ""
    model_short = model.split("/")[-1] if "/" in model else model
    text = (
        f"You are powered by the model named {model_short}. "
        f"The exact model ID is {model}. "
        f"When asked what model you are, always answer based on this information, "
        f"not on any model name returned by the API."
    )
    return SectionResult(label="alibaba_model_override", text=text)


def _environment_hints_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    from agent.prompt_builder import build_environment_hints
    text = build_environment_hints()
    if not text:
        return None
    return SectionResult(label="environment_hints", text=text)


def _platform_hint_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    from agent.prompt_builder import PLATFORM_HINTS
    platform_key = (ctx.get("platform") or "").lower().strip()
    if not platform_key:
        return None
    if platform_key in PLATFORM_HINTS:
        return SectionResult(label="platform_hint", text=PLATFORM_HINTS[platform_key])
    try:
        from gateway.platform_registry import platform_registry
        entry = platform_registry.get(platform_key)
        if entry and entry.platform_hint:
            return SectionResult(label="platform_hint", text=entry.platform_hint)
    except Exception:
        pass
    return None


def _system_message_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    """Caller-supplied system_message (NOT ephemeral_system_prompt —
    that's injected at API-call time only, per the GATE-A scope)."""
    msg = ctx.get("system_message")
    if msg is None:
        return None
    return SectionResult(label="system_message", text=str(msg))


def _context_files_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    if ctx.get("skip_context_files"):
        return None
    from agent.prompt_builder import build_context_files_prompt
    terminal_cwd = ctx.get("terminal_cwd") or None
    text = build_context_files_prompt(
        cwd=terminal_cwd, skip_soul=bool(ctx.get("identity_loaded", False)),
    )
    if not text:
        return None
    return SectionResult(label="context_files", text=text)


def _memory_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    store = ctx.get("memory_store")
    if store is None or not ctx.get("memory_enabled"):
        return None
    text = store.format_for_system_prompt("memory")
    if not text:
        return None
    return SectionResult(label="memory", text=text)


def _user_profile_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    store = ctx.get("memory_store")
    if store is None or not ctx.get("user_profile_enabled"):
        return None
    text = store.format_for_system_prompt("user")
    if not text:
        return None
    return SectionResult(label="user_profile", text=text)


def _external_memory_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    manager = ctx.get("memory_manager")
    if manager is None:
        return None
    try:
        text = manager.build_system_prompt()
    except Exception:
        return None
    if not text:
        return None
    return SectionResult(label="external_memory", text=text)


def _timestamp_provider(ctx: Dict[str, Any]) -> Optional[SectionResult]:
    """Per-turn timestamp + session_id + model + provider line.

    Tests freeze the time via ``ctx['now_fn']`` (Patch 1's required
    determinism hook for the byte-for-byte regression test); the
    production path defaults to ``hermes_time.now``.
    """
    now_fn = ctx.get("now_fn")
    if now_fn is None:
        from hermes_time import now as _now
        now_fn = _now
    now = now_fn()
    line = f"Conversation started: {now.strftime('%A, %B %d, %Y %I:%M %p')}"
    if ctx.get("pass_session_id") and ctx.get("session_id"):
        line += f"\nSession ID: {ctx['session_id']}"
    if ctx.get("model"):
        line += f"\nModel: {ctx['model']}"
    if ctx.get("provider"):
        line += f"\nProvider: {ctx['provider']}"
    return SectionResult(label="timestamp", text=line)


# ── Default composer setup ────────────────────────────────────────────


# Default tier+order matches the pre-Sprint-36 hardcoded order in
# ``AIAgent._build_system_prompt_parts``. The composer accepts a
# config override per-section; this list is the in-code default the
# vanilla install uses when ``config/prompt.config.yaml`` is absent.
_DEFAULT_SECTIONS: Tuple[Tuple[str, SectionProvider, int, str], ...] = (
    # stable
    ("identity",                       _identity_provider,                       10, "stable"),
    ("grove_agent_help",               _grove_agent_help_provider,               20, "stable"),
    ("tool_affordances",               _tool_affordances_provider,               25, "stable"),
    ("tool_guidance",                  _tool_guidance_provider,                  30, "stable"),
    ("computer_use_guidance",          _computer_use_guidance_provider,          31, "stable"),
    ("nous_subscription",              _nous_subscription_provider,              35, "stable"),
    ("tool_use_enforcement",           _tool_use_enforcement_provider,           40, "stable"),
    ("model_operational_guidance",     _model_operational_guidance_provider,     41, "stable"),
    ("skills_index",                   _skills_index_provider,                   50, "stable"),
    ("alibaba_model_override",         _alibaba_model_override_provider,         55, "stable"),
    ("environment_hints",              _environment_hints_provider,              60, "stable"),
    ("platform_hint",                  _platform_hint_provider,                  70, "stable"),
    # context
    ("system_message",                 _system_message_provider,                 10, "context"),
    ("context_files",                  _context_files_provider,                  20, "context"),
    # volatile
    ("memory",                         _memory_provider,                         10, "volatile"),
    ("user_profile",                   _user_profile_provider,                   20, "volatile"),
    ("external_memory",                _external_memory_provider,                30, "volatile"),
    ("timestamp",                      _timestamp_provider,                      100, "volatile"),
)


def build_default_composer(
    config: Optional[Dict[str, Any]] = None,
) -> PromptComposer:
    """Construct a ``PromptComposer`` with the v0.1 section providers
    registered at their default tier+order.

    ``config`` is the ``prompt`` block from ``runtime_ctx.config`` (or
    a parsed ``prompt.config.yaml`` payload). Per-section ``enabled`` /
    ``order`` / ``tier`` entries override the in-code defaults.

    Sprint 37 adds ``contextual_preamble`` at ``tier="volatile",
    order=15`` per GRV-006 § II.
    """
    composer = PromptComposer(config=config)
    for name, provider, order, tier in _DEFAULT_SECTIONS:
        composer.register_section(name, provider, order=order, tier=tier)

    from grove.prompt.preamble import build_contextual_preamble_provider
    preamble_cfg: Dict[str, Any] = {}
    if config and isinstance(config.get("sections"), dict):
        section_entry = config["sections"].get("contextual_preamble")
        if isinstance(section_entry, dict):
            preamble_cfg = section_entry
    preamble_kwargs: Dict[str, Any] = {}
    if "top_k" in preamble_cfg:
        preamble_kwargs["top_k"] = preamble_cfg["top_k"]
    if "recency_decay" in preamble_cfg:
        preamble_kwargs["recency_decay"] = preamble_cfg["recency_decay"]
    if "outcome_filter" in preamble_cfg:
        preamble_kwargs["outcome_filter"] = preamble_cfg["outcome_filter"]
    composer.register_section(
        "contextual_preamble",
        build_contextual_preamble_provider(**preamble_kwargs),
        order=15,
        tier="volatile",
    )
    return composer

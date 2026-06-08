"""Grove Context Budget — Sprint 29 context-budget-optimization-v1.

Selective per-turn tool loading. Reads the Sprint 12 classification
output (intent_class, complexity_signal) and the tool-group taxonomy at
``config/tool_groups.yaml`` (or the operator copy at
``~/.grove/tool_groups.yaml``) to compute the set of tool names the Agent
exposes for the turn.

Sprint 24a measured per-turn tool schemas at 31,003 tokens — 63.6% of
turn-1 context. The Sprint 29 design targets:

* Simple / moderate intent → ~12-14K tokens (core + reads + small
  domain chunk); a 55-58% reduction from the all-loaded baseline.
* Complex / novel intent → ~18-22K tokens (the above + exploratory).
* Unknown intent → load ALL tools as the maximal fallback per the
  Architectural Prime Directive (loud, not silent — logged to the
  Kaizen Ledger by the Dispatcher).

This module is a pure-function surface: ``load_taxonomy`` reads + caches
the YAML; ``resolve_tool_set`` computes the per-turn allowed name set;
``filter_tools_by_name`` applies the set to a tool list. The Dispatcher
glues these to the Agent via the per-turn ``_tools_for_turn`` attribute
(Sprint 29 Phase 2 GATE-A Option X).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, FrozenSet, List, Optional, Set, Tuple

import yaml

if TYPE_CHECKING:
    from grove.tier_budget import TierBudget

logger = logging.getLogger(__name__)

# Mirrors grove.tier_budget.WILDCARD. Kept local so this lower-level Sprint 29
# module carries no runtime import dependency on the budget loader (which
# lazily imports load_taxonomy from here).
_WILDCARD = "*"

__all__ = [
    "CO_LOCATED_TOOLS",
    "ToolResolution",
    "load_taxonomy",
    "resolve_tool_set",
    "resolve_tools_for_tier",
    "filter_tools_by_name",
    "reset_taxonomy_cache",
]


_REQUIRED_TOP_LEVEL = frozenset({
    "version", "core", "domain_chunks", "exploratory",
})


# ── Co-location guard ────────────────────────────────────────────────
# Discovery tools and their execution vehicles MUST appear in the same
# resolved tool set. Loading discovery without execution is the
# silent-degradation antipattern that produced the skill_view → clarify
# freeze loop: the Agent could SEE skills but not RUN them, asked the
# operator a clarifying question, looked again, asked again, dead air.
#
# Each tuple is ``(discovery_tool, execution_tool)``. The guard fires
# AFTER ``resolve_tool_set`` computes the per-turn set: if the discovery
# tool is present and the execution tool is missing, RuntimeError. No
# fallback that silently loads the missing tool — the operator gets
# told exactly which pair broke for which intent, and where to fix the
# taxonomy. Per the Architectural Prime Directive: fail loud.
#
# v0.1 seed. Future pairs (e.g., MCP discovery + MCP execute) add one
# tuple here; the validator is automatic.
CO_LOCATED_TOOLS = (
    ("skill_view", "terminal"),
)


def _validate_co_location(
    selected: Set[str],
    intent_class: str,
) -> None:
    """Raise RuntimeError if any CO_LOCATED_TOOLS pair is half-loaded.

    Runs against the materialized per-turn set, never against the
    maximal-fallback (None) path — when every tool is loaded the
    invariant holds trivially. The Agent sees this at construction
    time; the Andon is the message, not a downstream hang.
    """
    for discovery, execution in CO_LOCATED_TOOLS:
        if discovery in selected and execution not in selected:
            raise RuntimeError(
                f"co-location invariant violated: discovery tool "
                f"{discovery!r} is in the resolved tool set for "
                f"intent_class={intent_class!r}, but its execution "
                f"vehicle {execution!r} is not. Add {execution!r} to "
                f"the ``core`` chunk in tool_groups.yaml (or to the "
                f"domain chunk for {intent_class!r}). Discovery without "
                f"execution is the silent-degradation antipattern that "
                f"freezes the Agent in a discover-clarify loop."
            )


# Module-level cache. Reset between tests via the conftest fixture.
_taxonomy_cache: Optional[dict] = None


def _resolve_taxonomy_path() -> Path:
    """Find the active taxonomy file.

    Resolution order matches the schema-loader pattern from Sprint 04:
    operator runtime copy at ``$GROVE_HOME/tool_groups.yaml`` first,
    then the repo template at ``config/tool_groups.yaml``.
    """
    from hermes_constants import get_hermes_home
    runtime = Path(get_hermes_home()) / "tool_groups.yaml"
    if runtime.exists():
        return runtime
    # Repo template — grove/ is one level under the repo root.
    return Path(__file__).resolve().parents[1] / "config" / "tool_groups.yaml"


def load_taxonomy(path: Optional[Path] = None) -> dict:
    """Load and validate the tool-group taxonomy.

    Cached after the first call when ``path`` is None. Tests pass an
    explicit path to bypass the cache. Schema violations raise
    ``ValueError`` — fail loud per the Architectural Prime Directive.

    Args:
        path: explicit YAML path. When None, resolves via
            :func:`_resolve_taxonomy_path` and caches the result.

    Returns:
        The parsed taxonomy dict with validated structure.

    Raises:
        ValueError: schema validation failed (missing keys, wrong types,
            unsupported version).
        FileNotFoundError: neither the runtime nor the repo template
            exists.
    """
    global _taxonomy_cache
    if path is None and _taxonomy_cache is not None:
        return _taxonomy_cache

    target = Path(path) if path is not None else _resolve_taxonomy_path()
    with target.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError(
            f"tool_groups.yaml at {target} is not a mapping (got "
            f"{type(raw).__name__})"
        )

    missing = _REQUIRED_TOP_LEVEL - set(raw.keys())
    if missing:
        raise ValueError(
            f"tool_groups.yaml at {target} missing required keys: "
            f"{sorted(missing)}"
        )

    if raw["version"] != 1:
        raise ValueError(
            f"tool_groups.yaml at {target} unsupported schema_version "
            f"{raw['version']!r} (expected 1)"
        )

    if not isinstance(raw["core"], list):
        raise ValueError(
            f"tool_groups.yaml at {target}: core must be a list"
        )
    if not isinstance(raw["domain_chunks"], dict):
        raise ValueError(
            f"tool_groups.yaml at {target}: domain_chunks must be a mapping"
        )
    for intent, tools in raw["domain_chunks"].items():
        if not isinstance(tools, list):
            raise ValueError(
                f"tool_groups.yaml at {target}: domain_chunks[{intent!r}] "
                f"must be a list"
            )
    if not isinstance(raw["exploratory"], list):
        raise ValueError(
            f"tool_groups.yaml at {target}: exploratory must be a list"
        )

    if path is None:
        _taxonomy_cache = raw
    return raw


def _resolve_intent_groups(
    intent_class: Optional[str],
    complexity_signal: Optional[str],
    taxonomy: dict,
) -> Optional[Set[str]]:
    """The tool-GROUP names an intent selects, or ``None`` for the maximal-
    fallback signal (unknown / missing intent).

    Single source of truth for the Sprint 29 group-selection rule: ``core``
    always, the matching ``domain_chunks`` group, plus ``exploratory`` for
    complex / novel turns. Both :func:`resolve_tool_set` (legacy, name-set
    return) and :func:`resolve_tools_for_tier` (tier-aware) resolve groups
    through this helper so the two paths can never drift (Phase 4 consolidates
    them onto one surface).
    """
    if intent_class is None or intent_class == "unknown":
        return None
    groups: Set[str] = {"core"}
    domain = taxonomy.get("domain_chunks") or {}
    if intent_class in domain:
        groups.add(intent_class)
    if complexity_signal in ("complex", "novel"):
        groups.add("exploratory")
    return groups


def _materialize(groups: Set[str], taxonomy: dict) -> Set[str]:
    """Expand group names to the union of their tool names per the taxonomy."""
    names: Set[str] = set()
    domain = taxonomy.get("domain_chunks") or {}
    for group in groups:
        if group == "core":
            names.update(taxonomy.get("core", []))
        elif group == "exploratory":
            names.update(taxonomy.get("exploratory", []))
        elif group in domain:
            names.update(domain[group])
    return names


def resolve_tool_set(
    intent_class: Optional[str],
    complexity_signal: Optional[str],
    taxonomy: dict,
) -> Optional[Set[str]]:
    """Compute the per-turn allowed tool-name set (Sprint 29, intent-only).

    Returns ``None`` when the intent is unknown or missing — the signal
    to the caller to load ALL tools. The Architectural Prime Directive
    says fail loud, not silent: callers MUST surface this fallback to
    the Kaizen Ledger so the operator can audit how often the
    classifier failed to give the optimizer enough to work with.

    This is the LEGACY (tier-unaware) surface — behavior is unchanged from
    Sprint 29. The tier-aware cap lives in :func:`resolve_tools_for_tier`
    (Sprint 73); this function and that one share :func:`_resolve_intent_groups`
    so neither can drift from the group-selection rule.

    Args:
        intent_class: one of the Sprint 12 INTENT_CLASSES, or
            ``"unknown"`` / ``None`` for the maximal-fallback signal.
        complexity_signal: one of the Sprint 12 COMPLEXITY_SIGNALS
            (``simple`` / ``moderate`` / ``complex`` / ``novel``).
            ``complex`` / ``novel`` add the exploratory group.
        taxonomy: the dict returned by :func:`load_taxonomy`.

    Returns:
        Set of tool names to expose this turn, or None for "load all".
    """
    groups = _resolve_intent_groups(intent_class, complexity_signal, taxonomy)
    if groups is None:
        logger.info(
            "[grove.context_budget] tool selection: maximal fallback "
            "(intent_class=%r) — full registry loaded",
            intent_class,
        )
        return None
    # MCP tools (mcp_*) are not gated here — the per-turn filter governs MCP
    # exposure (Sprint 73: per-tier exclude_mcp when a budget is threaded;
    # legacy: unconditional pass when none is).
    selected = _materialize(groups, taxonomy)
    _validate_co_location(selected, intent_class)
    return selected


def _name_of(tool: dict) -> Optional[str]:
    """OpenAI tool-dict name extraction (matches the legacy inline form)."""
    fn = tool.get("function")
    return fn.get("name") if isinstance(fn, dict) else None


def _is_mcp(name: Optional[str]) -> bool:
    return isinstance(name, str) and name.startswith("mcp_")


def _mcp_server_of(name: str) -> Optional[str]:
    """Extract the MCP server segment from a tool name, or ``None`` if the
    name cannot be parsed.

    Handles both registered forms: ``mcp_<server>_<tool>`` (e.g.
    ``mcp_notion_API_post_page``) and ``mcp__<server>__<tool>``. Crash-proof by
    contract — never raises. A ``None`` return means "unparseable"; the caller
    admits the tool by default AND records it (logged + surfaced in
    provenance), so an un-gateable MCP name is never a silent unbudgeted pass.
    """
    if not isinstance(name, str) or not name.startswith("mcp_"):
        return None
    rest = name[4:].lstrip("_")  # drop 'mcp_' and any extra '_' (mcp__server__)
    if not rest:
        return None
    for sep in ("__", "_"):
        if sep in rest:
            head = rest.split(sep, 1)[0]
            return head or None
    return rest or None


def _partition_tools(
    tools: List[dict],
    allowed: Optional[Set[str]],
    tier_budget: Optional["TierBudget"],
) -> Tuple[List[dict], Set[str], List[str]]:
    """Single source of truth for per-tool admission (D4).

    Returns ``(kept, excluded_mcp_servers, unparseable_mcp_names)``.

    * MCP tool (name starts ``mcp_``): admitted UNLESS ``tier_budget`` excludes
      its server (``"*"`` in ``exclude_mcp`` = all). An unparseable MCP name is
      admitted by default and recorded — never silently swallowed.
    * non-MCP tool: admitted when ``allowed is None`` (pass-through) or its name
      is in ``allowed``.

    With ``tier_budget=None`` the exclude set is empty, so every MCP passes —
    the legacy Sprint 29 behavior is the empty-exclude case of the rule, not a
    separate code path.
    """
    exclude: Set[str] = (
        set(tier_budget.tools.exclude_mcp) if tier_budget is not None else set()
    )
    exclude_all = _WILDCARD in exclude
    kept: List[dict] = []
    excluded_mcp: Set[str] = set()
    unparseable: List[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = _name_of(tool)
        if _is_mcp(name):
            server = _mcp_server_of(name)  # type: ignore[arg-type]
            if server is None:
                logger.warning(
                    "[grove.context_budget] MCP tool name %r is unparseable — "
                    "admitted by default (cannot tier-gate); surfaced in "
                    "provenance, not silently swallowed",
                    name,
                )
                unparseable.append(name)  # type: ignore[arg-type]
                kept.append(tool)
                continue
            if exclude_all or server in exclude:
                excluded_mcp.add(server)
                continue
            kept.append(tool)
            continue
        # non-MCP
        if allowed is None or (isinstance(name, str) and name in allowed):
            kept.append(tool)
    return kept, excluded_mcp, unparseable


def filter_tools_by_name(
    tools: List[dict],
    allowed: Optional[Set[str]],
    *,
    tier_budget: Optional["TierBudget"] = None,
) -> List[dict]:
    """Filter an OpenAI-format tools list by tool name, with optional per-tier
    MCP gating (Sprint 73, D4).

    Args:
        tools: list of tool dicts in the
            ``{"type": "function", "function": {"name": ..., ...}}`` shape.
        allowed: set of names the turn should expose, OR ``None`` for non-MCP
            pass-through (the maximal-fallback signal).
        tier_budget: when supplied, an MCP tool passes only if its server is
            NOT in the tier's ``exclude_mcp`` (``"*"`` = exclude all). When
            ``None`` (the legacy call shape) every MCP passes — byte-for-byte
            the pre-Sprint-73 behavior.

    Returns:
        The filtered list, preserving insertion order.
    """
    # Legacy fast-path: no tier and no name set returns the list verbatim
    # (including any non-dict entries) exactly as Sprint 29 did.
    if tier_budget is None and allowed is None:
        return tools
    kept, _, _ = _partition_tools(tools, allowed, tier_budget)
    return kept


@dataclass(frozen=True)
class ToolResolution:
    """The tier-aware per-turn tool resolution (Sprint 73, D4 / D8 / D10).

    The canonical surface Phase 4 wires the Dispatcher onto. Carries both the
    admitted tool list and the provenance the escalation net (D8) and the
    ``/context`` + ledger telemetry (D10) need:

    * ``tools`` — the filtered tool list the agent receives.
    * ``allowed_names`` — non-MCP names admitted by the group cap.
    * ``stripped_groups`` — intent groups the tier's ``allow_groups`` removed.
      Non-empty ⇒ the tier cannot serve the intent's full tool need; Phase 4
      escalates via the Sprint 30 contract (D8) — NEVER a silent strip.
    * ``excluded_mcp`` — MCP servers the tier excluded (e.g. ``notion``).
    * ``unparseable_mcp`` — MCP names that could not be parsed; admitted by
      default and surfaced here (logged, never silently swallowed).
    * ``fallback`` — the classifier yielded no usable intent; the budget was
      still honored (capped to ``allow_groups``), loudly.
    """

    tools: Tuple[dict, ...]
    allowed_names: FrozenSet[str]
    stripped_groups: FrozenSet[str]
    excluded_mcp: FrozenSet[str]
    unparseable_mcp: Tuple[str, ...]
    fallback: bool


def resolve_tools_for_tier(
    tools: List[dict],
    intent_class: Optional[str],
    complexity_signal: Optional[str],
    taxonomy: dict,
    tier_budget: "TierBudget",
) -> ToolResolution:
    """Resolve the per-turn tool surface under a tier budget (R1 + D4 + D8).

    Composition is R1 — intersection: the intent selects groups (Sprint 29),
    the tier's ``allow_groups`` caps which of them survive (``"*"`` = no cap, so
    the intent selection passes through unchanged — the T3 "unchanged / full
    load" case). A group the intent needs but the tier forbids is reported in
    ``stripped_groups`` for the escalation net (D8); it is never silently
    dropped. MCP exposure is gated by the tier's ``exclude_mcp`` (D4).

    On an unknown intent (maximal fallback) the budget is STILL honored — the
    surface is capped to ``allow_groups`` (or left full when the tier allows
    ``"*"``) and marked ``fallback`` loudly, rather than loading the whole
    registry past the tier's prefill ceiling. Phase 4 decides whether an
    unknown-intent turn on a budgeted tier escalates.
    """
    allow: Set[str] = set(tier_budget.tools.allow_groups)
    wildcard_groups = _WILDCARD in allow

    groups = _resolve_intent_groups(intent_class, complexity_signal, taxonomy)
    if groups is None:
        fallback = True
        stripped: Set[str] = set()
        if wildcard_groups:
            allowed: Optional[Set[str]] = None  # tier permits the full registry
        else:
            allowed = _materialize(allow, taxonomy)
        logger.info(
            "[grove.context_budget] maximal fallback under tier budget "
            "(intent_class=%r) — capped to allow_groups=%s, MCP gated by "
            "exclude_mcp",
            intent_class,
            "*" if wildcard_groups else sorted(allow),
        )
    else:
        fallback = False
        if wildcard_groups:
            kept_groups = set(groups)
            stripped = set()
        else:
            kept_groups = groups & allow
            stripped = groups - allow
        allowed = _materialize(kept_groups, taxonomy)
        if allowed:
            _validate_co_location(allowed, intent_class or "")

    kept, excluded_mcp, unparseable = _partition_tools(tools, allowed, tier_budget)
    return ToolResolution(
        tools=tuple(kept),
        allowed_names=frozenset(allowed or ()),
        stripped_groups=frozenset(stripped),
        excluded_mcp=frozenset(excluded_mcp),
        unparseable_mcp=tuple(unparseable),
        fallback=fallback,
    )


def reset_taxonomy_cache() -> None:
    """Drop the module-level taxonomy cache.

    Tests call this via the autouse conftest fixture so per-test
    GROVE_HOME isolation extends to the taxonomy resolution path —
    otherwise the first test's runtime taxonomy path would be cached
    and subsequent tests would read a stale (deleted) file.
    """
    global _taxonomy_cache
    _taxonomy_cache = None

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
from pathlib import Path
from typing import List, Optional, Set

import yaml

logger = logging.getLogger(__name__)

__all__ = [
    "load_taxonomy",
    "resolve_tool_set",
    "filter_tools_by_name",
    "reset_taxonomy_cache",
]


_REQUIRED_TOP_LEVEL = frozenset({
    "version", "core", "domain_chunks", "exploratory", "mcp_notion",
})
_REQUIRED_MCP_KEYS = frozenset({"reads", "writes", "write_intents"})


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
    if not isinstance(raw["mcp_notion"], dict):
        raise ValueError(
            f"tool_groups.yaml at {target}: mcp_notion must be a mapping"
        )
    mcp_missing = _REQUIRED_MCP_KEYS - set(raw["mcp_notion"].keys())
    if mcp_missing:
        raise ValueError(
            f"tool_groups.yaml at {target}: mcp_notion missing keys "
            f"{sorted(mcp_missing)}"
        )
    for key in ("reads", "writes", "write_intents"):
        if not isinstance(raw["mcp_notion"][key], list):
            raise ValueError(
                f"tool_groups.yaml at {target}: mcp_notion[{key!r}] "
                f"must be a list"
            )

    if path is None:
        _taxonomy_cache = raw
    return raw


def resolve_tool_set(
    intent_class: Optional[str],
    complexity_signal: Optional[str],
    taxonomy: dict,
) -> Optional[Set[str]]:
    """Compute the per-turn allowed tool-name set.

    Returns ``None`` when the intent is unknown or missing — the signal
    to the caller to load ALL tools. The Architectural Prime Directive
    says fail loud, not silent: callers MUST surface this fallback to
    the Kaizen Ledger so the operator can audit how often the
    classifier failed to give the optimizer enough to work with.

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
    if intent_class is None or intent_class == "unknown":
        logger.info(
            "[grove.context_budget] tool selection: maximal fallback "
            "(intent_class=%r) — full registry loaded",
            intent_class,
        )
        return None

    selected: Set[str] = set()
    selected.update(taxonomy["core"])
    selected.update(taxonomy["mcp_notion"]["reads"])
    selected.update(taxonomy["domain_chunks"].get(intent_class, []))
    if complexity_signal in ("complex", "novel"):
        selected.update(taxonomy["exploratory"])
    if intent_class in taxonomy["mcp_notion"]["write_intents"]:
        selected.update(taxonomy["mcp_notion"]["writes"])
    return selected


def filter_tools_by_name(
    tools: List[dict],
    allowed: Optional[Set[str]],
) -> List[dict]:
    """Filter an OpenAI-format tools list by tool name.

    Args:
        tools: list of tool dicts in the
            ``{"type": "function", "function": {"name": ..., ...}}``
            shape ``get_tool_definitions`` returns.
        allowed: set of names the turn should expose, OR ``None`` for
            pass-through (the maximal-fallback signal from
            :func:`resolve_tool_set`).

    Returns:
        The filtered list, or the input list unchanged when allowed is
        None. Preserves insertion order — the registry's order survives
        the filter.
    """
    if allowed is None:
        return tools
    out: List[dict] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        name = t.get("function", {}).get("name") if isinstance(
            t.get("function"), dict
        ) else None
        if name in allowed:
            out.append(t)
    return out


def reset_taxonomy_cache() -> None:
    """Drop the module-level taxonomy cache.

    Tests call this via the autouse conftest fixture so per-test
    GROVE_HOME isolation extends to the taxonomy resolution path —
    otherwise the first test's runtime taxonomy path would be cached
    and subsequent tests would read a stale (deleted) file.
    """
    global _taxonomy_cache
    _taxonomy_cache = None

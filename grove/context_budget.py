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

__all__ = [
    "CO_LOCATED_TOOLS",
    "ToolResolution",
    "load_taxonomy",
    "resolve_tool_set",
    "resolve_tools_for_tier",
    "min_covering_tier",
    "filter_tools_by_name",
    "reset_taxonomy_cache",
    "reset_caps_index_cache",
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


def _selected_group_names(
    intent_class: Optional[str],
    complexity_signal: Optional[str],
) -> Optional[Set[str]]:
    """The tool-GROUP names a turn selects, or ``None`` for the maximal-fallback
    signal (unknown / missing intent).

    GRV-009 E5 C-RETIRE — taxonomy-free. The group-selection rule is purely
    structural: ``core`` always, the intent's own group (the intent_class name
    IS its group name), plus ``exploratory`` on complex/novel turns. It needs no
    ``tool_groups.yaml`` read — the tool->group taxonomy the records subsumed.
    This is the group-NAME logic the resolver uses for the ``stripped`` / fallback
    provenance; native tool admission itself is registry-driven
    (``_registry_allowed_names``).
    """
    if intent_class is None or intent_class == "unknown":
        return None
    from grove.classify import INTENT_CLASSES
    groups: Set[str] = {"core"}
    if intent_class in INTENT_CLASSES:
        groups.add(intent_class)
    if complexity_signal in ("complex", "novel"):
        groups.add("exploratory")
    return groups


def resolve_tool_set(
    intent_class: Optional[str],
    complexity_signal: Optional[str],
    taxonomy: Optional[dict] = None,
) -> Optional[Set[str]]:
    """Compute the per-turn allowed tool-name set (Sprint 29, intent-only).

    Returns ``None`` when the intent is unknown or missing — the signal
    to the caller to load ALL tools. The Architectural Prime Directive
    says fail loud, not silent: callers MUST surface this fallback to
    the Kaizen Ledger so the operator can audit how often the
    classifier failed to give the optimizer enough to work with.

    The tier-unaware surface; the tier-aware gate lives in
    :func:`resolve_tools_for_tier`. Both derive the native surface from the same
    :func:`_registry_allowed_names` admission (this path passes ``current_tier=None``
    — the eligibility gate bypassed), so neither can drift from the selection rule.

    Args:
        intent_class: one of the Sprint 12 INTENT_CLASSES, or
            ``"unknown"`` / ``None`` for the maximal-fallback signal.
        complexity_signal: one of the Sprint 12 COMPLEXITY_SIGNALS
            (``simple`` / ``moderate`` / ``complex`` / ``novel``).
            ``complex`` / ``novel`` add the exploratory group.
        taxonomy: GRV-009 E5 C-RETIRE — accepted for back-compatibility and
            IGNORED; native selection is registry-driven and reads no
            ``tool_groups.yaml`` taxonomy.

    Returns:
        Set of tool names to expose this turn, or None for "load all".
    """
    groups = _selected_group_names(intent_class, complexity_signal)
    if groups is None:
        logger.info(
            "[grove.context_budget] tool selection: maximal fallback "
            "(intent_class=%r) — full registry loaded",
            intent_class,
        )
        return None
    # GRV-009 E5 C-RESOLVE — the intent-only (tier-unaware) native surface now
    # derives from the capability registry, NOT _materialize over tool_groups.
    # Tier-unaware == the wildcard case of the registry resolver (every group
    # admitted; only the intent/complexity/disclosure gate applies). MCP tools
    # (mcp_*) are not enumerated here — the per-turn filter governs MCP exposure.
    # Tier-unaware == ``current_tier=None`` (the eligibility gate bypassed): every
    # intent-selected record admitted, no tier cap, nothing stripped.
    selected, _ = _registry_allowed_names(
        intent_class, complexity_signal, current_tier=None
    )
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
    mcp_allow: Optional[Set[str]] = None,
) -> Tuple[List[dict], Set[str], List[str]]:
    """Single source of truth for per-tool admission (D4 + Sprint 74 Phase 2).

    Returns ``(kept, excluded_mcp_servers, unparseable_mcp_names)``.

    * MCP tool (name starts ``mcp_``): gated by the ``mcp_allow`` set alone.
      ``mcp_allow`` (computed by ``run_agent._compute_mcp_allow`` from the
      ``kind=mcp`` Capability records via per-turn ``trigger`` match) is the
      SOLE MCP gate. ``None`` ⇒ no records (flip OFF, every MCP passes —
      vanilla/legacy). A set ⇒ a server passes ONLY if it is in the set;
      otherwise withheld. ``excluded_mcp_servers`` is always empty (kept for
      the return shape / provenance).
      An unparseable MCP name is admitted by default and recorded — never
      silently swallowed, and never subject to the match flip.
    * non-MCP tool: admitted when ``allowed is None`` (pass-through) or its name
      is in ``allowed``. The MCP flip never touches native tools.

    With ``mcp_allow=None`` every MCP passes — the no-records / vanilla case.
    """
    # ``mcp_allow`` (registry-driven, kind=mcp records via trigger match) is the
    # sole MCP gate. ``excluded_mcp`` is always empty (kept for the return shape /
    # the D10 provenance field).
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
            # Disclose-on-match (GRV-009 E4): the registry-driven ``mcp_allow``.
            # None ⇒ no mcp records (legacy allow-by-default). A set ⇒ admit
            # only the servers eligible-on-tier AND trigger-matched this turn.
            if mcp_allow is not None and server not in mcp_allow:
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
    mcp_allow: Optional[Set[str]] = None,
) -> List[dict]:
    """Filter an OpenAI-format tools list by tool name, with optional per-tier
    MCP gating (Sprint 73, D4).

    Args:
        tools: list of tool dicts in the
            ``{"type": "function", "function": {"name": ..., ...}}`` shape.
        allowed: set of names the turn should expose, OR ``None`` for non-MCP
            pass-through (the maximal-fallback signal).
        tier_budget: vestigial back-compat positional — ignored. Per-tier MCP
            exposure is no longer a budget concern; MCP gating is governed solely
            by ``mcp_allow`` (the registry-driven per-turn disclose set).
        mcp_allow: an MCP tool passes only if its server is in this set; ``None``
            ⇒ every MCP passes (the legacy allow-by-default signal). ``None`` for
            all of ``tier_budget`` / ``allowed`` / ``mcp_allow`` is the legacy
            pass-through fast-path.

    Returns:
        The filtered list, preserving insertion order.
    """
    # Legacy fast-path: no tier, no name set, and no MCP match-gate returns the
    # list verbatim (including any non-dict entries) exactly as Sprint 29 did.
    if tier_budget is None and allowed is None and mcp_allow is None:
        return tools
    kept, _, _ = _partition_tools(tools, allowed, tier_budget, mcp_allow)
    return kept


@dataclass(frozen=True)
class ToolResolution:
    """The tier-aware per-turn tool resolution (Sprint 73, D4 / D8 / D10).

    The canonical surface Phase 4 wires the Dispatcher onto. Carries both the
    admitted tool list and the provenance the escalation net (D8) and the
    ``/context`` + ledger telemetry (D10) need:

    * ``tools`` — the filtered tool list the agent receives.
    * ``allowed_names`` — non-MCP names admitted by the tier-eligibility gate.
    * ``stripped_capabilities`` — the capabilities the turn's intent SELECTED that
      the tier made ineligible (``current_tier ∉ tier_rule.eligible``), each as
      ``(cap_id, eligible_tuple)`` (Option B — re-sourced off ``stripped_groups``).
      Non-empty ⇒ the tier cannot serve the intent's full tool need; Phase 4
      escalates ONCE to the minimum covering tier (D8) — NEVER a silent strip.
    * ``excluded_mcp`` — MCP servers the tier excluded (e.g. ``notion``).
    * ``unparseable_mcp`` — MCP names that could not be parsed; admitted by
      default and surfaced here (logged, never silently swallowed).
    * ``fallback`` — the classifier yielded no usable intent; the budget was
      still honored (gated by ``tier_rule.eligible``), loudly.
    """

    tools: Tuple[dict, ...]
    allowed_names: FrozenSet[str]
    stripped_capabilities: FrozenSet[Tuple[str, Tuple[int, ...]]]
    excluded_mcp: FrozenSet[str]
    unparseable_mcp: Tuple[str, ...]
    fallback: bool


# ── GRV-009 E5 C-RESOLVE — registry-driven native admission ──────────────────
# The native offered surface derives from the capability registry (each record's
# disclosure mode + intents + bindings.tools) gated by ONE tier rule:
# ``current_tier in tier_rule.eligible`` (web-surface-admission-fix, Option B —
# allow_groups retired; the records subsume the tool_groups.yaml taxonomy).
# Cached: load_capabilities() does file I/O and the resolver runs per turn.

_caps_index_cache: Optional[List[tuple]] = None


def _caps_index() -> List[tuple]:
    """Cached projection of the registry for native admission.

    Each entry is ``(cap_id, disclosure, always, intents_frozenset,
    eligible_frozenset, native_tools_tuple)`` for records that govern at least one
    native (non-MCP) tool. ``eligible_frozenset`` is the record's
    ``tier_rule.eligible`` — the SOLE tier gate (Option B, ``allow_groups``
    retired): a record's tools load on tier ``T`` iff ``T`` is in this set (or no
    tier is routed). ``cap_id`` rides so the D8 escalation net can name the
    capabilities a tier stripped, not just their groups. MCP tools are gated
    separately by ``mcp_allow`` (E4) and excluded here.
    """
    global _caps_index_cache
    if _caps_index_cache is None:
        from grove.capability_registry import load_capabilities

        index: List[tuple] = []
        for c in load_capabilities().values():
            native = tuple(t for t in c.bindings.tools if not _is_mcp(t))
            if not native:
                continue
            index.append(
                (
                    c.id,
                    c.trigger.disclosure.value,
                    bool(c.trigger.always),
                    frozenset(c.trigger.intents),
                    frozenset(c.tier_rule.eligible),
                    native,
                )
            )
        _caps_index_cache = index
    return _caps_index_cache


def reset_caps_index_cache() -> None:
    """Drop the registry projection cache (conftest resets between tests)."""
    global _caps_index_cache
    _caps_index_cache = None


def _registry_allowed_names(
    intent_class: Optional[str],
    complexity_signal: Optional[str],
    current_tier: Optional[int],
) -> Tuple[Set[str], List[Tuple[str, Tuple[int, ...]]]]:
    """The native tool-name surface admitted this turn, plus the capabilities the
    tier stripped (C-RESOLVE / D8 — Option B: ``tier_rule.eligible`` is the SOLE
    tier gate; ``allow_groups`` is retired).

    Admission is two predicates per capability:

    ``intent_match`` (tier-independent) — does this turn SELECT the record?
      * ``fallback`` disclosure — selected only on the unknown maximal fallback.
      * ``complexity`` disclosure — the exploratory surface; selected on a
        complex/novel turn (or the unknown fallback).
      * ``proactive`` + ``always`` — the core surface; always selected.
      * ``proactive`` + ``intents`` — selected iff the turn's intent is one of the
        record's; on the unknown fallback every proactive record is selected.

    ``tier_ok`` — ``current_tier in tier_rule.eligible``. ``current_tier is None``
    (cloud / vanilla — no tier routed) BYPASSES the gate and admits. An EMPTY
    ``eligible`` admits at NO tier; a core record with too-narrow ``eligible``
    therefore surfaces as a stripped capability (a RECORD bug), never a hidden
    branch special-case.

    A record the turn SELECTED but the tier makes ineligible is STRIPPED —
    returned as ``(cap_id, eligible_tuple)`` for the D8 escalation net, never
    silently dropped. Stripping is meaningful only on a CLASSIFIED turn: the
    unknown maximal fallback strips nothing (it could not name an intent to
    cover). Returns ``(admitted_names, stripped_capabilities)``.
    """
    unknown = intent_class is None or intent_class == "unknown"
    cx_high = complexity_signal in ("complex", "novel")
    names: Set[str] = set()
    stripped: List[Tuple[str, Tuple[int, ...]]] = []
    for cap_id, disclosure, always, intents, eligible, native_tools in _caps_index():
        if disclosure == "fallback":
            intent_match = unknown
        elif disclosure == "complexity":
            intent_match = True if unknown else cx_high
        elif always:  # proactive core
            intent_match = True
        else:  # proactive intent
            intent_match = True if unknown else (intent_class in intents)
        if not intent_match:
            continue
        # tier_rule.eligible is inert at admission (neuter-tier-eligible-gate):
        # the cognitive router picks the tier; the zone system governs mutation
        # safety. Every intent-matched capability is admitted; nothing is
        # tier-stripped. ``current_tier`` / ``eligible`` remain in the loop
        # unpacking only to preserve the vestigial signature shape.
        names.update(native_tools)
    return names, stripped


def resolve_tools_for_tier(
    tools: List[dict],
    intent_class: Optional[str],
    complexity_signal: Optional[str],
    taxonomy: dict,
    tier_budget: Optional["TierBudget"] = None,
    *,
    mcp_allow: Optional[Set[str]] = None,
    current_tier: Optional[int] = None,
) -> ToolResolution:
    """Resolve the per-turn tool surface under a tier (R1 + D4 + D8 — Option B).

    The native surface derives from the capability registry gated by ONE rule:
    ``current_tier in tier_rule.eligible`` (``allow_groups`` retired). ``current_tier``
    is the int tier (1/2/3) bound from ``run_agent._current_tier_int()``; escalation
    re-runs this builder at the new tier. ``current_tier is None`` (cloud / vanilla —
    no tier routed) bypasses the gate. A capability the intent selected but the
    tier makes ineligible is reported in ``stripped_capabilities`` for the D8
    escalation net; it is never silently dropped. MCP exposure is gated solely by
    ``mcp_allow`` (GRV-009 E4 C4): a server is admitted only if it is in the
    matched set. ``mcp_allow=None`` ⇒ no records, allow-by-default.

    On an unknown intent (maximal fallback) the surface is the full registry
    capped by ``tier_rule.eligible`` and marked ``fallback`` loudly; the unknown
    fallback strips nothing (it could not name an intent to cover). The
    ``taxonomy`` and ``tier_budget`` args are vestigial back-compat positionals —
    the resolver reads neither (no ``tool_groups.yaml``, no ``allow_groups``);
    ``tier_budget`` rides only to ``_partition_tools``, which ignores it.
    """
    fallback = intent_class is None or intent_class == "unknown"

    # C-RESOLVE / Option B: the native admitted surface and the stripped set both
    # come from the capability registry gated by tier_rule.eligible — NOT
    # allow_groups, NOT _materialize over tool_groups.
    allowed, stripped_caps = _registry_allowed_names(
        intent_class, complexity_signal, current_tier
    )
    if not fallback and allowed:
        _validate_co_location(allowed, intent_class or "")
    if fallback:
        logger.info(
            "[grove.context_budget] maximal fallback under tier budget "
            "(intent_class=%r) — registry-driven surface gated by "
            "tier_rule.eligible (current_tier=%r), MCP gated by the registry "
            "mcp_allow",
            intent_class,
            current_tier,
        )

    kept, excluded_mcp, unparseable = _partition_tools(
        tools, allowed, tier_budget, mcp_allow
    )
    return ToolResolution(
        tools=tuple(kept),
        allowed_names=frozenset(allowed or ()),
        stripped_capabilities=frozenset(stripped_caps),
        excluded_mcp=frozenset(excluded_mcp),
        unparseable_mcp=tuple(unparseable),
        fallback=fallback,
    )


def min_covering_tier(
    stripped_capabilities: "FrozenSet[Tuple[str, Tuple[int, ...]]]",
    current_tier: Optional[int],
) -> Optional[int]:
    """The minimum tier ``>= current_tier`` at which EVERY stripped capability is
    eligible — the single-jump D8 escalation target (Option B).

    ``target = min{ T >= current_tier : for all cap in stripped, T in
    eligible(cap) }`` — a strict intersection of the stripped caps'
    ``tier_rule.eligible`` sets, floored at the current tier. Returns ``None`` when
    no single tier covers the whole set (a null intersection: e.g. a ``[2]``-only
    cap co-stripped with a ``[3]``-only cap, or a cap eligible only BELOW the
    current tier). The caller FAILS LOUD on ``None`` — never silently picks a tier
    and strands a capability. Returns ``current_tier`` unchanged when nothing is
    stripped.

    Loop invariant (guaranteed by construction): a non-``None`` result is drawn
    from the intersection, so at that tier ``stripped_capabilities`` re-evaluates
    to empty.
    """
    if not stripped_capabilities:
        return current_tier
    eligible_sets = [set(elig) for (_cid, elig) in stripped_capabilities]
    common = set.intersection(*eligible_sets)
    candidates = [t for t in common if current_tier is None or t >= current_tier]
    return min(candidates) if candidates else None


def reset_taxonomy_cache() -> None:
    """Drop the module-level taxonomy cache.

    Tests call this via the autouse conftest fixture so per-test
    GROVE_HOME isolation extends to the taxonomy resolution path —
    otherwise the first test's runtime taxonomy path would be cached
    and subsequent tests would read a stale (deleted) file.
    """
    global _taxonomy_cache
    _taxonomy_cache = None

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
* Unknown intent → admit the always:true CORE ONLY (Andon-on-
  uncertainty, fallback-retirement-v1 Phase 3). The maximal "load
  everything" fallback is retired per the Architectural Prime Directive
  (never silently inflate the tool surface); the classifier failure is
  surfaced loudly and asynchronously (WARNING + operator Andon next turn),
  not by dumping the full registry.

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
from typing import FrozenSet, List, Optional, Set, Tuple

import yaml

logger = logging.getLogger(__name__)

__all__ = [
    "CO_LOCATED_TOOLS",
    "ToolResolution",
    "load_taxonomy",
    "resolve_tool_set",
    "resolve_tools_for_tier",
    "hidden_verdict_reasons",
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

    Runs against a CLASSIFIED per-turn set only; skipped on the unknown /
    fallback core surface (which cannot half-load an intent's co-located pair).
    The Agent sees this at construction time; the Andon is the message, not a
    downstream hang.
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


def resolve_tool_set(
    intent_class: Optional[str],
    complexity_signal: Optional[str],
) -> Set[str]:
    """Compute the per-turn allowed tool-name set (Sprint 29, intent-only).

    The tier-unaware surface; the tier-aware gate lives in
    :func:`resolve_tools_for_tier`. Both derive the native surface from the SAME
    :func:`_registry_allowed_names` admission,
    so neither can drift.

    On an unknown / missing intent this returns the always:true CORE ONLY —
    identical to the production hot path (fallback-retirement-v1 Phase 3,
    Andon-on-uncertainty). The maximal "load everything" fallback is retired per
    the Architectural Prime Directive (never silently inflate the tool surface on
    uncertainty); the classifier failure is surfaced loudly (WARNING) and, in
    production, via the operator Andon.

    Args:
        intent_class: one of the Sprint 12 INTENT_CLASSES, or ``"unknown"`` /
            ``None`` (the unclassified turn → always:true core surface).
        complexity_signal: one of the Sprint 12 COMPLEXITY_SIGNALS
            (``simple`` / ``moderate`` / ``complex`` / ``novel``);
            ``complex`` / ``novel`` add the exploratory (complexity) surface on a
            CLASSIFIED turn only.

    Returns:
        The set of native tool names to expose this turn (NEVER ``None``).
    """
    unknown = intent_class is None or intent_class == "unknown"
    if unknown:
        logger.warning(
            "[grove.context_budget] Andon: classifier returned unknown intent "
            "(intent_class=%r) — tier-unaware surface admits always:true CORE tools "
            "only (maximal fallback retired; identical to the production hot path).",
            intent_class,
        )
    # Registry-driven native admission. Unknown yields baseline + the
    # always:true core; a classified turn yields baseline + core +
    # intent-matched + (complex/novel) exploratory records. MCP tools (mcp_*) are
    # not enumerated here — the per-turn filter governs MCP exposure.
    selected = _registry_allowed_names(intent_class, complexity_signal)
    if not unknown:
        # Co-location is validated on a CLASSIFIED surface only, mirroring
        # resolve_tools_for_tier (which skips the guard on the fallback path).
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
    mcp_allow: Optional[Set[str]] = None,
) -> List[dict]:
    """Filter an OpenAI-format tools list by tool name, with optional per-turn
    MCP gating (Sprint 73, D4).

    Args:
        tools: list of tool dicts in the
            ``{"type": "function", "function": {"name": ..., ...}}`` shape.
        allowed: set of names the turn should expose, OR ``None`` for non-MCP
            pass-through (a no-filter signal — e.g. the resolver-crash
            degradation in run_agent; NOT produced by resolve_tool_set, which
            now returns the always:true core, never None).
        mcp_allow: an MCP tool passes only if its server is in this set; ``None``
            ⇒ every MCP passes (the legacy allow-by-default signal). ``None`` for
            both ``allowed`` and ``mcp_allow`` is the legacy pass-through fast-path.

    Returns:
        The filtered list, preserving insertion order.
    """
    # Legacy fast-path: no name set and no MCP match-gate returns the list
    # verbatim (including any non-dict entries) exactly as Sprint 29 did.
    if allowed is None and mcp_allow is None:
        return tools
    kept, _, _ = _partition_tools(tools, allowed, mcp_allow)
    return kept


@dataclass(frozen=True)
class ToolResolution:
    """The tier-aware per-turn tool resolution (Sprint 73, D4 / D8 / D10).

    The canonical surface Phase 4 wires the Dispatcher onto. Carries both the
    admitted tool list and the provenance the escalation net (D8) and the
    ``/context`` + ledger telemetry (D10) need:

    * ``tools`` — the filtered tool list the agent receives.
    * ``allowed_names`` — non-MCP names admitted by registry admission.
      (``stripped_capabilities`` and the D8 escalation contract are DELETED —
      retrieval-ambient-class-v1 P2; the inert tier gate never stripped.)
    * ``excluded_mcp`` — MCP servers the match-gate excluded (e.g. ``notion``).
    * ``unparseable_mcp`` — MCP names that could not be parsed; admitted by
      default and surfaced here (logged, never silently swallowed).
    * ``fallback`` — the classifier yielded no usable intent; the budget was
      still honored (gated by ``tier_rule.eligible``), loudly.
    """

    tools: Tuple[dict, ...]
    allowed_names: FrozenSet[str]
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
    native_tools_tuple)`` for records that govern at least one native (non-MCP)
    tool. ``cap_id`` keys the additive admission overlay merge. The
    ``eligible`` column and the D8 stripped-capability threading are DELETED
    (retrieval-ambient-class-v1 P2 — the inert tier gate and its dead
    escalation net are demolished; tier governance is the Cognitive Router's
    job). MCP tools are gated separately by ``mcp_allow`` (E4) and excluded
    here.
    """
    global _caps_index_cache
    if _caps_index_cache is None:
        from grove.capability import NULL_CAPABILITY_STATES
        from grove.capability_registry import load_capabilities

        index: List[tuple] = []
        for c in load_capabilities().values():
            # retrieval-ambient-class-v1 P4 (ruling B, narrowed) — lifecycle
            # wall: a deprecated/suspended record yields NULL effective
            # capability. Its tools are NOT admitted regardless of overlay
            # grants or disclosure class (lifecycle outranks baseline).
            if c.lifecycle.state in NULL_CAPABILITY_STATES:
                continue
            native = tuple(t for t in c.bindings.tools if not _is_mcp(t))
            if not native:
                continue
            index.append(
                (
                    c.id,
                    c.trigger.disclosure.value,
                    bool(c.trigger.always),
                    frozenset(c.trigger.intents),
                    native,
                )
            )
        _caps_index_cache = index
    return _caps_index_cache


def reset_caps_index_cache() -> None:
    """Drop the registry projection cache (conftest resets between tests)."""
    global _caps_index_cache
    _caps_index_cache = None


def _effective_caps_index() -> List[tuple]:
    """The cached repo projection with the ADDITIVE admission overlay merged in,
    read PER TURN (operator-mutable-admission-v1 P1).

    The repo projection (``_caps_index``) stays cached; the overlay is read fresh
    on every call (``read_admission_overlay`` — small flat files, no cache) so an
    operator/Kaizen edit takes effect on the next turn with no restart. Merge is
    ADDITIVE-ONLY, applied here so ALL builder branches (always / intent /
    complexity) see the merged values:

        effective_intents = repo trigger.intents  ∪  added_intents
        effective_always  = repo trigger.always  OR  force_always

    Structurally cannot shrink the offered surface (union + OR only). An overlay
    read failure logs and falls back to the repo-only projection — never raises
    into admission, never empties the surface (I2)."""
    base = _caps_index()
    try:
        from grove.capability_registry import read_admission_overlay
        overlay = read_admission_overlay()
    except Exception as exc:  # noqa: BLE001 — overlay I/O never breaks admission
        logger.warning(
            "[grove.context_budget] admission overlay read failed (%r) — "
            "resolving with repo definitions only.", exc,
        )
        overlay = {}
    if not overlay:
        return base
    merged: List[tuple] = []
    for row in base:
        cap_id, disclosure, always, intents, native_tools = row
        add = overlay.get(cap_id)
        if add is None:
            merged.append(row)
            continue
        added_intents, force_always = add
        merged.append(
            (
                cap_id,
                disclosure,
                always or force_always,
                intents | added_intents,
                native_tools,
            )
        )
    return merged


def _registry_allowed_names(
    intent_class: Optional[str],
    complexity_signal: Optional[str],
) -> Set[str]:
    """The native tool-name surface admitted this turn.

    Admission is ONE predicate per capability — ``intent_match`` (does this turn
    SELECT the record?):
      * ``proactive`` + ``always`` — the core surface; always selected (classified
        or not).
      * unknown / unclassified turn — ONLY the ``always`` core above is selected
        (fallback-retirement-v1 Phase 3, Andon-on-uncertainty). Intent-gated and
        complexity records are withheld; the maximal "load everything" fallback is
        retired. The classifier failure surfaces asynchronously, not by inflating
        the surface.
      * ``complexity`` disclosure — the exploratory surface; selected on a
        complex/novel CLASSIFIED turn.
      * ``proactive`` + ``intents`` — selected iff the classified turn's intent is
        one of the record's.

    The tier-eligibility gate is DELETED (retrieval-ambient-class-v1 P2 —
    inerted by fallback-retirement-v1 Phase 2, demolished here along with its
    ``current_tier`` plumbing and the always-empty stripped-capability return):
    tier governance is the Cognitive Router's job; the zone system governs
    mutation safety. Every intent-matched capability is admitted. Returns the
    admitted name set.
    """
    unknown = intent_class is None or intent_class == "unknown"
    cx_high = complexity_signal in ("complex", "novel")
    names: Set[str] = set()
    for _cap_id, disclosure, always, intents, native_tools in _effective_caps_index():
        # Precedence: baseline > complexity > always (retrieval-ambient-class-v1
        # P1). ``baseline`` is the ambient retrieval class — admitted
        # unconditionally: every intent class, every complexity signal, every
        # tier, INCLUDING unknown/unclassified turns (the ambient class is the
        # floor the Andon-on-uncertainty path stands on, not a surface it
        # withholds). Green-zone-only by loader validation (capability.py).
        # complexity stays checked BEFORE always: some exploratory records
        # (browser_read, delegate_task) carry disclosure=complexity AND always=True;
        # complexity must win so they ride ONLY complex/novel turns, never every
        # turn. (Reordering these branches leaks surface — load-bearing precedence.)
        if disclosure == "baseline":
            intent_match = True
        elif disclosure == "complexity":
            # Exploratory surface — only on a genuinely complex/novel CLASSIFIED
            # turn. Withheld on unknown: fallback-retirement-v1 Phase 3 admits the
            # core only on uncertainty, and exploratory tools are the specialized
            # surface the Andon withholds (never inflate on an unclassified turn).
            intent_match = (not unknown) and cx_high
        elif always:
            # Proactive CORE — the always-offered native surface, admitted on
            # every turn, classified or not.
            intent_match = True
        elif unknown:
            # Andon-on-uncertainty (fallback-retirement-v1 Phase 3): an
            # unclassified turn admits ONLY the always:true core above. Intent-gated
            # and complexity records are NOT inflated onto an unknown turn — the
            # turn answers with the core surface and the classification failure is
            # surfaced asynchronously (resolve_tools_for_tier marks ``fallback``;
            # run_agent raises the operator Andon on the NEXT turn). This replaces
            # the retired maximal "load everything" fallback — Prime Directive:
            # never silently inflate the tool surface on uncertainty.
            intent_match = False
        else:
            # Proactive + intents — selected iff the classified intent matches.
            intent_match = intent_class in intents
        if not intent_match:
            continue
        names.update(native_tools)
    return names


def resolve_tools_for_tier(
    tools: List[dict],
    intent_class: Optional[str],
    complexity_signal: Optional[str],
    *,
    mcp_allow: Optional[Set[str]] = None,
) -> ToolResolution:
    """Resolve the per-turn tool surface (R1 + D4).

    The native surface derives from the capability registry via
    intent/disclosure admission (``_registry_allowed_names``). The tier-
    eligibility gate and its ``current_tier`` plumbing are DELETED
    (retrieval-ambient-class-v1 P2 — inerted by fallback-retirement-v1
    Phase 2, demolished here): tier governance is the Cognitive Router's
    responsibility, and the zone system governs mutation safety. MCP exposure
    is gated solely by ``mcp_allow`` (GRV-009 E4 C4): a server is admitted only if
    it is in the matched set. ``mcp_allow=None`` ⇒ no records, allow-by-default.

    On an unknown intent the surface is the baseline + always:true CORE ONLY
    (Andon-on-uncertainty, fallback-retirement-v1 Phase 3 — the maximal "load
    everything" fallback is retired). The result is marked ``fallback`` so
    run_agent answers with that surface and raises the operator Andon
    asynchronously.
    """
    fallback = intent_class is None or intent_class == "unknown"

    allowed = _registry_allowed_names(intent_class, complexity_signal)
    if not fallback and allowed:
        _validate_co_location(allowed, intent_class or "")
    if fallback:
        logger.warning(
            "[grove.context_budget] Andon: classifier returned unknown intent "
            "(intent_class=%r) — admitting baseline + always:true CORE tools "
            "only. Specialized tools withheld; operator may see reduced capability "
            "(surfaced asynchronously next turn).",
            intent_class,
        )

    kept, excluded_mcp, unparseable = _partition_tools(
        tools, allowed, mcp_allow
    )
    return ToolResolution(
        tools=tuple(kept),
        allowed_names=frozenset(allowed or ()),
        excluded_mcp=frozenset(excluded_mcp),
        unparseable_mcp=tuple(unparseable),
        fallback=fallback,
    )


# min_covering_tier (the D8 single-jump escalation target) is DELETED —
# retrieval-ambient-class-v1 P2: its sole feeder (stripped_capabilities from
# the inert tier gate) was structurally always empty, so the helper could
# never fire.


def hidden_verdict_reasons(
    hidden_names: "Set[str]",
    intent_class: Optional[str],
    complexity_signal: Optional[str],
) -> "Dict[str, List[str]]":
    """retrieval-ambient-class-v1 P5 — attribute each HIDDEN (not-admitted)
    tool to the DECIDING gate from the post-P2 census, grouped by reason
    (compact telemetry inversion: {reason: [names]}).

    Reasons: ``lifecycle-null`` (P4 wall — deprecated/suspended record),
    ``complexity-gate`` (G5 — complexity record on a non-complex turn),
    ``trigger-miss`` (proactive-intent record, unmatched intent — incl. the
    unknown-turn withholding), ``mcp-allow-miss`` (G7 — server not in the
    matched set), ``recordless`` (no governing record). The co-location gate
    (G15) never appears: it raises an Andon rather than hiding a tool, so it
    cannot produce a verdict.

    Reads ``load_capabilities`` directly (NOT the walled projection): the
    lifecycle-nulled records this must name are exactly the ones the
    admission index skips. STANDING CONSTRAINT (P5 ruling 3): this unwalled
    read path is TELEMETRY-ONLY and must never inform control — an
    observational read that ever gates admission or disclosure becomes a
    second authority over the walled projection.
    """
    from typing import Dict as _Dict, List as _List

    out: "_Dict[str, _List[str]]" = {}
    if not hidden_names:
        return out
    mcp_hidden = sorted(n for n in hidden_names if _is_mcp(n))
    if mcp_hidden:
        out["mcp-allow-miss"] = mcp_hidden
    native_hidden = {n for n in hidden_names if not _is_mcp(n)}
    if not native_hidden:
        return out
    try:
        from grove.capability import NULL_CAPABILITY_STATES
        from grove.capability_registry import load_capabilities

        by_tool = {}
        for c in load_capabilities().values():
            for t in c.bindings.tools:
                if not _is_mcp(t):
                    by_tool.setdefault(t, c)
    except Exception as exc:  # noqa: BLE001 — telemetry never breaks the turn
        logger.warning(
            "[grove.context_budget] hidden-verdict attribution failed (%r); "
            "reporting unattributed.", exc,
        )
        out["unattributed"] = sorted(native_hidden)
        return out
    unknown = intent_class is None or intent_class == "unknown"
    cx_high = complexity_signal in ("complex", "novel")
    for name in sorted(native_hidden):
        cap = by_tool.get(name)
        if cap is None:
            out.setdefault("recordless", []).append(name)
        elif cap.lifecycle.state in NULL_CAPABILITY_STATES:
            out.setdefault("lifecycle-null", []).append(name)
        elif cap.trigger.disclosure.value == "complexity" and not (
            (not unknown) and cx_high
        ):
            out.setdefault("complexity-gate", []).append(name)
        else:
            out.setdefault("trigger-miss", []).append(name)
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

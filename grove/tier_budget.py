"""Grove Tier Budget — Sprint 73 declarative-jit-budget-v1 (Phase 1).

Loader + fail-loud validator for the ``tier_budgets`` block in
``routing.config.yaml``. The budget is the per-tier prefill governor: for each
cognition tier it declares which gateable context blocks compose and which
tool groups + MCP servers are exposed. Policy lives here (D1, in
``routing.config.yaml``); the tool-group taxonomy stays in
``tool_groups.yaml`` (D2) — this module never restates what is *in* a group.

This is the parse-and-validate surface ONLY (Phase 1). It performs NO
enforcement and is wired to neither the PromptComposer nor the Sprint 29 tool
filter until Phase 4 — import-only until then.

Fail-loud discipline (D7, Architectural Prime Directive): a provider-backed
routing tier with no budget entry, a malformed entry, or an unknown
context-block / tool-group name raises ``ValueError`` at load. There is no
silent full-load fallback.

The gateable context blocks (D5) — a tier's ``context`` allow-list names these
and only these. The always-on baseline (identity, environment, platform,
register, the Tier-0 routing manifest) is never listed and always composes:

    claude_contract   the CLAUDE.md contract (``context_files`` provider, ~4.5K)
    goal_record       the Dock Tier-1 per-goal record (on-match injection)
    skills_index      the promoted-skills index (~2.8K)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

__all__ = [
    "GATEABLE_CONTEXT_BLOCKS",
    "WILDCARD",
    "ToolBudget",
    "TierBudget",
    "load_tier_budgets",
]

# D5 — the only names a tier's ``context`` allow-list may contain. Everything
# else in the prompt is always-on baseline and is never budget-listed.
GATEABLE_CONTEXT_BLOCKS: frozenset = frozenset(
    {"claude_contract", "goal_record", "skills_index"}
)

# Wildcard token. ``allow_groups: ["*"]`` = the full tool registry;
# ``exclude_mcp: ["*"]`` = every MCP server excluded on the tier.
WILDCARD = "*"


@dataclass(frozen=True)
class ToolBudget:
    """The ``tools`` half of a tier budget (D4).

    ``allow_groups`` — tool-group names (from ``tool_groups.yaml``) whose tools
    may load on this tier; ``("*",)`` admits the full registry. ``exclude_mcp``
    — MCP server names excluded on this tier; ``("*",)`` excludes every MCP.
    Both are stored in declared order.
    """

    allow_groups: Tuple[str, ...]
    exclude_mcp: Tuple[str, ...]


@dataclass(frozen=True)
class TierBudget:
    """One tier's prefill governor: gateable context + tool exposure."""

    context: Tuple[str, ...]
    tools: ToolBudget


def load_tier_budgets(
    config_path: Optional[Path] = None,
    *,
    taxonomy: Optional[Dict[str, Any]] = None,
    taxonomy_path: Optional[Path] = None,
) -> Dict[str, TierBudget]:
    """Load + validate the ``tier_budgets`` block from ``routing.config.yaml``.

    Args:
        config_path: explicit ``routing.config.yaml`` path (tests pass this).
            When ``None``, resolves the runtime sovereign copy
            (``$GROVE_HOME/routing.config.yaml``) then the repo template.
        taxonomy: a pre-loaded tool-group taxonomy dict (tests inject this to
            stay hermetic). When ``None``, the taxonomy is loaded via
            ``grove.context_budget.load_taxonomy(taxonomy_path)`` for the
            ``allow_groups`` cross-check (D2).
        taxonomy_path: explicit ``tool_groups.yaml`` path, used only when
            ``taxonomy`` is ``None``.

    Returns:
        A mapping of tier name → :class:`TierBudget`, one entry per
        provider-backed tier declared in ``routing.tier_preferences``.

    Raises:
        ValueError: the budget is absent, malformed, names an unknown context
            block or tool group, or fails to cover (exactly) the configured
            provider-backed tiers. Fail-loud per D7 — no silent full-load.
    """
    target = (
        Path(config_path) if config_path is not None else _resolve_routing_config_path()
    )
    with target.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError(
            f"routing config at {target} is not a mapping (got "
            f"{type(raw).__name__})"
        )

    routing = raw.get("routing")
    if not isinstance(routing, dict):
        raise ValueError(
            f"routing config at {target}: no 'routing' mapping — cannot "
            f"determine the configured tier set to validate budgets against"
        )

    inference_tiers = _inference_tiers(routing, target)

    budgets_raw = raw.get("tier_budgets")
    if budgets_raw is None:
        raise ValueError(
            f"routing config at {target}: no 'tier_budgets' block, but "
            f"provider-backed tier(s) {sorted(inference_tiers)} require a budget "
            f"(D7 — no silent full-load). Add a tier_budgets entry per tier."
        )
    if not isinstance(budgets_raw, dict):
        raise ValueError(
            f"routing config at {target}: 'tier_budgets' must be a mapping "
            f"(got {type(budgets_raw).__name__})"
        )

    valid_groups = _valid_group_names(taxonomy, taxonomy_path)

    budgets: Dict[str, TierBudget] = {}
    for tier_name, spec in budgets_raw.items():
        budgets[str(tier_name)] = _parse_tier_budget(
            str(tier_name), spec, target, valid_groups
        )

    # Exact cover: every provider-backed tier has a budget; no budget names a
    # non-inference or unknown tier. Both halves are fail-loud (D7).
    budget_tiers = set(budgets)
    missing = inference_tiers - budget_tiers
    if missing:
        raise ValueError(
            f"routing config at {target}: provider-backed tier(s) "
            f"{sorted(missing)} have no tier_budgets entry (D7 — no silent "
            f"full-load). Every inference tier must declare a budget."
        )
    extra = budget_tiers - inference_tiers
    if extra:
        raise ValueError(
            f"routing config at {target}: tier_budgets declares {sorted(extra)} "
            f"which is not a provider-backed tier in routing.tier_preferences. "
            f"Non-inference tiers (e.g. the T0 pattern cache) have no prefill to "
            f"govern — remove the stray entry or fix the tier name."
        )

    return budgets


def _resolve_routing_config_path() -> Path:
    """Runtime sovereign copy then repo template.

    Mirrors the resolution the Sprint 29 taxonomy loader and the Dock use:
    operator copy at ``$GROVE_HOME/routing.config.yaml`` first, else the repo
    template at ``config/routing.config.yaml`` (``grove/`` is one level under
    the repo root).
    """
    from hermes_constants import get_hermes_home

    runtime = Path(get_hermes_home()) / "routing.config.yaml"
    if runtime.exists():
        return runtime
    return Path(__file__).resolve().parents[1] / "config" / "routing.config.yaml"


def _inference_tiers(routing: Dict[str, Any], target: Path) -> Set[str]:
    """The provider-backed tiers that require a budget.

    A tier is provider-backed (an inference tier) when it has a ``provider``
    and no ``handler``. Handler tiers (e.g. T0 ``pattern_cache``) make no model
    call and compose no prompt — they are exempt from the budget.
    """
    prefs = routing.get("tier_preferences")
    if not isinstance(prefs, dict) or not prefs:
        raise ValueError(
            f"routing config at {target}: 'routing.tier_preferences' is missing "
            f"or empty — cannot determine which tiers require budgets"
        )
    tiers: Set[str] = set()
    for name, spec in prefs.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("handler"):
            continue  # non-inference (e.g. T0 pattern cache) — exempt
        if spec.get("provider"):
            tiers.add(str(name))
    return tiers


def _valid_group_names(
    taxonomy: Optional[Dict[str, Any]], taxonomy_path: Optional[Path]
) -> frozenset:
    """The set of tool-group names ``allow_groups`` may reference (besides ``*``).

    Derived from ``tool_groups.yaml``: the ``core`` group, the ``exploratory``
    group, and every ``domain_chunks`` key. Policy references taxonomy names;
    the catalog itself stays in ``tool_groups.yaml`` (D2).
    """
    if taxonomy is None:
        from grove.context_budget import load_taxonomy

        taxonomy = load_taxonomy(taxonomy_path)
    if not isinstance(taxonomy, dict):
        raise ValueError(
            "tool-group taxonomy did not load as a mapping; cannot validate "
            "allow_groups names"
        )
    names: Set[str] = {"core", "exploratory"}
    domain = taxonomy.get("domain_chunks")
    if isinstance(domain, dict):
        names |= {str(k) for k in domain.keys()}
    return frozenset(names)


def _parse_tier_budget(
    tier_name: str,
    spec: Any,
    target: Path,
    valid_groups: frozenset,
) -> TierBudget:
    """Validate one ``tier_budgets[<tier>]`` entry and build a TierBudget."""
    if not isinstance(spec, dict):
        raise ValueError(
            f"routing config at {target}: tier_budgets[{tier_name!r}] must be a "
            f"mapping (got {type(spec).__name__})"
        )

    # ── context: allow-list over the gateable heavy blocks (D5) ──────────
    context_raw = spec.get("context")
    if context_raw is None:
        raise ValueError(
            f"routing config at {target}: tier_budgets[{tier_name!r}].context is "
            f"required (use [] for none)"
        )
    if not isinstance(context_raw, list):
        raise ValueError(
            f"routing config at {target}: tier_budgets[{tier_name!r}].context "
            f"must be a list (got {type(context_raw).__name__})"
        )
    context: List[str] = []
    for item in context_raw:
        if not isinstance(item, str):
            raise ValueError(
                f"routing config at {target}: tier_budgets[{tier_name!r}].context "
                f"entries must be strings (got {item!r})"
            )
        if item not in GATEABLE_CONTEXT_BLOCKS:
            raise ValueError(
                f"routing config at {target}: tier_budgets[{tier_name!r}].context "
                f"names unknown block {item!r}; the gateable blocks are "
                f"{sorted(GATEABLE_CONTEXT_BLOCKS)} (D5). The always-on baseline "
                f"is never listed."
            )
        context.append(item)

    # ── tools: allow_groups + exclude_mcp (D4) ───────────────────────────
    tools_raw = spec.get("tools")
    if not isinstance(tools_raw, dict):
        raise ValueError(
            f"routing config at {target}: tier_budgets[{tier_name!r}].tools must "
            f"be a mapping with 'allow_groups' and 'exclude_mcp' (got "
            f"{type(tools_raw).__name__})"
        )
    allow_groups = _parse_str_list(
        tools_raw.get("allow_groups"), tier_name, "tools.allow_groups", target
    )
    exclude_mcp = _parse_str_list(
        tools_raw.get("exclude_mcp"), tier_name, "tools.exclude_mcp", target
    )

    # allow_groups cross-check against the taxonomy (D2). '*' is the wildcard;
    # any other name must be a real group, or it silently strips every tool —
    # exactly the file_ops/terminal defect this catches at load.
    for group in allow_groups:
        if group == WILDCARD:
            continue
        if group not in valid_groups:
            raise ValueError(
                f"routing config at {target}: tier_budgets[{tier_name!r}]."
                f"tools.allow_groups names unknown group {group!r}; valid groups "
                f"are '*' plus {sorted(valid_groups)} (defined in "
                f"tool_groups.yaml, D2)."
            )

    return TierBudget(
        context=tuple(context),
        tools=ToolBudget(
            allow_groups=tuple(allow_groups),
            exclude_mcp=tuple(exclude_mcp),
        ),
    )


def _parse_str_list(
    value: Any, tier_name: str, field: str, target: Path
) -> List[str]:
    """Validate a required list-of-strings budget field. Fail-loud."""
    if value is None:
        raise ValueError(
            f"routing config at {target}: tier_budgets[{tier_name!r}].{field} is "
            f"required (use [] for empty, ['*'] for wildcard)"
        )
    if not isinstance(value, list):
        raise ValueError(
            f"routing config at {target}: tier_budgets[{tier_name!r}].{field} "
            f"must be a list (got {type(value).__name__})"
        )
    out: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(
                f"routing config at {target}: tier_budgets[{tier_name!r}].{field} "
                f"entries must be strings (got {item!r})"
            )
        out.append(item)
    return out

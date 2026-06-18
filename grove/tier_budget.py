"""Grove Tier Budget — Sprint 73 declarative-jit-budget-v1 (Phase 1).

Loader + fail-loud validator for the ``tier_budgets`` block in
``routing.config.yaml``. The budget is the per-tier prefill governor: for each
cognition tier it declares which gateable context blocks compose. Policy lives
here (D1, in ``routing.config.yaml``).

Per-tier TOOL exposure is no longer a budget concern: the ``allow_groups``
dual-gate is retired (web-surface-admission-fix, Option B). ``tier_rule.eligible``
on each Capability record is the SOLE tier gate for the offered surface, applied
by ``grove.context_budget.resolve_tools_for_tier`` and enforced by
``run_agent._seam5_tier_refusal``.

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
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

import yaml

__all__ = [
    "GATEABLE_CONTEXT_BLOCKS",
    "TierBudgetMissing",
    "PrefillCeilingExceeded",
    "TierBudget",
    "load_tier_budgets",
    "tier_admits_context_block",
]


class TierBudgetMissing(RuntimeError):
    """A routed inference tier has no resolvable prefill budget — fail-loud.

    The Prime-Directive backstop for invariant 3 (Phase 4a): on an inference
    tier the budget carrier is ALWAYS populated. If a routed tier cannot
    resolve a budget, raise this — never silently revert to the eager payload.
    A ``raise`` (not an ``assert``) so it fires under ``python -O``.
    """


class PrefillCeilingExceeded(RuntimeError):
    """A local-tier prefill exceeds the tier's memory ceiling and could not
    escalate — fail-loud, never send (Sprint 77.0a local-prefill-governor-v1).

    The pre-flight governor (``run_agent._run_turn_generator``) raises this on
    the deny branch: the composed prefill for a local, memory-budgeted tier is
    over the tier's ``prefill_ceiling_tokens`` AND the escalation to a covering
    tier was denied (or ``escalation_policy`` is disabled). Sending the prefill
    to the local endpoint would OOM the host (the Sprint 71 24K-prefill crash),
    so the governor refuses rather than degrade silently. A ``raise`` (not a
    fall-through to the send) is the Prime-Directive guarantee that the
    over-ceiling prefill never reaches the wire.
    """

# D5 — the only names a tier's ``context`` allow-list may contain. Everything
# else in the prompt is always-on baseline and is never budget-listed.
GATEABLE_CONTEXT_BLOCKS: frozenset = frozenset(
    {"claude_contract", "goal_record", "skills_index"}
)

@dataclass(frozen=True)
class TierBudget:
    """One tier's prefill governor: gateable context + tool exposure + an
    optional memory ceiling.

    ``prefill_ceiling_tokens`` (Sprint 77.0a) — the maximum composed-prefill
    token count this tier's endpoint may safely receive. ``None`` (the default,
    and the only value for cloud tiers) means no memory governor: the tier has
    an effectively unbounded window and the pre-flight governor no-ops. A
    positive int is set ONLY for a local, memory-constrained tier (the qwen-mac
    MLX endpoint bound in Sprint 77.1) — it is what makes the governor
    "local-path only" by configuration rather than by a hardcoded provider
    name. The value is the measured prefill knee minus margin (interim:
    operator-confirmed live ~5K prefill + margin, until 77.1 measures the knee).
    """

    context: Tuple[str, ...]
    prefill_ceiling_tokens: Optional[int] = None


def tier_admits_context_block(
    block: str, tier_context_blocks: Optional[FrozenSet[str]]
) -> bool:
    """Whether a gateable context block rides this turn under the tier allow-list.

    Sprint 73 (D5) — the single admission predicate shared by the composer gate
    and the Dock injection seam so context and tools can never disagree on what
    a tier admits. ``tier_context_blocks`` is the tier's allow-list of gateable
    block names, or ``None`` when no tier budget is threaded.

    ``None`` ⇒ admitted — the Phase 3 isolation default / legacy path. NOTE
    (Phase 4 invariant): on a wired inference tier the carrier is ALWAYS
    populated; a ``None`` there must raise/escalate upstream, never silently
    re-admit the eager payload. Absent is not eager.
    """
    return tier_context_blocks is None or block in tier_context_blocks


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
        taxonomy: accepted for back-compatibility and IGNORED. The
            ``allow_groups`` cross-check (D2) that consumed it is retired with
            ``allow_groups`` (web-surface-admission-fix, Option B).
        taxonomy_path: accepted for back-compatibility and IGNORED.

    Returns:
        A mapping of tier name → :class:`TierBudget`, one entry per
        provider-backed tier declared in ``routing.tier_preferences``.

    Raises:
        ValueError: the budget is absent, malformed, names an unknown context
            block, or fails to cover (exactly) the configured provider-backed
            tiers. Fail-loud per D7 — no silent full-load.
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

    budgets: Dict[str, TierBudget] = {}
    for tier_name, spec in budgets_raw.items():
        budgets[str(tier_name)] = _parse_tier_budget(str(tier_name), spec, target)

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


def _parse_tier_budget(
    tier_name: str,
    spec: Any,
    target: Path,
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

    # ── tools: retired. Per-tier tool exposure is now governed solely by each
    # Capability record's ``tier_rule.eligible`` (web-surface-admission-fix,
    # Option B); the ``tools.allow_groups`` budget key is gone. A leftover
    # ``tools:`` block in an old/sovereign config is silently ignored (not read,
    # not required) so the loader never crashes on a stale operator copy. ──────

    # ── prefill_ceiling_tokens: optional memory governor (Sprint 77.0a) ───
    # Absent ⇒ None ⇒ the pre-flight governor no-ops for this tier (every
    # cloud tier; an unbounded window). Present ⇒ a positive int — the local
    # memory ceiling. Fail-loud on malformed (D7 / Prime Directive); ``bool``
    # is rejected explicitly because ``isinstance(True, int)`` is True.
    ceiling_raw = spec.get("prefill_ceiling_tokens")
    prefill_ceiling_tokens: Optional[int] = None
    if ceiling_raw is not None:
        if isinstance(ceiling_raw, bool) or not isinstance(ceiling_raw, int) or ceiling_raw <= 0:
            raise ValueError(
                f"routing config at {target}: tier_budgets[{tier_name!r}]."
                f"prefill_ceiling_tokens must be a positive integer when present "
                f"(got {ceiling_raw!r}); omit it for an unbounded (cloud) tier."
            )
        prefill_ceiling_tokens = ceiling_raw

    return TierBudget(
        context=tuple(context),
        prefill_ceiling_tokens=prefill_ceiling_tokens,
    )

"""cellar_knowledge — the wiki-cellar PromptComposer provider.

unified-retrieval-provider-v1. Surfaces canonical cellar pages (fleet briefs,
Dock goals, graduated memory) relevant to the turn, queried from the K1
``WikiIndex`` via BM25. Registered at ``tier="context", order=11`` — strictly
between ``system_message`` (10) and ``accumulated_domain_memory`` (15).

K6 (dynamic-context-assembly-v1) — this provider is gated as the
``cellar_context`` block (registration name stays ``cellar_knowledge``; see
``_PROVIDER_GATEABLE_BLOCK``). Its fill budget is the routed tier's
``cellar_context_ceiling`` (threaded into the compose context), falling back to
the constructor ``token_budget`` (1500) on non-routed composes.

ADDITIVE, never a replacement: this runs ALONGSIDE
``accumulated_domain_memory``. 28/29 active MemoryRecords are ungraduated and
have no cellar page; the JSONL provider remains their only serving path
(GATE-A finding). Removing it makes that knowledge dark.

Relevance:
  * ``user_message`` populated → BM25 over the message text + dock_goal boost.
  * ``user_message`` empty (construction / pre-turn composes) → a synthetic
    query from the active Dock goal NAMES (plus a goal dict's ``keywords`` when
    present) so the cellar still surfaces goal-relevant pages. BM25 needs
    non-empty text and ``dock_goal`` is a boost only — it cannot drive
    retrieval alone, so the synthetic text is required.
  * empty message AND no active goals → graceful no-op (the WikiIndex is never
    queried; the Composer skips an empty section).

Dock goals come from the injected ``dock_goals_loader`` closure
(``load_active_dock_goal_dicts`` by default), NEVER from the context dict
(GATE-A RULING a / GUARD P2-a). The highest-priority active goal (the first
the loader returns, in Dock order) supplies the ``dock_goal`` boost slug.

Freshness vs hot path: a fresh ``WikiIndex`` is built per call (so newly
graduated pages surface without a restart), but its per-query mtime scan
(``_ensure_fresh``) is gated by a 60s TTL on the provider closure — the
turn-start query does not stat the pages tree every turn.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from grove.prompt.composer import SectionResult
from grove.prompt.portal_links import resolve_portal_base_url
from grove.wiki.index import WikiIndex, WikiResult

__all__ = ["create_cellar_provider"]

_SECTION_LABEL = "cellar_knowledge"
_SECTION_HEADER = "## Cellar Knowledge"
# legacy-memory-retirement headroom: the cellar gets a larger budget than the
# JSONL provider (1000) — canonical pages are denser and fewer.
_CELLAR_TOKEN_BUDGET = 1500
_QUERY_K = 5
# TTL-gate the per-turn WikiIndex mtime scan (seconds). monotonic, not
# wall-clock — immune to clock adjustments.
_REFRESH_TTL = 60.0


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _format_result(result: WikiResult, base_url: Optional[str] = None) -> str:
    """Render one cellar page block. When ``base_url`` is set, a portal deep
    link follows the title so the agent hands the operator the exact rendered
    page — it can't reconstruct the hash-suffixed page_id otherwise.

    ``page_id`` follows the SAME contract as ``handle_cellar_page_detail`` in
    grove/api/portal.py: ``source_path`` (already relative to the pages root),
    ``.md`` stripped, posix slashes. Hash-routed (``#fragments``) so the link
    lands in the full styled shell.
    """
    header = f"### {result.title} ({result.source_type})"
    if base_url:
        page_id = Path(result.source_path).with_suffix("").as_posix()
        link = (
            f"📄 [View in portal]({base_url}/portal#fragments/cellar/pages/{page_id})"
        )
        return f"{header}\n{link}\n{result.snippet}"
    return f"{header}\n{result.snippet}"


def _synthetic_query(goals: List[Dict[str, Any]]) -> str:
    """Build a fallback query from active goal names (+ any ``keywords``).

    GATE-A: ``load_active_dock_goal_dicts`` exposes ``{slug, name, status,
    vector}`` — no keywords — so names are the primary signal; a dict carrying
    a ``keywords`` list (injected loaders / future shapes) contributes too.
    Terms are deduplicated preserving order; BM25 handles term frequency.
    """
    terms: List[str] = []
    for goal in goals:
        name = goal.get("name")
        if name:
            terms.append(str(name))
        for kw in goal.get("keywords", []) or []:
            terms.append(str(kw))
    seen: set = set()
    deduped = [t for t in terms if not (t in seen or seen.add(t))]
    return " ".join(deduped)


def create_cellar_provider(
    *,
    wiki_root: Optional[Path] = None,
    dock_goals_loader: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    index_factory: Optional[Callable[[], Any]] = None,
    token_budget: int = _CELLAR_TOKEN_BUDGET,
    k: int = _QUERY_K,
    ttl: float = _REFRESH_TTL,
    time_fn: Callable[[], float] = time.monotonic,
) -> Callable[[Dict[str, Any]], Optional[SectionResult]]:
    """Factory returning the cellar_knowledge SectionProvider.

    ``dock_goals_loader`` supplies the active Dock goals (default: the runtime
    Dock). ``index_factory`` builds the WikiIndex per call (default: a fresh
    ``WikiIndex(wiki_root)``); injectable for tests. ``ttl`` / ``time_fn`` gate
    the per-query mtime refresh.
    """
    if dock_goals_loader is None:
        from grove.memory.lifecycle import load_active_dock_goal_dicts
        dock_goals_loader = load_active_dock_goal_dicts
    if index_factory is None:
        index_factory = lambda: WikiIndex(wiki_root=wiki_root)  # noqa: E731

    state: Dict[str, Optional[float]] = {"last_refresh": None}

    def _provider(context: Dict[str, Any]) -> Optional[SectionResult]:
        goals = dock_goals_loader() or []
        # Highest-priority active goal (first in Dock order) → boost slug.
        slug = goals[0].get("slug") if goals else None

        user_message = (context.get("user_message") or "").strip()
        if user_message:
            query_text = user_message
        else:
            query_text = _synthetic_query(goals)
        if not query_text.strip():
            # No turn text AND no active goals — nothing to query (no-op).
            return None

        # TTL-gated freshness: refresh (stat the pages tree) only when the TTL
        # has elapsed since the last refreshing query.
        now = time_fn()
        last = state["last_refresh"]
        fresh = last is None or (now - last) >= ttl
        if fresh:
            state["last_refresh"] = now

        index = index_factory()
        results = index.query(query_text, k=k, dock_goal=slug, ensure_fresh=fresh)
        if not results:
            return None

        # K6 (D3) — per-tier ceiling. The compose context threads the routed
        # tier's ``cellar_context_ceiling`` (1000/1500/2000 for T1/T2/T3). SPEC-
        # commanded fallback to the constructor ``token_budget`` (default 1500)
        # when absent — construction-time / pre-route / no-dispatcher composes
        # thread no tier budget (backward compat during rollout, NOT a silent
        # degradation: the fallback is explicit per the K6 SPEC). The loader has
        # already validated any threaded value as a positive int.
        _ceiling = context.get("cellar_context_ceiling")
        effective_budget = (
            _ceiling if isinstance(_ceiling, int) and not isinstance(_ceiling, bool)
            and _ceiling > 0 else token_budget
        )

        # Resolve the portal base URL ONCE for this render and reuse it for
        # every page block. No args → the resolver reads the FULL sovereign
        # config itself (load_config); the prompt sub-block we'd otherwise have
        # lacks portal.base_url. Empty/unusable → skip the link lines rather
        # than emit a hostless URL (don't break the existing render).
        base_url = (resolve_portal_base_url() or "").strip() or None

        # Greedy fill in rank order; skip a block that would overflow the
        # budget (lowest-ranked drop first).
        lines: List[str] = []
        used = 0
        _dropped_blocks = 0
        _dropped_tokens = 0
        for result in results:
            block = _format_result(result, base_url=base_url)
            cost = _approx_tokens(block)
            if used + cost > effective_budget:
                # composer-observability-v1 (Wave 1, F2) — count the budget drop
                # with THIS provider's own _approx_tokens (the floor measure the
                # gate just used at ``cost``), so the ledger agrees with the
                # decision that dropped the block. The ``continue`` is PRESERVED.
                _dropped_blocks += 1
                _dropped_tokens += cost
                continue
            used += cost
            lines.append(block)

        # F2 sink: record drops on the compose-seeded channel BEFORE the
        # all-dropped early return, so a section that drops EVERY block still
        # reports its truncation. No-op outside compose() (sink absent).
        if _dropped_blocks:
            _sink = context.get("_composer_drops")
            if _sink is not None:
                _sink[_SECTION_LABEL] = {
                    "dropped_blocks": _dropped_blocks,
                    "dropped_tokens": _dropped_tokens,
                }

        if not lines:
            return None

        text = _SECTION_HEADER + "\n" + "\n\n".join(lines)
        return SectionResult(label=_SECTION_LABEL, text=text)

    return _provider

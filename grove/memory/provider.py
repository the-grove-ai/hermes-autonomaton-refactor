"""accumulated_domain_memory — the memory PromptComposer provider.

Surfaces the operator's accumulated memory into the system prompt at
``context:15``, mirroring the Sprint 37 contextual-preamble provider
(``grove/prompt/preamble.py``): a factory returns a pure closure of the
compose context that returns ``None`` (clean skip) when there is nothing to
serve.

Relevance: the Composer context carries ``session_id`` / ``pattern_hash`` /
``intent_class`` but NO ``user_message`` (SPEC amendment — verified against
the compose() call site). So this provider cannot keyword-filter against the
turn; it surfaces accumulated memory weighted toward the operator's active
Dock goals (query Dock-goal boost) and confidence, capped at a token budget.
Turn-keyword relevance is a Sprint B follow-on (it needs the message text
threaded into the compose context).

Freshness: the default store factory builds a MemoryStore per call, so
records approved out-of-process (the ``flywheel memory approve`` CLI) become
visible without an agent restart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from grove.memory.store import MemoryStore
from grove.prompt.composer import SectionResult

__all__ = ["create_memory_provider"]

_SECTION_LABEL = "accumulated_domain_memory"
_SECTION_HEADER = "## Accumulated Domain Memory"
_DEFAULT_TOKEN_BUDGET = 500

# turn-keyword-relevance-v1 — deterministic keyword extraction (no LLM). Common
# function/closed-class words + low-signal request verbs are filtered so a turn
# like "help me explain the deploy script" keys on {deploy, script}.
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "shall", "may", "might", "can",
    "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "during", "before", "after", "about",
    "between", "under", "above", "up", "down", "out", "off", "over",
    "then", "than", "so", "no", "not", "only", "very", "just",
    "also", "how", "what", "which", "who", "whom", "when", "where",
    "why", "this", "that", "these", "those", "it", "its", "i", "me",
    "my", "we", "our", "you", "your", "he", "she", "they", "them",
    "and", "but", "or", "if", "because", "while", "although",
    "help", "please", "think", "know", "want", "need", "like",
    "tell", "show", "explain", "describe", "give",
}


def _extract_keywords(text: str, max_keywords: int = 8) -> List[str]:
    """Deterministic keyword extraction: split, strip punctuation, filter
    stopwords + short tokens, cap at ``max_keywords``."""
    import re
    words = re.findall(r"[a-zA-Z0-9_.-]+", text.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 2][:max_keywords]


def _default_store_factory() -> MemoryStore:
    from hermes_constants import get_hermes_home
    return MemoryStore(base_dir=Path(get_hermes_home()))


def _format_line(record: Any) -> str:
    line = f"- [{record.entity_type}] {record.content} ({record.confidence:.2f})"
    if record.dock_goal_ref:
        line += f" [{record.dock_goal_ref}]"
    return line


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def create_memory_provider(
    store: Optional[MemoryStore] = None,
    *,
    store_factory: Optional[Callable[[], MemoryStore]] = None,
    dock_goals_loader: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    token_budget: int = _DEFAULT_TOKEN_BUDGET,
) -> Callable[[Dict[str, Any]], Optional[SectionResult]]:
    """Factory returning the accumulated_domain_memory SectionProvider.

    ``store`` pins a fixed store (tests); otherwise ``store_factory`` (or
    the default, which builds a fresh MemoryStore from the hermes home per
    call) supplies one. ``dock_goals_loader`` supplies the active Dock goals
    for the relevance boost (default: the runtime Dock).
    """
    if store_factory is None:
        store_factory = (lambda: store) if store is not None else _default_store_factory
    if dock_goals_loader is None:
        from grove.memory.lifecycle import load_active_dock_goal_dicts
        dock_goals_loader = load_active_dock_goal_dicts

    def _provider(context: Dict[str, Any]) -> Optional[SectionResult]:
        active_store = store_factory()
        slugs = [g.get("slug") for g in dock_goals_loader() if g.get("slug")]
        # turn-keyword-relevance-v1 — keyword-as-boost: turn keywords ELEVATE
        # matching records but Dock-goal memory still surfaces (zero-hit records
        # survive). Empty/absent user_message → keywords None → Dock-boost-only
        # (backward compatible).
        user_message = context.get("user_message", "")
        keywords = _extract_keywords(user_message) if user_message else None
        records = active_store.query(
            dock_goal_refs=slugs or None,
            keywords=keywords or None,
            require_keyword_match=False,
        )
        if not records:
            return None

        # Fill the budget in priority order (Dock-boost then confidence,
        # already applied by query); skip records that would overflow —
        # the lowest-priority/confidence records drop first.
        served: List[Any] = []
        lines: List[str] = []
        used = 0
        for record in records:
            line = _format_line(record)
            cost = _approx_tokens(line)
            if used + cost > token_budget:
                continue
            used += cost
            lines.append(line)
            served.append(record)

        if not served:
            return None

        # Fix 1 (telemetry debounce): collect served ids for a single
        # per-session batch flush at sweep — no per-turn event write.
        session_id = context.get("session_id") or ""
        for record in served:
            active_store.mark_accessed(session_id, record.id)

        text = _SECTION_HEADER + "\n" + "\n".join(lines)
        return SectionResult(label=_SECTION_LABEL, text=text)

    return _provider

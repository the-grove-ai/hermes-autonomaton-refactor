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

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from grove.memory.store import MemoryStore
from grove.prompt.composer import SectionResult

__all__ = ["create_memory_provider"]

logger = logging.getLogger(__name__)

_SECTION_LABEL = "accumulated_domain_memory"
_SECTION_HEADER = "## Accumulated Domain Memory"
# legacy-memory-retirement-v1: bumped 500 -> 1000. With the legacy
# MEMORY.md/USER.md sections retired (~1.3k tokens reclaimed), the Grove
# substrate gets half that headroom back as the sole memory voice.
_DEFAULT_TOKEN_BUDGET = 1000

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


def _orphaned_graduated_records(store: MemoryStore) -> List[Any]:
    """K6 (D5) — graduated records whose cellar page is missing on disk.

    For each graduated record, recompute the source-stable cellar hash exactly
    as ``grove/wiki/pipeline.py:_write_page`` does — the ``_MEMORY_SOURCE_PREFIX``
    and ``_HASH_LEN`` are IMPORTED, never re-spelled, so this can't silently
    desync from the writer (GUARD P2-d) — and check that
    ``pages/memory_graduated/*-<hash>.md`` exists under the wiki root.

    A missing page is a P1 signal: a graduated record has gone dark (suppressed
    from JSONL by the K4 closure AND absent from the cellar). Log it and return
    the record so the provider serves it from JSONL as the fail-safe. This does
    NOT halt boot (A4 / D5 refinement) — the warning is the signal; the JSONL
    serve is the safety net.
    """
    import hashlib

    # Resolve the wiki root the SAME way grove/wiki/pipeline.py:_write_page does
    # — through the pipeline's ``get_wiki_path`` binding (itself re-exported from
    # hermes_constants), NOT a fresh hermes_constants import. Same object in
    # production; the difference matters only under test, where the wiki-path
    # seam is monkeypatched on ``grove.wiki.pipeline`` — so the reconciliation
    # reads the exact cellar root the writer wrote to (SPEC amendment: import
    # source corrected from hermes_constants to grove.wiki.pipeline for
    # writer-parity / patch-consistency; the hash recipe is unchanged).
    from grove.wiki.pipeline import (
        _HASH_LEN,
        _MEMORY_SOURCE_PREFIX,
        get_wiki_path,
    )

    graduated_dir = Path(get_wiki_path()) / "pages" / "memory_graduated"
    dir_exists = graduated_dir.exists()
    orphans: List[Any] = []
    for rec in store.iter_graduated():
        source = _MEMORY_SOURCE_PREFIX + rec.id
        short_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()[:_HASH_LEN]
        matches = list(graduated_dir.glob(f"*-{short_hash}.md")) if dir_exists else []
        if not matches:
            logger.warning(
                "[grove.memory] graduated record %s has no cellar page — "
                "serving from JSONL as fallback",
                rec.id,
            )
            orphans.append(rec)
    return orphans


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

    # K6 (D5, A-Phase3 ruling) — graduated-record reconciliation, computed ONCE
    # on first compose (init) and cached, NOT per turn. The factory rebuilds a
    # fresh store per compose for active-record freshness; the set of orphaned
    # graduated records (status="graduated" but cellar page missing) is a
    # startup sanity check, so it is a snapshot taken on the first provider call.
    _orphans_state: Dict[str, Any] = {"computed": False, "records": []}

    def _provider(context: Dict[str, Any]) -> Optional[SectionResult]:
        active_store = store_factory()

        # First-call init: reconcile graduated records against the cellar. An
        # orphan (graduated, no cellar page) is served from JSONL here as the
        # D5 fail-safe — overriding the K4 suppression so the knowledge does not
        # go dark. The P1 warning fired once during this scan.
        if not _orphans_state["computed"]:
            _orphans_state["records"] = _orphaned_graduated_records(active_store)
            _orphans_state["computed"] = True
        orphans: List[Any] = _orphans_state["records"]

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
        # D5 merge — orphaned graduated records (cellar page missing) are served
        # from JSONL as the fail-safe. They cannot appear in query() output
        # (status != "active"), so dedup is defensive only. Appended after the
        # relevance-ranked active records: active turn-relevant memory leads;
        # orphans fill remaining budget (the P1 warning is the durable remedy
        # signal regardless of whether budget admits an orphan this turn).
        if orphans:
            _seen = {r.id for r in records}
            records = list(records) + [o for o in orphans if o.id not in _seen]
        if not records:
            return None

        # Fill the budget in priority order (Dock-boost then confidence,
        # already applied by query); skip records that would overflow —
        # the lowest-priority/confidence records drop first.
        served: List[Any] = []
        lines: List[str] = []
        used = 0
        _dropped_blocks = 0
        _dropped_tokens = 0
        for record in records:
            line = _format_line(record)
            cost = _approx_tokens(line)
            if used + cost > token_budget:
                # composer-observability-v1 (Wave 1, F2) — count the budget drop
                # with THIS provider's own _approx_tokens (the floor measure the
                # gate just used at ``cost``), so the ledger agrees with the
                # decision that dropped the record. The ``continue`` is PRESERVED.
                _dropped_blocks += 1
                _dropped_tokens += cost
                continue
            used += cost
            lines.append(line)
            served.append(record)

        # F2 sink: record drops on the compose-seeded channel BEFORE the
        # all-dropped early return, so a section that drops EVERY record still
        # reports its truncation. No-op outside compose() (sink absent).
        if _dropped_blocks:
            _sink = context.get("_composer_drops")
            if _sink is not None:
                _sink[_SECTION_LABEL] = {
                    "dropped_blocks": _dropped_blocks,
                    "dropped_tokens": _dropped_tokens,
                }

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

"""GRV-006 Compositional Preamble — feed-consumer prototype.

Sprint 37 — registers a single section provider against Sprint 36's
PromptComposer that surfaces historical intent records matching the
current turn's classification. Closes the feed-first loop:

    operator interaction
      → intent record (Sprint 28)
      → preamble query (this module)
      → model sees observed patterns
      → response shaped by prior outcomes
      → new intent record (next turn)

Per GRV-006 § II the rendered preamble is one section with three
labeled sub-blocks: Contextual Anchor, Historical State, Outcome
Signal. Per § III three operator-tunable knobs (top_k, recency_decay,
outcome_filter) govern selection. Per § IV the Sprint 28 IntentStore
is the sole source.

The provider returns ``None`` when the store is empty, when no records
match the current turn's classification, or when classification is
absent (cold-start turn or pre-Sprint-35 path). No silent failure —
empty preamble means empty data, not a swallowed error.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from grove.intent_store import IntentRecord, IntentStore, get_store
from grove.prompt.composer import SectionResult


_DEFAULT_TOP_K = 3
_DEFAULT_RECENCY_DECAY = 0.85
_DEFAULT_OUTCOME_FILTER: frozenset[str] = frozenset({"success", "correction", "drop"})
_SECTION_LABEL = "contextual_preamble"
_SECTION_HEADER = "## Compositional Context"
_PATTERN_HASH_PREFIX = 8


def _select_records(
    *,
    store: IntentStore,
    pattern_hash: Optional[str],
    intent_class: Optional[str],
    top_k: int,
    recency_decay: float,
    outcome_filter: Iterable[str],
) -> List[IntentRecord]:
    """Run the GRV-006 § II selection: pattern_hash-primary, intent_class-fallback.

    Walks ``latest_by_turn`` (provisional-write collapse — GRV-004's
    invariant), filters by outcome whitelist, sorts by timestamp DESC,
    and applies recency_decay as a per-position weight. The primary
    predicate is the current turn's ``pattern_hash``; if fewer than
    ``top_k`` records match, ``intent_class`` fallback fills the
    remaining slots without re-introducing pattern_hash duplicates.
    """
    if top_k <= 0:
        return []
    allowed = set(outcome_filter)

    matches_primary: List[IntentRecord] = []
    matches_fallback: List[IntentRecord] = []
    seen_turn_ids: set[str] = set()

    for record in store.latest_by_turn():
        if record.outcome not in allowed:
            continue
        if pattern_hash and record.pattern_hash == pattern_hash:
            matches_primary.append(record)
            seen_turn_ids.add(record.turn_id)
        elif intent_class and record.intent_class == intent_class:
            matches_fallback.append(record)

    matches_primary.sort(key=lambda r: r.timestamp, reverse=True)
    matches_fallback.sort(key=lambda r: r.timestamp, reverse=True)

    combined: List[IntentRecord] = []
    for record in matches_primary:
        if len(combined) >= top_k:
            break
        combined.append(record)
    for record in matches_fallback:
        if len(combined) >= top_k:
            break
        if record.turn_id in seen_turn_ids:
            continue
        combined.append(record)

    if recency_decay < 1.0 and combined:
        scored: List[Tuple[float, int, IntentRecord]] = []
        for position, record in enumerate(combined):
            score = recency_decay ** position
            scored.append((score, position, record))
        scored.sort(key=lambda t: (-t[0], t[1]))
        combined = [t[2] for t in scored]

    return combined


def _render(
    *,
    intent_class: str,
    pattern_hash: str,
    records: List[IntentRecord],
) -> str:
    """Render the three-block preamble per GRV-006 § II."""
    anchor_hash = (pattern_hash or "")[:_PATTERN_HASH_PREFIX] or "—"
    anchor = (
        "### Contextual Anchor\n"
        f"Intent class: {intent_class or 'unknown'} · "
        f"Pattern: {anchor_hash}"
    )

    history_rows: List[str] = []
    outcomes_seen: Dict[str, int] = {}
    for record in records:
        ts_short = (record.timestamp or "")[:16].replace("T", " ")
        stem = (record.user_message_stem or "").strip()
        if len(stem) > 60:
            stem = stem[:57] + "..."
        history_rows.append(
            f'- {ts_short} — "{stem}" — outcome: {record.outcome}'
        )
        outcomes_seen[record.outcome] = outcomes_seen.get(record.outcome, 0) + 1

    historical = "### Historical State\n" + "\n".join(history_rows)

    signal_parts = [
        f"{outcome}: {outcomes_seen.get(outcome, 0)}"
        for outcome in ("success", "correction", "drop")
    ]
    signal = "### Outcome Signal\n" + " · ".join(signal_parts)

    return "\n\n".join([_SECTION_HEADER, anchor, historical, signal])


def build_contextual_preamble_provider(
    *,
    store_factory: Callable[[], IntentStore] = get_store,
    top_k: int = _DEFAULT_TOP_K,
    recency_decay: float = _DEFAULT_RECENCY_DECAY,
    outcome_filter: Iterable[str] = _DEFAULT_OUTCOME_FILTER,
) -> Callable[[Dict[str, Any]], Optional[SectionResult]]:
    """Factory returning the contextual-preamble provider.

    The factory pattern lets the Dispatcher inject the store factory at
    composer-build time (tests pass a tmp-path store; production uses
    the module-level singleton). Knobs default to GRV-006 § III
    defaults; the composer's section config overrides them via the
    ``context`` dict the composer passes to the provider.
    """
    frozen_outcome_filter = frozenset(outcome_filter)

    def _provider(context: Dict[str, Any]) -> Optional[SectionResult]:
        pattern_hash = context.get("pattern_hash") or ""
        intent_class = context.get("intent_class") or ""
        if not pattern_hash and not intent_class:
            return None

        store = store_factory()
        records = _select_records(
            store=store,
            pattern_hash=pattern_hash,
            intent_class=intent_class,
            top_k=top_k,
            recency_decay=recency_decay,
            outcome_filter=frozen_outcome_filter,
        )
        if not records:
            return None

        text = _render(
            intent_class=intent_class,
            pattern_hash=pattern_hash,
            records=records,
        )
        return SectionResult(label=_SECTION_LABEL, text=text)

    return _provider

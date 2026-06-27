"""Deterministic push-relevance gate for crystallization proposals
(crystallization-cadence-v1, Gap 2).

A staged memory proposal earns its proactive push ("Shop floor note —") on a
turn ONLY when its ``entity_type`` is relevant to the turn's ``intent_class``,
per the declarative table in ``config/crystallization_relevance.yaml`` — OR
when the proposal is tagged to a Dock goal active this turn (Dock override).

Pure + deterministic: a table lookup, no LLM call. Repo-config only (no
``~/.grove`` override — structural tuning tied to the intent taxonomy).

Fallback discipline (maximal-fallback = "don't spam"):
  * unknown / null ``intent_class``      -> suppress (return False)
  * ``intent_class`` absent from the map -> suppress
  * ``entity_type`` not listed for it    -> suppress
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Optional

_RELEVANCE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "config"
    / "crystallization_relevance.yaml"
)

# Cache: the parsed ``{intent_class: frozenset(entity_types)}`` map. Loaded once.
_relevance_cache: Optional[Dict[str, frozenset]] = None


def _load_relevance() -> Dict[str, frozenset]:
    """Parse and cache the relevance map. A missing/malformed file yields an
    empty map — which suppresses ALL pushes (fail toward silence, never toward
    spam). Fail-loud only structurally: a present-but-unparseable file logs.
    """
    global _relevance_cache
    if _relevance_cache is None:
        import yaml

        raw: Dict[str, Any] = {}
        try:
            if _RELEVANCE_PATH.exists():
                loaded = yaml.safe_load(_RELEVANCE_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw = loaded.get("relevance") or {}
        except Exception:  # noqa: BLE001 — degrade to suppress-all, never raise
            raw = {}
        _relevance_cache = {
            str(intent): frozenset(str(e) for e in (entities or []))
            for intent, entities in raw.items()
            if isinstance(entities, (list, tuple, set))
        }
    return _relevance_cache


def reset_relevance_cache() -> None:
    """Drop the cached map so the next call re-reads disk (tests / hot edits)."""
    global _relevance_cache
    _relevance_cache = None


def is_push_relevant(
    intent_class: Optional[str],
    entity_type: Optional[str],
    *,
    goal_ref: Optional[str] = None,
    active_goal_ids: Optional[Iterable[str]] = None,
) -> bool:
    """Return True when a crystallization proposal of ``entity_type`` may push
    on a turn classified ``intent_class``.

    Dock override: if ``goal_ref`` names a goal in ``active_goal_ids``, the
    proposal is always relevant (Dock-tagged insight is always welcome),
    regardless of the intent map.
    """
    # Dock override — a proposal tied to a goal active this turn always pushes.
    if goal_ref and active_goal_ids and goal_ref in set(active_goal_ids):
        return True

    if not intent_class or not entity_type:
        return False  # maximal fallback: unknown turn → don't spam
    eligible = _load_relevance().get(intent_class)
    if not eligible:
        return False  # intent absent from the map → suppress all
    return entity_type in eligible

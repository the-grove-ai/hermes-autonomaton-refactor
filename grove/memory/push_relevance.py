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
    goal_alignment: Optional[str] = None,
    engaged_goal_id: Optional[str] = None,
) -> bool:
    """Return True when a crystallization proposal of ``entity_type`` may push
    on a turn classified ``intent_class``.

    Dock override (crystallization-cadence-v1.1): a proposal tagged to a Dock
    goal overrides the intent gate ONLY when the turn is actually ENGAGING that
    goal — all four conditions of the locked spec must hold:

      1. ``goal_ref`` is present on the proposal,
      2. ``goal_ref`` is an ACTIVE goal (``goal_ref in active_goal_ids``),
      3. the turn is goal-aligned (``goal_alignment`` is ``direct``/``indirect``,
         not ``orthogonal``/``no_goals_set``),
      4. the turn's engaged goal IS ``goal_ref`` (``engaged_goal_id == goal_ref``),
         not merely some other active goal.

    The v1 bug checked only (1)+(2), so an always-active umbrella goal
    (``hermes-autonomaton``) overrode the gate on EVERY turn — including a
    ``scheduling`` "what's on my calendar" turn. Condition (3) alone kills that
    case (orthogonal alignment); (4) prevents a goal-A turn from surfacing a
    goal-B proposal.
    """
    if (
        goal_ref
        and active_goal_ids
        and goal_ref in set(active_goal_ids)
        and goal_alignment in ("direct", "indirect")
        and engaged_goal_id == goal_ref
    ):
        return True

    if not intent_class or not entity_type:
        return False  # maximal fallback: unknown turn → don't spam
    eligible = _load_relevance().get(intent_class)
    if not eligible:
        return False  # intent absent from the map → suppress all
    return entity_type in eligible

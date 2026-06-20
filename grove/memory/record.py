"""MemoryRecord — the projected state compiled from the event log.

A MemoryRecord is NOT stored in the log. It is the materialized view the
:class:`~grove.memory.store.MemoryStore` compiles by folding the event stream
(``MemoryCreated`` / ``MemorySuperseded`` / ``MemoryDeprecated`` /
``MemoryAccessed``). The log is the source of truth; this is the projection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

__all__ = ["MemoryRecord", "DECAY_RATES", "decay_rate_for"]


# Entity-type decay rates (constants, not config — Sprint A SPEC).
#   ProjectState        — 0.95 daily time-based decay
#   OperatorPreference  — 1.0  no time decay (access-based only, Sprint B)
#   ArchitecturalRule   — 1.0  zero decay, supersede only
#   DomainFact          — 1.0  zero decay, supersede only
DECAY_RATES: Dict[str, float] = {
    "ProjectState": 0.95,
    "OperatorPreference": 1.0,
    "ArchitecturalRule": 1.0,
    "DomainFact": 1.0,
}


def decay_rate_for(entity_type: str) -> float:
    """Return the daily decay rate for ``entity_type``.

    Raises ``ValueError`` for an unknown entity type. The closed set is
    enforced here so a malformed entity_type cannot silently default to
    "no decay" (Architectural Prime Directive: fail loud, no fallbacks).
    """
    try:
        return DECAY_RATES[entity_type]
    except KeyError:
        raise ValueError(
            f"unknown entity_type {entity_type!r}; "
            f"expected one of {sorted(DECAY_RATES)}"
        ) from None


@dataclass
class MemoryRecord:
    """The compiled, current state of one memory record.

    Mutable by design: the store folds events into these and applies
    decay in place on the live index. It is never serialized to the event
    log — only to the derived ``memory_index.json`` projection cache.
    """

    id: str
    entity_type: str
    content: str
    confidence: float
    dock_goal_ref: Optional[str]
    sources: List[Dict] = field(default_factory=list)
    status: str = "active"              # active | superseded | deprecated
    supersedes: Optional[str] = None
    created_at: str = ""
    last_accessed: Optional[str] = None
    access_count: int = 0
    decay_rate: float = 1.0            # entity-type-derived

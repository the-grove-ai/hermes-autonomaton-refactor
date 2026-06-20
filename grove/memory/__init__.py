"""Grove memory substrate — memory-substrate-v1 (epic memory-lifecycle-engine-v1).

An LLM-Wiki memory substrate for the grove-autonomaton. Event-sourced: an
append-only JSONL log of :class:`MemoryEvent`s is the source of truth, and a
projected index of :class:`MemoryRecord`s is compiled from it (R4 invariant —
the projection is reconstructible from the log alone).

Mirrors the IntentStore design (``grove/intent_store.py``): single-file JSONL,
frozen-dataclass events, in-process lock-guarded appends.
"""

from grove.memory.events import (
    MemoryAccessed,
    MemoryCreated,
    MemoryDeprecated,
    MemorySuperseded,
    new_event_id,
    new_record_id,
)
from grove.memory.detector import ContextPersistenceDetector
from grove.memory.digest import MemoryProposalHandler, run_digest
from grove.memory.lifecycle import (
    dormant_session_ids,
    load_active_dock_goal_dicts,
    run_memory_extraction,
)
from grove.memory.provider import create_memory_provider
from grove.memory.record import DECAY_RATES, MemoryRecord, decay_rate_for
from grove.memory.store import MemoryStore
from grove.memory.transcript_filter import filter_transcript_for_extraction

__all__ = [
    "MemoryAccessed",
    "MemoryCreated",
    "MemoryDeprecated",
    "MemorySuperseded",
    "MemoryRecord",
    "MemoryStore",
    "ContextPersistenceDetector",
    "create_memory_provider",
    "MemoryProposalHandler",
    "run_digest",
    "dormant_session_ids",
    "run_memory_extraction",
    "load_active_dock_goal_dicts",
    "filter_transcript_for_extraction",
    "DECAY_RATES",
    "decay_rate_for",
    "new_event_id",
    "new_record_id",
]

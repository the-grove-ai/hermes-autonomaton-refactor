"""MemoryEvent schema — the four event types of the memory event log.

Event-sourced design (GATE-B Gemini ratification, 2026-06-20): the memory
graph is never mutated in place. Every change is an append-only event; the
:class:`~grove.memory.record.MemoryRecord` projection is compiled from the
event stream (see :mod:`grove.memory.store`).

Frozen dataclasses, following the ``IntentRecord`` pattern in
``grove/intent_store.py`` — hashable, never mutated after construction.
Identifiers are minted with :func:`new_event_id` / :func:`new_record_id`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

__all__ = [
    "MemoryCreated",
    "MemorySuperseded",
    "MemoryDeprecated",
    "MemoryAccessed",
    "MemoryGraduated",
    "MemoryEvent",
    "new_event_id",
    "new_record_id",
]


def new_event_id() -> str:
    """Mint an event id: ``"evt_" + first 8 chars of a uuid4``."""
    return "evt_" + uuid.uuid4().hex[:8]


def new_record_id() -> str:
    """Mint a record id: ``"mem_" + first 8 chars of a uuid4``."""
    return "mem_" + uuid.uuid4().hex[:8]


@dataclass(frozen=True)
class MemoryCreated:
    """A new memory record enters the graph."""

    event_id: str                       # "evt_" + uuid8
    timestamp: str                      # ISO-8601 UTC
    record_id: str                      # "mem_" + uuid8
    entity_type: str                    # DomainFact|OperatorPreference|ProjectState|ArchitecturalRule
    content: str                        # standalone statement
    confidence: float                   # 0.0-1.0
    dock_goal_ref: Optional[str]        # goal slug or None
    sources: List[Dict]                 # [{session_id, turn_id}]
    supersedes: Optional[str]           # mem_id of old record (None for a fresh create)


@dataclass(frozen=True)
class MemorySuperseded:
    """A new record replaces an existing one.

    Same field surface as :class:`MemoryCreated`, but ``supersedes`` is
    required — it names the ``record_id`` of the record this one retires.
    """

    event_id: str                       # "evt_" + uuid8
    timestamp: str                      # ISO-8601 UTC
    record_id: str                      # the NEW record's id
    entity_type: str
    content: str
    confidence: float
    dock_goal_ref: Optional[str]
    sources: List[Dict]
    supersedes: str                     # required: mem_id of the retired record


@dataclass(frozen=True)
class MemoryDeprecated:
    """A record is retired without a replacement."""

    event_id: str
    timestamp: str
    record_id: str
    reason: str


@dataclass(frozen=True)
class MemoryAccessed:
    """A record was served into prompt context (system telemetry)."""

    event_id: str
    timestamp: str
    record_id: str
    session_id: str
    context: str                        # query keywords that triggered the access


@dataclass(frozen=True)
class MemoryGraduated:
    """A record is graduated into the permanent wiki cellar.

    memory-cellar-graduation-v1: graduation projects the record into the wiki
    as a permanent page. It does NOT retire the record — the dual-serve
    invariant keeps ``status == "active"`` so the record is still served via
    the JSONL/query path. The fold records only ``graduated_at`` (suppression
    is deferred to K4). Minimal identity shape: ``record_id`` + ``timestamp``.
    """

    event_id: str
    timestamp: str
    record_id: str


MemoryEvent = Union[
    MemoryCreated,
    MemorySuperseded,
    MemoryDeprecated,
    MemoryAccessed,
    MemoryGraduated,
]

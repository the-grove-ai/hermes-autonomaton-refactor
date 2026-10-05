"""turn-identity-v1 — a globally unique, time-ordered id for every turn.

Pipeline stage: Telemetry. ``session#N`` is the human-readable ordinal within a
session; it depends on session-id uniqueness and is not unique across nodes.
The id minted here is the turn's permanent identity: the link key that tool-call
records carry, and the record id a provenance chain covers.

It is assigned ONCE, when the turn starts, before any record is written, and is
never derived from content — records are written in stages, and an identity
must not change as they fill in.

UUIDv7 (RFC 9562): 48-bit Unix-millisecond timestamp, then random bits, so ids
sort by creation time. Implemented here because the standard library only
gained ``uuid.uuid7`` in Python 3.14.
"""

from __future__ import annotations

import os
import time
import uuid

__all__ = ["new_turn_uid"]


def new_turn_uid() -> str:
    """Return a fresh UUIDv7 as its canonical 36-character string."""
    ms = time.time_ns() // 1_000_000
    raw = bytearray(ms.to_bytes(6, "big") + os.urandom(10))
    raw[6] = (raw[6] & 0x0F) | 0x70   # version 7
    raw[8] = (raw[8] & 0x3F) | 0x80   # RFC 4122 variant
    return str(uuid.UUID(bytes=bytes(raw)))

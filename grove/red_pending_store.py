"""Session-volatile store for pending RED governance proposals.

propose-approve-deadlock-v1 Phase 1a (GOVERNANCE CORE).

A ``.env`` (RED) ``propose_governance_change`` is the operator's request to
persist a secret. The dispatcher classifies ``.env`` RED, but RED has no
store-and-resume path — it hard-cancels (Phase 0 verdict: STRUCTURAL bug). This
module is the CORE half of the fix: a **dispatcher-owned, in-memory** map of
pending RED proposals that a later operator approval mints and executes.

Confinement (deliberate):
  * IN-MEMORY ONLY. Never written to disk. A gateway restart drops every pending
    entry — a durable, restart-surviving store is NET-NEW infrastructure that
    Phase 0 (Task 2) flagged as scope-material and DEFERRED to a later gate. 1a
    is the volatile, same-session bridge.
  * NOT A TOOL. This object is held by the Dispatcher and is unreachable by any
    model-invoked tool (write_file/patch/shell only ever cross the registry;
    this class is never registered). The secret ``.env`` payload therefore never
    passes through an agent-readable surface.

The map is keyed by ``proposal_id`` = ``sha256(canonical .env content)`` so the
same proposed body is idempotent and the id doubles as the integrity anchor.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

# The proposal ``type`` written (opaque) to the agent-reachable queue as the
# 1b portal-render bridge. Namespaced so existing flywheel/portal consumers
# render it generically and never mis-route it to a non-RED approve handler.
RED_PENDING_PROPOSAL_TYPE = "governance_env_pending"

# Match ``KEY=...`` env lines to name the affected keys (names are NOT secret;
# values are) for the masked operator-facing description.
_ENV_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)


def content_proposal_id(content: str) -> str:
    """``sha256`` of the canonical ``.env`` content — the map key + integrity anchor."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def extract_env_keys(content: str) -> List[str]:
    """Key NAMES present in a proposed ``.env`` body (values excluded).

    Names are safe to surface (the operator authored them); values never are.
    Order-preserving, de-duplicated.
    """
    seen: Dict[str, None] = {}
    for m in _ENV_KEY_RE.finditer(content or ""):
        seen.setdefault(m.group(1), None)
    return list(seen.keys())


def masked_env_description(target_file: str, key_names: List[str]) -> str:
    """Operator-facing description — names the target + keys, MASKS every value."""
    where = target_file or ".env"
    if key_names:
        shown = ", ".join(key_names[:6])
        more = "" if len(key_names) <= 6 else f" (+{len(key_names) - 6} more)"
        return f"Persist credential(s) to {where}: {shown}{more} — values hidden."
    return f"Persist credential(s) to {where} — values hidden."


@dataclass
class PendingRedProposal:
    """One pending RED ``.env`` proposal. Holds the secret payload IN-MEMORY only."""

    proposal_id: str          # sha256(content) — key + integrity anchor
    target_file: str          # e.g. ~/.grove/.env — NEVER surfaced to agent/queue
    content: str              # the .env body (SECRET; in-memory only)
    content_sha256: str       # == proposal_id; re-verified at execute (TOCTOU)
    effect_signature: str     # canonical_effect_signature(tool, args) — mint anchor
    rationale: str
    description: str          # masked operator-facing copy (no values)
    created_at: str
    zone: str = "red"


class RedPendingStore:
    """Dispatcher-owned, session-volatile map of pending RED proposals.

    Not a tool; no agent surface reaches it. Thread-safe (the gateway may touch
    it from the turn thread and an approval thread).
    """

    def __init__(self) -> None:
        self._by_id: Dict[str, PendingRedProposal] = {}
        self._lock = threading.Lock()

    def put(self, entry: PendingRedProposal) -> None:
        with self._lock:
            self._by_id[entry.proposal_id] = entry

    def get(self, proposal_id: str) -> Optional[PendingRedProposal]:
        with self._lock:
            return self._by_id.get(proposal_id)

    def pop(self, proposal_id: str) -> Optional[PendingRedProposal]:
        """Remove and return an entry (used on successful execute / abort)."""
        with self._lock:
            return self._by_id.pop(proposal_id, None)

    def masked_description(self, proposal_id: str) -> Optional[str]:
        """The masked operator-facing description for a proposal, or None."""
        entry = self.get(proposal_id)
        return entry.description if entry else None

    def has(self, proposal_id: str) -> bool:
        """True iff a live payload is held for *proposal_id*.

        propose-approve-deadlock-v1 Phase 1b-i (Step 3) — the portal render calls
        this (via ``request.app["red_pending_store"]``) to distinguish a live
        pending proposal from an ORPHAN: the durable queue row persists across a
        restart, but this in-memory payload does not, so a metadata row whose
        ``has()`` is False renders EXPIRED (1b-ii) rather than a dead approve.
        """
        with self._lock:
            return proposal_id in self._by_id

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_id)


# ── Process-level singleton (propose-approve-deadlock-v1 Phase 1b-i, Step 1) ──
# The store MUST survive across turns and requests: the gateway rebuilds the
# Dispatcher per turn (ThreadPoolExecutor) and the portal approve is a SEPARATE
# request, so the 1a per-Dispatcher instance was GC'd before approve — the
# proposal was unreachable. This shared process singleton (mirrors
# grove.grants.get_grant_store) is the ONE reachable, lifetime-scoped store both
# the store-on-propose (any Dispatcher) and the portal approve handler use. Still
# in-memory (restart drops it), still not a tool, still no agent surface reaches
# it. Thread-safe: the gateway touches it from executor threads AND the loop.
_STORE: Optional[RedPendingStore] = None


def get_red_pending_store() -> RedPendingStore:
    """Return the process-level RedPendingStore singleton (creates on first call)."""
    global _STORE
    if _STORE is None:
        _STORE = RedPendingStore()
    return _STORE

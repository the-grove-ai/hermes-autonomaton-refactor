"""Phase 3.1 — proactive surfacing of memory proposals in the turn response.

The picker/renderer behind ``AIAgent._append_memory_offer``. It is a SEPARATE
system from the routing-proposal push (``run_agent._append_pending_offer`` /
``flywheel_cli.compose_offering``): memory proposals live in their own
``memory_proposals.jsonl`` with a different record shape, are born from PRIOR
dormant sessions (so the routing "current-session-only" eligibility rule does
NOT apply — any unshown pending proposal is eligible), and approve/reject
stays the Phase 3 CLI (conversational approval is Phase 3.2).

Reuses the CLI reader (``cli._pending``), the CLI short-id
(``memory_proposal_short_id``), and the Phase 3 renderer
(``MemoryProposalHandler.summary_renderer``) — no new parser, no new id
scheme, no new render path.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional, Set, Tuple

logger = logging.getLogger(__name__)

__all__ = ["select_memory_push_note"]

_PUSH_TEMPLATE = (
    "\n\n---\n"
    "Shop floor note: I crystallized a memory from a recent session — "
    "{summary}\n"
    "Say `flywheel memory approve {short_id}` to commit it, or "
    "`flywheel memory reject {short_id}` to dismiss."
)


def select_memory_push_note(
    *, shown_ids: Set[str], base_dir: Any,
) -> Optional[Tuple[str, str]]:
    """Pick the highest-confidence unshown pending memory proposal and render
    its push note.

    Returns ``(short_id, note)`` or ``None`` when there is nothing fresh to
    surface (no proposals file, no pending records, or every pending proposal
    already shown this session).
    """
    from grove.memory.cli import _pending, memory_proposal_short_id

    base = Path(base_dir)
    candidates = []  # (confidence, short_id, proposal)
    for _full_id, record in _pending(base):
        proposal = record.get("proposal")
        if not isinstance(proposal, dict):
            continue
        short_id = memory_proposal_short_id(proposal)
        if short_id in shown_ids:
            continue
        raw_conf = proposal.get("proposed_record", {}).get("confidence", 0.0)
        try:
            confidence = float(raw_conf)
        except (TypeError, ValueError):
            confidence = 0.0
        candidates.append((confidence, short_id, proposal))

    if not candidates:
        return None

    # Highest confidence first (highest-value knowledge surfaces first).
    candidates.sort(key=lambda c: c[0], reverse=True)
    _confidence, short_id, proposal = candidates[0]

    # summary_renderer reads only the proposal dict (store-independent), but
    # it is an instance method — construct the handler with a store bound to
    # the same home. Built only on a surfacing turn (rare: once per proposal
    # per session, since the short_id then enters the shown-set).
    from grove.memory.digest import MemoryProposalHandler
    from grove.memory.store import MemoryStore

    summary = MemoryProposalHandler(
        MemoryStore(base_dir=base)
    ).summary_renderer(proposal)
    note = _PUSH_TEMPLATE.format(summary=summary, short_id=short_id)
    return short_id, note

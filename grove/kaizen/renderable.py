"""KaizenRenderable — the one shape every proposal type presents to the
unified Kaizen surface (kaizen-proposal-surface-unification-v1).

A renderable exposes the minimum the unified renderer + push surface need:
``type`` (registry key), ``short_id`` (shown-set dedup key), ``sort_key``
(within-priority tiebreak), and ``is_push_eligible`` (per-type push window,
encapsulated so the push method has no per-type if/else). Future proposal
types implement this protocol and inherit the unified surface — they do NOT
add a bespoke push method or renderer.

RoutingProposal satisfies this protocol directly (frozen dataclasses allow
properties/methods). Memory proposals are dicts, so MemoryProposalRenderable
adapts a memory_proposals.jsonl record without changing the underlying shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class KaizenRenderable(Protocol):
    @property
    def type(self) -> str: ...

    @property
    def short_id(self) -> str: ...

    @property
    def sort_key(self) -> float: ...

    def is_push_eligible(self, session_start: Optional[datetime]) -> bool: ...

    def push_body(self, core: str) -> str:
        """The type-specific middle clause of the conversational push note
        (the shared 'Shop floor note —' frame + approve/dismiss tail wrap it).
        Each type phrases its own offer; routing keeps 'I noticed I could …',
        memory speaks as a crystallized insight — never 'I noticed I could'."""
        ...


class MemoryProposalRenderable:
    """Adapt a ``memory_proposals.jsonl`` record to :class:`KaizenRenderable`.

    Carries the raw record; ``proposal_dict`` exposes the inner proposal the
    memory summary renderer reads. Eligibility: memory proposals are born from
    PRIOR dormant sessions (the Phase 3.1 finding), so the routing
    current-session window deliberately does NOT apply — any pending proposal
    is push-eligible (the shown-set handles one-at-a-time).
    """

    def __init__(self, record: Dict[str, Any]) -> None:
        self._record = record

    @property
    def type(self) -> str:
        return "memory_context"

    @property
    def proposal_dict(self) -> Dict[str, Any]:
        return self._record.get("proposal") or {}

    @property
    def short_id(self) -> str:
        from grove.memory.cli import memory_proposal_short_id
        return memory_proposal_short_id(self.proposal_dict)

    @property
    def sort_key(self) -> float:
        proposal = self.proposal_dict
        if proposal.get("action") == "graduate":
            # Graduate proposals carry confidence at the top level (no
            # proposed_record), so rank by the record's real confidence.
            raw = proposal.get("confidence", 0.0)
        else:
            raw = proposal.get("proposed_record", {}).get("confidence", 0.0)
        try:
            confidence = float(raw)
        except (TypeError, ValueError):
            confidence = 0.0
        # Negated: highest confidence sorts first under an ascending sort.
        return -confidence

    def is_push_eligible(self, session_start: Optional[datetime] = None) -> bool:
        return self._record.get("status") == "pending"

    def push_body(self, core: str) -> str:
        # Memory-specific voice — a crystallized insight, NOT "I noticed I could".
        # Deprecation inverts the frame: this is governed forgetting, not capture.
        # Graduation is promotion: a proven memory ascends to the permanent cellar.
        action = self.proposal_dict.get("action")
        if action == "deprecate":
            return f"I'm recommending we retire a stale memory — {core}"
        if action == "graduate":
            return f"I'm graduating a proven memory to the permanent cellar — {core}"
        return f"I crystallized a domain insight — {core}"

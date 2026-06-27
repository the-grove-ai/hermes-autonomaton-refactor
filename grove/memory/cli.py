"""`flywheel memory` operator CLI — the pull-review approval surface.

Operator-ratified Option 3 (Phase 3): review staged memory_context
proposals on demand via list / show / approve / reject, mirroring the
existing `flywheel` proposal CLI. Every approve/reject routes through
:func:`grove.memory.digest.run_digest`, so the store mutation and the
kaizen_disposition ledger event are identical to any other surface.

Async conversational push (surfacing pending proposals in a turn response)
is the eventual UX and is tracked as the Phase 3.1 follow-on; it is not this
module.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from grove.memory.digest import MemoryProposalHandler, run_digest
from grove.memory.store import MemoryStore

__all__ = [
    "cli_memory_list",
    "cli_memory_show",
    "cli_memory_approve",
    "cli_memory_reject",
    "memory_proposal_short_id",
]

_SHORT_ID_LEN = 12
_MIN_PARTIAL = 6


def _base(base_dir: Any) -> Path:
    if base_dir is not None:
        return Path(base_dir)
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _proposals_path(base: Path) -> Path:
    return base / "memory_proposals.jsonl"


def _full_id(proposal: Dict[str, Any]) -> str:
    """Content-addressable selector id (hex) for a memory proposal."""
    seed = json.dumps(proposal, sort_keys=True, default=str)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def memory_proposal_short_id(proposal: Dict[str, Any]) -> str:
    """The short selector id operators type (first 12 hex chars)."""
    return _full_id(proposal)[:_SHORT_ID_LEN]


def _pending(base: Path) -> List[Tuple[str, Dict[str, Any]]]:
    """Pending (full_id, record) pairs from the proposals file."""
    path = _proposals_path(base)
    if not path.exists():
        return []
    out: List[Tuple[str, Dict[str, Any]]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("status") == "pending" and "proposal" in rec:
            out.append((_full_id(rec["proposal"]), rec))
    return out


def _resolve(base: Path, partial: str) -> Tuple[Optional[str], str]:
    """Resolve a partial id to a unique full id, or return an error message.

    Returns ``(full_id, "")`` on a unique match, else ``(None, message)``.
    """
    partial = (partial or "").strip().lower()
    if len(partial) < _MIN_PARTIAL:
        return None, (
            f"{partial!r} is too short; use at least {_MIN_PARTIAL} characters."
        )
    # Match RECORDS, not unique ids: two pending records with identical
    # content collide on the same content-addressable id. Counting records
    # makes that pair ambiguous (refuse) rather than silently approving both
    # — run_digest's decide matches by id and would otherwise double-apply.
    matches = [(fid, rec) for fid, rec in _pending(base) if fid.startswith(partial)]
    if not matches:
        return None, f"No pending memory proposal matches {partial!r}."
    if len(matches) > 1:
        return None, (
            f"{partial!r} matches {len(matches)} proposals; be more specific."
        )
    return matches[0][0], ""


def cli_memory_list(*, base_dir: Any = None) -> int:
    base = _base(base_dir)
    pending = _pending(base)
    if not pending:
        print("No pending memory proposals.")
        return 0
    handler = MemoryProposalHandler(MemoryStore(base_dir=base))
    print(f"{len(pending)} pending memory proposal(s):")
    for full_id, rec in pending:
        print(f"  {full_id[:_SHORT_ID_LEN]}  {handler.summary_renderer(rec['proposal'])}")
    return 0


def cli_memory_show(partial_id: str, *, base_dir: Any = None) -> int:
    base = _base(base_dir)
    full_id, err = _resolve(base, partial_id)
    if full_id is None:
        print(err, file=sys.stderr)
        return 1
    rec = next(r for fid, r in _pending(base) if fid == full_id)
    proposal = rec["proposal"]
    pr = proposal.get("proposed_record", {})
    print(f"id:            {full_id[:_SHORT_ID_LEN]}")
    print(f"action:        {proposal.get('action')}")
    print(f"entity_type:   {pr.get('entity_type')}")
    print(f"content:       {pr.get('content')}")
    print(f"confidence:    {pr.get('confidence')}")
    print(f"justification: {pr.get('justification')}")
    if proposal.get("dock_goal_ref"):
        print(f"dock_goal_ref: {proposal['dock_goal_ref']}")
    if proposal.get("target_id"):
        print(f"supersedes:    {proposal['target_id']}")
    print(f"session:       {rec.get('session_id')}")
    return 0


def cli_memory_approve(
    partial_id: str, *, base_dir: Any = None, ledger_dir: Any = None,
) -> int:
    base = _base(base_dir)
    full_id, err = _resolve(base, partial_id)
    if full_id is None:
        print(err, file=sys.stderr)
        return 1
    store = MemoryStore(base_dir=base)

    def decide(_summary: str, proposal: Dict[str, Any]) -> str:
        return "approve" if _full_id(proposal) == full_id else "defer"

    counts = run_digest(
        store=store, proposals_path=_proposals_path(base),
        decide=decide, ledger_dir=ledger_dir,
    )
    if counts["approved"]:
        print(f"Approved memory proposal {full_id[:_SHORT_ID_LEN]} — "
              f"applied to the memory store.")
        return 0
    print(f"Memory proposal {full_id[:_SHORT_ID_LEN]} was not applied.",
          file=sys.stderr)
    return 1


def cli_memory_reject(
    partial_id: str, *, reason: Optional[str] = None,
    base_dir: Any = None, ledger_dir: Any = None,
) -> int:
    base = _base(base_dir)
    full_id, err = _resolve(base, partial_id)
    if full_id is None:
        print(err, file=sys.stderr)
        return 1
    store = MemoryStore(base_dir=base)

    def decide(_summary: str, proposal: Dict[str, Any]) -> str:
        return "reject" if _full_id(proposal) == full_id else "defer"

    counts = run_digest(
        store=store, proposals_path=_proposals_path(base),
        decide=decide, ledger_dir=ledger_dir,
    )
    if counts["rejected"]:
        suffix = f" ({reason})" if reason else ""
        print(f"Rejected memory proposal {full_id[:_SHORT_ID_LEN]}{suffix}.")
        return 0
    print(f"Memory proposal {full_id[:_SHORT_ID_LEN]} was not rejected.",
          file=sys.stderr)
    return 1


def cli_memory_dismiss(
    partial_id: str, *, base_dir: Any = None, ledger_dir: Any = None,
) -> int:
    """Soft-dismiss a memory proposal (crystallization-cadence-v1, Gap 3).

    Flips the proposal to ``status="dismissed"`` via the shared digest engine:
    it loses push eligibility and stays in the backlog, but — unlike reject —
    does NOT feed the detector's rejection memory. Mirrors ``cli_memory_reject``;
    the only difference is the decision verb the digest applies.
    """
    base = _base(base_dir)
    full_id, err = _resolve(base, partial_id)
    if full_id is None:
        print(err, file=sys.stderr)
        return 1
    store = MemoryStore(base_dir=base)

    def decide(_summary: str, proposal: Dict[str, Any]) -> str:
        return "dismiss" if _full_id(proposal) == full_id else "defer"

    counts = run_digest(
        store=store, proposals_path=_proposals_path(base),
        decide=decide, ledger_dir=ledger_dir,
    )
    if counts["dismissed"]:
        print(f"Dismissed memory proposal {full_id[:_SHORT_ID_LEN]}.")
        return 0
    print(f"Memory proposal {full_id[:_SHORT_ID_LEN]} was not dismissed.",
          file=sys.stderr)
    return 1

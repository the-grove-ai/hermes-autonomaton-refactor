"""FreshnessDetector — governed forgetting for the memory substrate.

Where the Context Persistence Detector crystallizes NEW knowledge, the
FreshnessDetector retires STALE knowledge: it applies entity-type decay and
stages deprecation proposals for records whose confidence has fallen below the
store's deprecation floor. It never mutates the active graph — it stages
proposals (``action: "deprecate"``) the operator reviews through the same
Kaizen pipeline (``memory_proposals.jsonl`` → ``MemoryProposalHandler``).

Entity-type-aware by construction (R2): only ``ProjectState`` carries a
sub-1.0 decay rate, so only ProjectState confidence ever falls. ``DomainFact``,
``ArchitecturalRule`` and ``OperatorPreference`` (all decay_rate 1.0) never
drop and are never proposed. Dock-active records are suspended inside
:meth:`MemoryStore.apply_decay` (DI-3), so a record tied to a live operator
goal is never proposed for deprecation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from grove.memory.store import _DEPRECATION_FLOOR, MemoryStore

logger = logging.getLogger(__name__)

__all__ = ["FreshnessDetector"]

# Cognitive budget — at most this many deprecation proposals per sweep,
# mirroring the persistence detector's _MAX_PROPOSALS.
_MAX_PROPOSALS = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FreshnessDetector:
    """Stage deprecation proposals for decayed memory records."""

    def __init__(self, base_dir: Path) -> None:
        self._proposals_path = Path(base_dir) / "memory_proposals.jsonl"

    @property
    def proposals_path(self) -> Path:
        return self._proposals_path

    # ── detection ────────────────────────────────────────────────────────

    def detect(
        self,
        memory_store: MemoryStore,
        active_dock_goals: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Apply decay, then propose deprecation for records below the floor.

        1. ``apply_decay`` updates active records' confidence in place
           (Dock-active records are suspended — DI-3).
        2. Scan active records for ``confidence < _DEPRECATION_FLOOR``.
        3. Rank by confidence ascending (most decayed first).
        4. Take the top 3 (cognitive budget).
        5. Return deprecation proposals.

        ``active_dock_goals`` is the ``{slug, name, status, vector}`` dict list
        from ``load_active_dock_goal_dicts``; only the slugs feed ``apply_decay``.
        """
        active_slugs = [g.get("slug") for g in active_dock_goals if g.get("slug")]
        memory_store.apply_decay(active_slugs)

        # Only decaying entity types (ProjectState; decay_rate < 1.0 per R2)
        # can fall. Guarding on decay_rate as well as confidence makes the
        # immune types (DomainFact / ArchitecturalRule / OperatorPreference)
        # un-proposable even if a record was ever seeded below the floor.
        stale = [
            rec
            for rec in memory_store.projected_records().values()
            if rec.status == "active"
            and rec.decay_rate < 1.0
            and rec.confidence < _DEPRECATION_FLOOR
        ]
        stale.sort(key=lambda rec: rec.confidence)  # most decayed first
        return [self._build_proposal(rec) for rec in stale[:_MAX_PROPOSALS]]

    @staticmethod
    def _build_proposal(record: Any) -> Dict[str, Any]:
        """Shape one deprecation proposal from a decayed record.

        Carries ``content`` alongside ``target_id`` + ``reason`` so the
        store-less unified push renderer can speak the record in natural
        language without a second index lookup.
        """
        pct = round(record.confidence * 100)
        anchor = record.last_accessed or record.created_at or ""
        since = anchor[:10]  # YYYY-MM-DD
        if since:
            reason = f"Confidence decayed to {pct}% -- not accessed since {since}"
        else:
            reason = f"Confidence decayed to {pct}% -- below deprecation threshold"
        return {
            "action": "deprecate",
            "target_id": record.id,
            "reason": reason,
            "content": record.content,
        }

    # ── staging ──────────────────────────────────────────────────────────

    def stage_proposals(
        self, proposals: List[Dict[str, Any]], session_id: str
    ) -> int:
        """Append each proposal to ``memory_proposals.jsonl`` as pending.

        Uses the persistence detector's record format
        (``{session_id, status, timestamp, proposal}``). A target already
        carrying a pending deprecation is skipped, so a record that stays
        stale across dispatcher restarts is proposed once, not once per init.
        Returns the number actually staged.
        """
        already = self._pending_deprecation_targets()
        staged = 0
        for proposal in proposals:
            if proposal.get("target_id") in already:
                continue
            self._append_record({
                "session_id": session_id,
                "status": "pending",
                "timestamp": _now_iso(),
                "proposal": proposal,
            })
            already.add(proposal.get("target_id"))
            staged += 1
        return staged

    # ── proposals file I/O (mirrors detector.py) ─────────────────────────

    def _pending_deprecation_targets(self) -> set:
        targets: set = set()
        for rec in self._read_records():
            if rec.get("status") != "pending":
                continue
            proposal = rec.get("proposal")
            if not isinstance(proposal, dict):
                continue
            if proposal.get("action") == "deprecate" and proposal.get("target_id"):
                targets.add(proposal["target_id"])
        return targets

    def _read_records(self) -> List[Dict[str, Any]]:
        if not self._proposals_path.exists():
            return []
        records: List[Dict[str, Any]] = []
        for line in self._proposals_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                logger.warning(
                    "[grove.memory.freshness] malformed proposals line: %r", exc
                )
        return records

    def _append_record(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        self._proposals_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._proposals_path, "a", encoding="utf-8") as fh:
            fh.write(line)

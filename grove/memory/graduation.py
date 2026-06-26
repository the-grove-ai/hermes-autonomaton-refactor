"""GraduationDetector — governed promotion of stable memory to the cellar.

memory-cellar-graduation-v1. Where the :class:`~grove.memory.freshness.
FreshnessDetector` retires decayed knowledge, the GraduationDetector PROMOTES
stable, high-confidence, frequently-accessed memory into the permanent wiki
cellar. It never mutates the active graph and never changes a record's status
— it stages ``action: "graduate"`` proposals the operator reviews through the
same Kaizen pipeline (``memory_proposals.jsonl`` → ``MemoryProposalHandler``).
On approval the record is projected to a cellar page while STILL being served
via the JSONL/query path (the dual-serve invariant; suppression is deferred to
K4).

Floor (ALL must hold), read POST-decay — graduation runs AFTER the freshness
sweep, whose ``apply_decay`` mutates the shared store's records in place, so
``confidence`` here is the decayed value:

  - ``status == "active"``
  - ``graduated_at is None``               (not already graduated)
  - ``confidence >= _MIN_CONFIDENCE``
  - ``access_count >= _MIN_ACCESS_COUNT``
  - ``age >= _MIN_AGE_DAYS``               (anchored on ``created_at``)
  - ``confidence >= _DEPRECATION_FLOOR``   (above the forgetting floor)

Thresholds are module constants at the file top — declarative and tunable.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from grove.memory.store import _DEPRECATION_FLOOR, MemoryStore

logger = logging.getLogger(__name__)

__all__ = ["GraduationDetector"]

# Graduation floor (declarative, tunable). A record graduates only once it has
# proven stable: high confidence, repeatedly accessed, and old enough that the
# crystallization is not a transient spike.
_MIN_CONFIDENCE = 0.80
_MIN_ACCESS_COUNT = 3
_MIN_AGE_DAYS = 2

# Cognitive budget — at most this many graduation proposals per sweep. Per
# detector and independent of the freshness/persistence detectors' own caps.
_MAX_PROPOSALS = 5

# Synthetic session id stamped on a staged graduation proposal (the sweep is
# not tied to one operator session), mirroring the freshness sweep.
_SWEEP_SESSION_ID = "memory-graduation-sweep"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class GraduationDetector:
    """Stage graduation proposals for stable, high-value memory records."""

    def __init__(self, base_dir: Path) -> None:
        self._proposals_path = Path(base_dir) / "memory_proposals.jsonl"

    @property
    def proposals_path(self) -> Path:
        return self._proposals_path

    # ── detection ─────────────────────────────────────────────────────────

    def detect(
        self,
        store: MemoryStore,
        dock: Optional[List[Dict[str, Any]]] = None,
        *,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Return ranked graduation proposals for eligible records.

        ``dock`` is optional context (the active Dock goals); the floor does
        not depend on it, so the detector runs with or without it. ``now`` is a
        test seam for the age predicate.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        eligible = [
            rec
            for rec in store.projected_records().values()
            if self._is_eligible(rec, now)
        ]
        # Rank: confidence DESC, then access_count DESC (tiebreaker).
        eligible.sort(key=lambda r: (-r.confidence, -r.access_count))
        return [self._build_proposal(rec) for rec in eligible[:_MAX_PROPOSALS]]

    @staticmethod
    def _is_eligible(record: Any, now: datetime) -> bool:
        if record.status != "active":
            return False
        if record.graduated_at is not None:
            return False
        if record.confidence < _MIN_CONFIDENCE:
            return False
        if record.access_count < _MIN_ACCESS_COUNT:
            return False
        if record.confidence < _DEPRECATION_FLOOR:
            return False  # above the forgetting floor (dominated by MIN_CONFIDENCE)
        anchor = datetime.fromisoformat(record.created_at)
        age_days = (now - anchor).total_seconds() / 86400.0
        if age_days < _MIN_AGE_DAYS:
            return False
        return True

    @staticmethod
    def _build_proposal(record: Any) -> Dict[str, Any]:
        """The SINGLE definition of the graduate proposal shape (GUARD P3-a).

        P4's renderer branches read exactly these keys — this dict is never
        re-spelled elsewhere. A field added here is the only way to surface a
        new field downstream; a missing one fails P4's tests loudly.
        """
        return {
            "action": "graduate",
            "target_id": record.id,
            "content": record.content,
            "entity_type": record.entity_type,
            "confidence": record.confidence,
            "access_count": record.access_count,
        }

    # ── staging ───────────────────────────────────────────────────────────

    def stage_proposals(
        self,
        store: MemoryStore,
        dock: Optional[List[Dict[str, Any]]] = None,
        *,
        now: Optional[datetime] = None,
    ) -> int:
        """Detect and append graduation proposals as pending; return the count
        actually staged.

        A target already carrying a pending graduate proposal is skipped, so a
        record that stays eligible across dispatcher restarts is proposed once,
        not once per init (mirrors the FreshnessDetector).
        """
        proposals = self.detect(store, dock, now=now)
        already = self._pending_graduation_targets()
        staged = 0
        for proposal in proposals:
            if proposal["target_id"] in already:
                continue
            self._append_record({
                "session_id": _SWEEP_SESSION_ID,
                "status": "pending",
                "timestamp": _now_iso(),
                "proposal": proposal,
            })
            already.add(proposal["target_id"])
            staged += 1
        return staged

    # ── proposals file I/O (mirrors freshness.py) ──────────────────────────

    def _pending_graduation_targets(self) -> set:
        targets: set = set()
        for rec in self._read_records():
            if rec.get("status") != "pending":
                continue
            proposal = rec.get("proposal")
            if not isinstance(proposal, dict):
                continue
            if proposal.get("action") == "graduate" and proposal.get("target_id"):
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
                    "[grove.memory.graduation] malformed proposals line: %r", exc
                )
        return records

    def _append_record(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        self._proposals_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._proposals_path, "a", encoding="utf-8") as fh:
            fh.write(line)

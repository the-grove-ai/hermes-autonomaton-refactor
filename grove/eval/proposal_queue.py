"""GRV-008 § II proposal queue — ~/.grove/proposals.jsonl.

Sprint 47. Defines the ``RoutingProposal`` dataclass and the queue I/O
the Flywheel pipeline writes to and the operator review CLI reads
from. Append-only JSON Lines; one record per line; idempotent on
duplicate ``proposal_id`` (content-addressable hashes are the GRV-008
§ II uniqueness contract).

Hashes
------
``proposal_id`` is a SHA-256 over ``type | sorted-payload-JSON |
sorted-evidence-CSV``. Same logical proposal — same id, regardless of
when the detector ran or how many sessions contributed evidence.

``eval_hash`` is a SHA-256 over the EvalReport's deterministic
projection: per-prompt ``(prompt_id, observed_intent,
observed_complexity, observed_tier, sorted-tools, passed)``.
Confidence is deliberately excluded — small-band T-telemetry variance
must not invalidate the gate signature on otherwise structurally
identical outcomes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


__all__ = [
    "RoutingProposal",
    "compute_proposal_id",
    "compute_eval_hash",
    "default_queue_path",
    "append",
    "read_all",
    "read",
    "remove",
]


# ── Public dataclass ─────────────────────────────────────────────────


@dataclass(frozen=True)
class RoutingProposal:
    """One Flywheel-authored routing change waiting for operator review.

    Schema invariants per GRV-008 § II:

    * ``proposal_id``: content-addressable SHA-256 (see
      :func:`compute_proposal_id`).
    * ``type``: ``"routing_update"`` for Sprint 47; future proposal
      classes register additional values here.
    * ``payload``: structured diff. For ``routing_update`` the shape
      is ``{"rule": "downward"|"upward", "add_intents": [str]}``.
      Removal-from-list is intentionally out of scope for v1
      (operator GATE-A revision).
    * ``evidence``: the ``turn_id`` values that triggered the
      detector. Carried as a tuple so the dataclass is hashable.
    * ``eval_hash``: SHA-256 over the EvalReport projection that
      gated this proposal (see :func:`compute_eval_hash`).
    * ``created_at``: ISO 8601 UTC.
    """

    proposal_id: str
    type: str
    payload: Dict[str, Any]
    evidence: Tuple[str, ...]
    eval_hash: str
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["evidence"] = list(data["evidence"])
        return data


# ── Hashing ──────────────────────────────────────────────────────────


def compute_proposal_id(
    *,
    type: str,
    payload: Dict[str, Any],
    evidence: Tuple[str, ...],
) -> str:
    """Compute the content-addressable proposal_id.

    Deterministic across runs: sorted JSON for the payload, sorted CSV
    for evidence. The same logical proposal — same id — even when the
    detector reruns or evidence accumulates from new sessions.
    """
    payload_json = json.dumps(payload, sort_keys=True, default=str)
    evidence_csv = ",".join(sorted(evidence))
    seed = f"{type}|{payload_json}|{evidence_csv}"
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def compute_eval_hash(report: Any) -> str:
    """Compute the EvalReport projection hash for the gate signature.

    Confidence is excluded so small-band T-telemetry variance does not
    invalidate the signature on otherwise identical structural
    outcomes. Tools are sorted for set-comparison stability.
    """
    parts: List[Dict[str, Any]] = []
    for r in getattr(report, "results", ()):
        tools = getattr(r, "observed_tools", None)
        tools_sorted = sorted(tools) if tools is not None else None
        parts.append({
            "prompt_id": r.prompt_id,
            "intent": r.observed_intent,
            "complexity": r.observed_complexity,
            "tier": r.observed_tier,
            "tools": tools_sorted,
            "passed": bool(r.passed),
        })
    seed = json.dumps(parts, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Queue I/O ────────────────────────────────────────────────────────


_lock = threading.Lock()


def default_queue_path() -> Path:
    """Resolve ``~/.grove/proposals.jsonl`` via the standard hermes_home."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "proposals.jsonl"


def _read_records(path: Path) -> List[RoutingProposal]:
    """Stream RoutingProposals from ``path``; skip malformed lines.

    Malformed lines log at debug and are skipped — the queue is
    operator-facing and must not crash on a damaged entry.
    """
    if not path.exists():
        return []
    out: List[RoutingProposal] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.debug(
                    "[proposal_queue] malformed record line %d in %s: %r",
                    line_no, path, exc,
                )
                continue
            if isinstance(data.get("evidence"), list):
                data["evidence"] = tuple(data["evidence"])
            try:
                out.append(RoutingProposal(**data))
            except (TypeError, ValueError) as exc:
                logger.debug(
                    "[proposal_queue] schema mismatch line %d in %s: %r",
                    line_no, path, exc,
                )
    return out


def append(
    proposal: RoutingProposal,
    *,
    path: Optional[Path] = None,
) -> bool:
    """Append ``proposal`` to the queue; return True on append, False
    on duplicate.

    Idempotent on duplicate ``proposal_id``: a re-run of the detector
    that produces the same logical proposal does NOT pollute the queue.
    """
    target = Path(path) if path is not None else default_queue_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        existing = _read_records(target)
        for existing_proposal in existing:
            if existing_proposal.proposal_id == proposal.proposal_id:
                return False
        line = json.dumps(proposal.to_dict(), sort_keys=True, default=str) + "\n"
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line)
    return True


def read_all(*, path: Optional[Path] = None) -> List[RoutingProposal]:
    """Return all pending proposals in append order."""
    target = Path(path) if path is not None else default_queue_path()
    with _lock:
        return _read_records(target)


def read(
    proposal_id: str,
    *,
    path: Optional[Path] = None,
) -> Optional[RoutingProposal]:
    """Look up one proposal by ``proposal_id``."""
    target = Path(path) if path is not None else default_queue_path()
    with _lock:
        for proposal in _read_records(target):
            if proposal.proposal_id == proposal_id:
                return proposal
    return None


def remove(
    proposal_id: str,
    *,
    path: Optional[Path] = None,
) -> bool:
    """Remove the proposal with ``proposal_id`` from the queue.

    Returns True on removal, False when no proposal matched.
    Rewrites the file omitting the matched record so the queue stays
    JSON-Lines-clean (no tombstones, no commented-out lines).
    """
    target = Path(path) if path is not None else default_queue_path()
    with _lock:
        existing = _read_records(target)
        keep = [p for p in existing if p.proposal_id != proposal_id]
        if len(keep) == len(existing):
            return False
        if keep:
            tmp = target.with_suffix(target.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                for proposal in keep:
                    fh.write(
                        json.dumps(
                            proposal.to_dict(), sort_keys=True, default=str,
                        )
                        + "\n"
                    )
            tmp.replace(target)
        else:
            target.unlink()
    return True

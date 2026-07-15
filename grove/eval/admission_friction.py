"""admission_friction producer — operator-mutable-admission-v1 Phase 4.

The self-healing toolbox loop's DETECT stage: reads the ``capability_refusals``
feed (Phase 3), aggregates recurrence per ``(governing_record, intent)`` arm, and
— over a config threshold — proposes an ADDITIVE admission-overlay edit for
operator approval. Approval writes ``~/.grove`` via the sanctioned
``set_admission_overlay`` (Phase 1); dismissal writes an evidence tombstone so a
dismissed arm is never re-proposed (binding-telemetry-v1 R-B2 pattern).

INVARIANTS (test-pinned):
  * ADDITIVE-ONLY — the only proposal verbs are ``add_intents`` (union the refused
    intent) and ``force_always`` (repo OR true). Never remove, never shrink.
  * GREEN-SCOPED force_always — ``force_always`` is proposed ONLY for a GREEN
    record that accrued friction across ≥ N distinct intents. A non-GREEN record
    never yields a force_always proposal (its zone-gated safety is the
    Dispatcher's, and always:true would widen a non-green surface).
  * GENERALIZABLE — this module contains ZERO tool-name or intent-class literals
    (grep-testable I7). Arms come from the feed; zone from the registry.
  * TOMBSTONE grain = ``(record, intent)``; force_always uses a record-level
    sentinel arm. A dismiss of ``(A, X)`` never suppresses ``(A, Y)``.

Threshold is CONFIG DATA (``~/.grove/flywheel.config.yaml`` ``admission_friction``
block), not a code constant.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_ADMISSION_FRICTION,
    RoutingProposal,
    compute_proposal_id,
    default_queue_path,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AdmissionFrictionConfig",
    "load_admission_friction_config",
    "build_admission_friction_proposals",
    "record_tombstone",
    "default_tombstone_path",
]

# The record-level sentinel arm for a force_always proposal's tombstone grain.
# NOT an intent class and NOT a tool name (I7) — a mechanism marker only.
_FORCE_ALWAYS_ARM = "__force_always__"

_TOMBSTONE_FILENAME = "admission_friction_tombstones.json"


# ── config (declarative threshold, NOT a code constant) ────────────────────


@dataclass(frozen=True)
class AdmissionFrictionConfig:
    """The producer's tunables — loaded from the operator's flywheel config."""

    friction_threshold: int = 3          # min refusals for an arm to arm a proposal
    window_days: int = 30                # recency window over the refusals feed
    green_force_always_distinct_intents: int = 3  # ≥N distinct intents ⇒ force_always


def load_admission_friction_config(
    config_path: Optional[Path] = None,
) -> AdmissionFrictionConfig:
    """Load the ``admission_friction`` block from ``~/.grove/flywheel.config.yaml``.

    An absent file or absent block uses the documented code defaults. A present
    block is validated fail-loud (positive ints)."""
    import yaml

    if config_path is None:
        from hermes_constants import get_hermes_home

        config_path = Path(get_hermes_home()) / "flywheel.config.yaml"
    if not config_path.exists():
        return AdmissionFrictionConfig()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    block = raw.get("admission_friction")
    if not isinstance(block, dict):
        return AdmissionFrictionConfig()

    def _pos(key: str, default: int) -> int:
        v = block.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise ValueError(
                f"admission_friction.{key} must be a positive integer (got {v!r})"
            )
        return v

    return AdmissionFrictionConfig(
        friction_threshold=_pos("friction_threshold", 3),
        window_days=_pos("window_days", 30),
        green_force_always_distinct_intents=_pos(
            "green_force_always_distinct_intents", 3
        ),
    )


# ── tombstone store (mirrors binding_scan; beside proposals.jsonl in ~/.grove) ─


def default_tombstone_path() -> Path:
    """``~/.grove/admission_friction_tombstones.json`` — beside the proposal
    queue, OUTSIDE the repo tree, so a ``git reset --hard`` deploy cannot wipe a
    dismissal and resurrect the proposal (Gemini S1 loop guard)."""
    return default_queue_path().with_name(_TOMBSTONE_FILENAME)


def _load_tombstones(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    p = path or default_tombstone_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "[admission_friction] tombstone store unreadable at %s (%s) — treating "
            "as empty; dismissed proposals may re-surface until repaired.", p, exc,
        )
        return []
    entries = data.get("tombstones") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def _write_tombstones(entries: List[Dict[str, Any]], path: Optional[Path] = None) -> None:
    p = path or default_tombstone_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"tombstones": entries}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def _arm_of(proposal: Any) -> Tuple[Optional[str], Optional[str]]:
    """The ``(record, intent)`` grain a proposal tombstones on. A force_always
    proposal tombstones on the record-level sentinel arm."""
    payload = getattr(proposal, "payload", None) or {}
    record = payload.get("record")
    if payload.get("verb") == "force_always":
        return record, _FORCE_ALWAYS_ARM
    add = payload.get("add_intents") or []
    return record, (add[0] if add else None)


def record_tombstone(proposal: Any, *, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Reject-disposition hook — called by the handler's reject_callback BEFORE
    queue removal. Keys on ``(record, intent)`` (or the force_always sentinel)."""
    record, intent = _arm_of(proposal)
    if not record or intent is None:
        logger.warning(
            "[admission_friction] rejected proposal %s carries no identifiable "
            "(record, intent) — no tombstone written",
            getattr(proposal, "proposal_id", "?"),
        )
        return None
    entry = {
        "record": record,
        "intent": intent,
        "rejected_at": datetime.now(timezone.utc).isoformat(),
        "proposal_id": getattr(proposal, "proposal_id", None),
    }
    entries = _load_tombstones(path)
    entries.append(entry)
    _write_tombstones(entries, path)
    logger.info("[admission_friction] tombstone recorded: %s / %s", record, intent)
    return entry


def _suppressed(tombstones: List[Dict[str, Any]], record: str, intent: str) -> bool:
    """A ``(record, intent)`` arm is suppressed iff an exact-grain tombstone
    exists. ``(A, X)`` dismissed never suppresses ``(A, Y)``."""
    return any(
        t.get("record") == record and t.get("intent") == intent for t in tombstones
    )


# ── the producer ───────────────────────────────────────────────────────────


def _read_refusals(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    from grove.capability_refusals import refusals_path

    p = path or refusals_path()
    if not p.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("[admission_friction] skipping malformed refusals line")
    return rows


def _parse_ts(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _proposal(
    record: str, verb: str, intent: Optional[str], evidence_block: Dict[str, Any],
    arm: str, now: datetime,
) -> RoutingProposal:
    payload: Dict[str, Any] = {"record": record, "verb": verb}
    if verb == "add_intents":
        payload["add_intents"] = [intent]
    payload["evidence_block"] = evidence_block
    identity = {k: payload[k] for k in ("record", "verb")}
    if "add_intents" in payload:
        identity["add_intents"] = payload["add_intents"]
    evidence: Tuple[str, ...] = (f"{record}|{arm}",)
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_ADMISSION_FRICTION, payload=identity, evidence=evidence,
        ),
        type=PROPOSAL_TYPE_ADMISSION_FRICTION,
        payload=payload,
        evidence=evidence,
        eval_hash="",
        created_at=now.isoformat(),
        proposer="admission_friction",
    )


def build_admission_friction_proposals(
    *,
    refusals_path: Optional[Path] = None,
    tombstone_path: Optional[Path] = None,
    config_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    caps: Optional[Dict[str, Any]] = None,
) -> List[RoutingProposal]:
    """Aggregate the refusals feed and emit additive admission-overlay proposals.

    One proposal per over-threshold ``(record, intent)`` arm (verb ``add_intents``),
    EXCEPT a GREEN record with ≥ ``green_force_always_distinct_intents`` distinct
    over-threshold intents, which yields a SINGLE ``force_always`` proposal instead.
    Tombstoned arms are skipped. Records absent from the registry are skipped (no
    overlay target)."""
    cfg = load_admission_friction_config(config_path)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=cfg.window_days)

    arms: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in _read_refusals(refusals_path):
        record = r.get("governing_record")
        intent = r.get("intent")
        if not isinstance(record, str) or not isinstance(intent, str):
            continue
        ts = _parse_ts(r.get("ts"))
        if ts is not None and ts < cutoff:
            continue
        a = arms.setdefault(
            (record, intent), {"count": 0, "last": r.get("ts"), "sessions": set()}
        )
        a["count"] += 1
        if r.get("ts"):
            a["last"] = r["ts"]
        if r.get("session_id"):
            a["sessions"].add(r["session_id"])

    tombstones = _load_tombstones(tombstone_path)
    if caps is None:
        from grove.capability_registry import load_capabilities

        caps = load_capabilities()
    from grove.capability import Zone

    by_record: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    for (record, intent), a in arms.items():
        if a["count"] >= cfg.friction_threshold:
            by_record.setdefault(record, []).append((intent, a))

    proposals: List[RoutingProposal] = []
    for record in sorted(by_record):
        cap = caps.get(record)
        if cap is None:
            continue  # record no longer exists — no overlay target
        hits = sorted(by_record[record])
        distinct = [intent for intent, _a in hits]
        is_green = getattr(cap, "zone", None) is Zone.GREEN

        if is_green and len(distinct) >= cfg.green_force_always_distinct_intents:
            if _suppressed(tombstones, record, _FORCE_ALWAYS_ARM):
                continue
            eb = {
                "verb": "force_always",
                "zone": Zone.GREEN.value,
                "window_days": cfg.window_days,
                "threshold": cfg.friction_threshold,
                "distinct_intents": distinct,
                "arms": [
                    {"intent": i, "count": a["count"], "last_seen": a["last"],
                     "sessions": len(a["sessions"])}
                    for i, a in hits
                ],
            }
            proposals.append(
                _proposal(record, "force_always", None, eb, _FORCE_ALWAYS_ARM, now)
            )
            continue

        for intent, a in hits:
            if _suppressed(tombstones, record, intent):
                continue
            eb = {
                "verb": "add_intents",
                "zone": getattr(getattr(cap, "zone", None), "value", None),
                "window_days": cfg.window_days,
                "threshold": cfg.friction_threshold,
                "arms": [
                    {"intent": intent, "count": a["count"], "last_seen": a["last"],
                     "sessions": len(a["sessions"])}
                ],
            }
            proposals.append(_proposal(record, "add_intents", intent, eb, intent, now))

    return proposals

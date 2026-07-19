"""Producer-failure recurrence detector — detector-sweep-resilience-v1 P3.

Deterministic scan of the Kaizen ledger's ``producer_failure`` events
(filed by the P1 shared guard): a producer that failed on
``distinct_days`` DISTINCT UTC days within ``window_days`` stages ONE
``producer_failure_recurrence`` card — the operator's pause offer. Same-day
failure storms never trip the predicate (one bad morning is an incident;
the same producer breaking across days is a pattern).

Skips (both consulted fresh per run):

* already-paused producers (:func:`grove.eval.producer_pauses.
  read_producer_pauses`) — the operator has already ruled; the card would
  be noise. Unpausing re-arms detection automatically.
* disposition-suppressed cards (R-7): a rejected/dismissed
  ``kaizen_disposition`` for this card's stable id within the window —
  the fault_triage ``_suppressed`` shape verbatim (fault_triage.py:608-609).
  Post-window, the SAME content-addressed id re-stages cleanly: the queue
  keeps no tombstones (gate ruling a — the A1 dissolution).

Identity is MINIMAL — payload ``{producer}``, evidence ``(producer,)`` —
so the card is stable across failure counts; the evidence (failure_count /
distinct_dates / last_error / window_days) rides the id-EXCLUDED ``detail``
envelope. Thresholds load from the operator's ``flywheel.config.yaml``
``producer_resilience:`` block, mirroring
:func:`grove.eval.fault_triage.load_fault_triage_thresholds` (absent →
defaults 3/14; present-but-invalid → fail loud).

Invoked as the THIRD SIBLING at Dispatcher init, wrapped by
``_run_guarded_producer`` under its own producer name — it is itself
pausable, by design.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_PRODUCER_FAILURE_RECURRENCE,
    RoutingProposal,
    append,
    compute_proposal_id,
)
from grove.eval.producer_pauses import read_producer_pauses

logger = logging.getLogger(__name__)

PROPOSER = "producer_recurrence_detector"

__all__ = [
    "ProducerResilienceThresholds",
    "build_producer_recurrence_proposals",
    "load_producer_resilience_thresholds",
]


@dataclass(frozen=True)
class ProducerResilienceThresholds:
    """Declarative thresholds for the recurrence detector.

    Defaults are the documented baseline (``config/flywheel.config.yaml``,
    ``producer_resilience`` block): a producer must fail on at least
    ``distinct_days`` distinct UTC days inside the sliding ``window_days``
    before the pause card stages. An absent operator config means "use the
    default"; a present-but-invalid value fails loud.
    """

    distinct_days: int = 3
    window_days: int = 14


def _require_positive_int(block: Dict[str, object], key: str, default: int) -> int:
    """Read ``key`` from a present config block, fail loud on a bad value."""
    if key not in block:
        return default
    value = block[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"flywheel.config.yaml producer_resilience.{key} must be an "
            f"integer, got {value!r} ({type(value).__name__})."
        )
    if value < 1:
        raise ValueError(
            f"flywheel.config.yaml producer_resilience.{key} must be >= 1, "
            f"got {value}."
        )
    return value


def load_producer_resilience_thresholds(
    config_path: Optional[Path] = None,
) -> ProducerResilienceThresholds:
    """Load thresholds from the operator's ``flywheel.config.yaml``.

    Mirrors :func:`grove.eval.fault_triage.load_fault_triage_thresholds`:
    absent file / absent ``producer_resilience`` block → documented
    defaults; a present block is validated key-by-key and any malformed
    value raises LOUD. Malformed YAML propagates from the parser.
    """
    if config_path is None:
        from hermes_constants import get_hermes_home

        config_path = Path(get_hermes_home()) / "flywheel.config.yaml"
    if not config_path.exists():
        return ProducerResilienceThresholds()

    import yaml

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if raw is None:
        return ProducerResilienceThresholds()
    if not isinstance(raw, dict):
        raise ValueError(
            f"{config_path} must be a YAML mapping, got {type(raw).__name__}."
        )
    block = raw.get("producer_resilience")
    if block is None:
        return ProducerResilienceThresholds()
    if not isinstance(block, dict):
        raise ValueError(
            f"{config_path} producer_resilience must be a mapping, got "
            f"{type(block).__name__}."
        )
    return ProducerResilienceThresholds(
        distinct_days=_require_positive_int(block, "distinct_days", 3),
        window_days=_require_positive_int(block, "window_days", 14),
    )


def _parse_timestamp(ts_raw: object) -> Optional[datetime]:
    """ISO-8601 → tz-aware UTC; naive treated as UTC; unparseable → None."""
    if not isinstance(ts_raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts_raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_events(path: Path) -> List[Dict[str, Any]]:
    """Tolerant JSONL read — a torn line never aborts the scan."""
    out: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _latest_dispositions(ledger_dir: Path) -> Dict[str, Dict[str, Any]]:
    """proposal_id → its most recent ``kaizen_disposition`` event (the
    fault_triage._latest_dispositions shape — no new state file)."""
    latest: Dict[str, Dict[str, Any]] = {}
    if not ledger_dir.is_dir():
        return latest
    for path in sorted(ledger_dir.glob("*.jsonl")):
        for event in _read_events(path):
            if event.get("event_type") != "kaizen_disposition":
                continue
            pid = event.get("proposal_id")
            if not pid:
                continue
            prior = latest.get(pid)
            if prior is None or str(event.get("timestamp") or "") > str(
                prior.get("timestamp") or ""
            ):
                latest[pid] = event
    return latest


def _suppressed(
    disposition_event: Optional[Dict[str, Any]], *, window_start: datetime
) -> bool:
    """R-7 — the fault_triage windowed suppression shape verbatim
    (fault_triage.py:608-609): rejected/dismissed in-window → suppressed
    for the remainder of the window; post-expiry the card re-stages."""
    if disposition_event is None:
        return False
    disposition = disposition_event.get("disposition")
    disposition_ts = _parse_timestamp(disposition_event.get("timestamp"))
    if disposition in ("rejected", "dismissed"):
        return disposition_ts is not None and disposition_ts >= window_start
    return False


def build_producer_recurrence_proposals(
    ledger_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> List[str]:
    """Run one deterministic detection pass; return the staged proposal ids.

    Content-addressed identity + the queue's live-row dedup give one card
    max per producer per run (a re-run against an unchanged ledger appends
    nothing).
    """
    from grove.kaizen_ledger import default_ledger_dir

    thresholds = load_producer_resilience_thresholds(config_path)
    ledger = ledger_dir or default_ledger_dir()
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(days=thresholds.window_days)

    # Aggregate producer_failure events inside the window.
    by_producer: Dict[str, Dict[str, Any]] = {}
    if ledger.is_dir():
        for path in sorted(ledger.glob("*.jsonl")):
            for event in _read_events(path):
                if event.get("event_type") != "producer_failure":
                    continue
                producer = event.get("producer")
                ts = _parse_timestamp(event.get("timestamp"))
                if not producer or ts is None or ts < window_start:
                    continue
                agg = by_producer.setdefault(
                    producer,
                    {"dates": set(), "count": 0, "last_error": None,
                     "last_ts": None},
                )
                agg["dates"].add(ts.date().isoformat())
                agg["count"] += 1
                if agg["last_ts"] is None or ts > agg["last_ts"]:
                    agg["last_ts"] = ts
                    agg["last_error"] = event.get("error")

    if not by_producer:
        return []

    paused = read_producer_pauses()
    dispositions = _latest_dispositions(ledger)
    staged: List[str] = []
    for producer in sorted(by_producer):
        agg = by_producer[producer]
        if producer in paused:
            continue  # operator already ruled — unpausing re-arms detection
        if len(agg["dates"]) < thresholds.distinct_days:
            continue  # a same-day storm never trips the predicate
        payload = {"producer": producer}
        evidence = (producer,)
        pid = compute_proposal_id(
            type=PROPOSAL_TYPE_PRODUCER_FAILURE_RECURRENCE,
            payload=payload,
            evidence=evidence,
        )
        if _suppressed(dispositions.get(pid), window_start=window_start):
            continue  # R-7 — the operator said no this window
        detail = {
            "failure_count": agg["count"],
            "distinct_dates": sorted(agg["dates"]),
            "last_error": agg["last_error"],
            "window_days": thresholds.window_days,
        }
        proposal = RoutingProposal(
            proposal_id=pid,
            type=PROPOSAL_TYPE_PRODUCER_FAILURE_RECURRENCE,
            payload=payload,
            evidence=evidence,
            eval_hash="",
            created_at=now.isoformat(),
            proposer=PROPOSER,
            semantic_justification=(
                f"producer {producer} failed on {len(agg['dates'])} distinct "
                f"day(s) in the last {thresholds.window_days}d"
            ),
            detail=detail,
        )
        if append(proposal):
            staged.append(pid)
            logger.info(
                "[producer_recurrence] staged pause card %s for %s "
                "(%d failure(s), %d distinct day(s))",
                pid, producer, agg["count"], len(agg["dates"]),
            )
    return staged

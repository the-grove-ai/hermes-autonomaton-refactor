"""kaizen-exploration-proposals-v1 Phase 3 — attended-interaction evidence.

The PARALLEL attended reader (R-P0-3). ``collect_arms``
(grove.kaizen.binding_evidence) reads ONLY the fleet worker event stream;
attended interactive turns live on the MAIN plane as IntentRecords
(grove/intent_store.py) whose field vocabulary (outcome / model_used /
tier_selected) is incompatible with the fleet event shape (status /
quality_score / rubric_version). Rather than conflate the two inside
``collect_arms``, this module is a SEPARATE reader with an IntentRecord→arm
adapter.

Attended arms are SUCCESS-RATE-ONLY (IntentRecords carry no quality score) and
carry ``source: "attended"`` so they are STRUCTURALLY distinguishable from
fleet-observed arms — never silently promoted as if fleet-observed. The guard
that keeps them out of the ranked candidate set lives in
``build_binding_proposals``; this module only produces the arms.

READ-only; never writes back (mirrors ``collect_arms``' discipline). No
dispatch-path imports (the catalog-isolation ratchet walls router / dispatcher /
tier_budget / providers / router_merge — none are touched here).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from grove.intent_store import VALID_OUTCOMES, IntentStore

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 30

# Terminal outcomes carry a settled signal. "pending" is NON-terminal (the turn
# has not resolved) — excluded so an in-flight turn never inflates the arm. Among
# terminal outcomes only "success" is a success; every other terminal outcome
# (drop / error / correction / governance_terminated) counts AGAINST.
_TERMINAL_OUTCOMES = VALID_OUTCOMES - {"pending"}


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _build_attended_arm(
    context: str,
    model: str,
    acc: Dict[str, int],
    window_days: int,
    since: datetime,
    now_dt: datetime,
) -> Dict[str, Any]:
    n = acc["n"]
    successes = acc["successes"]
    return {
        "context": context,  # tier_selected — the attended grain (NOT a skill)
        "model": model,
        "n": n,
        "successes": successes,
        "failures": n - successes,
        "success_rate": (successes / n) if n else 0.0,
        "source": "attended",
        # Success-rate-only: IntentRecords carry no quality score. These fields
        # are explicitly None/False so an attended arm can NEVER present as a
        # scored (downgrade-eligible) fleet arm — provenance is honest by shape.
        "scored_n": 0,
        "score_mean": None,
        "score_variance": None,
        "redraft_rate": None,
        "comparability_key": None,
        "self_judged": False,
        "family_judged": False,
        "mixed_judge": False,
        "window": {
            "days": window_days,
            "since": since.isoformat(),
            "until": now_dt.isoformat(),
        },
    }


def collect_attended_arms(
    *,
    store_path: Optional[Path] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Aggregate attended IntentRecords into per-(tier, model) success-rate arms.

    Returns::

        {
          "arms": [<arm dict>, ...],          # sorted by (context, model)
          "window": {"days": int, "since": iso, "until": iso},
          "counts": {...},                    # observability, never load-bearing
        }

    Filter: ``model_used`` present AND ``outcome`` terminal (VALID_OUTCOMES minus
    "pending"). Keyed on ``(tier_selected, model_used)`` — the attended grain.
    Reads the effective per-turn record via ``IntentStore.latest_by_turn`` so a
    provisional ``pending`` joined by a terminal finalization counts once, at its
    settled outcome. READ-only; never raises on a single bad record (the store's
    own reader skips malformed lines).
    """
    now_dt = now or datetime.now(timezone.utc)
    since = now_dt - timedelta(days=window_days)
    counts = {
        "records_seen": 0,
        "turns_counted": 0,
        "skipped_no_model": 0,
        "skipped_non_terminal": 0,
        "skipped_bad_ts": 0,
        "skipped_out_of_window": 0,
    }
    acc: Dict[Tuple[str, str], Dict[str, int]] = {}

    store = IntentStore(store_path)
    for rec in store.latest_by_turn():
        counts["records_seen"] += 1
        model = rec.model_used
        if not isinstance(model, str) or not model:
            counts["skipped_no_model"] += 1
            continue
        if rec.outcome not in _TERMINAL_OUTCOMES:
            counts["skipped_non_terminal"] += 1
            continue
        ts = _parse_ts(rec.timestamp)
        if ts is None:
            counts["skipped_bad_ts"] += 1
            continue
        if ts < since or ts > now_dt:
            counts["skipped_out_of_window"] += 1
            continue
        context = rec.tier_selected or ""
        a = acc.setdefault((context, model), {"n": 0, "successes": 0})
        a["n"] += 1
        counts["turns_counted"] += 1
        if rec.outcome == "success":
            a["successes"] += 1

    arms: List[Dict[str, Any]] = []
    for (context, model), a in sorted(acc.items()):
        arms.append(
            _build_attended_arm(context, model, a, window_days, since, now_dt)
        )

    return {
        "arms": arms,
        "window": {
            "days": window_days,
            "since": since.isoformat(),
            "until": now_dt.isoformat(),
        },
        "counts": counts,
    }

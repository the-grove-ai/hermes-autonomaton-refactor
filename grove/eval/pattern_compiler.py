"""T0 pattern compiler — scanner (Sprint 48 Phase 1) + compiler (Phase 2).

Sibling to ``tier_ratchet.py``: both read the IntentStore evidence. The tier
ratchet aggregates by ``intent_class`` to propose tier moves; this module
aggregates by ``(intent_class, t0_key)`` to identify stable patterns that can
retire to the deterministic T0 cache, and compiles them into cache entries.

T0 is DETERMINISTIC — a T0 hit returns a compiled pattern with no model call.
Per GATE-A: the system PROPOSES T0 promotion; the operator approves; the
system never self-promotes. Thresholds live in ``routing.config.yaml`` under
``pattern_cache``.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from grove.pattern_cache import t0_key

# Defaults — used when routing.config.yaml carries no pattern_cache section.
# Mirror the GATE-A decision-4 values.
_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "min_repetitions": 5,
    "within_days": 14,
    "max_rejections": 0,
    "max_response_variance": 0,
    "exclude_intents": ["unknown", "system_admin"],
}

# Intent classes whose answers are stable artifacts → cache the response
# STRING (static). Everything else that qualifies caches the tool invocation
# (executable). factual_retrieval is the Sprint-47-era synonym of the
# Sprint-54 factual_lookup; both are static.
_STATIC_INTENTS = {"factual_lookup", "memory_operation", "factual_retrieval"}


@dataclass(frozen=True)
class Candidate:
    """A pattern_hash group that meets the T0 promotion thresholds."""
    t0_key: str
    intent_class: str
    cacheable_type: str            # "static" | "executable"
    repetition_count: int
    time_span_days: float
    rejection_count: int
    sample_queries: tuple          # first 3 user_message_stems
    evidence_turn_ids: tuple


def load_pattern_cache_config() -> Dict[str, Any]:
    """Read the ``pattern_cache`` thresholds from routing.config.yaml.

    Operator copy (``~/.grove/routing.config.yaml``) wins over the repo
    default (``config/routing.config.yaml``). Missing/partial sections fall
    back to :data:`_DEFAULTS`."""
    import yaml

    cfg = dict(_DEFAULTS)
    candidates = (
        Path.home() / ".grove" / "routing.config.yaml",
        Path(__file__).resolve().parents[2] / "config" / "routing.config.yaml",
    )
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        pc = data.get("pattern_cache")
        if isinstance(pc, dict):
            cfg["enabled"] = pc.get("enabled", cfg["enabled"])
            if isinstance(pc.get("exclude_intents"), list):
                cfg["exclude_intents"] = pc["exclude_intents"]
            prom = pc.get("promotion")
            if isinstance(prom, dict):
                for k in ("min_repetitions", "within_days",
                          "max_rejections", "max_response_variance"):
                    if k in prom:
                        cfg[k] = prom[k]
        break
    return cfg


def _days_between(a_iso: str, b_iso: str) -> float:
    try:
        a = datetime.fromisoformat(a_iso)
        b = datetime.fromisoformat(b_iso)
        return abs((b - a).total_seconds()) / 86400.0
    except Exception:
        return 0.0


def _cacheable_type(intent_class: str) -> str:
    return "static" if intent_class in _STATIC_INTENTS else "executable"


def scan_candidates(store: Any, config: Optional[Dict[str, Any]] = None) -> List[Candidate]:
    """Group the intent store by ``(intent_class, t0_key)`` and return the
    groups that meet the promotion thresholds.

    Precision-first (GATE-A decision 4): a group qualifies only with
    ``>= min_repetitions`` turns, all within a ``within_days`` span, and
    ``<= max_rejections`` correction outcomes. ``exclude_intents`` (the
    OAuth-callback / unknown noise) are dropped. Records are collapsed by
    turn so a provisional + finalized pair counts once."""
    cfg = config or load_pattern_cache_config()
    if not cfg.get("enabled", True):
        return []

    exclude = set(cfg.get("exclude_intents", []))
    min_rep = int(cfg.get("min_repetitions", 5))
    within = float(cfg.get("within_days", 14))
    max_rej = int(cfg.get("max_rejections", 0))

    # Honor the retention policy (decision 3) before reading.
    try:
        store.purge_expired_content(int(within))
    except Exception:
        pass

    groups: Dict[tuple, list] = collections.defaultdict(list)
    for rec in store.latest_by_turn():
        ic = rec.intent_class
        if not ic or ic == "unknown" or ic in exclude:
            continue
        key = t0_key(ic, rec.user_message_stem)
        groups[(ic, key)].append(rec)

    out: List[Candidate] = []
    for (intent_class, key), recs in groups.items():
        if len(recs) < min_rep:
            continue
        stamps = sorted(r.timestamp for r in recs)
        span = _days_between(stamps[0], stamps[-1])
        if span > within:
            continue
        rejection_count = sum(1 for r in recs if r.outcome == "correction")
        if rejection_count > max_rej:
            continue
        out.append(Candidate(
            t0_key=key,
            intent_class=intent_class,
            cacheable_type=_cacheable_type(intent_class),
            repetition_count=len(recs),
            time_span_days=round(span, 2),
            rejection_count=rejection_count,
            sample_queries=tuple(r.user_message_stem for r in recs[:3]),
            evidence_turn_ids=tuple(r.turn_id for r in recs),
        ))
    out.sort(key=lambda c: -c.repetition_count)
    return out

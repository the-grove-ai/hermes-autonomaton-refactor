"""Grove Kaizen — binding evidence reader (binding-telemetry-v1 P1).

Pure read+group over the fleet worker EVENT STREAM (the sole fleet telemetry
surface — the fleet plane writes no IntentRecords), the detector.py shape:
this module READs and AGGREGATEs; it proposes nothing. The P2 producer
consumes its arms; the P4 renderer consumes its annotations.

Evidence model — one ARM per observed (skill, model) pair within the window:

* ``n`` / ``success_rate`` — success + failed runs whose event carries a
  resolved ``model`` (a no_work run exercised nothing and carries none; an
  event with a null model is unattributable and skipped, counted).
* Score statistics — from SUCCESS events carrying a non-null
  ``quality_score``, grouped by the COMPARABILITY KEY ``(rubric_version,
  evaluator_model)`` (R-A8): scores compare only within one key; an arm whose
  window spans multiple keys is annotated ``mixed_judge`` and its top-level
  score fields stay None — per-key ``judge_groups`` are retained verbatim,
  NEVER averaged across keys.
* ``self_judged`` (R-A2) — the arm's model IS its evaluator model; such
  evidence is excluded from downgrade arguments at the producer, annotated
  here. ``family_judged`` (R-B5) — same provider org prefix, different
  model; annotation for operator judgment, not an exclusion.
* Legacy tolerance — pre-rider events (shipped before the quality rider)
  carry no quality keys at all; absent keys read as the null rider
  (ungated-equivalent): they count toward success_rate arms and contribute
  no score. A malformed event (unparseable JSON, non-mapping, missing/bad
  ``ts``) is skipped with a LOUD warning and a count — one bad file must
  never crash the scan.

Events are immutable facts: every exclusion and annotation computes HERE, at
aggregation time — nothing is ever written back.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Precision-first window precedent (fault detector window_days / pattern_cache
# within_days family).
DEFAULT_WINDOW_DAYS = 30

# Terminal statuses that constitute an attributable RUN. no_work is excluded:
# nothing was exercised, and the event carries no model.
_RUN_STATUSES = frozenset({"success", "failed"})


def _default_events_root() -> Optional[Path]:
    from hermes_constants import get_hermes_home

    try:
        return Path(get_hermes_home()) / "fleet"
    except (OSError, ValueError):  # unresolvable home — caller sees empty scan
        return None


def _provider_org(slug: Optional[str]) -> Optional[str]:
    if not slug or "/" not in slug:
        return None
    return slug.split("/", 1)[0]


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Naive timestamps never ship (worker _now_iso is UTC-aware), but a
    # malformed one must not crash the comparison.
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _mean_var(values: List[float]) -> "tuple[float, float]":
    """Mean and POPULATION variance (n denominator — deterministic for the
    small-n arms this reader sees; the producer thresholds on n separately)."""
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return round(mean, 4), round(var, 6)


def iter_events(events_root: Optional[Path] = None):
    """Yield ``(path, event_dict)`` for every parseable worker event under
    ``<root>/<worker>/events/*.json``; yield ``(path, None)`` for a malformed
    one (logged loud) so the caller can count skips without crashing."""
    root = events_root if events_root is not None else _default_events_root()
    if root is None or not root.is_dir():
        return
    for path in sorted(root.glob("*/events/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "[kaizen.binding_evidence] skipping malformed event %s (%s) — "
                "one bad file never crashes the scan", path, exc,
            )
            yield path, None
            continue
        if not isinstance(data, dict):
            logger.warning(
                "[kaizen.binding_evidence] skipping non-mapping event %s", path,
            )
            yield path, None
            continue
        yield path, data


def collect_arms(
    *,
    events_root: Optional[Path] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Aggregate the event stream into per-(skill, model) evidence arms.

    Returns::

        {
          "arms": [<arm dict>, ...],          # sorted by (skill, model)
          "window": {"days": int, "since": iso, "until": iso},
          "counts": {
            "events_seen": int,               # parseable events found
            "runs_counted": int,              # success|failed w/ model, in-window
            "skipped_malformed": int,         # unparseable / non-mapping / bad ts
            "skipped_out_of_window": int,
            "skipped_non_run": int,           # no_work etc.
            "skipped_unattributed": int,      # run status but null model
          },
        }

    READ-only; never raises on event-content problems.
    """
    now_dt = now or datetime.now(timezone.utc)
    since = now_dt - timedelta(days=window_days)

    counts = {
        "events_seen": 0,
        "runs_counted": 0,
        "skipped_malformed": 0,
        "skipped_out_of_window": 0,
        "skipped_non_run": 0,
        "skipped_unattributed": 0,
    }
    # (skill, model) -> accumulator
    acc: Dict["tuple[str, str]", Dict[str, Any]] = {}

    for _path, ev in iter_events(events_root):
        if ev is None:
            counts["skipped_malformed"] += 1
            continue
        counts["events_seen"] += 1
        ts = _parse_ts(ev.get("ts"))
        if ts is None:
            counts["skipped_malformed"] += 1
            logger.warning(
                "[kaizen.binding_evidence] event %s has a missing/bad ts — "
                "skipped", _path,
            )
            continue
        if ts < since or ts > now_dt:
            counts["skipped_out_of_window"] += 1
            continue
        status = ev.get("status")
        if status not in _RUN_STATUSES:
            counts["skipped_non_run"] += 1
            continue
        skill = ev.get("skill") or ""
        model = ev.get("model")
        if not skill or not isinstance(model, str) or not model:
            counts["skipped_unattributed"] += 1
            continue

        a = acc.setdefault(
            (skill, model),
            {"n": 0, "successes": 0, "scored": []},
        )
        a["n"] += 1
        counts["runs_counted"] += 1
        if status == "success":
            a["successes"] += 1
            # Quality rider — .get() covers both the null rider (ungated) and
            # the pre-rider legacy shape (keys absent entirely): either way the
            # run counts toward success_rate and contributes no score.
            score = ev.get("quality_score")
            if score is not None:
                a["scored"].append(
                    {
                        "score": float(score),
                        "rubric_version": ev.get("rubric_version"),
                        "evaluator_model": ev.get("evaluator_model"),
                        "redrafted": bool(ev.get("redraft_count") or 0),
                    }
                )

    arms: List[Dict[str, Any]] = []
    for (skill, model), a in sorted(acc.items()):
        arm = _build_arm(skill, model, a, window_days, since, now_dt)
        arms.append(arm)

    return {
        "arms": arms,
        "window": {
            "days": window_days,
            "since": since.isoformat(),
            "until": now_dt.isoformat(),
        },
        "counts": counts,
    }


def _build_arm(
    skill: str,
    model: str,
    a: Dict[str, Any],
    window_days: int,
    since: datetime,
    now_dt: datetime,
) -> Dict[str, Any]:
    n = a["n"]
    successes = a["successes"]

    # R-A8 — group scored evidence by the comparability key; never merge
    # across keys.
    groups: Dict["tuple", List[Dict[str, Any]]] = {}
    for s in a["scored"]:
        key = (s["rubric_version"], s["evaluator_model"])
        groups.setdefault(key, []).append(s)

    judge_groups: List[Dict[str, Any]] = []
    for (rubric_version, evaluator_model), scored in sorted(
        groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))
    ):
        mean, var = _mean_var([s["score"] for s in scored])
        redraft_rate = round(
            sum(1 for s in scored if s["redrafted"]) / len(scored), 4
        )
        judge_groups.append(
            {
                "rubric_version": rubric_version,
                "evaluator_model": evaluator_model,
                "scored_n": len(scored),
                "score_mean": mean,
                "score_variance": var,
                "redraft_rate": redraft_rate,
                # R-A2 — the arm's model IS the judge: self-graded homework,
                # excluded from downgrade evidence at the producer.
                "self_judged": model == evaluator_model,
                # R-B5 — same provider org, different model: annotated for
                # operator judgment, not an automatic exclusion.
                "family_judged": (
                    model != evaluator_model
                    and _provider_org(model) is not None
                    and _provider_org(model) == _provider_org(evaluator_model)
                ),
            }
        )

    mixed_judge = len(judge_groups) > 1
    sole = judge_groups[0] if len(judge_groups) == 1 else None

    return {
        "skill": skill,
        "model": model,
        "n": n,
        "successes": successes,
        "failures": n - successes,
        "success_rate": round(successes / n, 4),
        "scored_n": sum(g["scored_n"] for g in judge_groups),
        # Top-level score fields carry the SOLE key's statistics; a mixed-judge
        # window keeps them None (annotated, never averaged — R-A8).
        "score_mean": sole["score_mean"] if sole else None,
        "score_variance": sole["score_variance"] if sole else None,
        "redraft_rate": sole["redraft_rate"] if sole else None,
        "comparability_key": (
            [sole["rubric_version"], sole["evaluator_model"]] if sole else None
        ),
        "mixed_judge": mixed_judge,
        "judge_groups": judge_groups,
        "self_judged": bool(sole and sole["self_judged"]),
        "family_judged": bool(sole and sole["family_judged"]),
        "window": {
            "days": window_days,
            "since": since.isoformat(),
            "until": now_dt.isoformat(),
        },
    }

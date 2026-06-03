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
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from grove.pattern_cache import CompiledPattern, STATUS_SUSPENDED, t0_key

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


# Trailing characters that are pure formatting, not answer content. The static
# variance gate compares responses AFTER stripping these so a model that
# answers "4" on one turn and "4." on the next is recognized as STABLE, not
# varying (Sprint 56 Fix #2). Genuine answer differences ("4" vs "5") still
# diverge — only leading/trailing whitespace and sentence punctuation collapse.
_RESPONSE_TRIM = " \t\n\r.!?"


def _normalize_response(text: str) -> str:
    """Collapse trailing/leading whitespace + sentence punctuation for the
    static variance comparison. ``"4."`` and ``"4"`` → ``"4"``; ``"4"`` and
    ``"5"`` stay distinct."""
    return text.strip().strip(_RESPONSE_TRIM).strip()


def _modal_response(responses: List[str]) -> str:
    """The most common raw response among the evidence — cached verbatim so
    the operator sees a natural answer. Ties resolve to first-seen (Counter
    preserves insertion order in CPython 3.7+), keeping the choice
    deterministic across runs."""
    return collections.Counter(responses).most_common(1)[0][0]


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


# ── compilation (Sprint 48 Phase 2) ───────────────────────────────────


def _evidence_hash(turn_ids) -> str:
    seed = ",".join(sorted(turn_ids))
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def compile_candidate(
    candidate: Candidate,
    evidence_records: List[Any],
    *,
    now_iso: Optional[str] = None,
) -> Optional[CompiledPattern]:
    """Compile a candidate into a ``CompiledPattern`` (status=suspended), or
    ``None`` if the evidence cannot be safely compiled.

    static: every captured ``response_content`` across the evidence must be
            IDENTICAL (the variance gate — GATE-A decision 3/4). None if they
            vary, or if no response was captured (legacy records).
    executable: every captured ``tool_invocation`` must name the SAME tool;
            stores the most-recent ``{tool, args}`` as the representative
            invocation. None if the tool varies or none was captured.
    """
    now = now_iso or datetime.now(timezone.utc).isoformat()
    cached_response: Optional[str] = None
    compiled_invocation: Optional[str] = None

    if candidate.cacheable_type == "static":
        responses = [
            r.response_content for r in evidence_records
            if getattr(r, "response_content", None) is not None
        ]
        if not responses:
            return None
        # Sprint 56 Fix #2 — compare on the normalized form (trailing
        # punctuation/whitespace stripped) so trivial formatting differences
        # don't read as variance; cache the modal RAW form so the operator
        # sees a natural answer.
        if len({_normalize_response(r) for r in responses}) != 1:
            return None
        cached_response = _modal_response(responses)
    else:  # executable
        invocations = [
            r.tool_invocation for r in evidence_records
            if getattr(r, "tool_invocation", None) is not None
        ]
        if not invocations:
            return None
        tools = set()
        for inv in invocations:
            try:
                tools.add(json.loads(inv).get("tool"))
            except Exception:
                tools.add(None)
        if len(tools) != 1 or None in tools:
            return None   # tool varies / unparseable → not a clean executable
        compiled_invocation = invocations[-1]

    promotion_evidence = json.dumps({
        "repetition_count": candidate.repetition_count,
        "time_span_days": candidate.time_span_days,
        "rejection_count": candidate.rejection_count,
    }, sort_keys=True)

    return CompiledPattern(
        pattern_id=candidate.t0_key,
        t0_key=candidate.t0_key,
        intent_class=candidate.intent_class,
        cacheable_type=candidate.cacheable_type,
        cached_response=cached_response,
        compiled_invocation=compiled_invocation,
        evidence_hash=_evidence_hash(candidate.evidence_turn_ids),
        status=STATUS_SUSPENDED,
        created_at=now,
        promotion_evidence=promotion_evidence,
    )


def compile_from_store(
    candidate: Candidate, store: Any, *, now_iso: Optional[str] = None,
) -> Optional[CompiledPattern]:
    """Fetch the candidate's evidence records from ``store`` (by turn id) and
    compile. Convenience wrapper over :func:`compile_candidate`."""
    wanted = set(candidate.evidence_turn_ids)
    evidence = [r for r in store.latest_by_turn() if r.turn_id in wanted]
    return compile_candidate(candidate, evidence, now_iso=now_iso)


# ── promotion proposals (Sprint 48 Phase 3) ───────────────────────────


def _synth_pattern_eval_hash(pattern_id: str) -> str:
    return "sha256:" + hashlib.sha256(
        f"pattern_promotion|{pattern_id}".encode("utf-8")
    ).hexdigest()


def propose_pattern_promotions(
    store: Any,
    pattern_store: Any,
    *,
    queue_path: Optional[Path] = None,
    now_iso: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Scan → compile → queue. For each candidate not already in the pattern
    store (never re-propose a known/rejected pattern), compile it; if it
    compiles safely, write the suspended entry to ``pattern_store`` and queue a
    ``pattern_promotion`` proposal. Returns the queued proposal ids.

    The system PROPOSES; the operator approves (GATE-A). This never activates
    a pattern."""
    from grove.eval.proposal_queue import (
        RoutingProposal,
        PROPOSAL_TYPE_PATTERN_PROMOTION,
        compute_proposal_id,
        append as _queue_append,
    )

    cfg = config or load_pattern_cache_config()
    candidates = scan_candidates(store, cfg)
    known = {p.pattern_id for p in pattern_store.all()}  # compiled / active / rejected
    now = now_iso or datetime.now(timezone.utc).isoformat()
    queued: List[str] = []

    for cand in candidates:
        if cand.t0_key in known:
            continue  # never re-propose a known pattern (incl. rejected)
        compiled = compile_from_store(cand, store, now_iso=now)
        if compiled is None:
            continue  # not safely compilable (variance / legacy / tool drift)
        pattern_store.upsert(compiled)  # status=suspended until approved

        payload = {
            "pattern_id": cand.t0_key,
            "t0_key": cand.t0_key,
            "intent_class": cand.intent_class,
            "cacheable_type": cand.cacheable_type,
            "evidence_hash": compiled.evidence_hash,
            "promotion_evidence": {
                "repetition_count": cand.repetition_count,
                "time_span_days": cand.time_span_days,
                "rejection_count": cand.rejection_count,
            },
            "sample_queries": list(cand.sample_queries),
        }
        evidence = tuple(cand.evidence_turn_ids)
        proposal = RoutingProposal(
            proposal_id=compute_proposal_id(
                type=PROPOSAL_TYPE_PATTERN_PROMOTION, payload=payload, evidence=evidence,
            ),
            type=PROPOSAL_TYPE_PATTERN_PROMOTION,
            payload=payload,
            evidence=evidence,
            eval_hash=_synth_pattern_eval_hash(cand.t0_key),
            created_at=now,
        )
        if _queue_append(proposal, path=queue_path):
            queued.append(proposal.proposal_id)
    return queued

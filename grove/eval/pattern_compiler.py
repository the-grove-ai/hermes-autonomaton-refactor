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
    # conversation is small-talk: no tool, no stable answer — excluded so it
    # doesn't drop every scan (Sprint 56 Fix #4).
    "exclude_intents": ["unknown", "system_admin", "conversation"],
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
        # GRV-010 C1c-i — store the promotion-time realpath-canonical effect
        # signature alongside the invocation. The T0 hit site re-derives it
        # (realpath re-resolves) and binds-and-verifies; a symlink swapped under
        # the target since promotion, or stale args, fail the check and fall to
        # the classified path. Unsignable → leave unsigned (hit-site fail-safe).
        try:
            from grove.effect_signature import canonical_effect_signature
            _inv_obj = json.loads(compiled_invocation)
            _inv_obj["approved_signature"] = canonical_effect_signature(
                _inv_obj.get("tool"), _inv_obj.get("args") or {},
            )
            compiled_invocation = json.dumps(_inv_obj, sort_keys=True)
        except Exception:
            pass

    promotion_evidence = json.dumps({
        "repetition_count": candidate.repetition_count,
        "time_span_days": candidate.time_span_days,
        "rejection_count": candidate.rejection_count,
        # Sprint 56 — carry the sample queries so `flywheel patterns list`
        # can show the operator WHAT each pattern matches, not just a hash.
        "sample_queries": list(candidate.sample_queries),
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


# Disposition status values (Sprint 56 Fix #1 — no silent drops). Every
# candidate the scanner finds gets one, surfaced to the operator.
DISPOSITION_PROPOSED = "proposed"
DISPOSITION_SKIPPED_KNOWN = "skipped_known"
DISPOSITION_DROPPED_VARIANCE = "dropped_variance"
DISPOSITION_DROPPED_NO_CONTENT = "dropped_no_content"
DISPOSITION_DROPPED_NO_TOOL = "dropped_no_tool"
DISPOSITION_DROPPED_TOOL_DRIFT = "dropped_tool_drift"


@dataclass(frozen=True)
class CandidateDisposition:
    """What happened to one scanned candidate in ``propose_pattern_promotions``.

    The operator sees one of these per candidate — no candidate is ever
    dropped silently (Sprint 56 Fix #1 / FAIL LOUD)."""
    t0_key: str
    intent_class: str
    cacheable_type: str
    sample_query: str
    repetition_count: int
    status: str
    detail: str
    proposal_id: Optional[str] = None


@dataclass(frozen=True)
class PromotionResult:
    """The outcome of a ``--propose`` run: one disposition per candidate."""
    dispositions: tuple

    @property
    def proposed(self) -> List[str]:
        """The queued proposal ids (back-compat with the prior list return)."""
        return [
            d.proposal_id for d in self.dispositions
            if d.status == DISPOSITION_PROPOSED and d.proposal_id
        ]


def drop_reason(candidate: Candidate, evidence: List[Any]) -> Optional[str]:
    """Why :func:`compile_candidate` would drop this candidate — or ``None``
    if it compiles cleanly.

    Mirrors the compile gates and SHARES :func:`_normalize_response` with the
    static branch, so the disposition the operator sees can never disagree
    with what the compiler actually did (Sprint 56 Fix #1)."""
    if candidate.cacheable_type == "static":
        responses = [
            r.response_content for r in evidence
            if getattr(r, "response_content", None) is not None
        ]
        if not responses:
            return DISPOSITION_DROPPED_NO_CONTENT
        if len({_normalize_response(r) for r in responses}) != 1:
            return DISPOSITION_DROPPED_VARIANCE
        return None
    invocations = [
        r.tool_invocation for r in evidence
        if getattr(r, "tool_invocation", None) is not None
    ]
    if not invocations:
        return DISPOSITION_DROPPED_NO_TOOL
    tools = set()
    for inv in invocations:
        try:
            tools.add(json.loads(inv).get("tool"))
        except Exception:
            tools.add(None)
    if len(tools) != 1 or None in tools:
        return DISPOSITION_DROPPED_TOOL_DRIFT
    return None


def propose_pattern_promotions(
    store: Any,
    pattern_store: Any,
    *,
    queue_path: Optional[Path] = None,
    now_iso: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> "PromotionResult":
    """Scan → compile → queue, returning a :class:`PromotionResult` with one
    :class:`CandidateDisposition` per scanned candidate.

    Every candidate is accounted for — proposed, skipped because it is already
    in the cache, or dropped with a specific reason (no captured content,
    response variance, no tool, tool drift). NOTHING is dropped silently
    (Sprint 56 Fix #1 / FAIL LOUD). Read ``result.proposed`` for the queued
    ids (back-compat with the prior list return).

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
    # Fetch the evidence once and index by turn id so the per-candidate
    # disposition and the compile read the SAME records.
    by_turn = {r.turn_id: r for r in store.latest_by_turn()}
    dispositions: List[CandidateDisposition] = []

    def _disp(cand: Candidate, status: str, detail: str,
              proposal_id: Optional[str] = None) -> CandidateDisposition:
        return CandidateDisposition(
            t0_key=cand.t0_key,
            intent_class=cand.intent_class,
            cacheable_type=cand.cacheable_type,
            sample_query=cand.sample_queries[0] if cand.sample_queries else "",
            repetition_count=cand.repetition_count,
            status=status,
            detail=detail,
            proposal_id=proposal_id,
        )

    for cand in candidates:
        if cand.t0_key in known:
            dispositions.append(_disp(
                cand, DISPOSITION_SKIPPED_KNOWN,
                "already compiled/active/rejected in the pattern cache",
            ))
            continue

        evidence = [by_turn[t] for t in cand.evidence_turn_ids if t in by_turn]
        reason = drop_reason(cand, evidence)
        if reason is not None:
            _DETAIL = {
                DISPOSITION_DROPPED_NO_CONTENT:
                    "no response_content captured in the evidence (legacy records)",
                DISPOSITION_DROPPED_VARIANCE:
                    "the captured responses differ — not safely static-cacheable",
                DISPOSITION_DROPPED_NO_TOOL:
                    "no tool invocation captured — not an executable pattern",
                DISPOSITION_DROPPED_TOOL_DRIFT:
                    "the captured tool invocations name different tools",
            }
            dispositions.append(_disp(cand, reason, _DETAIL[reason]))
            continue

        compiled = compile_candidate(cand, evidence, now_iso=now)
        if compiled is None:
            # drop_reason said this compiles, but compile disagreed — a real
            # inconsistency, not something to swallow. FAIL LOUD.
            raise RuntimeError(
                f"compile/drop_reason disagree for {cand.t0_key}: "
                f"drop_reason=ok but compile_candidate returned None"
            )
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
        evidence_ids = tuple(cand.evidence_turn_ids)
        proposal = RoutingProposal(
            proposal_id=compute_proposal_id(
                type=PROPOSAL_TYPE_PATTERN_PROMOTION, payload=payload,
                evidence=evidence_ids,
            ),
            type=PROPOSAL_TYPE_PATTERN_PROMOTION,
            payload=payload,
            evidence=evidence_ids,
            eval_hash=_synth_pattern_eval_hash(cand.t0_key),
            created_at=now,
            proposer="pattern_compiler",  # proposal-proposer-attribution-v1 (#8)
        )
        if _queue_append(proposal, path=queue_path):
            dispositions.append(_disp(
                cand, DISPOSITION_PROPOSED, "queued for operator approval",
                proposal_id=proposal.proposal_id,
            ))
        else:
            # Idempotent queue: an identical proposal is already pending. Not a
            # drop — surface it as already-known so the count is honest.
            dispositions.append(_disp(
                cand, DISPOSITION_SKIPPED_KNOWN,
                "an identical proposal is already in the queue",
                proposal_id=proposal.proposal_id,
            ))

    return PromotionResult(dispositions=tuple(dispositions))

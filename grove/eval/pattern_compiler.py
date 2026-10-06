"""T0 pattern compiler — scanner (Sprint 48 Phase 1) + compiler (Phase 2).

Sibling to ``tier_ratchet.py``: both read the IntentStore evidence. The tier
ratchet aggregates by ``intent_class`` to propose tier moves; this module
aggregates by ``(intent_class, t0_key)`` to identify stable patterns that can
retire to the deterministic T0 cache, and compiles them into cache entries.

T0 is DETERMINISTIC — a T0 hit returns a compiled pattern with no model call.
Per GATE-A: the system PROPOSES T0 promotion; the operator approves; the
system never self-promotes. Thresholds live in ``routing.operational.yaml`` under
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
from grove.router_merge import load_operational_routing_config

# Defaults — used when routing.operational.yaml carries no pattern_cache section.
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
    # How closely two requests' content words must overlap to count as the
    # same request, for clustering and for serving (grove.intent_match).
    # 1.0 = identical content words only.
    "match_threshold": 0.8,
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
    # Every distinct phrasing in the cluster, and the overlap threshold that
    # clustered them — stored on the compiled entry so serving matches the
    # same way detection did.
    phrasings: tuple = ()
    match_threshold: float = 1.0


def load_pattern_cache_config() -> Dict[str, Any]:
    """Read the ``pattern_cache`` thresholds from routing.operational.yaml.

    Operator copy (``~/.grove/routing.operational.yaml``) wins over the repo
    default (``config/routing.operational.yaml``). Missing/partial sections fall
    back to :data:`_DEFAULTS`."""
    cfg = dict(_DEFAULTS)
    candidates = (
        Path.home() / ".grove" / "routing.operational.yaml",
        Path(__file__).resolve().parents[2] / "config" / "routing.operational.yaml",
    )
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = load_operational_routing_config(
                path, path.parent / "routing.autonomaton.yaml"
            ) or {}
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

    # Group by meaning, not by exact text: a request joins the first cluster
    # (same intent class) whose founding phrasing it overlaps at or above the
    # configured threshold (grove.intent_match — deterministic token overlap).
    # A cluster is keyed by its founding phrasing's t0_key, so a cluster of
    # identically-worded requests is exactly the group it always was. At
    # threshold 1.0 only identical content words cluster.
    from grove.intent_match import overlap

    threshold = float(cfg.get("match_threshold", 0.8))
    founders: Dict[str, list] = collections.defaultdict(list)   # intent → [(stem, key)]
    groups: Dict[tuple, list] = collections.defaultdict(list)
    for rec in store.latest_by_turn():
        ic = rec.intent_class
        if not ic or ic == "unknown" or ic in exclude:
            continue
        stem = rec.user_message_stem or ""
        exact = t0_key(ic, stem)
        key = exact if (ic, exact) in groups else None
        if key is None:
            for founder_stem, founder_key in founders[ic]:
                if overlap(stem, founder_stem) >= threshold:
                    key = founder_key
                    break
        if key is None:
            key = exact
            founders[ic].append((stem, key))
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
            phrasings=tuple(dict.fromkeys(r.user_message_stem for r in recs)),
            match_threshold=threshold,
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
        # A cluster of differently-worded requests is served the way it was
        # detected: by overlap with its recorded phrasings. A pattern whose
        # requests were all worded alike declares no match block and serves
        # exact text only.
        **({"match": {"examples": list(candidate.phrasings),
                      "threshold": candidate.match_threshold}}
           if len(candidate.phrasings) > 1 else {}),
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
    ledger: Any = None,
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
        read_all as _queue_read_all,
    )

    cfg = config or load_pattern_cache_config()
    candidates = scan_candidates(store, cfg)
    # compiled / active / rejected. A keg's pattern_id is not its t0_key, so
    # match on both: a request a keg already answers is never also compiled as a
    # plain replay of one cached tool call.
    known = set()
    for p in pattern_store.all():
        known.add(p.pattern_id)
        known.add(p.t0_key)
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
        # The scanner is a Jidoka detector: repetition. It pulls the andon
        # cord and returns; the handler routes the event to Kaizen, whose
        # answer files the proposal compiled above. A rescan of a pattern
        # already waiting saw nothing new and raises nothing.
        already_queued = any(
            p.proposal_id == proposal.proposal_id for p in _queue_read_all(path=queue_path)
        )
        queued = {"ok": False}

        def _file(andon: Dict[str, Any], _proposal=proposal, _queued=queued) -> Optional[str]:
            _queued["ok"] = bool(_queue_append(_proposal, path=queue_path))
            return _proposal.proposal_id if _queued["ok"] else None

        if not already_queued:
            _raise_repetition(cand, _file, ledger=ledger)
        if queued["ok"]:
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


def _goal_for_request(request: str) -> Optional[str]:
    """The Dock goal a repeated request relates to, by the goal's own declared
    keywords, or None. A Dock fault means "no goal", never a failed scan."""
    try:
        from grove.decision_work import _keyword_matches
        from grove.dock import load_dock

        dock = load_dock()
        for goal in (getattr(dock, "goals", None) or ()):
            if _keyword_matches(request, goal.keywords):
                return goal.id
    except Exception:  # noqa: BLE001
        return None
    return None


def _raise_repetition(cand: Candidate, propose: Any, *, ledger: Any = None) -> Dict[str, Any]:
    """The scanner as a detector: pull the andon cord for a repeated request.
    ``propose`` — the proposal this scan compiled — rides along as context,
    and the handler's route to Kaizen is what files it."""
    from grove.andon import raise_andon
    from grove.keg import FLAG_TIER_DOWN_PATTERN

    request = cand.sample_queries[0] if cand.sample_queries else ""
    return raise_andon(
        FLAG_TIER_DOWN_PATTERN, detector="repetition",
        goal=_goal_for_request(request),
        summary=(
            f"the same {cand.intent_class} request was answered the same way "
            f"{cand.repetition_count} times"
        ),
        evidence=[{"turn_id": t} for t in cand.evidence_turn_ids],
        details={"t0_key": cand.t0_key, "cacheable_type": cand.cacheable_type},
        observed_input={"request": request, "phrasings": len(cand.phrasings)},
        context={"propose": propose},
        ledger=ledger,
    )


# ── keg proposals (GRV-004 executable kegs on the T0 cache) ───────────
#
# Kaizen's half of the improvement loop. Jidoka flags (a tier-down pattern or
# an anomaly) and pulls the andon cord; the caller hands that flag here with
# the drafted rules and the history to replay. This writes the DRAFT cache
# entry (suspended — a draft never serves) and queues the proposal. It never
# activates anything: only the operator's signature does.

BACKTEST_UNCHANGED = "unchanged"
BACKTEST_WOULD_CHANGE = "would_change"
BACKTEST_NOT_COVERED = "not_covered"
_BACKTEST_ORDER = {BACKTEST_WOULD_CHANGE: 0, BACKTEST_NOT_COVERED: 1, BACKTEST_UNCHANGED: 2}

DISPOSITION_DROPPED_BACKTEST_CONFLICT = "dropped_backtest_conflict"


@dataclass(frozen=True)
class KegProposalResult:
    """What happened to one drafted keg: proposed, skipped because the
    identical keg is already known, or dropped because its replay contradicts
    a coding the operator confirmed."""
    status: str
    detail: str
    pattern_id: str
    version: int
    proposal_id: Optional[str] = None
    backtest: Optional[Dict[str, Any]] = None


def backtest_keg(spec: Dict[str, Any], history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Replay ``spec`` over ``history`` and return the backtest envelope.

    Each history case is ``{"ref", "label", "inputs", "served", "confirmed"}``:
    ``served`` is what standard work produced at the time, ``confirmed`` is the
    operator's ground truth for that case (None when never confirmed). A case is

    * ``unchanged`` — the keg answers exactly what was served;
    * ``would_change`` — the keg answers differently (``agrees_with_confirmed``
      says whether the new answer matches the operator's ground truth);
    * ``not_covered`` — the keg does not answer it; it stays with the
      interpreter, so nothing changes for it either.

    A case marked ``served_by_keg`` that this keg would NOT answer (it defers,
    or no rule matches) is a ``would_change``: work standard work used to
    answer goes back to the interpreter.

    Cases come back changed-first, so a reviewer reads the edge cases before
    the routine ones."""
    from grove.keg import defers, evaluate

    cases: List[Dict[str, Any]] = []
    for index, case in enumerate(history):
        served = dict(case.get("served") or {})
        confirmed = case.get("confirmed")
        answer = evaluate(spec, case.get("inputs") or {})
        deferred = answer is None and defers(spec, case.get("inputs") or {})
        if answer is None and case.get("served_by_keg"):
            # Standard work answered this case before; this keg would hand it
            # back to the interpreter. That IS a change. It agrees with the
            # operator when they corrected the old answer; handing back an
            # answer they confirmed loses good coverage and is a conflict.
            result = BACKTEST_WOULD_CHANGE
            agrees = None if confirmed is None else dict(confirmed) != served
        elif answer is None:
            result, agrees = BACKTEST_NOT_COVERED, None
        else:
            same = all(answer.get(k) == served.get(k) for k in answer)
            result = BACKTEST_UNCHANGED if same else BACKTEST_WOULD_CHANGE
            agrees = None if confirmed is None else all(
                answer.get(k) == confirmed.get(k) for k in answer
            )
        cases.append({
            "ref": str(case.get("ref") or ""),
            "label": str(case.get("label") or ""),
            "inputs": dict(case.get("inputs") or {}),
            "served": served,
            "confirmed": None if confirmed is None else dict(confirmed),
            "keg": answer,
            "deferred": bool(deferred),
            "result": result,
            "agrees_with_confirmed": agrees,
            "_order": index,
        })
    cases.sort(key=lambda c: (_BACKTEST_ORDER[c["result"]], c["_order"]))
    for c in cases:
        del c["_order"]
    counts = collections.Counter(c["result"] for c in cases)
    return {
        "kind": "keg_backtest",
        "replayed": len(cases),
        # "not covered" changes nothing: the interpreter keeps that work.
        "unchanged": counts[BACKTEST_UNCHANGED] + counts[BACKTEST_NOT_COVERED],
        "would_change": counts[BACKTEST_WOULD_CHANGE],
        "not_covered": counts[BACKTEST_NOT_COVERED],
        "cases": cases,
    }


def propose_keg(
    pattern_store: Any,
    *,
    name: str,
    request: str,
    requests: Any = (),
    match_threshold: Optional[float] = None,
    verb_bonus: Optional[float] = None,
    sessions: Optional[str] = None,
    intent_class: str,
    tool_name: str,
    tool_args: Optional[Dict[str, Any]] = None,
    inputs: Dict[str, Any],
    outputs: Dict[str, Any],
    conditions: List[Dict[str, Any]],
    scope_text: str,
    reserve: str,
    dock_goal: str,
    scope: str,
    authority_level: str,
    flag: str,
    flag_detail: str,
    evidence_turn_ids: Any,
    history: List[Dict[str, Any]],
    feedback: Optional[List[str]] = None,
    lineage: Optional[str] = None,
    andon_id: Optional[str] = None,
    queue_path: Optional[Path] = None,
    ledger: Any = None,
    now_iso: Optional[str] = None,
) -> KegProposalResult:
    """Draft a keg, backtest it on history and propose it.

    ``request`` is the operator request the keg answers (the T0 lookup key);
    ``tool_name`` / ``tool_args`` are the model-free invocation that applies
    the keg. The whole keg — scope, authority level and the rule table — rides
    INSIDE that invocation's arguments, which the bind-and-verify signature
    covers: what the operator signs is byte-for-byte what T0 runs.

    Nothing here knows the domain. The field names, the rules, the scope and
    authority level, the request and the tool all arrive from the caller, which
    reads them from the Dock goal's config and its skill; a new kind of work is
    a new goal and a new skill, not a change to this function.

    ``flag`` is what Jidoka flagged (``tier_down_pattern`` or ``anomaly``).
    The version is the next one after the last SIGNED version of this keg; a
    draft the operator sent back does not consume a number. ``lineage`` scopes
    that count: versions are numbered within one lineage (a decision-work run),
    so a fresh run starts again at v1 while earlier runs' kegs stay on record.
    ``andon_id`` is the andon event this proposal answers.
    """
    from grove import keg as keg_mod
    from grove.effect_signature import canonical_effect_signature
    from grove.eval.proposal_queue import (
        RoutingProposal,
        PROPOSAL_TYPE_PATTERN_PROMOTION,
        compute_proposal_id,
        append as _queue_append,
    )
    from grove.intent_store import normalize_message_stem

    if flag not in keg_mod.FLAGS:
        raise ValueError(f"keg flag must be one of {keg_mod.FLAGS}, got {flag!r}")
    evidence_ids = tuple(str(t) for t in evidence_turn_ids)
    if not evidence_ids:
        raise ValueError("a keg proposal needs evidence turn ids")
    now = now_iso or datetime.now(timezone.utc).isoformat()
    slug = keg_mod.keg_slug(name)

    # Version lineage: the last signed version of this keg, if any.
    prior_id: Optional[str] = None
    prior_version = 0
    for existing in pattern_store.all():
        record = keg_mod.keg_record(existing).get("keg") or {}
        if record.get("slug") != slug or not existing.promoted_at:
            continue
        if record.get("lineage") != lineage:
            continue
        if int(record.get("version") or 0) > prior_version:
            prior_version, prior_id = int(record["version"]), existing.pattern_id
    version = prior_version + 1

    spec = {
        "protocol": keg_mod.KEG_PROTOCOL,
        "protocolVersion": keg_mod.KEG_PROTOCOL_VERSION,
        "name": name,
        "version": version,
        "scope": scope,
        "authority_level": authority_level,   # what it may do once granted
        "dock_goal": dock_goal,
        "reserve": reserve,
        "trigger": {
            "request": request,
            # Further requests the keg also answers, matched as exact text.
            **({"requests": [str(r) for r in requests]} if requests else {}),
            # How closely a request must overlap one of those examples to be
            # this keg's (grove.intent_match). Absent: exact text only.
            **({"match_threshold": float(match_threshold)}
               if match_threshold is not None else {}),
            # Where the keg answers. "goal_isolated": only in a session
            # isolated to its Dock goal — elsewhere the request is simply not
            # the keg's. Absent: any session.
            **({"verb_bonus": float(verb_bonus)} if verb_bonus else {}),
            **({"sessions": sessions} if sessions else {}),
            "intent_class": intent_class,
        },
        "inputs": inputs,
        "outputs": outputs,
        "conditions": conditions,
    }
    keg_mod.validate_spec(spec)

    digest = keg_mod.rules_digest(spec, evidence_ids, lineage)
    pattern_id = f"keg:{slug}:v{version}:{digest[:12]}"
    # The slug in a keg's bookkeeping is what ties versions together; read it
    # with its lineage (see keg_record) rather than by parsing this id.
    backtest = backtest_keg(spec, history)

    def _result(status: str, detail: str, proposal_id: Optional[str] = None):
        return KegProposalResult(
            status=status, detail=detail, pattern_id=pattern_id, version=version,
            proposal_id=proposal_id, backtest=backtest,
        )

    if pattern_store.get(pattern_id) is not None:
        return _result(
            DISPOSITION_SKIPPED_KNOWN,
            "this exact keg (same rules, same evidence) was already drafted",
        )
    conflicts = [
        c for c in backtest["cases"] if c["agrees_with_confirmed"] is False
    ]
    if conflicts:
        # Kaizen never proposes an update that contradicts ground truth.
        return _result(
            DISPOSITION_DROPPED_BACKTEST_CONFLICT,
            f"replay contradicts {len(conflicts)} confirmed case(s): "
            + ", ".join(c["ref"] or c["label"] for c in conflicts),
        )

    args = dict(tool_args or {})
    args["keg"] = spec
    invocation = {
        "tool": tool_name,
        "args": args,
        "approved_signature": canonical_effect_signature(tool_name, args),
    }
    key = t0_key(intent_class, normalize_message_stem(request))
    record = {
        "keg": {"name": name, "slug": slug, "version": version, "lineage": lineage,
                "dock_goal": dock_goal},
        "andon_id": andon_id,
        "repetition_count": len(evidence_ids),
        "flag": flag,
        "supersedes": prior_id,
        "feedback": list(feedback or []),
    }
    pattern_store.upsert(CompiledPattern(
        pattern_id=pattern_id,
        t0_key=key,
        intent_class=intent_class,
        cacheable_type="executable",
        cached_response=None,
        compiled_invocation=json.dumps(invocation, sort_keys=True),
        evidence_hash=_evidence_hash(evidence_ids),
        status=STATUS_SUSPENDED,     # a draft never serves
        created_at=now,
        promotion_evidence=json.dumps(record, sort_keys=True),
    ))

    payload = {
        "pattern_id": pattern_id,
        "t0_key": key,
        "intent_class": intent_class,
        "cacheable_type": "executable",
        "evidence_hash": _evidence_hash(evidence_ids),
        "promotion_evidence": {"repetition_count": len(evidence_ids)},
        "sample_queries": [request],
        "keg": {
            **spec,
            "flag": flag,
            "flag_detail": flag_detail,
            "andon_id": andon_id,
            "lineage": lineage,
            "supersedes": prior_id,
            "supersedes_version": prior_version or None,
            "feedback": list(feedback or []),
        },
    }
    proposal = RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_PATTERN_PROMOTION, payload=payload,
            evidence=evidence_ids,
        ),
        type=PROPOSAL_TYPE_PATTERN_PROMOTION,
        payload=payload,
        evidence=evidence_ids,
        eval_hash=_synth_pattern_eval_hash(pattern_id),
        created_at=now,
        semantic_justification=scope_text,
        proposer="kaizen",
        detail=backtest,
    )
    if not _queue_append(proposal, path=queue_path):
        return _result(
            DISPOSITION_SKIPPED_KNOWN,
            "an identical proposal is already in the queue",
            proposal_id=proposal.proposal_id,
        )
    if ledger is None:
        from grove.andon import _bus
        ledger = _bus()                # the one bus: this session's ledger
    ledger.record(
        keg_mod.LOOP_KAIZEN_PROPOSAL,
        loop_step=keg_mod.LOOP_KAIZEN_PROPOSAL,
        proposal_id=proposal.proposal_id,
        pattern_id=pattern_id,
        keg=name,
        version=version,
        flag=flag,
        andon_id=andon_id,
        supersedes=prior_id,
        dock_goal=dock_goal,
        replayed=backtest["replayed"],
        unchanged=backtest["unchanged"],
        would_change=backtest["would_change"],
        evidence_count=len(evidence_ids),
    )
    return _result(
        DISPOSITION_PROPOSED, "queued for operator signature",
        proposal_id=proposal.proposal_id,
    )

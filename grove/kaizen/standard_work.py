"""Kaizen's answer to an andon event on a goal's decision work.

The butler: it writes the update with full context, backtests it on history
and proposes it. It never commits — only the operator's signature turns a
draft into standard work.

Two drafts, one path (``pattern_compiler.propose_keg``):

  * Tier-down pattern → keg v1, drafted DETERMINISTICALLY from the goal's
    reference table: one rule per key that has a single value there. The
    confirmed decisions are the evidence that the table and the operator's
    judgment agree; the keg learns the procedure, not the list of items seen.
  * Anomaly (a miss) or operator feedback → a revision. Nothing deterministic
    can write the condition that separates the missed case, so a model drafts
    ONE condition; everything around it is checked. Kaizen prefers the
    narrowest change: on a single miss the new rule DEFERS the separated cases
    to the interpreter. Answering them with a new value needs the same
    repeated, confirmed evidence as any other rule.

Drafting follows the tier ladder upward: the cheapest declared tier first, and
a draft that fails its checks is retried one tier up with the reason. When
every tier fails, Kaizen says so through Jidoka. It never guesses a rule.

Pipeline stage: Compilation (a proposal is a draft of how future requests
compile). The model call goes through the tier primitive ``grove.t1_call`` —
routed by tier name, schema-bound — never a direct provider call.

Nothing here knows the domain: fields, tables, names and tiers all come from
the goal's ``decision_work`` declaration.
"""

from __future__ import annotations

import collections
import json
import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

from grove import keg as keg_mod

logger = logging.getLogger(__name__)

# How a keg's scope text introduces the cases it hands back. The proposal
# card shows those cases in plain words from the rules themselves, and reads
# the scope text only up to this phrase.
HANDS_BACK = " Hands back to the interpreter: "

DRAFT_TOOL = {
    "name": "propose_condition",
    "description": "Propose one condition that separates the cases described.",
    "input_schema": {
        "type": "object",
        "properties": {
            "condition": {
                "type": "string",
                "description": "One condition in the keg grammar.",
            },
            "rationale": {
                "type": "string",
                "description": "One sentence on what this condition separates.",
            },
        },
        "required": ["condition", "rationale"],
    },
}

_SYSTEM = (
    "You draft one rule condition for a deterministic rule table. A human "
    "expert reviews your draft against a replay of past work before anything "
    "changes. Write the narrowest condition that does the job."
)

_GRAMMAR = (
    "Grammar (nothing else is allowed):\n"
    "  <input> == '<text>'\n"
    "  <input> IN ['<a>', '<b>']\n"
    "  <input> NOT IN ['<a>', '<b>']\n"
    "  <input> CONTAINS '<text>'      (text inputs only; case-insensitive substring)\n"
    "  join clauses with AND / OR (AND binds tighter; no parentheses)\n"
    "Text comparisons ignore case and extra spaces."
)


def _quote(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def reference_rules(work: Any) -> List[Dict[str, Any]]:
    """One rule per single-value key in the goal's reference table."""
    ref = work.config.reference
    table = work.reference()
    return [
        {"if": f"{ref.key_input} == {_quote(key)}", "then": {ref.value_output: value}}
        for key, value in table.single_value_rows()
    ]


def _intent_class(turn_ids: List[str]) -> str:
    """The intent class the keg's T0 entry is filed under. The T0 lookup tries
    every class, so any valid one serves; prefer the class the evidence turns
    were actually classified as."""
    from grove.classify import INTENT_CLASSES

    try:
        from grove.intent_store import IntentStore
        wanted = set(turn_ids)
        seen = [
            r.intent_class for r in IntentStore().latest_by_turn()
            if r.turn_id in wanted and r.intent_class in INTENT_CLASSES
        ]
        if seen:
            return collections.Counter(seen).most_common(1)[0][0]
    except Exception as exc:  # noqa: BLE001 — cosmetic attribution only
        logger.debug("[kaizen] evidence intent class unavailable: %r", exc)
    return INTENT_CLASSES[0]


# ── drafting one condition with a model ───────────────────────────────


def _draft_prompt(
    work: Any,
    rules: List[Mapping[str, Any]],
    history: List[Mapping[str, Any]],
    *,
    target: Optional[Mapping[str, Any]],
    reason: str,
    failure: Optional[str],
) -> str:
    cfg = work.config
    lines = [
        f"Work: {cfg.goal_id}.",
        "Declared inputs: " + ", ".join(
            f"{name} ({(decl or {}).get('data_type', 'string')})"
            for name, decl in cfg.inputs.items()
        ) + ".",
        "",
        "Current rules, checked in order (first match wins):",
    ]
    for rule in rules:
        outcome = "DEFER to the interpreter" if rule.get("defer") else json.dumps(rule.get("then"))
        lines.append(f"  if {rule.get('if')} -> {outcome}")
    lines += ["", "Past cases:"]
    for case in history:
        reviewed = case["confirmed"] is not None
        lines.append(
            f"  {case['ref']}: inputs={json.dumps(case['inputs'])} "
            f"answered={json.dumps(case['served'])} "
            + (f"operator={json.dumps(case['confirmed'])}" if reviewed
               else "operator=(not reviewed: no evidence either way)")
            + ("  <-- CORRECTED" if reviewed and case["confirmed"] != case["served"] else "")
            # The operator's own reason is the best evidence of what differs.
            + (f"  operator said: {json.dumps(case['operator_said'])}"
               if case.get("operator_said") else "")
        )
    lines += ["", reason, ""]
    if target is not None:
        lines.append(
            "Write ONE condition that is TRUE for the corrected case "
            f"({json.dumps(target)}) and FALSE for every case the operator "
            "confirmed. Use what differs between them."
        )
    else:
        lines.append(
            "Write ONE condition selecting exactly the cases the feedback "
            "says the rules should stop answering, and nothing else."
        )
    lines += ["", _GRAMMAR]
    if failure:
        lines += ["", f"Your previous draft was rejected: {failure} Fix that."]
    return "\n".join(lines)


def _check_condition(
    work: Any,
    condition: str,
    history: List[Mapping[str, Any]],
    *,
    target: Optional[Mapping[str, Any]],
) -> Optional[str]:
    """Why a drafted condition is unusable, or None when it passes."""
    probe = {"inputs": work.config.inputs,
             "conditions": [{"if": condition, "defer": True}]}
    try:
        keg_mod.parse_condition(condition, work.config.inputs)
    except ValueError as exc:
        return f"it does not parse ({exc})."
    if target is None:
        # Drafted from the operator's feedback: what it should select is the
        # operator's call, shown to them in the backtest. Only the grammar is
        # checkable here.
        return None
    if not keg_mod.defers(probe, target):
        return "it is not true for the corrected case."
    wrongly = [
        case["ref"] for case in history
        if case["confirmed"] == case["served"] and keg_mod.defers(probe, case["inputs"])
    ]
    if wrongly:
        return (
            "it is also true for cases the operator confirmed: "
            + ", ".join(wrongly) + "."
        )
    return None


# A reasoning model spends output tokens thinking before it answers; a tight
# cap cuts the answer off before it exists (seen live at T2, 2026-10-06).
_DRAFT_MAX_TOKENS = 2000


def _ask(call: Any, prompt: str, tier: str) -> str:
    """One drafting call at one tier, returning the condition text.

    Schema-bound first (a forced tool call). Some routes refuse a forced tool
    (seen live at T3: a 400 on ``tool_choice``); for those the same question is
    asked as plain text, answer on one line. Either way the text is only a
    DRAFT: it is parsed against the grammar and checked before anything is
    proposed, so the transport cannot smuggle in an unchecked rule."""
    try:
        out = call(prompt, system=_SYSTEM, tool=DRAFT_TOOL, tier=tier,
                   max_tokens=_DRAFT_MAX_TOKENS)
        return str((out or {}).get("condition") or "").strip()
    except Exception as exc:  # noqa: BLE001
        if "tool_choice" not in str(exc) and "tool" not in type(exc).__name__.lower():
            raise
        logger.info("[kaizen] %s refused a forced tool; asking as text: %r", tier, exc)
    text = call(
        prompt + "\n\nReply with the condition ONLY, on one line. No prose, no quotes around it.",
        system=_SYSTEM, tier=tier, max_tokens=_DRAFT_MAX_TOKENS,
    )
    lines = [ln.strip().strip("`") for ln in str(text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


class DraftOverBudget(Exception):
    """A drafting call gave no answer inside the goal's time budget for one
    model call. That tier's draft has failed; the next tier is tried."""


def _within(seconds: Optional[float], fn: Any) -> Any:
    """Run ``fn`` and return its result, waiting at most ``seconds`` (None: as
    long as it takes). Past the budget the call is left to finish on its own
    and :class:`DraftOverBudget` is raised: nothing waits on it any longer.
    The caller's context goes with the call, so what it spends is still
    metered on the attempt."""
    if not seconds:
        return fn()
    import contextvars
    import threading

    box: Dict[str, Any] = {}
    context = contextvars.copy_context()

    def run() -> None:
        try:
            box["value"] = context.run(fn)
        except BaseException as exc:  # noqa: BLE001 — handed back to the caller
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True, name="kaizen-draft")
    worker.start()
    worker.join(float(seconds))
    if worker.is_alive():
        raise DraftOverBudget(f"no answer in {float(seconds):g} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def draft_condition(
    work: Any,
    rules: List[Mapping[str, Any]],
    history: List[Mapping[str, Any]],
    *,
    target: Optional[Mapping[str, Any]],
    reason: str,
    call: Any = None,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Draft one separating condition, cheapest tier first. Returns
    ``(condition or None, attempts)``; ``attempts`` records every tier tried
    and why its draft was refused — the trail of failing upward."""
    if call is None:
        from grove.t1_call import call_t1 as call
    attempts: List[Dict[str, Any]] = []
    failure: Optional[str] = None
    for tier in work.config.keg.revision_tiers:
        prompt = _draft_prompt(work, rules, history, target=target,
                               reason=reason, failure=failure)
        from grove import t1_call
        from grove.decision_work import call_budget_for
        # The goal's time budget for one model call covers this call too: a
        # tier that does not answer inside it has failed, and the next is tried.
        budget = call_budget_for(getattr(work.config, "call_budget_seconds", None), tier)
        with t1_call.meter() as used:
            try:
                condition = _within(budget, lambda: _ask(call, prompt, tier))
                problem = (
                    "it was empty." if not condition
                    else _check_condition(work, condition, history, target=target)
                )
            except DraftOverBudget:
                condition, problem = "", (
                    f"the {tier} call gave no answer inside the {budget:g} s time budget.")
                logger.warning("[kaizen] draft call at %s over its time budget (%gs); "
                               "trying the next tier", tier, budget)
            except Exception as exc:  # noqa: BLE001 — a failed call is a failed tier
                condition, problem = "", f"the {tier} call failed ({type(exc).__name__})."
                logger.warning("[kaizen] draft call at %s failed: %r", tier, exc)
        attempt = {"tier": tier, "condition": condition, "refused": problem}
        if used["calls"]:
            # What this draft cost, kept with the attempt: learning is paid
            # for once, and the scorecard shows it as such.
            attempt["tokens"] = dict(used)
        attempts.append(attempt)
        if problem is None:
            return condition, attempts
        failure = f"\"{condition}\" was refused because {problem}"
    return None, attempts


# ── the answer ────────────────────────────────────────────────────────


def goal_kegs(work: Any, statuses: tuple, *, store: Any = None) -> List[Any]:
    """This goal's keg entries in the current run's lineage, by status."""
    from grove.pattern_cache import PatternCacheStore

    run = work.log.current_run() or {}
    out = []
    for entry in (store or PatternCacheStore()).all():
        record = keg_mod.keg_record(entry).get("keg") or {}
        if (
            record.get("dock_goal") == work.config.goal_id
            and record.get("lineage") == run.get("run_id")
            and entry.status in statuses
        ):
            out.append(entry)
    return out


def _waiting_drafts(work: Any) -> List[Any]:
    """This goal's keg proposals still waiting for a signature, in this run."""
    from grove.eval.proposal_queue import read_all

    run = work.log.current_run() or {}
    return [
        p for p in read_all()
        if ((p.payload or {}).get("keg") or {}).get("dock_goal") == work.config.goal_id
        and ((p.payload or {}).get("keg") or {}).get("lineage") == run.get("run_id")
    ]


def propose_direct_rule(
    work: Any, andon: Mapping[str, Any], *, key: Any, corrected: Mapping[str, Any],
    store: Any = None, how: str = "corrected", because: Optional[str] = None,
) -> Dict[str, Any]:
    """Propose a revision of the goal's signed keg that answers ``key`` with
    the value the operator has repeatedly given it: corrected it to
    (``how="corrected"``) or confirmed a model's answer as
    (``how="confirmed"``). Called only once that has been seen as often as
    the goal's evidence rule asks. With no signed keg to revise there is
    nothing to propose.

    When a revision of this keg is already waiting for a signature, the new
    rule is added TO it and the waiting draft is withdrawn in favor of one
    card that carries both: two drafts built on the same signed version would
    each erase the other's rule when signed."""
    from grove.eval.pattern_compiler import propose_keg
    from grove.pattern_cache import PatternCacheStore, STATUS_ACTIVE, STATUS_HALTED

    cfg = work.config
    store = store or PatternCacheStore()
    current = goal_kegs(work, (STATUS_ACTIVE, STATUS_HALTED), store=store)
    if cfg.keg is None or not current or key is None or not corrected:
        return {"status": "no_keg_to_revise",
                "detail": "no signed keg for this goal to add the rule to"}
    ref = cfg.reference
    signed = list((keg_mod.keg_of(current[-1]) or {}).get("conditions") or [])
    rule = {"if": f"{ref.key_input} == {_quote(key)}", "then": dict(corrected)}
    waiting = _waiting_drafts(work)
    base = signed
    if waiting:
        base = list(((waiting[-1].payload or {}).get("keg") or {}).get("conditions") or [])
        if any(c.get("if") == rule["if"] and c.get("then") == rule["then"] for c in base):
            return {"status": "already_proposed",
                    "detail": f"a rule for {key!r} is already waiting for signature"}
    history = work.history()
    conditions = [rule] + [c for c in base if c.get("if") != rule["if"]]
    # Every rule this draft adds to the SIGNED keg, each with the operator's
    # own decisions behind it.
    known = {c.get("if") for c in signed}
    added, matching = [], []
    for condition in conditions:
        if condition.get("if") in known or condition.get("defer") or "then" not in condition:
            continue
        behind = [c for c in history
                  if keg_mod.evaluate({"conditions": [condition], "inputs": cfg.inputs},
                                      c["inputs"]) is not None
                  and c["confirmed"] == dict(condition["then"])]
        matching += behind
        named = condition["if"].split("==", 1)[-1].strip()
        if because and condition.get("if") == rule["if"]:
            # A rule with its own stated ground (an alias of an existing key).
            added.append(f"{ref.key_input} {named} is answered "
                         f"{work.value_text(dict(condition['then']))}: {because}")
            continue
        asked = (cfg.evidence.confirmed_key_threshold
                 if how == "confirmed" and cfg.evidence is not None else None)
        added.append(f"{ref.key_input} {named} is answered "
                     f"{work.value_text(dict(condition['then']))}, as you {how} it "
                     f"{len(behind)} times"
                     + (f" (the goal's rule asks for {asked})" if asked else ""))
    run = work.log.current_run() or {}
    result = propose_keg(
        store,
        name=cfg.keg.name, request=cfg.keg.request, requests=cfg.keg.requests,
        match_threshold=cfg.keg.match_threshold,
        verb_bonus=cfg.keg.verb_bonus,
        sessions="goal_isolated" if cfg.isolated else None,
        intent_class=_intent_class([str(c.get("turn_id")) for c in matching]),
        tool_name=cfg.tool, tool_args={"verb": "apply_keg"},
        inputs=cfg.inputs, outputs=cfg.outputs,
        conditions=conditions,
        scope_text=(
            ("Adds one rule: " if len(added) == 1 else f"Adds {len(added)} rules: ")
            + "; ".join(added) + "."
        ),
        reserve=(
            f"Any {ref.key_input} with more than one value in {ref.path.name}; "
            f"any {ref.key_input} not covered by a rule."
        ),
        dock_goal=cfg.goal_id, scope=cfg.keg.scope,
        authority_level=cfg.keg.authority_level,
        flag=(keg_mod.FLAG_ANOMALY if how == "corrected" and not because
              else keg_mod.FLAG_TIER_DOWN_PATTERN),
        flag_detail=str(andon.get("summary") or ""),
        evidence_turn_ids=[str(c.get("turn_id") or c["ref"]) for c in matching]
        or [str(andon.get("andon_id"))],
        history=history, lineage=run.get("run_id"), andon_id=andon.get("andon_id"),
    )
    summary = _summary(result)
    summary["rules_added"] = len(added)
    if summary.get("status") == "proposed" and waiting:
        # One card, not two: the waiting drafts are withdrawn, on the record.
        from grove.eval import proposal_queue
        from grove.flywheel_cli import _record_kaizen_disposition
        from grove.pattern_cache import STATUS_DEMOTED

        for draft in waiting:
            proposal_queue.remove(draft.proposal_id)
            _record_kaizen_disposition(
                draft, disposition="withdrawn",
                reason="replaced by one draft that carries this rule and a further one")
            old = (draft.payload or {}).get("pattern_id")
            if old and store.get(old) is not None:
                store.set_status(old, STATUS_DEMOTED)
        summary["replaced"] = [d.proposal_id for d in waiting]
    return summary


def request_operator_condition(work: Any, andon: Mapping[str, Any]) -> Optional[str]:
    """Ask the operator to write the condition Kaizen could not draft. Files a
    card in the portal that carries the miss and every refused attempt; what
    the operator writes comes back through :func:`answer` as an ordinary keg
    proposal — backtested and signed like any other."""
    import hashlib
    from datetime import datetime, timezone
    from grove.eval.proposal_queue import (
        PROPOSAL_TYPE_KAIZEN_REQUEST, RoutingProposal, append, compute_proposal_id,
    )

    details = andon.get("details") or {}
    origin = details.get("miss") or details.get("originating_details") or {}
    payload = {
        "goal": work.config.goal_id,
        "andon_id": andon.get("andon_id"),
        "originating_andon": details.get("originating_andon_id"),
        "miss": {"item_id": origin.get("item_id"), "inputs": origin.get("inputs"),
                 "served": origin.get("served"), "corrected": origin.get("corrected")},
        "attempts": details.get("attempts") or [],
        "inputs": sorted(work.config.inputs),
    }
    evidence = (str(details.get("originating_andon_id") or andon.get("andon_id")),)
    proposal = RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_KAIZEN_REQUEST, payload=payload, evidence=evidence),
        type=PROPOSAL_TYPE_KAIZEN_REQUEST, payload=payload, evidence=evidence,
        eval_hash="sha256:" + hashlib.sha256(
            f"kaizen_request|{evidence[0]}".encode("utf-8")).hexdigest(),
        created_at=datetime.now(timezone.utc).isoformat(),
        semantic_justification=(
            "Kaizen could not draft a rule that separates the corrected case "
            "from the ones you confirmed. Write the condition and it will be "
            "backtested and brought back for your signature."
        ),
        proposer="kaizen",
    )
    append(proposal)
    return proposal.proposal_id


def _summary(result: Any) -> Dict[str, Any]:
    backtest = result.backtest or {}
    return {
        "status": result.status,
        "detail": result.detail,
        "proposal_id": result.proposal_id,
        "pattern_id": result.pattern_id,
        "version": result.version,
        "replayed": backtest.get("replayed"),
        "unchanged": backtest.get("unchanged"),
        "would_change": backtest.get("would_change"),
        "not_covered": backtest.get("not_covered"),
        "not_covered_cases": [
            c["label"] + (f": {c['note']}" if c.get("note") else "")
            for c in backtest.get("cases") or [] if c["result"] == "not_covered"
        ],
    }


def answer(
    work: Any,
    andon: Mapping[str, Any],
    *,
    ledger: Any = None,
    store: Any = None,
    call: Any = None,
    queue_path: Any = None,
    operator_condition: Optional[str] = None,
) -> Dict[str, Any]:
    """Draft, backtest and propose the keg (or keg revision) that answers
    ``andon``. Returns a summary; ``status`` says what happened in plain
    terms. Never activates anything."""
    from grove.eval.pattern_compiler import propose_keg
    from grove.flywheel_cli import keg_feedback_history
    from grove.pattern_cache import PatternCacheStore, STATUS_HALTED

    cfg = work.config
    if cfg.keg is None:
        return {"status": "no_keg_declared",
                "detail": f"goal {cfg.goal_id} declares no keg; nothing to propose"}
    store = store or PatternCacheStore()
    run = work.log.current_run() or {}
    lineage = run.get("run_id")
    history = work.history()
    ref = cfg.reference
    table_name = ref.path.name
    feedback = keg_feedback_history(cfg.keg.name, lineage=lineage, store=store)

    evidence_ids = [
        str(e.get("turn_id") or e.get("item_id")) for e in (work.evidence().get("evidence") or [])
    ]
    target = None
    miss: Optional[Dict[str, Any]] = None
    drafted: Optional[str] = None
    attempts: List[Dict[str, Any]] = []

    if andon.get("flag") == keg_mod.FLAG_ANOMALY:
        halted = goal_kegs(work, (STATUS_HALTED,), store=store)
        if not halted:
            return {"status": "no_keg_to_revise",
                    "detail": "the corrected decision was not served by a keg"}
        base = list((keg_mod.keg_of(halted[-1]) or {}).get("conditions") or [])
        details = andon.get("details") or {}
        if not details.get("inputs"):
            return {"status": "no_miss_on_record",
                    "detail": "the corrected case is not on this event; nothing to separate"}
        target = dict(details["inputs"])
        miss = {k: details.get(k) for k in ("item_id", "inputs", "served", "corrected")}
        reason = (
            f"The rules answered {json.dumps(details.get('served'))} for "
            f"{details.get('item_id')} and the operator corrected it to "
            f"{json.dumps(details.get('corrected'))}."
        )
        if feedback:
            reason += (
                " The operator sent the last draft back with this feedback, "
                "which the new condition must also satisfy: " + " | ".join(feedback)
            )
        miss_ref = str(details.get("item_id"))
        evidence_ids.append(
            str((andon.get("provenance") or [{}])[0].get("turn_id") or miss_ref)
        )
    else:
        base = reference_rules(work)
        reason = ""
        if feedback:
            reason = "The operator sent the last draft back with this feedback: " + " | ".join(feedback)

    if operator_condition:
        # The operator wrote the condition. It gets the same checks as a
        # drafted one — and then the same backtest and signature.
        problem = _check_condition(work, operator_condition, history, target=target)
        if problem is not None:
            return {"status": "condition_refused", "attempts": [
                {"tier": "operator", "condition": operator_condition, "refused": problem}],
                "detail": f"the condition was refused because {problem}"}
        drafted, attempts = operator_condition, [
            {"tier": "operator", "condition": operator_condition, "refused": None}]
    elif target is not None or feedback:
        drafted, attempts = draft_condition(
            work, base, history, target=target, reason=reason, call=call,
        )
        if drafted is None:
            # Kaizen has no valid draft. It reports that and nothing else: the
            # andon handler raises it as the next event and routes it back.
            return {"status": "draft_failed", "attempts": attempts,
                    "detail": "no tier produced a usable condition; nothing proposed"}
    if drafted is not None:
        # Narrowest change: the separated cases go back to the interpreter.
        # Teaching the keg a NEW answer for them takes the same repeated,
        # confirmed evidence as any rule — a single miss never earns it.
        new_rule: Dict[str, Any] = {"if": drafted, "defer": True}
        if target is not None:
            probe = {"inputs": cfg.inputs, "conditions": [new_rule]}
            agreeing = [
                c for c in history
                if keg_mod.defers(probe, c["inputs"])
                and c["confirmed"] == (andon.get("details") or {}).get("corrected")
            ]
            if len(agreeing) >= cfg.evidence.threshold:
                new_rule = {"if": drafted, "then": dict(agreeing[0]["confirmed"])}
        conditions = [new_rule] + base
    else:
        conditions = base

    deferring = [c for c in conditions if c.get("defer")]
    scope_text = (
        f"Answers {ref.value_output} directly from {table_name} when "
        f"{ref.key_input} has exactly one value there. No model call."
    )
    if deferring:
        scope_text += (
            HANDS_BACK + "; ".join(str(c["if"]) for c in deferring) + "."
        )
    reserve = (
        f"Any {ref.key_input} with more than one value in {table_name}; any "
        f"{ref.key_input} not listed there"
        + ("; and the cases it now hands back" if deferring else "") + "."
    )
    result = propose_keg(
        store,
        name=cfg.keg.name,
        request=cfg.keg.request,
        requests=cfg.keg.requests,
        match_threshold=cfg.keg.match_threshold,
        verb_bonus=cfg.keg.verb_bonus,
        sessions="goal_isolated" if cfg.isolated else None,
        intent_class=_intent_class(evidence_ids),
        tool_name=cfg.tool,
        tool_args={"verb": "apply_keg"},
        inputs=cfg.inputs,
        outputs=cfg.outputs,
        conditions=conditions,
        scope_text=scope_text,
        reserve=reserve,
        dock_goal=cfg.goal_id,
        scope=cfg.keg.scope,
        authority_level=cfg.keg.authority_level,
        flag=str(andon.get("flag")),
        flag_detail=str(andon.get("summary") or ""),
        evidence_turn_ids=evidence_ids,
        history=history,
        feedback=feedback,
        lineage=lineage,
        andon_id=andon.get("andon_id"),
        miss=miss,
        queue_path=queue_path,
        ledger=ledger,
    )
    summary = _summary(result)
    summary["drafted_condition"] = drafted
    summary["attempts"] = attempts
    return summary

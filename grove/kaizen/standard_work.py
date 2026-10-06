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
        lines.append(
            f"  {case['ref']}: inputs={json.dumps(case['inputs'])} "
            f"answered={json.dumps(case['served'])} "
            f"operator={json.dumps(case['confirmed'])}"
            + ("  <-- CORRECTED" if case["confirmed"] != case["served"] else "")
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
        try:
            out = call(prompt, system=_SYSTEM, tool=DRAFT_TOOL, tier=tier, max_tokens=400)
            condition = str((out or {}).get("condition") or "").strip()
            problem = (
                "it was empty." if not condition
                else _check_condition(work, condition, history, target=target)
            )
        except Exception as exc:  # noqa: BLE001 — a failed call is a failed tier
            condition, problem = "", f"the {tier} call failed ({type(exc).__name__})."
            logger.warning("[kaizen] draft call at %s failed: %r", tier, exc)
        attempts.append({"tier": tier, "condition": condition, "refused": problem})
        if problem is None:
            return condition, attempts
        failure = f"\"{condition}\" was refused because {problem}"
    return None, attempts


# ── the answer ────────────────────────────────────────────────────────


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
    }


def answer(
    work: Any,
    andon: Mapping[str, Any],
    *,
    ledger: Any = None,
    store: Any = None,
    call: Any = None,
    queue_path: Any = None,
) -> Dict[str, Any]:
    """Draft, backtest and propose the keg (or keg revision) that answers
    ``andon``. Returns a summary; ``status`` says what happened in plain
    terms. Never activates anything."""
    from grove import jidoka
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
    drafted: Optional[str] = None
    attempts: List[Dict[str, Any]] = []

    if andon.get("flag") == keg_mod.FLAG_ANOMALY:
        halted = [
            entry for entry in jidoka._goal_kegs(work, (STATUS_HALTED,), store=store)
        ]
        if not halted:
            return {"status": "no_keg_to_revise",
                    "detail": "the corrected decision was not served by a keg"}
        base = list((keg_mod.keg_of(halted[-1]) or {}).get("conditions") or [])
        details = andon.get("details") or {}
        target = dict(details.get("inputs") or {})
        reason = (
            f"The rules answered {json.dumps(details.get('served'))} for "
            f"{details.get('item_id')} and the operator corrected it to "
            f"{json.dumps(details.get('corrected'))}."
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

    if target is not None or feedback:
        drafted, attempts = draft_condition(
            work, base, history, target=target, reason=reason, call=call,
        )
        if drafted is None:
            failed = jidoka.flag(
                keg_mod.FLAG_ANOMALY, detector="kaizen_draft", goal=cfg.goal_id,
                summary="Kaizen could not draft a revision that passes its checks",
                evidence=list(andon.get("provenance") or []),
                details={"answering": andon.get("andon_id"), "attempts": attempts},
                ledger=ledger,
            )
            return {"status": "draft_failed", "attempts": attempts,
                    "andon_id": failed["andon_id"],
                    "detail": "no tier produced a usable condition; nothing proposed"}
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
            " Hands back to the interpreter: "
            + "; ".join(str(c["if"]) for c in deferring) + "."
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
        queue_path=queue_path,
        ledger=ledger,
    )
    summary = _summary(result)
    summary["drafted_condition"] = drafted
    summary["attempts"] = attempts
    return summary

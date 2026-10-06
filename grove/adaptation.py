"""Adaptation — how a goal's work learns the way its operator talks.

The system already learns what to DO without a model: a keg, signed in the
portal. This is the same ratchet pointed at Recognition. When a model keeps
reading the same phrase as a verb the work session already has, and the
operator never revises what it did, that phrase can become an exact phrase
for that verb — and the turn drops to T0.

The zone rule: green changes are approved in conversation; changes to
authority are signed in the portal. An alias is green because of one line in
the SIGNED session rule (``grove.decision_work.session_rule``) that lets the
goal's vocabulary supply phrases for verbs already in that rule. So an alias:

  * maps only to a verb the signed rule names — never a new verb, never a
    threshold, never wider scope;
  * acts only inside the goal's work session, and only on an exact match;
  * is honored only while adaptation is on and the rule's signature stands.

Nothing here is a side pipeline. Jidoka counts (``grove.detectors``), the
andon handler routes, Kaizen answers with a watch or a filed remedy
(``grove.kaizen.answers``), the operator's answer is a remedy accepted through
the proposal path (``grove.flywheel_cli``), and every step is on the chained
Kaizen ledger and the turn's own intent record. The vocabulary file is
configuration, not a record: it can be rebuilt from the ledger.

The model nominates; the records qualify. A model's reading is the thing
being replaced, so its say-so is never the evidence. The count comes from the
goal's decision log alone.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

DETECTOR_PHRASE_READING = "phrase_reading"
WRITE_CLASS = "vocabulary_alias"

STATE_COUNTING = "counting"
STATE_PROPOSED = "proposed"
STATE_LIVE = "live"
STATE_REVOKED = "revoked"

EXPIRED = "This question expired."
RESUME = "Resume the session to answer."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── the vocabulary file ───────────────────────────────────────────────


def path(goal_id: str) -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "vocabulary" / f"{goal_id}.yaml"


def load(goal_id: str) -> Dict[str, List[str]]:
    """The goal's learned phrases, by verb. A file that cannot be read as
    that is a defect to fix, never something to guess around."""
    target = path(goal_id)
    if not target.exists():
        return {}
    import yaml

    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(data, Mapping) or not all(
        isinstance(verb, str) and isinstance(phrases, list)
        and all(isinstance(p, str) and p.strip() for p in phrases)
        for verb, phrases in data.items()
    ):
        raise ValueError(
            f"vocabulary {target.name} must map each verb to a list of phrases "
            f"(quote each one: a bare yes or no is read as true or false)")
    return {verb: [p.strip() for p in phrases] for verb, phrases in data.items() if phrases}


def _write(goal_id: str, data: Mapping[str, List[str]]) -> None:
    import yaml

    target = path(goal_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "# Phrases the operator approved in conversation, by work-session verb.\n"
        "# Honored only while the goal's signed session rule lets the vocabulary\n"
        "# supply phrases for that verb. Exact match only.\n"
        + yaml.safe_dump({k: list(v) for k, v in data.items() if v},
                         sort_keys=True, allow_unicode=True)
    )
    scratch = target.with_suffix(".yaml.tmp")
    scratch.write_text(body, encoding="utf-8")
    os.replace(scratch, target)


# ── what may become an alias ──────────────────────────────────────────


def alias_pattern(cfg: Any, verb: str) -> Optional[Any]:
    from grove.decision_work import BECOMES_ALIAS

    if not cfg.adaptation.enabled:
        return None
    for pattern in cfg.adaptation.patterns:
        if pattern.becomes == BECOMES_ALIAS and pattern.verb == verb:
            return pattern
    return None


def signed_verbs(cfg: Any) -> List[str]:
    """The verbs the goal's session rule lets the vocabulary supply phrases
    for — and only while the operator's signature on that rule stands."""
    from grove.decision_work import session_rule, session_rule_grant

    block = (session_rule(cfg).get("work_session") or {}).get("vocabulary") or {}
    if not block or session_rule_grant(cfg) is None:
        return []
    return list(block.get("verbs") or [])


def refusal(cfg: Any, verb: str, phrase: Any) -> Optional[str]:
    """Why a phrase may not become an alias for ``verb``, or None when it may.
    An alias is one unambiguous thing to say: anything that already means
    something, or that reads as doubt, is refused."""
    from grove.decision_work import _domain_values, _phrase, score

    pattern = alias_pattern(cfg, verb)
    if pattern is None:
        return f"{verb} takes no alias in this goal"
    said = _phrase(phrase)
    words = said.split()
    if not words:
        return "nothing was said"
    if len(words) > pattern.max_words:
        return f"longer than {pattern.max_words} words"
    ws, ad = cfg.work_session, cfg.adaptation
    declared = {
        "confirm": ws.confirm, "revise": ws.revise, "start": ws.start,
        "pause": ws.pause, "batch": ws.batch,
        "request": ((cfg.keg.request, *cfg.keg.requests) if cfg.keg else ()),
    }
    for kind, phrases in declared.items():
        if said in {_phrase(p) for p in phrases}:
            return f"already a phrase for {kind}"
    if said in {_phrase(w) for w in ad.answer_words}:
        return "an answer to a question, not a phrase of its own"
    if set(words) & {_phrase(w) for w in ad.refuse_words}:
        return "reads as doubt or a refusal"
    if words[0] in {_phrase(w) for w in ad.forget}:
        return "reads as a request to forget a phrase"
    for domain in cfg.output_domains:
        if said in {_phrase(v) for v in _domain_values(domain)}:
            return "a value the work can decide"
    threshold = cfg.keg.match_threshold if cfg.keg is not None else 0.8
    for kind, phrases in declared.items():
        if kind != verb and phrases and score(said, phrases, cfg)[0] >= threshold:
            return f"too close to a phrase for {kind}"
    return None


def _reads_as(verb: str, record: Mapping[str, Any]) -> bool:
    from grove.decision_work import DECISION_CONFIRM

    return verb == "confirm" and record.get("decision") == DECISION_CONFIRM


def readings(work: Any, pattern: Any, phrase: Any, *, since: Any = None) -> List[Dict[str, Any]]:
    """The turns that count toward an alias: in the current run, a model read
    the operator's whole message as the verb, and the operator has not revised
    that item since. A revision of any item the phrase decided resets the
    count — only readings after it count."""
    from grove.decision_work import DECISION_CORRECT, KIND_DECIDED, _phrase

    target = _phrase(phrase)
    decided = [r for r in work.log.run_records() if r.get("kind") == KIND_DECIDED]
    hits = [
        (i, r) for i, r in enumerate(decided)
        if _reads_as(pattern.verb, r) and _phrase(r.get("operator_said")) == target
        and (not since or str(r.get("ts") or "") > str(since))
    ]
    reset = -1
    for i, record in enumerate(decided):
        if record.get("decision") == DECISION_CORRECT and any(
                j < i and hit.get("item_id") == record.get("item_id") for j, hit in hits):
            reset = i
    return [r for i, r in hits if i > reset]


def turns_failed_upward(work: Any) -> int:
    """How many turns of this goal's current run the ladder rule moved up a
    tier, read from the turn records."""
    from hermes_constants import get_hermes_home

    run = work.log.current_run() or {}
    source = Path(get_hermes_home()) / "intent_records.jsonl"
    if not source.exists():
        return 0
    turns: Dict[str, bool] = {}
    with open(source, encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            stages = row.get("stages") or {}
            telemetry = stages.get("telemetry") or {}
            if telemetry.get("goal_session") != work.config.goal_id or not row.get("turn_uid"):
                continue
            if str(telemetry.get("started_at") or "") < str(run.get("ts") or ""):
                continue
            escalation = (stages.get("compilation") or {}).get("escalation") or {}
            turns[row["turn_uid"]] = bool(escalation.get("attempts"))
    return sum(1 for climbed in turns.values() if climbed)


# ── the watch behind each phrase (Jidoka's count) ─────────────────────


def signature(pattern: Any, phrase: Any) -> Dict[str, Any]:
    from grove.decision_work import _phrase
    return {"pattern": pattern.id, "verb": pattern.verb, "phrase": _phrase(phrase)}


def _watch(cfg: Any, pattern: Any, phrase: Any) -> Dict[str, Any]:
    from grove.pattern_cache import watch_id, watch_record
    return watch_record(
        watch_id(cfg.goal_id, DETECTOR_PHRASE_READING, signature(pattern, phrase)))


def since(cfg: Any, pattern: Any, phrase: Any) -> Optional[str]:
    """When the count for a phrase last started over: the operator answered
    about it (not now, or took it back). Readings before that were already
    answered and count toward nothing. None when it never has."""
    return _watch(cfg, pattern, phrase).get("since")


def _rewatch(cfg: Any, pattern: Any, phrase: Any, **changes: Any) -> None:
    """Start the count again from now: the operator said not now, or took a
    phrase back. Earlier readings no longer count toward asking again."""
    from grove.kaizen import answers
    answers.rewatch(
        answers.watch_id(cfg.goal_id, DETECTOR_PHRASE_READING, signature(pattern, phrase)),
        detector=DETECTOR_PHRASE_READING, goal=cfg.goal_id,
        signature=signature(pattern, phrase), since=_now(), **changes)


# ── the question ──────────────────────────────────────────────────────


def pending(goal_id: str, verb: Optional[str] = None, phrase: Any = None) -> List[Any]:
    """The alias questions waiting for this goal's operator."""
    from grove.decision_work import _phrase
    from grove.eval.proposal_queue import PROPOSAL_TYPE_REMEDY, read_all

    out = []
    for proposal in read_all():
        payload = proposal.payload or {}
        action = payload.get("action") or {}
        if (proposal.type != PROPOSAL_TYPE_REMEDY or payload.get("write_class") != WRITE_CLASS
                or action.get("goal") != goal_id):
            continue
        if verb is not None and action.get("verb") != verb:
            continue
        if phrase is not None and _phrase(action.get("phrase")) != _phrase(phrase):
            continue
        out.append(proposal)
    return out


def card(cfg: Any, proposal: Any) -> Dict[str, Any]:
    """The question as the operator reads it, with the buttons that answer
    it. Each button names the proposal, so a press is about that question."""
    from grove.decision_work import alias_message

    action = (proposal.payload or {}).get("action") or {}
    phrase, verb = action.get("phrase"), action.get("verb")
    count = len(action.get("turns") or [])
    forget = cfg.adaptation.forget[0]
    short = proposal.proposal_id.split(":")[-1][:12]
    return {
        "proposal_id": proposal.proposal_id,
        "text": (
            f"I noticed you say “{phrase}” to mean {verb}: {count} times, never revised.\n"
            f"Want me to treat it that way from now on? It would {verb} with no "
            f"model, exactly as “{verb}” does.\n"
            f"To undo it later, say “{forget} {phrase}”."
        ),
        "buttons": [[label, alias_message(answer, short)]
                    for answer, label in cfg.adaptation.buttons],
    }


def offer(cfg: Any, proposal: Any, session_id: Any) -> bool:
    """Put the question in front of the operator, as its own card, once per
    session. The work goes on; the card waits."""
    if not session_id:
        return False
    from grove import reissue

    pattern = alias_pattern(cfg, ((proposal.payload or {}).get("action") or {}).get("verb"))
    phrase = ((proposal.payload or {}).get("action") or {}).get("phrase")
    asked = list(_watch(cfg, pattern, phrase).get("asked") or []) if pattern else []
    if str(session_id) in asked:
        return False
    reissue.offer_card(str(session_id), card(cfg, proposal))
    if pattern is not None:
        from grove.kaizen import answers
        answers.note_watch(
            answers.watch_id(cfg.goal_id, DETECTOR_PHRASE_READING,
                             signature(pattern, phrase)),
            asked=asked + [str(session_id)])
    return True


def propose(work: Any, andon: Mapping[str, Any], evidence: List[Mapping[str, Any]]) -> Any:
    """Kaizen's answer at the threshold: file the alias for the operator's
    answer in conversation, and ask. ``evidence`` is the readings that count.
    Returns the remedy answer."""
    from grove.kaizen import answers

    cfg, details = work.config, andon.get("details") or {}
    verb, phrase = details.get("verb"), details.get("phrase")
    turns = [str(p.get("turn_uid") or p.get("turn_id") or "") for p in evidence]
    answer = answers.file_remedy(
        andon, write_class=WRITE_CLASS,
        summary=(f"“{phrase}” was read as {verb} {len(turns)} times with no revision. "
                 f"Asked the operator whether it should mean {verb} from now on."),
        action={"goal": cfg.goal_id, "verb": verb, "phrase": phrase,
                "pattern": details.get("pattern"), "threshold": details.get("threshold"),
                "turns": turns,
                "items": [p.get("item_id") for p in evidence]},
        write_targets=[str(path(cfg.goal_id))],
    )
    for proposal in pending(cfg.goal_id, verb, phrase):
        if proposal.proposal_id == answer.artifact:
            offer(cfg, proposal, details.get("session_id"))
    return answer


def apply_alias(action: Mapping[str, Any]) -> Dict[str, Any]:
    """Carry out an alias the operator said yes to. Checked again here, at the
    moment of writing: the verb must be one the SIGNED session rule lets the
    vocabulary supply, the phrase must still be one that may be an alias, and
    the evidence must still hold."""
    from grove.andon import ScopeViolation
    from grove.decision_work import DecisionWork, _phrase, config_for_goal

    cfg = config_for_goal(str(action.get("goal")))
    verb, phrase = str(action.get("verb") or ""), _phrase(action.get("phrase"))
    if verb not in signed_verbs(cfg):
        raise ScopeViolation(
            f"an alias may only map to a verb the signed session rule lets the "
            f"vocabulary supply; {verb!r} is not one")
    why = refusal(cfg, verb, phrase)
    if why is not None:
        raise ScopeViolation(f"“{phrase}” cannot be an alias for {verb}: {why}")
    pattern = alias_pattern(cfg, verb)
    found = readings(DecisionWork(cfg), pattern, phrase,
                     since=_watch(cfg, pattern, phrase).get("since"))
    if len(found) < pattern.threshold:
        raise ScopeViolation(
            f"the evidence for “{phrase}” no longer holds "
            f"({len(found)} of {pattern.threshold})")
    learned = load(cfg.goal_id)
    if phrase not in learned.get(verb, []):
        learned.setdefault(verb, []).append(phrase)
        _write(cfg.goal_id, learned)
    return {"alias": {"goal": cfg.goal_id, "verb": verb, "phrase": phrase},
            "evidence_turns": [str(r.get("turn_uid") or "") for r in found]}


def revoke(cfg: Any, verb: str, phrase: Any, *, surface: str = "chat") -> bool:
    """Take a phrase back. Narrowing is always allowed, in conversation or
    from the portal; it is recorded on the Kaizen ledger either way."""
    from grove.decision_work import _phrase
    from grove.kaizen_ledger import KaizenLedger

    said = _phrase(phrase)
    learned = load(cfg.goal_id)
    if said not in learned.get(verb, []):
        return False
    learned[verb] = [p for p in learned[verb] if p != said]
    _write(cfg.goal_id, learned)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    KaizenLedger(f"operator-{stamp}").record(
        "operator_applied",
        action="alias_forgotten", goal=cfg.goal_id, applied_by="operator",
        approval_surface=surface, verb=verb, phrase=said,
    )
    pattern = alias_pattern(cfg, verb)
    if pattern is not None:
        _rewatch(cfg, pattern, said, revoked_at=_now())
    return True


# ── the work session's side of it ─────────────────────────────────────


def session_action(work: Any, message: Any) -> Optional[Dict[str, Any]]:
    """What a work-session message is, when it is about the vocabulary:

      alias_yes / alias_later — a press on a question card, naming it
      forget                  — "forget <a learned phrase>"
      hold                    — a bare yes or no while a question is open

    The last one is collision safety. With a question open, a typed "yes"
    could be about the card or about the item waiting, so it does neither:
    it never approves an alias and never confirms an item by accident. The
    card is answered by its buttons; the item by its own words."""
    from grove.decision_work import ALIAS_PRESS, _phrase, says

    cfg = work.config
    text = str(message or "").strip()
    pressed = ALIAS_PRESS.fullmatch(text)
    if pressed:
        return {"action": "alias_yes" if pressed.group(1).lower() == "yes" else "alias_later",
                "proposal": pressed.group(2), "button": True}
    said = _phrase(text)
    for word in cfg.adaptation.forget:
        lead = _phrase(word) + " "
        if said.startswith(lead):
            for verb, phrase in cfg.work_session.learned:
                if _phrase(phrase) == said[len(lead):]:
                    return {"action": "forget", "verb": verb, "phrase": phrase}
    if says(text, cfg.adaptation.answer_words) and pending(cfg.goal_id):
        return {"action": "hold"}
    return None


def _quietly(call: Any, *args: Any, **kwargs: Any) -> int:
    """Run a proposal-path entry point that reports on stdout/stderr, and
    keep what it said for the log instead of the gateway's console."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = call(*args, **kwargs)
    if code != 0:
        logger.error("[adaptation] %s failed (%s): %s", getattr(call, "__name__", call),
                     code, (err.getvalue() or out.getvalue()).strip()[:300])
    return code


def step(work: Any, action: Mapping[str, Any], provenance: Any) -> Dict[str, Any]:
    """Carry out one vocabulary action from the work session. Nothing here
    decides an item."""
    from grove import flywheel_cli
    from grove.decision_work import DecisionRefused, _phrase

    cfg, kind = work.config, action.get("action")
    if not cfg.adaptation.enabled:
        raise DecisionRefused("adaptation_off", "This goal's adaptation is switched off.")
    waiting = work.pending()

    def _out(reply: str) -> Dict[str, Any]:
        return {"reply": reply, "item_id": None, "decided": False, "presented": False}

    if kind == "hold":
        lines = ["A question is open, so that did nothing: it could mean the "
                 "question or the " + cfg.item_name[0] + " waiting.",
                 "Answer the question with its buttons."]
        if waiting is not None and cfg.work_session.confirm:
            lines.append(f"To confirm {waiting['item_id']}, use its Confirm button or "
                         f"type “{cfg.work_session.confirm[0]}”.")
        return _out("\n".join(lines))

    if kind == "forget":
        verb, phrase = str(action.get("verb")), _phrase(action.get("phrase"))
        if not revoke(cfg, verb, phrase, surface="chat"):
            raise DecisionRefused("not_learned", f"“{phrase}” is not a learned phrase.")
        return _out(f"Forgotten. “{phrase}” no longer means {verb}; it goes back to the model.")

    named = str(action.get("proposal") or "")
    match = [p for p in pending(cfg.goal_id)
             if named and p.proposal_id.split(":")[-1].startswith(named)]
    if len(match) != 1:
        raise DecisionRefused("stale_card", EXPIRED)
    proposal = match[0]
    detail = (proposal.payload or {}).get("action") or {}
    verb, phrase = str(detail.get("verb")), _phrase(detail.get("phrase"))
    pattern = alias_pattern(cfg, verb)

    if kind == "alias_later":
        _quietly(flywheel_cli.cli_reject, proposal.proposal_id, reason="not now")
        if pattern is not None:
            _rewatch(cfg, pattern, phrase)
        return _out(f"OK, not now. “{phrase}” keeps going to the model.")

    if kind != "alias_yes":
        raise DecisionRefused("unknown_action", f"Unknown vocabulary action {kind!r}.")
    found = readings(work, pattern, phrase,
                     since=_watch(cfg, pattern, phrase).get("since")) if pattern else []
    if pattern is None or len(found) < pattern.threshold or verb not in signed_verbs(cfg):
        # The ground under the question moved (an item it rested on was
        # revised, the rule changed). Withdraw it rather than apply it.
        _quietly(flywheel_cli.cli_reject, proposal.proposal_id,
                 reason="the evidence no longer holds")
        raise DecisionRefused("stale_card", EXPIRED)
    if _quietly(flywheel_cli.cli_approve, proposal.proposal_id) != 0:
        raise DecisionRefused(
            "alias_not_applied", f"“{phrase}” could not be added. Nothing changed.")
    return _out(
        f"Done. “{phrase}” now means {verb}, with no model.\n"
        f"To undo it, say “{cfg.adaptation.forget[0]} {phrase}”.")


def answer_press(answer: str, named: str, provenance: Any) -> str:
    """A press on a question card that arrived OUTSIDE the goal's own work
    session (it was paused, or the chat moved on). A press never goes to a
    model: it is carried out here against the proposal it names, or refused
    in one line. Returns what the operator reads."""
    from grove.decision_work import DecisionRefused, DecisionWork, config_for_goal
    from grove.eval.proposal_queue import PROPOSAL_TYPE_REMEDY, read_all

    match = [
        p for p in read_all()
        if p.type == PROPOSAL_TYPE_REMEDY and (p.payload or {}).get("write_class") == WRITE_CLASS
        and named and p.proposal_id.split(":")[-1].startswith(named)
    ]
    if len(match) != 1:
        return EXPIRED
    goal = ((match[0].payload or {}).get("action") or {}).get("goal")
    try:
        work = DecisionWork(config_for_goal(str(goal)))
        return step(work, {"action": "alias_yes" if answer.lower() == "yes" else "alias_later",
                           "proposal": named}, provenance)["reply"]
    except DecisionRefused as refused:
        return EXPIRED if refused.reason in ("stale_card", "adaptation_off") else str(refused)
    except ValueError:
        return EXPIRED


# ── what the system is watching ───────────────────────────────────────


def status(work: Any) -> List[Dict[str, Any]]:
    """Every declared pattern with its count against its threshold and its
    state, read from the records — for the goal page."""
    from grove.decision_work import (
        BECOMES_ALIAS, COUNTS_TURNS_FAILED_UPWARD, DECISION_CONFIRM, KIND_DECIDED, _phrase,
    )

    cfg, rows = work.config, []
    if not cfg.adaptation.enabled:
        return rows
    learned = {(verb, _phrase(p)) for verb, p in cfg.work_session.learned}
    for pattern in cfg.adaptation.patterns:
        if pattern.becomes != BECOMES_ALIAS:
            count = turns_failed_upward(work) if pattern.counts == COUNTS_TURNS_FAILED_UPWARD else 0
            rows.append({
                "pattern": pattern.id, "what": "turns the ladder moved up a tier",
                "becomes": pattern.becomes, "count": count,
                "threshold": pattern.threshold, "state": STATE_COUNTING,
                "proposes": pattern.propose,
            })
            continue
        asked = {_phrase(((p.payload or {}).get("action") or {}).get("phrase"))
                 for p in pending(cfg.goal_id, pattern.verb)}
        phrases = [p for verb, p in learned if verb == pattern.verb]
        phrases += sorted(asked)
        for record in work.log.run_records():
            said = _phrase(record.get("operator_said"))
            if (record.get("kind") == KIND_DECIDED and said
                    and record.get("decision") == DECISION_CONFIRM
                    and refusal(cfg, pattern.verb, said) is None):
                phrases.append(said)
        for said in dict.fromkeys(phrases):
            watch = _watch(cfg, pattern, said)
            count = len(readings(work, pattern, said, since=watch.get("since")))
            if (pattern.verb, said) in learned:
                state = STATE_LIVE
            elif said in asked:
                state = STATE_PROPOSED
            elif watch.get("revoked_at") and count == 0:
                state = STATE_REVOKED
            else:
                state = STATE_COUNTING
            rows.append({
                "pattern": pattern.id, "what": f"“{said}” read as {pattern.verb}",
                "becomes": pattern.becomes, "verb": pattern.verb, "phrase": said,
                "count": count, "threshold": pattern.threshold, "state": state,
                "proposes": pattern.propose,
            })
        if not phrases:
            rows.append({
                "pattern": pattern.id, "what": f"a phrase read as {pattern.verb}",
                "becomes": pattern.becomes, "verb": pattern.verb, "phrase": None,
                "count": 0, "threshold": pattern.threshold, "state": STATE_COUNTING,
                "proposes": pattern.propose,
            })
    return rows


def reset(cfg: Any, *, surface: str = "portal") -> Dict[str, Any]:
    """Starting the goal's work over also starts its vocabulary over: open
    questions are withdrawn and learned phrases taken back, each recorded."""
    from grove.eval import proposal_queue
    from grove.flywheel_cli import _record_kaizen_disposition

    out: Dict[str, Any] = {"aliases_forgotten": [], "questions_withdrawn": 0}
    for proposal in pending(cfg.goal_id):
        proposal_queue.remove(proposal.proposal_id)
        _record_kaizen_disposition(proposal, disposition="withdrawn", reason="work reset")
        out["questions_withdrawn"] += 1
    for verb, phrases in load(cfg.goal_id).items():
        for phrase in list(phrases):
            if revoke(cfg, verb, phrase, surface=surface):
                out["aliases_forgotten"].append([verb, phrase])
    return out

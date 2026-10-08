"""gl_coding — the GL invoice-coding adapter for decision work.

Everything specific to coding vendor invoices to GL accounts lives here, in the
coding skill and in the ``gl-invoice-coding`` Dock goal's ``decision_work``
config. The queue, the decision log, the isolation checks, the evidence rule
and the keg loop are generic (``grove.decision_work``, ``grove.keg``); this
adapter only knows how to read an invoice and how to word its three verbs.

Verbs:
  * ``next``   — the next uncoded invoice's fields, the vendor's row in the
                 guide and the full chart of accounts. Never a suggested code.
  * ``record`` — store the proposed GL code for that invoice.
  * ``decide`` — store the operator's confirmation or correction.

Pipeline stage: Execution (the Dispatcher runs it; the model only proposes the
code). Zone: Green as a contained write — the only thing it writes is the
goal's own append-only decision log.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from grove import turn_provenance
from grove.decision_work import (
    DECISION_CONFIRM,
    DECISION_CORRECT,
    DecisionRefused,
    DecisionWork,
    asks_for_work,
    config_for_goal,
)

GOAL_ID = "gl-invoice-coding"

_RULE_RE = re.compile(r"^-{10,}\s*$")
_LINE_RE = re.compile(
    r"^(?P<description>.+?)\s{2,}(?P<qty>[\d,]+)\s+(?P<unit>[\d,]+\.\d{2})\s+(?P<amount>[\d,]+\.\d{2})\s*$"
)
_TOTAL_RE = re.compile(r"^TOTAL DUE.*?(?P<total>[\d,]+\.\d{2})\s*$")
_FIELD_RE = re.compile(
    r"^(Invoice number|Invoice date|Terms|Customer account):\s*(.+?)\s*$")
_NOTICE_RE = re.compile(r"^NOTICE:\s*(.*)$", re.IGNORECASE)


def parse_invoice(text: str) -> Dict[str, Any]:
    """Read one plain-text invoice. Raises ValueError when the vendor or the
    line items cannot be found — an invoice that cannot be read is surfaced,
    never coded from a guess."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    body = [ln for ln in lines if ln.strip()]
    if not body or body[0].strip().upper() != "INVOICE" or len(body) < 2:
        raise ValueError("not an invoice: the first line is not INVOICE")
    vendor = body[1].strip()
    fields = {}
    for ln in lines:
        m = _FIELD_RE.match(ln.strip())
        if m:
            fields[m.group(1)] = m.group(2)
    rules = [i for i, ln in enumerate(lines) if _RULE_RE.match(ln)]
    if len(rules) < 2:
        raise ValueError("invoice has no line-item table")
    items: List[Dict[str, str]] = []
    for ln in lines[rules[0] + 1:rules[1]]:
        m = _LINE_RE.match(ln)
        if m:
            items.append({
                "description": m.group("description").strip(),
                "amount": m.group("amount"),
            })
    if not items:
        raise ValueError("invoice has no readable line items")
    total = None
    for ln in lines[rules[1]:]:
        m = _TOTAL_RE.match(ln.strip())
        if m:
            total = m.group("total")
            break
    # A notice printed on the invoice (a change of name, a new remit-to
    # address): the paragraph that begins "NOTICE:", up to the next blank line.
    notice: List[str] = []
    for index, ln in enumerate(lines):
        m = _NOTICE_RE.match(ln.strip())
        if m:
            notice = [m.group(1).strip()]
            for more in lines[index + 1:]:
                if not more.strip():
                    break
                notice.append(more.strip())
            break
    return {
        "vendor": vendor,
        "invoice_number": fields.get("Invoice number"),
        "invoice_date": fields.get("Invoice date"),
        "customer_account": fields.get("Customer account"),
        "notice": " ".join(notice),
        "lines": items,
        "total": total,
    }


# Every field this adapter can read off an invoice as a decision input. Which
# of them a goal's work actually uses is the goal's declaration, not this list.
def _readable_inputs(invoice: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "vendor": invoice["vendor"],
        "description": "; ".join(line["description"] for line in invoice["lines"]),
        "customer_account": invoice.get("customer_account") or "",
        "notice": invoice.get("notice") or "",
    }


def item_inputs(invoice: Dict[str, Any], declared: Any = None) -> Dict[str, Any]:
    """The decision inputs for one invoice (the fields a keg reads): the ones
    the goal DECLARES, when given; otherwise vendor and description, as
    before any further input was readable."""
    readable = _readable_inputs(invoice)
    names = list(declared) if declared else ["vendor", "description"]
    return {name: readable.get(name, "") for name in names}


def _read_rows(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [
            {k: (v or "").strip() for k, v in row.items() if k}
            for row in csv.DictReader(fh)
        ]


def _work() -> DecisionWork:
    return DecisionWork(config_for_goal(GOAL_ID))


def _next_step(exc: DecisionRefused) -> str:
    """What Kaizen proposed in answer to the andon event, in the operator's
    words: a refusal never arrives alone."""
    answer = exc.answer or {}
    kind, summary = answer.get("kind"), answer.get("summary") or ""
    if kind == "standard_work":
        return f"{summary} Sign it or send feedback there."
    if kind == "remedy":
        if (answer.get("detail") or {}).get("authorized") in ("standing_rule", "ladder_rule"):
            return summary
        return f"Proposed next step: {summary} Reply approve to do it, or say what to do instead."
    if kind == "watch":
        return summary
    return ""


def _refusal(exc: DecisionRefused) -> str:
    out = {"success": False, "refused": exc.reason, "message": str(exc)}
    if exc.andon_id:
        # An abnormality: Jidoka flagged it, the andon cord is pulled, and
        # Kaizen has answered. Tell the operator the proposed next step.
        step = _next_step(exc)
        out["andon_id"] = exc.andon_id
        out["stopped"] = True
        out["proposed_next_step"] = step
        out["answer"] = exc.answer
        # One message to relay, whole: what was refused and what happens next.
        # The operator is never left with only "no", and never told to do by
        # hand what the system is already doing.
        if step:
            out["message"] = f"{exc} {step}"
        out["tell_the_operator"] = (
            "Relay `message` as written. Do not add steps of your own."
        )
    return json.dumps(out, ensure_ascii=False)


def _next(work: DecisionWork) -> Dict[str, Any]:
    work.check_turn(turn_provenance.current())
    work.check_one_step_per_turn(turn_provenance.current())
    if work.backlog_first(turn_provenance.current()):
        # A backlog was just released: the keg decides what it covers before
        # any model is handed an invoice. The system runs that pass next.
        return {
            "success": True, "status": "backlog_first",
            "message": (
                "The backlog was just released. The system runs the keg over "
                "it first and then brings the exceptions. Do not fetch or code "
                "anything. Reply with exactly: " + work.BACKLOG_FIRST_MESSAGE),
        }
    waiting = work.pending()
    if waiting is not None:
        return {
            "success": True,
            "status": "awaiting_confirmation",
            "item_id": waiting["item_id"],
            "proposed_gl_code": waiting["output"].get("gl_code"),
            "message": (
                f"{waiting['item_id']} is coded {waiting['output'].get('gl_code')} "
                f"and is waiting for the operator to confirm or revise it."
            ),
        }
    path = work.next_item()
    if path is None:
        return {"success": True, "status": "queue_empty",
                "message": "Every invoice in the queue is coded."}
    invoice = parse_invoice(path.read_text(encoding="utf-8"))
    table = work.reference()
    guide_row = table.row(invoice["vendor"]) if table is not None else None
    chart: List[Dict[str, str]] = []
    for domain in work.config.output_domains:
        if domain.output == "gl_code":
            chart = _read_rows(domain.path)
    return {
        "success": True,
        "status": "ready",
        "item_id": path.stem,
        "invoice": invoice,
        "vendor_guide_row": guide_row,
        "vendor_in_guide": guide_row is not None,
        "chart_of_accounts": chart,
        "instructions": (
            "Choose the GL code from chart_of_accounts using the invoice and "
            "the vendor guide row. Then you MUST do exactly one of two things "
            "before you reply: (1) call record with gl_code and one line of "
            "reasoning; or (2) if the invoice and the guide do not let you "
            "choose (the guide says to confirm with the operator, or the "
            "lines are too vague), call ask and then put your question to the "
            "operator. Never tell the operator a code you have not recorded: "
            "a code that is not on record is not a decision."
        ),
    }


def _record(work: DecisionWork, args: Dict[str, Any]) -> Dict[str, Any]:
    prov = turn_provenance.current()
    work.check_turn(prov)  # before reading the item: refuse a tainted turn first
    work.check_one_step_per_turn(prov)
    path = work.next_item()
    if path is None and work.pending() is None:
        raise DecisionRefused("queue_empty", "Every invoice in the queue is coded.")
    gl_code = str(args.get("gl_code") or "").strip()
    if path is None:
        # A prior item is pending; DecisionWork.record states which.
        item_id, inputs = "", {}
    else:
        invoice = parse_invoice(path.read_text(encoding="utf-8"))
        item_id, inputs = path.stem, item_inputs(invoice, work.config.inputs)
    record = work.record(
        item_id=item_id,
        inputs=inputs,
        output={"gl_code": gl_code},
        reasoning=str(args.get("reasoning") or ""),
        provenance=prov,
    )
    return {
        "success": True,
        "status": "recorded_awaiting_confirmation",
        "item_id": record["item_id"],
        "gl_code": record["output"]["gl_code"],
        "tier": record.get("tier"),
        "message": (
            f"{record['item_id']} coded {record['output']['gl_code']}. Ask the "
            f"operator to confirm or revise it."
        ),
    }


def _asked_for_the_next(work: DecisionWork, exc: DecisionRefused, args: Dict[str, Any]) -> bool:
    """Whether a refused confirmation came on a turn whose request, in the
    operator's own words, asks for the next invoice: nothing was waiting (or
    the invoice named is already decided), no value was given to revise to,
    and the message matches the goal's declared request. Read from the turn's
    record and the goal's config; the model's call decides nothing here."""
    if exc.andon_id or exc.reason not in ("nothing_pending", "already_ruled"):
        return False
    if str(args.get("decision") or "").strip().lower() != "confirm":
        return False
    request = str((turn_provenance.current() or {}).get("request") or "")
    return bool(request) and asks_for_work(request, work.config)


def _named_item(named: str, decided: Any) -> str:
    """The id of the decided invoice the operator named. An id is matched as
    given; otherwise the name is read as the invoice's number ("BL-260912") or
    its place in the queue ("12"), and counts only when exactly one decided
    invoice answers to it. No match, or more than one: the name is returned
    unchanged and the ruling is refused as naming nothing decided."""
    if named in decided:
        return named
    said = named.strip().lstrip("#").lower()
    found = [
        item for item in decided
        if said and said in (item.lower(), item.split("_")[-1].lower(),
                             item.split("_")[0].lstrip("0"))
    ]
    return found[0] if len(found) == 1 else named


def _decide(work: DecisionWork, args: Dict[str, Any]) -> Dict[str, Any]:
    decision = str(args.get("decision") or "").strip().lower()
    corrected = str(args.get("corrected_gl_code") or "").strip()
    waiting = work.pending()
    named = str(args.get("item_id") or "").strip()
    revised_to = {"gl_code": corrected} if decision == DECISION_CORRECT and corrected else None
    if named and (waiting is None or named != waiting["item_id"]):
        # The operator is ruling on an invoice that is ALREADY decided — one
        # they confirmed earlier, or one the keg coded in a batch. They can
        # always change a call; a revision of a keg's answer is a miss.
        proposed, _ = work._state()
        named = _named_item(named, proposed)
        waiting = proposed.get(named)
        record = work.rule_on(
            named, decision=decision, corrected_output=revised_to,
            provenance=turn_provenance.current())
    else:
        record = work.decide(
            decision=decision, corrected_output=revised_to,
            provenance=turn_provenance.current(),
        )
    keg = (waiting or {}).get("keg")
    # What Jidoka did when it saw this decision land. Stated here so the
    # agent's sentence about a halt or a proposal rests on a tool result.
    halted: List[str] = []
    proposals: List[Dict[str, Any]] = []
    for event in work.last_observations:
        halted += event.get("halted") or []
        answer = event.get("answer") or {}
        if answer:
            detail = answer.get("detail") or {}
            proposals.append({
                "flag": event.get("flag"), "kind": answer.get("kind"),
                "status": "proposed" if answer.get("kind") == "standard_work"
                          else answer.get("kind"),
                "version": detail.get("version"), "summary": answer.get("summary"),
                "proposal_id": answer.get("artifact"),
            })
    parts = [
        "No keg was involved in this coding." if not keg
        else f"Coded by the keg {keg.get('name')} v{keg.get('version')}."
    ]
    if halted:
        parts.append(
            f"That revision halted the keg {keg.get('name')} v{keg.get('version')}: "
            "it no longer codes invoices, and the invoices it served run on a "
            "model until the operator signs a fix."
        )
    for p in proposals:
        if p["kind"] == "standard_work":
            parts.append(
                f"{p['summary']} It is waiting in the portal for the operator's "
                "signature and changes nothing until signed."
            )
        elif p["summary"]:
            parts.append(p["summary"])
    if work.config.work_session.enabled:
        parts.append(
            "Stop here and tell the operator it is recorded, in one short "
            "line. The system presents the next invoice itself, right after "
            "your reply: do not fetch or code it, and never tell the operator "
            "to ask for it or to say the word."
        )
    else:
        parts.append(
            "Stop here and tell the operator it is recorded. Do not fetch or "
            "code the next invoice in this turn; they will ask for it."
        )
    if work.last_observation_error:
        parts.append(
            "The watcher failed after recording this decision: "
            + work.last_observation_error
        )
    return {
        "success": True,
        "status": "confirmed" if decision == DECISION_CONFIRM else "revised",
        "item_id": record["item_id"],
        "final_gl_code": record["output"]["gl_code"],
        "proposed_gl_code": (waiting or {}).get("output", {}).get("gl_code"),
        "keg": keg,
        "keg_halted": bool(halted),
        "proposals": proposals,
        "message": " ".join(parts),
        "tell_the_operator": (
            "Relay `message` as written, numbers included. In the portal the "
            "operator signs a proposal or sends feedback; use those words."
        ),
    }


def _item_fields(work: DecisionWork, item_id: str) -> Dict[str, Any]:
    """What this adapter knows about one invoice, for the goal's item card."""
    for path in work.queue_items():
        if path.stem == item_id:
            invoice = parse_invoice(path.read_text(encoding="utf-8"))
            return {
                "invoice_number": invoice.get("invoice_number") or item_id,
                "amount": invoice.get("total") or "?",
                "invoice_date": invoice.get("invoice_date") or "",
                **item_inputs(invoice),
            }
    return {}


def _session(work: DecisionWork, args: Dict[str, Any]) -> str:
    """Dispatcher only: one work-session step (present the pending invoice,
    record the operator's confirm or revision, summarize the queue). Not in
    the model-facing schema, and refused unless the Dispatcher itself is
    running the step — a model can never record a decision this way."""
    prov = turn_provenance.current() or {}
    if not prov.get("session_step"):
        raise DecisionRefused(
            "not_a_session_step", "This step runs only when the system carries it out.")
    action = dict(args.get("action") or {})
    if action.get("action") == "batch":
        # How this adapter reads one queued invoice into the declared inputs.
        action["inputs_for"] = lambda path: item_inputs(
            parse_invoice(path.read_text(encoding="utf-8")), work.config.inputs)
    waiting = work.pending()
    fields = _item_fields(work, waiting["item_id"]) if waiting is not None else {}
    out = work.session_step(action, prov, fields)
    return json.dumps({"session": True, **out}, ensure_ascii=False, default=str)


def _t0_stop(exc: DecisionRefused) -> str:
    step = _next_step(exc)
    return json.dumps(
        {"t0_refused": True, "reason": exc.reason,
         "message": str(exc) + (f"\n\n{step}" if step else ""),
         "andon_id": exc.andon_id, "answer": exc.answer},
        ensure_ascii=False,
    )


def _apply_keg(work: DecisionWork, args: Dict[str, Any]) -> str:
    """T0 only: code the next invoice with a signed keg — no model. Returns
    the reply the operator reads, or a decline that hands the turn back to the
    interpreter. The verb is not in the model-facing schema, and it refuses
    any turn that is not a T0 serve of a compiled pattern."""
    prov = turn_provenance.current() or {}
    if prov.get("tier") != "T0" or not prov.get("t0_pattern"):
        raise DecisionRefused(
            "not_t0", "apply_keg runs only when a signed keg serves the request.")

    def _decline(reason: str, kind: str = "", **why: str) -> str:
        # ``handback`` says why in a form the turn's record keeps: the kind;
        # for a deferring rule, the rule's own condition; for no rule, the
        # reference key and its value on this invoice.
        return json.dumps({"t0_declined": True, "reason": reason,
                           "handback": {"kind": kind, **{k: v for k, v in why.items() if v}}},
                          ensure_ascii=False)

    spec = args.get("keg")
    if not isinstance(spec, dict):
        raise work.abnormal("keg_fault", "The keg was served without its rules.", prov)
    work.check_turn(prov)
    if work.pending() is not None:
        return _decline("a prior coding is waiting for the operator", "item_pending")
    path = work.next_item()
    if path is None:
        return _decline("the queue is empty", "queue_empty")
    invoice = parse_invoice(path.read_text(encoding="utf-8"))
    keg_ref = {
        "name": spec.get("name"), "version": spec.get("version"),
        "pattern_id": prov.get("t0_pattern"),
    }
    record = work.apply_keg(
        spec, item_id=path.stem, inputs=item_inputs(invoice, work.config.inputs),
        keg_ref=keg_ref, provenance=prov,
    )
    if record is None:
        from grove import keg as keg_mod
        rule = keg_mod.match(spec, item_inputs(invoice, work.config.inputs))
        alike = work.resembles_corrected(item_inputs(invoice, work.config.inputs))
        if alike is not None:
            return _decline("this invoice resembles one the operator revised",
                            "resembles_corrected", item=str(alike["item_id"]),
                            share=str(alike["share"]))
        if rule is not None and rule.get("defer"):
            return _decline("a signed change keeps this invoice on a model",
                            "rule_defers", rule=str(rule.get("if") or ""))
        key = work.config.reference.key_input if work.config.reference else ""
        return _decline("the keg does not cover this invoice", "no_rule", key=str(key),
                        value=str(item_inputs(invoice, work.config.inputs).get(key) or ""))
    if work.config.work_session.enabled:
        # The work session's card, from the goal's own template: the same
        # card a model-decided invoice gets.
        work.offer_buttons(record, prov)
        return work.card(record, _item_fields(work, record["item_id"]))
    code = record["output"]["gl_code"]
    account = ""
    for domain in work.config.output_domains:
        if domain.output == "gl_code":
            for row in _read_rows(domain.path):
                if row.get(domain.column) == code:
                    account = row.get("Account Name", "")
    return (
        f"Next invoice: {invoice['vendor']} {invoice.get('invoice_number') or path.stem}, "
        f"${invoice.get('total') or '?'} — coded {code}"
        + (f" {account}" if account else "") + ".\n\n"
        f"Coded by the keg {keg_ref['name']} v{keg_ref['version']}, with no "
        f"model call. Confirm or revise?"
    )


def gl_coding(args: Dict[str, Any]) -> str:
    verb = str((args or {}).get("verb") or "").strip().lower()
    work = None
    try:
        work = _work()
        if verb == "apply_keg":
            prov = turn_provenance.current() or {}
            try:
                return _apply_keg(work, args)
            except DecisionRefused as exc:
                if prov.get("tier") != "T0":
                    raise
                # A refusal at T0 is an abnormality, not a handoff. Jidoka has
                # flagged it (work.abnormal); the Dispatcher stops the turn
                # and shows the operator this message. No model takes over.
                return _t0_stop(exc)
            except (ValueError, OSError) as exc:
                if prov.get("tier") != "T0":
                    raise
                item = work.next_item()
                return _t0_stop(work.abnormal(
                    "item_unreadable", f"The next invoice could not be read: {exc}",
                    {**prov, "item_id": item.stem if item else None}))
        if verb == "session":
            prov = turn_provenance.current() or {}
            try:
                return _session(work, args)
            except DecisionRefused as exc:
                if not prov.get("session_step"):
                    raise
                return _t0_stop(exc)
        if verb == "pause":
            # Decided here, not left to the model: a message that IS the goal's
            # own request for the work is never "about something else", so it
            # cannot pause the session. (Found live, 2026-10-07: a model
            # answered "code the next invoice" by pausing, turn after turn.)
            asked = str((turn_provenance.current() or {}).get("request") or "")
            if asks_for_work(asked, work.config):
                return json.dumps({
                    "success": False, "status": "not_paused",
                    "message": ("The operator's message asks for the next invoice, so the "
                                "session is not paused. Call verb='next' now and carry on."),
                }, ensure_ascii=False)
            work.pause(turn_provenance.current())
            return json.dumps({
                "success": True, "status": "pausing",
                "message": ("The invoice session is pausing and the operator's "
                            "message will be answered outside it. Reply with "
                            "nothing more than a brief acknowledgment."),
            }, ensure_ascii=False)
        if verb == "ask":
            work.ask(turn_provenance.current(), str(args.get("question") or ""))
            return json.dumps({
                "success": True, "status": "asking",
                "message": (
                    "Ask the operator your question now, in one or two sentences. "
                    "Do not state or imply a code as decided; nothing is recorded "
                    "until you call record."),
            }, ensure_ascii=False)
        if verb == "next":
            result = _next(work)
        elif verb == "record":
            result = _record(work, args)
        elif verb == "decide":
            try:
                result = _decide(work, args)
            except DecisionRefused as exc:
                if not _asked_for_the_next(work, exc, args):
                    raise
                # Seen live 2026-10-08, three runs in five: the operator asks
                # for the next invoice and the model confirms the last one
                # again, is refused, and stops; the turn then fails and is
                # retried a tier up at many times the cost. Which step the
                # operator asked for is known from their message, so the tool
                # takes that step and says that it did.
                import logging
                logging.getLogger(__name__).warning(
                    "[gl_coding] a confirmation with nothing waiting (%s) on a request for "
                    "the next invoice: fetched the next invoice instead", exc.reason)
                result = {**_next(work), "instead_of": {
                    "verb": "decide", "refused": exc.reason,
                    "why": "nothing was waiting to be confirmed; the operator asked "
                           "for the next invoice"}}
        else:
            return json.dumps(
                {"success": False,
                 "message": "verb must be next, record, ask, decide or pause"},
                ensure_ascii=False,
            )
    except DecisionRefused as exc:
        return _refusal(exc)
    except (ValueError, OSError) as exc:
        # Config, queue or invoice defect: an abnormality. It goes through the
        # andon handler like any other, and comes back with a proposed step.
        try:
            item = work.next_item()
            prov = {**(turn_provenance.current() or {}),
                    "item_id": item.stem if item else None}
            return _refusal(work.abnormal(
                "item_unreadable", f"The next invoice could not be read: {exc}", prov))
        except Exception:  # noqa: BLE001 — work itself is unusable: say so plainly
            return json.dumps(
                {"success": False, "error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            )
    return json.dumps(result, ensure_ascii=False)


GL_CODING_SCHEMA = {
    "name": "gl_coding",
    "description": (
        "Code vendor invoices to GL accounts, one at a time. verb='next' "
        "returns the next uncoded invoice, the vendor's row in the vendor "
        "guide and the full chart of accounts. verb='record' stores your "
        "proposed gl_code with one line of reasoning; after next you must "
        "call record before replying, unless you need the operator to choose, "
        "in which case call verb='ask' and then ask your question. Never "
        "state a code you have not recorded. verb='decide' stores "
        "the operator's answer: decision='confirm', or decision='correct' "
        "with corrected_gl_code when the operator revises the code. Always "
        "ask the operator to confirm or revise each coding before moving on. "
        "Say 'revise' and 'revised' to the operator, never 'correct'. One "
        "invoice per request: after decide, stop — never call next or record "
        "again in the same turn. Never tell the operator to ask for the next "
        "invoice or to say the word: when a work session is on, the system "
        "presents it. If the operator's message is about something other "
        "than invoices, call verb='pause' and nothing else. If the operator "
        "wants to change an invoice already decided, call decide with "
        "decision='correct', corrected_gl_code and that invoice's item_id. "
        "Use only what this tool "
        "returns to choose a code. If the tool refuses, relay its `message` "
        "to the operator as written — it already says what happens next — "
        "and do not add instructions of your own (never tell the operator "
        "to start a new session; the system does that itself when needed)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "verb": {"type": "string", "enum": ["next", "record", "ask", "decide", "pause"]},
            "question": {
                "type": "string",
                "description": "ask: the question you are about to put to the operator.",
            },
            "gl_code": {
                "type": "string",
                "description": "record: the GL code from the chart of accounts.",
            },
            "reasoning": {
                "type": "string",
                "description": "record: one line on why this code applies.",
            },
            "decision": {
                "type": "string", "enum": [DECISION_CONFIRM, DECISION_CORRECT],
                "description": "decide: the operator's answer.",
            },
            "corrected_gl_code": {
                "type": "string",
                "description": "decide with decision='correct': the code the operator gave.",
            },
            "item_id": {
                "type": "string",
                "description": (
                    "decide only, and only when the operator names an invoice "
                    "that is ALREADY decided (not the one waiting) — to revise "
                    "a call they made earlier, or to rule on one the keg coded "
                    "in a batch: that invoice's id, as the tool reported it. "
                    "The operator can always revise an earlier call; never "
                    "tell them it cannot be changed."
                ),
            },
        },
        "required": ["verb"],
    },
}


def register(reg):
    """Auto-discovered by tools.registry.register_builtin_tools."""
    reg.register(
        name="gl_coding",
        toolset="decision_work",
        schema=GL_CODING_SCHEMA,
        handler=lambda args, **kw: gl_coding(args),
        emoji="🧾",
    )

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
    config_for_goal,
)

GOAL_ID = "gl-invoice-coding"

_RULE_RE = re.compile(r"^-{10,}\s*$")
_LINE_RE = re.compile(
    r"^(?P<description>.+?)\s{2,}(?P<qty>[\d,]+)\s+(?P<unit>[\d,]+\.\d{2})\s+(?P<amount>[\d,]+\.\d{2})\s*$"
)
_TOTAL_RE = re.compile(r"^TOTAL DUE.*?(?P<total>[\d,]+\.\d{2})\s*$")
_FIELD_RE = re.compile(r"^(Invoice number|Invoice date|Terms):\s*(.+?)\s*$")


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
    return {
        "vendor": vendor,
        "invoice_number": fields.get("Invoice number"),
        "invoice_date": fields.get("Invoice date"),
        "lines": items,
        "total": total,
    }


def item_inputs(invoice: Dict[str, Any]) -> Dict[str, Any]:
    """The declared decision inputs for one invoice (the fields a keg reads)."""
    return {
        "vendor": invoice["vendor"],
        "description": "; ".join(line["description"] for line in invoice["lines"]),
    }


def _read_rows(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [
            {k: (v or "").strip() for k, v in row.items() if k}
            for row in csv.DictReader(fh)
        ]


def _work() -> DecisionWork:
    return DecisionWork(config_for_goal(GOAL_ID))


def _refusal(exc: DecisionRefused) -> str:
    return json.dumps(
        {"success": False, "refused": exc.reason, "message": str(exc)},
        ensure_ascii=False,
    )


def _next(work: DecisionWork) -> Dict[str, Any]:
    work.check_turn(turn_provenance.current())
    waiting = work.pending()
    if waiting is not None:
        return {
            "success": True,
            "status": "awaiting_confirmation",
            "item_id": waiting["item_id"],
            "proposed_gl_code": waiting["output"].get("gl_code"),
            "message": (
                f"{waiting['item_id']} is coded {waiting['output'].get('gl_code')} "
                f"and is waiting for the operator to confirm or correct it."
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
            "the vendor guide row. Then call record with gl_code and one line "
            "of reasoning, tell the operator the code and why, and ask them to "
            "confirm or correct it."
        ),
    }


def _record(work: DecisionWork, args: Dict[str, Any]) -> Dict[str, Any]:
    prov = turn_provenance.current()
    work.check_turn(prov)  # before reading the item: refuse a tainted turn first
    path = work.next_item()
    if path is None and work.pending() is None:
        raise DecisionRefused("queue_empty", "Every invoice in the queue is coded.")
    gl_code = str(args.get("gl_code") or "").strip()
    if path is None:
        # A prior item is pending; DecisionWork.record states which.
        item_id, inputs = "", {}
    else:
        invoice = parse_invoice(path.read_text(encoding="utf-8"))
        item_id, inputs = path.stem, item_inputs(invoice)
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
            f"operator to confirm or correct it."
        ),
    }


def _decide(work: DecisionWork, args: Dict[str, Any]) -> Dict[str, Any]:
    decision = str(args.get("decision") or "").strip().lower()
    corrected = str(args.get("corrected_gl_code") or "").strip()
    waiting = work.pending()
    record = work.decide(
        decision=decision,
        corrected_output=(
            {"gl_code": corrected} if decision == DECISION_CORRECT and corrected else None
        ),
        provenance=turn_provenance.current(),
    )
    keg = (waiting or {}).get("keg")
    return {
        "success": True,
        "status": "confirmed" if decision == DECISION_CONFIRM else "corrected",
        "item_id": record["item_id"],
        "final_gl_code": record["output"]["gl_code"],
        "proposed_gl_code": (waiting or {}).get("output", {}).get("gl_code"),
        # Whether a keg produced the proposed code, and whether this decision
        # halted it. Stated here so the agent's sentence rests on a tool result.
        "keg": keg,
        "keg_halted": False,
        "message": (
            "No keg was involved in this coding."
            if not keg else
            f"Coded by keg {keg.get('name')} v{keg.get('version')}."
        ),
    }


def gl_coding(args: Dict[str, Any]) -> str:
    verb = str((args or {}).get("verb") or "").strip().lower()
    try:
        work = _work()
        if verb == "next":
            result = _next(work)
        elif verb == "record":
            result = _record(work, args)
        elif verb == "decide":
            result = _decide(work, args)
        else:
            return json.dumps(
                {"success": False, "message": "verb must be next, record or decide"},
                ensure_ascii=False,
            )
    except DecisionRefused as exc:
        return _refusal(exc)
    except (ValueError, OSError) as exc:
        # Config, queue or invoice defect: surface it, do not guess around it.
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
        "proposed gl_code with one line of reasoning. verb='decide' stores "
        "the operator's answer: decision='confirm', or decision='correct' "
        "with corrected_gl_code. Always ask the operator to confirm or "
        "correct each coding before moving on. Use only what this tool "
        "returns to choose a code. If the tool refuses, tell the operator "
        "exactly what it said."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "verb": {"type": "string", "enum": ["next", "record", "decide"]},
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

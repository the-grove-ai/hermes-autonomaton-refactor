"""The audit — is the record intact, and what did the work cost.

Two read-only reports over what turns already recorded. Neither calls a model
or writes anything.

  :func:`chain_report` — integrity. Recomputes every hash chain (intent
      records per session, Kaizen ledger per file), checks each session's
      newest record against its anchor, and checks every decision in a goal's
      current run has its turn on record.

  :func:`economics` — what each unit of work cost, by tier: model calls,
      tokens, seconds and dollars, and what standard work avoided. Read off
      the same records the integrity half just verified, priced from the
      prices the operator's routing config declares.

Both the command line (``scripts/verify-intent-chain.py``) and the portal's
Audit page call these, so the two can never disagree. A check run from the
command line is the independent one: it reads the files directly and does not
trust the running gateway. The portal's is the convenient view of the same
arithmetic.

Pipeline stage: Telemetry (read side).

What a unit of work costs is modeled as COMPONENTS, so a cost that is not yet
recorded per turn can be added without changing a reader:

  ``model_call``      — recorded: the turn's own model calls and tokens.
  ``classification``  — NOT yet recorded per turn (the intent classifier runs
                        on its own tier). Listed as not included, never as $0.

A T0 turn makes neither call, so every figure here UNDERSTATES what a keg
avoids. That is the safe direction for a savings claim.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

COMPONENT_MODEL_CALL = "model_call"
COMPONENT_CLASSIFICATION = "classification"
INCLUDED_COMPONENTS = (COMPONENT_MODEL_CALL,)
NOT_INCLUDED = {
    COMPONENT_CLASSIFICATION: (
        "the intent classifier's own call on each model turn — not yet "
        "recorded per turn; a T0 turn skips it too"
    ),
}

# Scales the Audit page can project a run to: MODEL CALLS a month, as the
# reader's agents make them today. Calls, not units of work: a reader knows
# their call volume, and it is the same unit whatever the work is.
SCALES = (100_000, 1_000_000, 10_000_000)


def _home(home: Optional[Path]) -> Path:
    if home is not None:
        return Path(home)
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _ledger_dir(home: Path) -> Path:
    """The Kaizen ledger directory under ``home``, named by the ledger module
    itself (this report never spells the location)."""
    from grove.kaizen_ledger import default_ledger_dir
    return home / default_ledger_dir().name


def _ledger_archive_dir(home: Path) -> Path:
    from grove.ledger_retention import default_archive_dir
    return home / default_archive_dir().name


def _routing_config_paths(home: Path) -> List[Path]:
    """Where model prices are declared: the operator's copy under ``home``,
    then the repo default. The file's name comes from the routing writer."""
    from grove.config.routing_writer import _default_config_path

    name = _default_config_path().name
    return [home / name, Path(__file__).resolve().parents[1] / "config" / name]


def _jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue      # the chain check reports an unreadable line
    return rows


# ── integrity ─────────────────────────────────────────────────────────


def _read_anchors(db_path: Path) -> Dict[str, str]:
    """``{session_id: latest record_hash}`` from the session database, opened
    read-only. A missing database is not an error (nothing anchored yet); an
    unreadable one is — fail loud, never report a tail check that did not run."""
    from grove.intent_store import CHAIN_HEAD_KEY_PREFIX

    if not db_path.exists():
        return {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT key, value FROM state_meta WHERE key LIKE ?",
            (CHAIN_HEAD_KEY_PREFIX + "%",),
        ).fetchall()
    finally:
        conn.close()
    return {k[len(CHAIN_HEAD_KEY_PREFIX):]: v for k, v in rows}


def _ledger_check(home: Path) -> Dict[str, Any]:
    from grove.kaizen_ledger import verify_ledger_chain

    out = {"files": 0, "chained": 0, "unchained": 0, "problems": []}
    directory = _ledger_dir(home)
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.jsonl")):
        archive = _ledger_archive_dir(home) / path.name
        lines = path.read_text(encoding="utf-8").splitlines()
        archived = archive.read_text(encoding="utf-8").splitlines() if archive.exists() else []
        report = verify_ledger_chain(lines, archived)
        out["files"] += 1
        out["chained"] += report["chained"]
        out["unchained"] += report["unchained"]
        for p in report["problems"]:
            out["problems"].append({
                "where": f"ledger {path.name}" + (f" line {p['line']}" if p.get("line") else ""),
                "subject": p.get("event_type") or "", "problem": p["problem"],
            })
    return out


# A decision is written during its turn; the turn's own record is written
# when the turn ends. One of the run's newest decisions, this young, with no
# turn yet is a turn still running, not a break. Past this window — or with a
# later decision's turn already on record — a missing turn IS a break.
IN_FLIGHT_SECONDS = 180


def _in_flight(record: Mapping[str, Any], now: Any = None) -> bool:
    from datetime import datetime, timezone

    made = _when(record.get("ts"))
    if made is None:
        return False
    if made.tzinfo is None:
        made = made.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return 0 <= (now - made).total_seconds() < IN_FLIGHT_SECONDS


def _run_check(home: Path, turn_uids: set) -> Dict[str, Any]:
    from grove.decision_work import (
        DECISION_CORRECT, KIND_DECIDED, KIND_PROPOSED, DecisionLog,
    )

    out = {"runs": [], "links": [], "problems": []}
    directory = home / "decisions"
    if not directory.is_dir():
        return out
    for log_path in sorted(directory.glob("*.jsonl")):
        log = DecisionLog(log_path.stem, directory=directory)
        try:
            run = log.current_run()
            records = log.run_records()
            proposed = [r for r in records if r.get("kind") == KIND_PROPOSED]
        except ValueError as exc:
            out["problems"].append({
                "where": f"decision log {log_path.name}", "subject": "", "problem": str(exc)})
            continue
        if run is None:
            continue
        found = sum(1 for r in proposed if r.get("turn_uid") in turn_uids)
        # Only the run's NEWEST decisions can be from a turn still running:
        # once any later decision has its turn on record, an earlier one
        # without a turn is a hole, however recent.
        running: set = set()
        for r in reversed(proposed):
            if r.get("turn_uid") in turn_uids or not _in_flight(r):
                break
            running.add(r.get("id"))
        out["runs"].append({
            "goal": log_path.stem, "run_number": run.get("run_number"),
            "label": run.get("label") or "", "decisions": len(proposed), "with_turn": found,
            **({"in_flight": len(running)} if running else {}),
        })
        # The latest ruling on each item decides how its link is drawn.
        latest = {r.get("ref"): r.get("decision") for r in records
                  if r.get("kind") == KIND_DECIDED}
        corrected = {ref for ref, decision in latest.items() if decision == DECISION_CORRECT}
        # One link per decision, in order: who decided it, whether the
        # operator corrected it, and whether its turn is on record.
        out["links"] += [{
            "goal": log_path.stem, "order": n, "item_id": r.get("item_id"),
            "keg": bool(r.get("keg")), "corrected": r.get("id") in corrected,
            "on_record": r.get("turn_uid") in turn_uids,
            "in_flight": r.get("id") in running,
        } for n, r in enumerate(proposed, 1)]
        for r in proposed:
            if r.get("turn_uid") not in turn_uids and r.get("id") not in running:
                out["problems"].append({
                    "where": f"decision log {log_path.name}",
                    "subject": r.get("turn_id") or r.get("item_id") or "",
                    "problem": f"decision {r.get('item_id')} has no turn in the audit trail",
                })
    return out


def chain_report(
    home: Optional[Path] = None, *, store_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Verify the record. ``store_path`` checks a COPY of an intent store on
    its own (file checks only: no anchors, no ledger, no runs — those need the
    live home). Raises FileNotFoundError when there is no intent store.

    ``result`` is ``intact``, ``nothing_to_verify`` or ``broken``; ``problems``
    lists every break as ``{where, subject, problem}``."""
    from grove.intent_store import verify_chain

    live = store_path is None
    base = _home(home) if live else None
    path = (base / "intent_records.jsonl") if live else Path(store_path)
    if not path.exists():
        raise FileNotFoundError(f"no intent store at {path}")
    anchors = _read_anchors(base / "telemetry.db") if live else {}
    with open(path, encoding="utf-8") as fh:
        chain = verify_chain(fh, anchors=anchors)

    problems = [{
        "where": (
            f"line {p['line']}" if p.get("line")
            else f"session {p.get('session_id')}"
        ),
        "subject": p.get("turn_id") or "", "problem": p["problem"],
    } for p in chain["problems"]]

    ledger = {"files": 0, "chained": 0, "unchained": 0, "problems": []}
    runs: List[Dict[str, Any]] = []
    links: List[Dict[str, Any]] = []
    if live:
        ledger = _ledger_check(base)
        uids = {r.get("turn_uid") for r in _jsonl(path)}
        run_check = _run_check(base, uids)
        runs, links = run_check["runs"], run_check["links"]
        problems += ledger["problems"] + run_check["problems"]

    if problems:
        result = "broken"
    elif chain["chained"]:
        result = "intact"
    else:
        result = "nothing_to_verify"
    return {
        "live": live,
        "store": str(path),
        "records": chain["records"],
        "chained": chain["chained"],
        "unchained": chain["unchained"],
        "chained_sessions": sum(1 for s in chain["sessions"].values() if s["chained"]),
        "anchored": chain["anchored"],
        "ledger": {k: ledger[k] for k in ("files", "chained", "unchained")},
        "runs": runs,
        "links": links,
        "problems": problems,
        "result": result,
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


# ── the check, in words ───────────────────────────────────────────────
# One set of words for the portal's Audit panel and the command line, so the
# independent check and the page tell the same story.

STATUS_TEXT = {
    "intact": "CHAIN INTACT",
    "nothing_to_verify": "NOTHING TO VERIFY",
    "broken": "CHAIN BROKEN",
}
HEADLINE = ("Every record accounted for.", "Nothing altered, removed or reordered.")
HOW_IT_WORKS = (
    "Each record carries a fingerprint of its own contents and of the record "
    "before it. Change, delete or reshuffle one, and the chain breaks at that "
    "exact spot."
)
NOTHING_YET = "No chained records in this store yet, so there is nothing to check."
WOULD_CATCH = (
    ("A record edited", "Its fingerprint no longer matches its contents."),
    ("A record deleted or moved",
     "The next record points to a fingerprint that isn't there."),
    ("The newest records removed",
     "Each session's latest fingerprint is stored separately and must still be found."),
)
PROOF_OF_CONCEPT = (
    "This page is the demo system checking its own records. It is a prototype "
    "of the pattern, not Sokori's production runtime. For an independent "
    "check, scripts/verify-intent-chain.py reads the record files directly and "
    "never asks the running system."
)


def _s(n: int, one: str, many: Optional[str] = None) -> str:
    return one if n == 1 else (many or one + "s")


def check_headline(report: Mapping[str, Any]) -> Dict[str, str]:
    """The verdict in a sentence and a clause. A broken chain names the first
    broken record and where it sits."""
    if report["result"] == "broken":
        first = report["problems"][0]
        at = " · ".join(x for x in (first.get("subject"), first.get("where")) if x)
        count = len(report["problems"])
        return {"lead": f"The chain breaks at {at}.",
                "clause": f"{count} {_s(count, 'problem')} found.",
                "detail": first["problem"]}
    if report["result"] == "nothing_to_verify":
        return {"lead": NOTHING_YET, "clause": "", "detail": ""}
    return {"lead": HEADLINE[0], "clause": HEADLINE[1], "detail": HOW_IT_WORKS}


def check_tiles(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """What was checked, as counts with one sentence each: ``{label, count,
    of, text}`` (``of`` is None where the count has no denominator)."""
    sessions = report["chained_sessions"]
    older = report["unchained"]
    tiles = [{
        "label": "TURN RECORDS", "count": report["chained"], "of": report["records"],
        "text": (f"hash-chained, across {sessions:,} {_s(sessions, 'session')}. "
                 + ("None older or unchained." if not older else
                    f"{older:,} older {_s(older, 'record')} "
                    f"{_s(older, 'predates', 'predate')} the chain and "
                    f"{_s(older, 'is', 'are')} counted, not chained.")),
    }]
    if not report["live"]:
        tiles.append({
            "label": "SESSIONS TAIL-CHECKED", "count": None, "of": None,
            "text": "Not run: this needs the live session database."})
        return tiles
    tiles.append({
        "label": "SESSIONS TAIL-CHECKED", "count": report["anchored"], "of": sessions,
        "text": "Each session's newest record is still present, so nothing "
                "was cut off the end.",
    })
    ledger = report["ledger"]
    tiles.append({
        "label": "LEARNING LEDGER", "count": ledger["chained"], "of": None,
        "text": (f"chained {_s(ledger['chained'], 'event')} in {ledger['files']:,} "
                 f"{_s(ledger['files'], 'file')}: every flag, proposal, signature and halt."
                 + (f" {ledger['unchained']:,} older {_s(ledger['unchained'], 'event')} "
                    f"{_s(ledger['unchained'], 'predates', 'predate')} the chain and "
                    f"{_s(ledger['unchained'], 'is', 'are')} counted, not chained."
                    if ledger["unchained"] else "")),
    })
    for run in report["runs"]:
        tiles.append({
            "label": "DECISIONS THIS RUN", "count": run["with_turn"], "of": run["decisions"],
            "text": "with the turn that produced them on record."
                    + (f" {run['in_flight']} more {_s(run['in_flight'], 'is', 'are')} from a "
                       f"turn still running, and will be checked when it ends."
                       if run.get("in_flight") else ""),
            "run": f"Run {run['run_number']} · {run['goal']}",
        })
    return tiles


def format_chain_report(report: Mapping[str, Any]) -> List[str]:
    """The report as the terminal prints it — the panel's own words."""
    bar = "=" * 72
    head = check_headline(report)
    checked = _when(report.get("checked_at"))
    lines = [
        bar, f"  AUDIT CHECK · {STATUS_TEXT[report['result']]}", bar,
        "  " + " ".join(x for x in (head["lead"], head["clause"]) if x),
    ]
    if head["detail"]:
        lines.append("  " + head["detail"])
    lines += [
        "",
        "  Last checked " + (checked.strftime("%Y-%m-%d %H:%M:%S %Z") if checked else "—")
        + f" · {report['records']:,} {_s(report['records'], 'record')}",
        f"  Store: {report['store']}",
        "",
    ]
    for tile in check_tiles(report):
        figure = ("—" if tile["count"] is None else f"{tile['count']:,}"
                  + (f" / {tile['of']:,}" if tile["of"] is not None else ""))
        label = tile["label"] + (f" ({tile['run']})" if tile.get("run") else "")
        lines += [f"  {label}: {figure}", f"    {tile['text']}"]
    if report["result"] == "broken":
        lines += ["", "  PROBLEMS"]
        lines += [f"    {p['where']}  {p['subject']}  {p['problem']}"
                  for p in report["problems"]]
    lines += ["", "  WHAT THE CHECK WOULD CATCH"]
    lines += [f"    {title}: {text}" for title, text in WOULD_CATCH]
    lines.append(bar)
    return lines


# ── economics ─────────────────────────────────────────────────────────


def _prices(home: Path) -> Dict[str, Any]:
    """Model prices and tier bindings as the operator's routing config
    declares them. Read as plain YAML: this report only reads figures, it
    enforces nothing about the config's shape."""
    import yaml

    for path in _routing_config_paths(home):
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            tiers = {}
            for name, spec in (data.get("tier_preferences") or {}).items():
                tiers[name] = spec.get("model") if isinstance(spec, dict) else spec
            return {"facts": data.get("model_facts") or {}, "tiers": tiers, "source": str(path)}
    return {"facts": {}, "tiers": {}, "source": None}


def _turn_cost(tokens: Mapping[str, Any], fact: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Price one turn's recorded tokens. Fresh input and output are priced
    when the model's prices are declared. Cached context re-read is priced
    ONLY when ``cost_per_mtok_cache_read`` is declared; otherwise it is
    reported as tokens and left out of the dollars (understating cost)."""
    fresh_in = int(tokens.get("input") or 0)
    out = int(tokens.get("output") or 0)
    cached = int(tokens.get("cache_read") or 0)
    fact = fact or {}
    p_in, p_out = fact.get("cost_per_mtok_input"), fact.get("cost_per_mtok_output")
    p_cache = fact.get("cost_per_mtok_cache_read")
    priced = isinstance(p_in, (int, float)) and isinstance(p_out, (int, float))
    cost = (fresh_in * p_in + out * p_out) / 1_000_000 if priced else None
    cache_priced = priced and isinstance(p_cache, (int, float))
    if cache_priced:
        cost += cached * p_cache / 1_000_000
    return {
        "input": fresh_in, "output": out, "cache_read": cached,
        "cost": cost, "priced": priced, "cache_read_priced": bool(cache_priced),
    }


def _turn(record: Optional[Mapping[str, Any]], prices: Mapping[str, Any]) -> Dict[str, Any]:
    """One turn's measured work, from its intent record."""
    if record is None:
        return {"on_record": False, "tier": None, "model": None, "model_calls": 0,
                "seconds": None, "input": 0, "output": 0, "cache_read": 0,
                "cost": None, "priced": False, "cache_read_priced": False}
    execution = (record.get("stages") or {}).get("execution") or {}
    model = record.get("model_used")
    cost = _turn_cost(execution.get("tokens") or {}, prices["facts"].get(model))
    if record.get("tier_selected") == "T0":
        cost.update(cost=0.0, priced=True)        # no model ran: nothing to price
    return {
        "on_record": True,
        "tier": record.get("tier_selected"),
        "model": model,
        "model_calls": int(execution.get("model_calls", record.get("api_calls") or 0) or 0),
        "seconds": (record.get("duration_ms") or 0) / 1000.0,
        **cost,
    }


def _mean(values: List[float]) -> Optional[float]:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def economics(home: Optional[Path] = None, *, goal: Optional[str] = None) -> Dict[str, Any]:
    """What the current run's work cost, per unit and by tier.

    One entry per goal that has a decision log (or only ``goal``). Each unit
    of work (an item decided once) has a DECIDING turn — the one that produced
    its answer, at T0 or by a model — and usually a CONFIRMING turn where the
    operator's decision was recorded. They are kept apart: standard work
    removes the deciding turn's cost, and how often an operator confirms is a
    choice about the work, not a property of the tier."""
    from grove.decision_work import (
        DECISION_ACCEPTED, DECISION_CONFIRM, DECISION_CORRECT, KIND_DECIDED,
        KIND_PROPOSED, DecisionLog,
    )

    base = _home(home)
    prices = _prices(base)
    intents: Dict[str, Dict[str, Any]] = {}
    for row in _jsonl(base / "intent_records.jsonl"):
        if row.get("turn_uid"):
            intents[row["turn_uid"]] = row        # later lines supersede
    directory = base / "decisions"
    goals: List[Dict[str, Any]] = []
    if not directory.is_dir():
        return {"goals": goals, "prices_source": prices["source"],
                "included": list(INCLUDED_COMPONENTS), "not_included": dict(NOT_INCLUDED)}

    for log_path in sorted(directory.glob("*.jsonl")):
        if goal and log_path.stem != goal:
            continue
        log = DecisionLog(log_path.stem, directory=directory)
        run = log.current_run()
        if run is None:
            continue
        records = log.run_records()
        shown = _presentation(log_path.stem)
        decided = {r["ref"]: r for r in records if r.get("kind") == KIND_DECIDED}
        deciding_uids = {r.get("turn_uid") for r in records if r.get("kind") == KIND_PROPOSED}
        # A batch decides many items in ONE turn. Each item's share of that
        # turn is the turn divided by the items it decided — never the whole
        # turn counted once per item.
        shared: Dict[Any, int] = {}
        for r in records:
            if r.get("kind") == KIND_PROPOSED and r.get("turn_uid"):
                shared[r["turn_uid"]] = shared.get(r["turn_uid"], 0) + 1
        units: List[Dict[str, Any]] = []
        for order, record in enumerate(
                [r for r in records if r.get("kind") == KIND_PROPOSED], 1):
            verdict = decided.get(record["id"])
            deciding = _turn(intents.get(record.get("turn_uid")), prices)
            split = shared.get(record.get("turn_uid"), 1)
            if split > 1:
                deciding = {
                    **deciding,
                    **{k: (deciding[k] / split if deciding[k] is not None else None)
                       for k in ("seconds", "cost")},
                    **{k: deciding[k] / split
                       for k in ("model_calls", "input", "output", "cache_read")},
                    "shared_with": split,
                }
            confirm_uid = (verdict or {}).get("turn_uid")
            # A turn that both recorded a confirmation and decided the next
            # item is counted once, as that item's deciding turn.
            confirming = (
                _turn(intents.get(confirm_uid), prices)
                if confirm_uid and confirm_uid not in deciding_uids else None
            )
            keg = record.get("keg") or None
            served = dict(record.get("output") or {})
            final = dict((verdict or {}).get("output") or {})
            units.append({
                "order": order,
                "item_id": record["item_id"],
                "label": str((record.get("inputs") or {}).get(shown["label_key"], ""))
                         if shown["label_key"] else "",
                "keg_version": keg.get("version") if keg else None,
                "served": served,
                "final": final,
                "at": record.get("ts"),
                "tier": record.get("tier") or deciding["tier"],
                "by": (f"{keg.get('name')} v{keg.get('version')}" if keg
                       else deciding["model"] or record.get("model")),
                "keg": bool(keg),
                "decision": (verdict or {}).get("decision"),
                "corrected": (verdict or {}).get("decision") == DECISION_CORRECT,
                # Decided under the keg's signed authority and not reviewed:
                # never the operator's confirmation.
                "accepted": (verdict or {}).get("decision") == DECISION_ACCEPTED,
                "confirmed": (verdict or {}).get("decision") == DECISION_CONFIRM,
                "batch": record.get("batch"),
                "deciding": deciding,
                "confirming": confirming,
            })

        by_tier: Dict[str, Dict[str, Any]] = {}
        for tier in sorted({u["tier"] or "?" for u in units}):
            group = [u for u in units if (u["tier"] or "?") == tier]
            turns = [u["deciding"] for u in group]
            by_tier[tier] = {
                "units": len(group),
                "confirmed": sum(1 for u in group if u["confirmed"]),
                "accepted": sum(1 for u in group if u["accepted"]),
                "corrected": sum(1 for u in group if u["corrected"]),
                "model_calls": _mean([t["model_calls"] for t in turns]),
                "seconds": _mean([t["seconds"] for t in turns]),
                "fresh_tokens": _mean([t["input"] + t["output"] for t in turns]),
                "cached_tokens": _mean([t["cache_read"] for t in turns]),
                "cost": _mean([t["cost"] for t in turns]),
            }

        model_units = [u for u in units if not u["keg"]]
        keg_units = [u for u in units if u["keg"]]
        model_avg = {
            "cost": _mean([u["deciding"]["cost"] for u in model_units]),
            "seconds": _mean([u["deciding"]["seconds"] for u in model_units]),
            "tokens": _mean([u["deciding"]["input"] + u["deciding"]["output"]
                             + u["deciding"]["cache_read"] for u in model_units]),
            "input": _mean([u["deciding"]["input"] for u in model_units]),
            "output": _mean([u["deciding"]["output"] for u in model_units]),
            "model_calls": _mean([u["deciding"]["model_calls"] for u in model_units]),
        }
        keg_avg = {
            "cost": _mean([u["deciding"]["cost"] for u in keg_units]),
            "seconds": _mean([u["deciding"]["seconds"] for u in keg_units]),
        }
        # What the same unit of work would cost at the frontier tier: this
        # run's measured average tokens, at the T3 model's declared prices. An
        # ESTIMATE — no frontier call was made.
        frontier_model = prices["tiers"].get("T3")
        frontier_fact = prices["facts"].get(frontier_model) or {}
        frontier_cost = None
        if (model_avg["input"] is not None
                and isinstance(frontier_fact.get("cost_per_mtok_input"), (int, float))
                and isinstance(frontier_fact.get("cost_per_mtok_output"), (int, float))):
            frontier_cost = (
                model_avg["input"] * frontier_fact["cost_per_mtok_input"]
                + model_avg["output"] * frontier_fact["cost_per_mtok_output"]
            ) / 1_000_000

        coverage = _coverage(log_path.stem, units, records)
        loop = _loop(base, log_path.stem, run, units)
        goals.append({
            "goal": log_path.stem,
            "title": shown["title"] or next(
                (v["name"] for v in loop["versions"] if v["name"]), log_path.stem),
            "item_name": shown["item_name"],
            "traceable": sum(1 for u in units if u["deciding"]["on_record"]),
            "periods": (periods := _with_open(
                _periods(units, shown), open_items := _open_items(log_path.stem, records))),
            "open": open_items,
            "why_model": (why := _why_model(log_path.stem, units, records, run)
                          if periods else {}),
            "headline": _headline(periods, why, shown["item_name"]),
            "learning": _learning(base, log_path.stem, run, prices),
            "tickets": _tickets(loop, shown.get("ticket_model"), months=len(periods) or 1),
            "events": loop["events"],
            "versions": loop["versions"],
            "drafts_returned": loop["drafts_returned"],
            "brake": loop["brake"],
            "run_number": run.get("run_number"),
            "label": run.get("label") or "",
            "units": units,
            "by_tier": by_tier,
            "model_avg": model_avg,
            "keg_avg": keg_avg,
            "keg_units": len(keg_units),
            "model_units": len(model_units),
            "frontier": {"model": frontier_model, "cost": frontier_cost},
            "coverage": coverage,
            "cache_read_priced": any(u["deciding"]["cache_read_priced"] for u in model_units),
            "signatures": loop["signatures"],
            "totals": {
                "cost": sum(u["deciding"]["cost"] or 0.0 for u in units),
                "seconds": sum(u["deciding"]["seconds"] or 0.0 for u in units),
                "model_calls": sum(u["deciding"]["model_calls"] for u in units),
                "tokens": sum(u["deciding"]["input"] + u["deciding"]["output"]
                              + u["deciding"]["cache_read"] for u in units),
            },
        })
    return {"goals": goals, "prices_source": prices["source"],
            "included": list(INCLUDED_COMPONENTS), "not_included": dict(NOT_INCLUDED)}


def trace(home: Optional[Path] = None, *, goal: Optional[str] = None) -> Dict[str, Any]:
    """Every decision of a goal's current run as one readable trace, in order.

    For each item: what arrived (its inputs), any question the model declared
    it was asking, who decided and why (the keg version and the rule that
    fired, or the model, its tier and its one-line reasoning), every ruling
    the operator made on it, and what the improvement loop did about it — each
    step with the turn that carries it. Read off the decision log, the intent
    records and the Kaizen ledger; nothing here is summarized by a model.

    ``example`` on each item is that decision as one training example: inputs,
    proposed answer, reasoning, who decided, the operator's final answer and
    verdict. Returns ``{"goals": [...]}`` like :func:`economics`."""
    from grove.decision_work import (
        DECISION_ACCEPTED, DECISION_CONFIRM, DECISION_CORRECT, KIND_DECIDED,
        KIND_PROPOSED, DecisionLog, DecisionWork, config_for_goal,
    )

    base = _home(home)
    rows = _jsonl(base / "intent_records.jsonl")
    intents: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if row.get("turn_uid"):
            intents[row["turn_uid"]] = row
    asked = sorted(
        (r for r in intents.values()
         if ((r.get("stages") or {}).get("execution") or {}).get("asked")),
        key=lambda r: r.get("timestamp") or "")
    events: List[Dict[str, Any]] = []
    ledger = _ledger_dir(base)
    if ledger.is_dir():
        for path in ledger.glob("*.jsonl"):
            events += _jsonl(path)
    events.sort(key=lambda e: e.get("timestamp") or "")
    proposals = {e["andon_id"]: e for e in events
                 if e.get("event_type") == "kaizen_proposal" and e.get("andon_id")}
    signed = {e["proposal_id"]: e for e in events
              if e.get("event_type") == "new_standard_work" and e.get("proposal_id")}

    def _step_turn(uid: Any) -> Dict[str, Any]:
        row = intents.get(uid) or {}
        stages = row.get("stages") or {}
        execution = stages.get("execution") or {}
        match = (stages.get("recognition") or {}).get("phrase_match") or {}
        return {
            "turn_id": row.get("turn_id"), "turn_uid": uid,
            "tier": row.get("tier_selected"), "model": row.get("model_used"),
            "model_calls": int(execution.get("model_calls", row.get("api_calls") or 0) or 0),
            "seconds": (row.get("duration_ms") or 0) / 1000.0 if row else None,
            "record_hash": row.get("record_hash"), "on_record": bool(row),
            "how": match.get("match"),
        }

    verdict_word = {DECISION_CONFIRM: "confirmed", DECISION_CORRECT: "revised",
                    DECISION_ACCEPTED: "not reviewed"}
    out: List[Dict[str, Any]] = []
    directory = base / "decisions"
    for log_path in sorted(directory.glob("*.jsonl")) if directory.is_dir() else []:
        if goal and log_path.stem != goal:
            continue
        log = DecisionLog(log_path.stem, directory=directory)
        run = log.current_run()
        if run is None:
            continue
        records = log.run_records()
        shown = _presentation(log_path.stem)
        try:
            work = DecisionWork(config_for_goal(log_path.stem))
        except ValueError:
            work = None
        proposed = [r for r in records if r.get("kind") == KIND_PROPOSED]
        rulings: Dict[str, List[Dict[str, Any]]] = {}
        for r in records:
            if r.get("kind") == KIND_DECIDED:
                rulings.setdefault(r["ref"], []).append(r)
        by_decided = {d["id"]: p for p in proposed for d in rulings.get(p["id"], [])}
        shared: Dict[Any, int] = {}
        for p in proposed:
            if p.get("turn_uid"):
                shared[p["turn_uid"]] = shared.get(p["turn_uid"], 0) + 1

        loop: Dict[str, List[Dict[str, Any]]] = {}
        started = run.get("ts") or ""
        for e in events:
            if e.get("event_type") != "andon_event" or e.get("goal") != log_path.stem:
                continue
            if (e.get("timestamp") or "") < started:
                continue
            details = e.get("details") or {}
            item_id = details.get("item_id")
            if not item_id:
                # A tier-down flag names the decisions it rests on; it belongs
                # to the last of them — the confirmation that met the rule.
                ids = [p.get("decided_id") for p in (e.get("provenance") or [])]
                owner = by_decided.get(ids[-1]) if ids else None
                item_id = owner["item_id"] if owner else None
            if not item_id:
                continue
            step = {"kind": "flagged", "at": e.get("timestamp"), "flag": e.get("flag"),
                    "detector": e.get("detector"), "andon_id": e.get("andon_id"),
                    "summary": e.get("summary"), "halted": list(e.get("halted") or [])}
            loop.setdefault(item_id, []).append(step)
            proposal = proposals.get(e.get("andon_id"))
            if proposal:
                loop[item_id].append({
                    "kind": "proposed", "at": proposal.get("timestamp"),
                    "version": proposal.get("version"),
                    "proposal_id": proposal.get("proposal_id"),
                    "replayed": proposal.get("replayed"),
                    "would_change": proposal.get("would_change")})
                sign = signed.get(proposal.get("proposal_id"))
                if sign:
                    loop[item_id].append({
                        "kind": "signed", "at": sign.get("timestamp"),
                        "version": sign.get("version"), "by": sign.get("signed_by")})

        items: List[Dict[str, Any]] = []
        previous_at = started
        for order, p in enumerate(proposed, 1):
            turn = _step_turn(p.get("turn_uid"))
            split = shared.get(p.get("turn_uid"), 1)
            if split > 1 and turn["seconds"] is not None:
                turn["seconds"] /= split
                turn["shared_with"] = split
            keg = p.get("keg") or None
            ruled = [{
                "decision": d.get("decision"), "word": verdict_word.get(d.get("decision"), "?"),
                "output": dict(d.get("output") or {}), "at": d.get("ts"),
                "after": d.get("after"), "by": d.get("by"),
                "said": d.get("operator_said"),
                **_step_turn(d.get("turn_uid")),
            } for d in rulings.get(p["id"], [])]
            final = ruled[-1] if ruled else None
            # A declared question belongs to the item proposed next after it,
            # in the same session.
            question = None
            for row in asked:
                at = row.get("timestamp") or ""
                if (previous_at <= at <= (p.get("ts") or "")
                        and row.get("session_id") == p.get("session_id")):
                    question = {"text": row["stages"]["execution"].get("question"),
                                "at": at, **_step_turn(row.get("turn_uid"))}
            previous_at = p.get("ts") or previous_at
            why = work.why(p) if work is not None else (p.get("reasoning") or "")
            value = (work.value_text if work is not None else
                     (lambda o: ", ".join(f"{k} {v}" for k, v in o.items())))
            items.append({
                "order": order, "item_id": p["item_id"],
                "label": str((p.get("inputs") or {}).get(shown["label_key"], ""))
                         if shown["label_key"] else "",
                "inputs": dict(p.get("inputs") or {}),
                "proposed": dict(p.get("output") or {}),
                "proposed_text": value(p.get("output") or {}),
                "final_text": value(final["output"]) if final else None,
                "decided_by": (f"keg v{keg.get('version')}" if keg
                               else f"model ({p.get('tier') or turn['tier'] or '?'})"),
                "keg": keg, "tier": p.get("tier") or turn["tier"],
                "reasoning": (p.get("reasoning") or "").strip(), "why": why,
                "at": p.get("ts"), "batch": p.get("batch"), "turn": turn,
                "said": p.get("operator_said"),
                "question": question, "rulings": ruled,
                "verdict": final["word"] if final else "awaiting the operator",
                "revised": bool(final and final["decision"] == DECISION_CORRECT),
                "loop": loop.get(p["item_id"], []),
                "example": {
                    "goal": log_path.stem, "run": run.get("run_number"),
                    "item_id": p["item_id"], "inputs": dict(p.get("inputs") or {}),
                    "proposed": dict(p.get("output") or {}),
                    "reasoning": (p.get("reasoning") or "").strip(),
                    "decided_by": "keg" if keg else "model",
                    "keg_version": keg.get("version") if keg else None,
                    "tier": p.get("tier") or turn["tier"], "model": turn["model"],
                    "question_asked": question["text"] if question else None,
                    # The operator's own words on this item: an answer to the
                    # question, or the reason given with a revision.
                    "operator_said": [
                        w for w in ([p.get("operator_said")]
                                    + [d.get("operator_said") for d in rulings.get(p["id"], [])])
                        if w],
                    "final": dict(final["output"]) if final else None,
                    "verdict": final["word"] if final else None,
                    "reviewed_by_operator": bool(
                        final and final["decision"] != DECISION_ACCEPTED),
                    "turn_uid": p.get("turn_uid"), "record_hash": turn["record_hash"],
                },
            })
        out.append({
            "goal": log_path.stem, "title": shown["title"] or log_path.stem,
            "item_name": shown["item_name"], "run_number": run.get("run_number"),
            "label": run.get("label") or "", "items": items,
        })
    return {"goals": out}


def trace_export(home: Optional[Path] = None, *, goal: Optional[str] = None) -> str:
    """The trace as JSON Lines: one decision per line, in order. Only decided
    items are exported — an item still waiting has no answer to learn from."""
    lines = [
        json.dumps(item["example"], sort_keys=True, ensure_ascii=False)
        for g in trace(home, goal=goal)["goals"] for item in g["items"]
        if item["example"]["final"] is not None
    ]
    return "\n".join(lines) + ("\n" if lines else "")


# "About the same" is a claim. It is made only inside this band.
SAME_BAND = 0.25


def _periods(units: List[Dict[str, Any]], shown: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The run before its batch and the batch itself, side by side and never
    blended: how many items each handled, how many needed a model, what the
    model calls cost, and what the same items would have cost had every one
    gone to a model. Empty when the run has no batch — there is one period.

    The all-model baseline is the item count times the FIRST period's measured
    cost per model-decided item (not its plain average, which already includes
    the items its keg served and would understate the baseline)."""
    batches: List[str] = []
    for u in units:
        if u["batch"] and u["batch"] not in batches:
            batches.append(u["batch"])
    if not batches:
        return []
    first = [u for u in units if not u["batch"]]
    first_model = [u for u in first if not u["keg"]]
    priced = [u["deciding"]["cost"] for u in first_model if u["deciding"]["cost"] is not None]
    rate = (sum(priced) / len(priced)) if priced else None

    def _awaiting(u: Mapping[str, Any]) -> bool:
        return not (u["confirmed"] or u["accepted"] or u["corrected"])

    def _who(group: List[Dict[str, Any]], keg: bool) -> Dict[str, Any]:
        rows = [u for u in group if bool(u["keg"]) == keg]
        return {
            "keg": keg, "units": len(rows),
            "confirmed": sum(1 for u in rows if u["confirmed"]),
            "accepted": sum(1 for u in rows if u["accepted"]),
            "revised": sum(1 for u in rows if u["corrected"]),
            "awaiting": sum(1 for u in rows if _awaiting(u)),
            "seconds": _mean([u["deciding"]["seconds"] for u in rows]),
            "cost": _mean([u["deciding"]["cost"] for u in rows]),
        }

    def _one(label: str, group: List[Dict[str, Any]]) -> Dict[str, Any]:
        n = len(group)
        model = [u for u in group if not u["keg"]]
        keg = n - len(model)
        calls = sum(u["deciding"]["model_calls"] for u in model)
        cost = sum(u["deciding"]["cost"] or 0.0 for u in group)
        baseline = rate * n if rate is not None else None
        return {
            "label": label, "units": n, "keg_units": keg, "model_units": len(model),
            "keg_share": keg / n if n else 0.0,
            "model_calls": calls,
            "calls_per_model_unit": calls / len(model) if model else None,
            "model_calls_per_unit": calls / n if n else None,
            "seconds_per_unit": _mean([u["deciding"]["seconds"] for u in group]),
            "cost": cost, "cost_per_unit": cost / n if n else None,
            "all_priced": all(u["deciding"]["priced"] for u in model),
            "baseline": baseline, "baseline_rate": rate,
            "savings": (baseline - cost) if baseline is not None else None,
            "confirmed": sum(1 for u in group if u["confirmed"]),
            "accepted": sum(1 for u in group if u["accepted"]),
            "revised": sum(1 for u in group if u["corrected"]),
            # Decided by a model or the keg and not yet ruled on by the
            # operator. With it, every count on the page adds up to ``units``.
            "awaiting": sum(1 for u in group if _awaiting(u)),
            # The one source for "who decided": the page's table reads this.
            "who": [_who(group, False), _who(group, True)],
            "of": n,
        }

    # One period before any batch, then one per batch in the order they began
    # (a batch is a released backlog stage). Labels come from the goal's own
    # declaration, stage by stage.
    stage_labels = list(shown.get("stage_labels") or [])
    base = shown.get("batch_label") or "Batch"
    out = [_one(shown.get("before_label") or "Before the batch", first)]
    for index, batch in enumerate(batches):
        label = (stage_labels[index] if index < len(stage_labels)
                 else base if index == 0 else f"{base} {index + 1}")
        period = _one(label, [u for u in units if u["batch"] == batch])
        period["batch"] = batch
        out.append(period)
    return out


def _open_items(goal: str, records: List[Dict[str, Any]]) -> int:
    """Items in the goal's queue that this run has not decided yet. A page
    read while work is under way must say so: its counts are "so far"."""
    try:
        from grove.decision_work import DecisionWork, config_for_goal

        work = DecisionWork(config_for_goal(goal))
        done = {r.get("item_id") for r in records if r.get("kind") == "proposed"}
        done |= set(work.set_aside_items())
        return sum(1 for path in work.queue_items() if path.stem not in done)
    except Exception:  # noqa: BLE001 — no readable queue: nothing is known to be open
        return 0


def _with_open(periods: List[Dict[str, Any]], open_items: int) -> List[Dict[str, Any]]:
    """Undecided items belong to the period under way: the last one."""
    if periods and open_items:
        periods[-1]["of"] = periods[-1]["units"] + open_items
    return periods


def _why_model(goal: str, units: List[Dict[str, Any]], records: List[Dict[str, Any]],
               run: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Why each batch item that went to a model went there, from the records:

      new       its reference key was never seen before the batch;
      judgment  the key was seen, and the goal's own standard work sends it
                to a model: the reference table has no single answer for it
                (several, or none), or the keg's own rule hands the case back;
      other     neither — the keg should arguably have covered it.

    ``key`` is the goal's declared reference input ("vendor"). Empty when the
    goal declares no reference table."""
    out = {"key": None, "new": 0, "judgment": 0, "other": 0, "other_items": []}
    try:
        from grove import keg as keg_mod
        from grove.decision_work import DecisionWork, _norm_key, config_for_goal
        from grove.pattern_cache import PatternCacheStore, STATUS_REJECTED

        cfg = config_for_goal(goal)
        if cfg.reference is None:
            return out
        key_input = cfg.reference.key_input
        table = DecisionWork(cfg).reference()
        # The run's own standard work: its latest signed keg version, whatever
        # has happened to it since (a later reset revokes it; the run's record
        # of why it sent an item to a model does not change).
        spec, latest = None, -1
        for entry in PatternCacheStore().all():
            record = keg_mod.keg_record(entry).get("keg") or {}
            version = int(record.get("version") or 0)
            if (record.get("dock_goal") == goal and entry.status != STATUS_REJECTED
                    and record.get("lineage") == (run or {}).get("run_id")
                    and version > latest and keg_mod.keg_of(entry)):
                spec, latest = keg_mod.keg_of(entry), version
    except Exception:  # noqa: BLE001 — the reasons are extra; the counts stand
        return out
    inputs = {r["item_id"]: r.get("inputs") or {} for r in records if r.get("kind") == "proposed"}
    last = next((u["batch"] for u in reversed(units) if u["batch"]), None)
    seen = {_norm_key(inputs.get(u["item_id"], {}).get(key_input))
            for u in units if u["batch"] != last}
    out["key"] = key_input
    for u in units:
        if u["batch"] != last or u["keg"]:
            continue
        given = inputs.get(u["item_id"], {})
        key = _norm_key(given.get(key_input))
        if key not in seen:
            out["new"] += 1
        elif (len(table.values(given.get(key_input)) if table else []) != 1
              or (spec is not None and keg_mod.defers(spec, given))):
            out["judgment"] += 1
        else:
            out["other"] += 1
            out["other_items"].append(u["order"])
    return out


def _pct(part: int, whole: int) -> str:
    """A share as a whole percent — one rounding, used everywhere."""
    return f"{int(100 * part / whole + 0.5)}%" if whole else "0%"     # halves round up


def _headline(periods: List[Dict[str, Any]], why: Mapping[str, Any],
              item_name: Any) -> Optional[Dict[str, Any]]:
    """The scorecard's opening, computed over however many periods the run
    has: what the signed rule handled in the LATEST one, how many items needed
    a model in each (count, then whole percent), and why the latest one's did.
    Nothing here is asserted that the counts do not show. ``same`` is whether
    the last two periods needed a model about equally often (SAME_BAND)."""
    if len(periods) < 2:
        return None
    before, batch = periods[0], periods[-1]
    one, many = item_name

    def short(period: Mapping[str, Any]) -> str:
        return str(period["label"]).split(" · ")[0]

    keys = f"{why['key']}s" if why.get("key") else None
    still = batch.get("of", batch["units"]) - batch["units"]
    so_far = (f" so far; {still} of {batch['of']} still to come" if still > 0 else "")
    if batch["keg_units"]:
        lead = (f"{short(batch)}: the signed rule handled {batch['keg_units']} of "
                f"{batch['units']} {many}{so_far}. No model call.")
    else:
        lead = (f"{short(batch)}: all {batch['units']} {many}{so_far} needed a model.")
    a, b = before["model_units"], batch["model_units"]
    first = (f"{short(before)} needed a model on {a} of {before['units']} "
             f"({_pct(a, before['units'])}).")
    # Every period between the first and the latest, one short sentence each.
    for middle in periods[1:-1]:
        first += (f" {short(middle)} needed one on {middle['model_units']} of "
                  f"{middle['units']} ({_pct(middle['model_units'], middle['units'])}).")
    second = (f"{short(batch)} needed one on {b} of {batch['units']} "
              f"({_pct(b, batch['units'])})")
    explained = why.get("new", 0) + why.get("judgment", 0)
    all_explained = bool(keys) and b > 0 and explained == b
    if all_explained:
        if why["new"] and why["judgment"]:
            second += f", all new {keys} or cases the rule handed back."
        elif why["new"]:
            second += f", all new {keys}."
        else:
            second += ", all cases the rule handed back."
    elif keys and b:
        second += (f": {why['new']} new {keys}, {why['judgment']} the rule handed back, "
                   f"{why['other']} neither.")
    else:
        second += "."
    prior = periods[-2]["model_units"]
    return {
        "lead": lead, "first": first, "second": second,
        "cost_before": before["cost_per_unit"], "cost_batch": batch["cost_per_unit"],
        "same": prior > 0 and abs(b - prior) / prior <= SAME_BAND,
        "all_explained": all_explained, "complete": still <= 0,
    }


def _tickets(loop: Mapping[str, Any], model: Any, months: int = 1) -> Dict[str, Any]:
    """The run's signed changes to standard work, as the fixes that would
    otherwise have been engineering tickets. One per keg version the operator
    signed (an alias, a revocation and a draft sent back are not changes to
    standard work and are not counted). A version proposed because the work
    kept agreeing is an enhancement; one proposed after a correction is an
    exception.

    Measured, from ledger timestamps: how long the operator took to sign each
    one after it was proposed, and — for an exception — how long from the
    correction to the signed fix. Estimated, from the goal's declared ticket
    model: the engineering hours and dollars those tickets would have cost.
    No model declared, no estimate."""
    halts = sorted((h for h in (loop.get("brake") or {}).get("halts") or [] if h.get("at")),
                   key=lambda h: str(h["at"]))
    changes = []
    for signed in loop.get("signatures") or []:
        exception = signed.get("flag") == "anomaly"
        fix = None
        if exception and signed.get("signed_at"):
            before = [h for h in halts if str(h["at"]) <= str(signed["signed_at"])]
            a, b = _when(before[-1]["at"]) if before else None, _when(signed["signed_at"])
            fix = (b - a).total_seconds() if a and b else None
        changes.append({
            "version": signed.get("version"),
            "kind": "exception" if exception else "enhancement",
            "review_seconds": signed.get("seconds"), "fix_seconds": fix,
            "signed_by": signed.get("signed_by"),
        })
    out: Dict[str, Any] = {
        "changes": changes, "count": len(changes),
        "exceptions": sum(1 for c in changes if c["kind"] == "exception"),
        "review_seconds": sum(c["review_seconds"] or 0.0 for c in changes),
        "model": None, "hours": None, "dollars": None, "break_even": None,
        # The run's pace: its signed changes over the months it covers (one
        # per period). What a month of this work avoids, at that pace.
        "months": months, "per_month": len(changes) / months if months else None,
        "dollars_per_month": None,
    }
    if model is not None:
        out["model"] = {
            "hours_per_ticket": model.hours_per_ticket, "loaded_rate": model.loaded_rate,
            "source": model.source, "price_per_month": model.price_per_month}
        out["hours"] = len(changes) * model.hours_per_ticket
        out["dollars"] = out["hours"] * model.loaded_rate
        out["dollars_per_month"] = out["dollars"] / months if months else None
        # Tickets a month whose avoided cost pays for the dock: its price over
        # what one ticket costs. Computed; None when no price is declared.
        ticket = model.hours_per_ticket * model.loaded_rate
        out["break_even"] = (model.price_per_month / ticket
                             if model.price_per_month and ticket else None)
    return out


def _learning(home: Path, goal: str, run: Mapping[str, Any],
              prices: Mapping[str, Any]) -> Dict[str, Any]:
    """What the run spent LEARNING, once, read off the Kaizen ledger: the
    model calls Kaizen made to draft standard work, and the backtests. Kept
    apart from the cost of deciding items so that neither hides in the other.

    A backtest is a replay of recorded cases against the draft, with no
    model: it costs nothing and is counted, not priced. A draft's tokens are
    on its answer record from 2026-10-06; earlier drafts are counted and
    marked as not recorded, never estimated."""
    events: List[Dict[str, Any]] = []
    directory = _ledger_dir(home)
    if directory.is_dir():
        for path in directory.glob("*.jsonl"):
            events += _jsonl(path)
    started = str(run.get("ts") or "")
    mine = {e.get("andon_id"): e for e in events
            if e.get("event_type") == "andon_event" and e.get("goal") == goal
            and str(e.get("timestamp") or "") >= started}
    out = {"drafts": 0, "unrecorded": 0, "calls": 0, "input": 0, "output": 0,
           "cost": 0.0, "priced": True, "from_table": 0, "backtests": 0}
    attempts: List[Dict[str, Any]] = []
    for e in events:
        if e.get("event_type") != "kaizen_answer" or e.get("andon_id") not in mine:
            continue
        origin = mine[e["andon_id"]]
        if e.get("kind") == "standard_work":
            out["backtests"] += 1
            if origin.get("detector") == "reference_agreement":
                out["from_table"] += 1          # built from the table: no model
                continue
            out["drafts"] += 1
            if e.get("drafting"):
                attempts += list(e["drafting"])
            else:
                out["unrecorded"] += 1
    # Drafts that failed at every tier are on the failure's own andon event.
    for e in mine.values():
        if e.get("detector") == "kaizen_failure":
            attempts += [a for a in ((e.get("details") or {}).get("attempts") or [])
                         if isinstance(a, dict) and a.get("tokens")]
    for attempt in attempts:
        tokens = attempt.get("tokens") or {}
        out["calls"] += int(tokens.get("calls") or 0)
        out["input"] += int(tokens.get("input") or 0)
        out["output"] += int(tokens.get("output") or 0)
        cost = _turn_cost({"input": tokens.get("input"), "output": tokens.get("output")},
                          prices["facts"].get(tokens.get("model")))
        if cost["priced"]:
            out["cost"] += cost["cost"]
        elif tokens.get("calls"):
            out["priced"] = False
    return out


def _coverage(goal: str, units: List[Dict[str, Any]], records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How much of this run's work the goal's SERVING keg answers: the signed
    keg replayed over every decided item's inputs. This — not the run's own
    mix, which includes the items that had to be decided before any keg
    existed — is the share standard work takes once it is in place."""
    from grove import keg as keg_mod
    from grove.pattern_cache import PatternCacheStore, STATUS_ACTIVE

    serving = None
    try:
        for entry in PatternCacheStore().all():
            record = keg_mod.keg_record(entry).get("keg") or {}
            if record.get("dock_goal") == goal and entry.status == STATUS_ACTIVE:
                serving = entry
    except Exception:  # noqa: BLE001 — no cache yet means no keg serving
        serving = None
    if serving is None:
        return {"keg": None, "version": None, "covered": 0, "of": len(units), "share": 0.0}
    spec = keg_mod.keg_of(serving) or {}
    inputs = {r["item_id"]: r.get("inputs") or {} for r in records if r.get("kind") == "proposed"}
    covered = sum(
        1 for u in units if keg_mod.evaluate(spec, inputs.get(u["item_id"], {})) is not None)
    return {
        "keg": f"{spec.get('name')} v{spec.get('version')}",
        "version": spec.get("version"),
        "covered": covered, "of": len(units),
        "share": covered / len(units) if units else 0.0,
    }


def _presentation(goal: str) -> Dict[str, Any]:
    """How the goal names its own work: a title, what one item is called, and
    which input labels an item. Read from the goal's declaration. A log whose
    goal is no longer in the Dock still reports, under plain defaults."""
    from grove.decision_work import load_config
    from grove.dock import load_dock

    dock = load_dock()     # a malformed Dock raises: a defect to fix, not to paper over
    cfg = next(
        (load_config(g) for g in (getattr(dock, "goals", None) or ()) if g.id == goal), None)
    if cfg is None:
        return {"title": None, "item_name": ("item", "items"), "label_key": None}
    return {
        "title": cfg.keg.name if cfg.keg else None,
        "item_name": tuple(cfg.item_name),
        "label_key": cfg.reference.key_input if cfg.reference else None,
        "before_label": cfg.work_session.before_label,
        "batch_label": cfg.work_session.batch_label,
        "ticket_model": cfg.ticket_model,
        # One label per backlog stage, in release order: the run's later periods.
        "stage_labels": [label for _folder, label in cfg.backlog_stages],
    }


def _when(value: Any):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _changed(served: Mapping[str, Any], final: Mapping[str, Any]) -> str:
    """What a correction changed, e.g. ``a → b`` (the field is named only
    when the work has more than one output)."""
    parts = [
        (k, served.get(k), final.get(k)) for k in sorted(set(served) | set(final))
        if served.get(k) != final.get(k)
    ]
    if len(set(served) | set(final)) == 1:
        return ", ".join(f"{a} → {b}" for _, a, b in parts)
    return ", ".join(f"{k} {a} → {b}" for k, a, b in parts)


def _trigger(andon: Optional[Mapping[str, Any]], draft: Optional[Mapping[str, Any]],
             by_item: Mapping[str, Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """What led to a keg version, as the ledger recorded it: the detector that
    fired and what it saw (from the andon event), and how the proposed version
    replayed on history (from Kaizen's proposal). None when the ledger holds
    no andon for the version. Nothing here is specific to one detector: the
    details are passed through as recorded."""
    if not andon:
        return None
    details = dict(andon.get("details") or {})
    out: Dict[str, Any] = {
        "detector": andon.get("detector"), "summary": andon.get("summary"),
        "confirmations": details.get("confirmations"),
        "threshold": details.get("threshold") or (details.get("rule") or {}).get("threshold"),
        "key": details.get("key"), "same_as": details.get("same_as"),
        "output": details.get("output"), "identity": details.get("identity") or [],
    }
    if details.get("item_id"):
        unit = by_item.get(details["item_id"]) or {}
        out["item"] = {"order": unit.get("order"), "label": unit.get("label") or "",
                       "change": _changed(details.get("served") or {},
                                          details.get("corrected") or {})}
    if draft and draft.get("replayed") is not None:
        out["replay"] = {k: draft.get(k) for k in ("replayed", "unchanged", "would_change")}
    return out


def _loop(home: Path, goal: str, run: Mapping[str, Any],
          units: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The improvement loop as this run saw it, read off the Kaizen ledger and
    the pattern cache: each keg version the operator signed (and how long
    after Kaizen proposed it), each correction that halted a keg, and where in
    the run's order each of those took effect. A keg belongs to the run whose
    lineage its cache entry carries. Also annotates each unit with what a
    correction changed and whether it halted the keg."""
    from grove import keg as keg_mod
    from grove.pattern_cache import PatternCacheStore, STATUS_REJECTED

    run_id = run.get("run_id")
    try:
        entries = [
            e for e in PatternCacheStore().all()
            if (keg_mod.keg_record(e).get("keg") or {}).get("lineage") == run_id
        ]
    except Exception:  # noqa: BLE001 — no cache yet means no keg in this run
        entries = []
    in_run = {e.pattern_id: e for e in entries}

    events: List[Dict[str, Any]] = []
    directory = _ledger_dir(home)
    if directory.is_dir():
        for path in directory.glob("*.jsonl"):
            events += _jsonl(path)
    events.sort(key=lambda e: e.get("timestamp") or "")

    def _takes_effect(at: Any) -> Optional[int]:
        """The first item decided after ``at`` — where the event shows up."""
        moment = _when(at)
        if moment is None:
            return None
        for u in units:
            decided_at = _when(u["at"])
            if decided_at is not None and decided_at > moment:
                return u["order"]
        return None

    started = _when(run.get("ts"))
    by_item = {u["item_id"]: u for u in units}
    proposed: Dict[str, Dict[str, Any]] = {}
    signed: List[Dict[str, Any]] = []
    marks: List[Dict[str, Any]] = []
    halts: List[Dict[str, Any]] = []
    andons: Dict[str, Dict[str, Any]] = {}       # what Jidoka flagged, by andon id
    drafts: Dict[str, Dict[str, Any]] = {}       # Kaizen's proposal, by keg version
    for e in events:
        kind = e.get("event_type")
        if kind == "andon_event" and e.get("goal") == goal and e.get("andon_id"):
            andons[e["andon_id"]] = e
        if kind == "kaizen_proposal" and e.get("pattern_id"):
            drafts[e["pattern_id"]] = e
        if kind == "kaizen_proposal" and e.get("proposal_id"):
            proposed[e["proposal_id"]] = e
        elif (kind == "new_standard_work" and e.get("dock_goal") == goal
              and e.get("proposal_id") in proposed and e.get("pattern_id") in in_run):
            start = proposed[e["proposal_id"]]
            a, b = _when(start.get("timestamp")), _when(e.get("timestamp"))
            signed.append({
                "version": e.get("version"), "pattern_id": e.get("pattern_id"),
                "seconds": (b - a).total_seconds() if a and b else None,
                "signed_at": e.get("timestamp"), "flag": start.get("flag"),
                "signed_by": e.get("signed_by"),
            })
            marks.append({"kind": "signed", "version": e.get("version"),
                          "at": e.get("timestamp"),
                          "before": _takes_effect(e.get("timestamp"))})
        elif (kind == "andon_event" and e.get("goal") == goal
              and set(e.get("halted") or ()) & set(in_run)):
            moment = _when(e.get("timestamp"))
            if started is not None and moment is not None and moment < started:
                continue
            details = e.get("details") or {}
            unit = by_item.get(details.get("item_id"))
            halts.append({"item": unit["order"] if unit else None,
                          "at": e.get("timestamp"),
                          "corrected": bool(details.get("corrected"))})
            if unit is not None:
                unit["halted_keg"] = True
            marks.append({"kind": "halted", "corrected": bool(details.get("corrected")),
                          "item": unit["order"] if unit else None,
                          "at": e.get("timestamp"),
                          "before": _takes_effect(e.get("timestamp"))})
    for u in units:
        u.setdefault("halted_keg", False)
        u["change"] = _changed(u["served"], u["final"]) if u["corrected"] else ""

    times = {s["pattern_id"]: s for s in signed}
    versions: List[Dict[str, Any]] = []
    for entry in entries:
        record = keg_mod.keg_record(entry)
        if not record.get("signed"):
            continue
        spec = keg_mod.keg_of(entry) or {}
        rules = spec.get("conditions") or []
        sig = times.get(entry.pattern_id) or {}
        versions.append({
            "version": spec.get("version"),
            "name": spec.get("name"),
            "pattern_id": entry.pattern_id,
            "state": keg_mod.lifecycle(entry.status)["state"],
            "serves": keg_mod.lifecycle(entry.status)["serves"],
            "decides": sum(1 for r in rules if not r.get("defer")),
            "hands_back": [str(r.get("if") or "") for r in rules if r.get("defer")],
            "reserve": str(spec.get("reserve") or ""),
            "scope": spec.get("scope"),
            "evidence": record.get("repetition_count"),
            "trigger": _trigger(andons.get(str(record.get("andon_id") or "")),
                                drafts.get(entry.pattern_id), by_item),
            "feedback": [str(f) for f in (record.get("feedback") or [])],
            "signed_by": (record.get("signed") or {}).get("by"),
            "signed_at": (record.get("signed") or {}).get("at"),
            "seconds_to_signature": sig.get("seconds"),
        })
    versions.sort(key=lambda v: (v["version"] or 0, v["signed_at"] or ""))

    misses = [u["order"] for u in units if u["keg"] and u["corrected"]]
    resumed = None
    if halts:
        last = _when(halts[-1]["at"])
        for s in signed:
            at = _when(s["signed_at"])
            if last is not None and at is not None and at > last:
                resumed = s["version"]
                break
    return {
        "signatures": signed,
        "events": marks,
        "versions": versions,
        "drafts_returned": sum(1 for e in entries if e.status == STATUS_REJECTED),
        "brake": {"misses": misses, "halts": halts, "resumed_version": resumed},
    }


def project_calls(goal_report: Mapping[str, Any], calls_per_month: int) -> Dict[str, Any]:
    """Project the run to a monthly volume stated in MODEL CALLS: "if your
    agents make this many model calls a month today". The calls are what an
    all-model month makes; the run's measured calls per model-decided unit
    turn them into units of work, and :func:`project` does the rest. Adds the
    per-call basis the page shows: measured cost and seconds per model call,
    and the share of calls the serving keg avoids."""
    model = goal_report["model_avg"]
    per_unit = model.get("model_calls") or 0.0
    units = (calls_per_month / per_unit) if per_unit else 0.0
    out = project(goal_report, units)
    out["calls"] = int(calls_per_month)
    out["per_call"] = {
        "calls_per_unit": per_unit or None,
        "cost": (model["cost"] / per_unit) if per_unit and model.get("cost") is not None else None,
        "seconds": ((model["seconds"] / per_unit)
                    if per_unit and model.get("seconds") is not None else None),
        "share_avoided": goal_report["coverage"]["share"],
    }
    return out


def project(goal_report: Mapping[str, Any], units_per_month: float) -> Dict[str, Any]:
    """Project one goal's measured per-unit figures to a monthly volume.

    Three ways of doing the same volume of work, each from MEASURED per-unit
    averages of this run (the frontier line alone is an estimate):

      ``all_model``    — every unit decided by the model, as before any keg;
      ``with_keg``     — the serving keg takes the share it covers, at T0;
      ``all_frontier`` — every unit at the frontier tier's declared prices.
    """
    n = units_per_month
    model, keg = goal_report["model_avg"], goal_report["keg_avg"]
    share = goal_report["coverage"]["share"]
    keg_seconds = keg["seconds"] if keg["seconds"] is not None else 0.0

    def _row(cost_per: Optional[float], seconds_per: Optional[float], calls_per: Optional[float],
             tokens_per: Optional[float]) -> Dict[str, Any]:
        return {
            "cost": None if cost_per is None else cost_per * n,
            "hours": None if seconds_per is None else seconds_per * n / 3600.0,
            "model_calls": None if calls_per is None else calls_per * n,
            "tokens": None if tokens_per is None else tokens_per * n,
        }

    def _blend(model_value: Optional[float], keg_value: float) -> Optional[float]:
        if model_value is None:
            return None
        return (1.0 - share) * model_value + share * keg_value

    all_model = _row(model["cost"], model["seconds"], model["model_calls"], model["tokens"])
    with_keg = _row(
        _blend(model["cost"], 0.0), _blend(model["seconds"], keg_seconds),
        _blend(model["model_calls"], 0.0), _blend(model["tokens"], 0.0))
    frontier = _row(goal_report["frontier"]["cost"], None, None, None)

    def _saved(key: str) -> Optional[float]:
        if all_model[key] is None or with_keg[key] is None:
            return None
        return all_model[key] - with_keg[key]

    return {
        "units": n, "share": share,
        "all_model": all_model, "with_keg": with_keg, "all_frontier": frontier,
        "avoided": {k: _saved(k) for k in ("cost", "hours", "model_calls", "tokens")},
        "frontier_with_keg": (
            None if frontier["cost"] is None else frontier["cost"] * (1.0 - share)),
        "avoided_vs_frontier": (
            None if frontier["cost"] is None or with_keg["cost"] is None
            else frontier["cost"] * share   # the covered share never reaches a frontier call
        ),
    }


# ── models: what our own records say about each one ───────────────────


def model_evidence(home: Any = None) -> List[Dict[str, Any]]:
    """Each model that has run turns here, with what the records show: how
    many turns, how many of them did not complete and failed upward to a
    higher tier, how many decisions it proposed and how many of those the
    operator revised, and what its turns cost. Read from the turn records and
    the goals' decision logs; nothing is written. Most-used first."""
    base = _home(home)
    prices = _prices(base)
    intents: Dict[str, Dict[str, Any]] = {}
    for row in _jsonl(base / "intent_records.jsonl"):
        if row.get("turn_uid"):
            intents[row["turn_uid"]] = row        # later lines supersede
    rows: Dict[str, Dict[str, Any]] = {}

    def _row(model: str) -> Dict[str, Any]:
        return rows.setdefault(model, {
            "model": model, "turns": 0, "failed_upward": 0, "proposed": 0,
            "revised": 0, "cost": 0.0, "priced_turns": 0, "tiers": set()})

    failed: set = set()
    for record in intents.values():
        escalation = ((record.get("stages") or {}).get("compilation") or {}).get(
            "escalation") or {}
        failed.update(str(a.get("turn_uid")) for a in (escalation.get("attempts") or [])
                      if a.get("turn_uid"))
    for uid, record in intents.items():
        model = record.get("model_used")
        if not model or record.get("tier_selected") == "T0" or "/" not in str(model):
            continue                      # no model ran (a keg, a session rule)
        row, turn = _row(str(model)), _turn(record, prices)
        row["turns"] += 1
        row["tiers"].add(record.get("tier_selected"))
        if uid in failed:
            row["failed_upward"] += 1
        if turn["priced"] and turn["cost"] is not None:
            row["cost"] += turn["cost"]
            row["priced_turns"] += 1
    directory = base / "decisions"
    if directory.is_dir():
        for log_path in sorted(directory.glob("*.jsonl")):
            records = _jsonl(log_path)
            revised = {r.get("ref") for r in records
                       if r.get("kind") == "decided" and r.get("decision") == "correct"}
            for record in records:
                model = (intents.get(record.get("turn_uid") or "") or {}).get("model_used")
                if record.get("kind") != "proposed" or not model or "/" not in str(model):
                    continue
                row = _row(str(model))
                row["proposed"] += 1
                if record.get("id") in revised:
                    row["revised"] += 1
    out = []
    for row in rows.values():
        row["tiers"] = sorted(t for t in row["tiers"] if t)
        row["cost_per_turn"] = (row["cost"] / row["priced_turns"]) if row["priced_turns"] else None
        out.append(row)
    return sorted(out, key=lambda r: -r["turns"])

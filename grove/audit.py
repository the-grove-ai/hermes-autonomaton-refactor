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

# Scales the Audit page can project a run to: units of work per month.
SCALES = (1_000, 10_000, 100_000, 1_000_000)


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


def _run_check(home: Path, turn_uids: set) -> Dict[str, Any]:
    from grove.decision_work import KIND_PROPOSED, DecisionLog

    out = {"runs": [], "problems": []}
    directory = home / "decisions"
    if not directory.is_dir():
        return out
    for log_path in sorted(directory.glob("*.jsonl")):
        log = DecisionLog(log_path.stem, directory=directory)
        try:
            run = log.current_run()
            proposed = [r for r in log.run_records() if r.get("kind") == KIND_PROPOSED]
        except ValueError as exc:
            out["problems"].append({
                "where": f"decision log {log_path.name}", "subject": "", "problem": str(exc)})
            continue
        if run is None:
            continue
        found = sum(1 for r in proposed if r.get("turn_uid") in turn_uids)
        out["runs"].append({
            "goal": log_path.stem, "run_number": run.get("run_number"),
            "label": run.get("label") or "", "decisions": len(proposed), "with_turn": found,
        })
        for r in proposed:
            if r.get("turn_uid") not in turn_uids:
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
    if live:
        ledger = _ledger_check(base)
        uids = {r.get("turn_uid") for r in _jsonl(path)}
        run_check = _run_check(base, uids)
        runs = run_check["runs"]
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
        "problems": problems,
        "result": result,
    }


RESULT_TEXT = {
    "intact": "CHAIN INTACT — no record altered, removed or reordered",
    "nothing_to_verify": "NOTHING TO VERIFY — no chained records in this store yet",
    "broken": "CHAIN BROKEN",
}


def format_chain_report(report: Mapping[str, Any]) -> List[str]:
    """The report as the terminal prints it."""
    bar = "=" * 62
    lines = [
        bar, "  AUDIT CHAIN CHECK", bar,
        f"  Store                      {report['store']}",
        f"  Records checked            {report['records']:,}",
        f"  Hash-chained records       {report['chained']:,}  "
        f"in {report['chained_sessions']:,} session(s)",
        f"  Older, pre-chain records   {report['unchained']:,}  (counted, not chained)",
    ]
    if report["live"]:
        lines.append(
            f"  Sessions tail-checked      {report['anchored']:,}  "
            "(newest record still present)")
        ledger = report["ledger"]
        if ledger["files"]:
            lines.append(
                f"  Kaizen ledger events       {ledger['chained']:,} chained in "
                f"{ledger['files']:,} file(s)"
                + (f"; {ledger['unchained']:,} older, pre-chain" if ledger["unchained"] else ""))
        for run in report["runs"]:
            label = f" ({run['label']})" if run["label"] else ""
            lines += [
                f"  Run {run['run_number']}{label} · {run['goal']}",
                f"    Decisions in this run    {run['decisions']:,}",
                f"    With a turn on record    {run['with_turn']:,} of {run['decisions']:,}",
            ]
    else:
        lines.append("  Sessions tail-checked      not run (needs the live session database)")
    lines.append(bar)
    if report["result"] == "broken":
        lines.append(f"  RESULT: CHAIN BROKEN — {len(report['problems'])} problem(s)")
        lines.append(bar)
        for p in report["problems"]:
            lines.append(f"  {p['where']}  {p['subject']}  {p['problem']}")
    else:
        lines += [f"  RESULT: {RESULT_TEXT[report['result']]}", bar]
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
        DECISION_CORRECT, KIND_DECIDED, KIND_PROPOSED, DecisionLog,
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
        decided = {r["ref"]: r for r in records if r.get("kind") == KIND_DECIDED}
        deciding_uids = {r.get("turn_uid") for r in records if r.get("kind") == KIND_PROPOSED}
        units: List[Dict[str, Any]] = []
        for order, record in enumerate(
                [r for r in records if r.get("kind") == KIND_PROPOSED], 1):
            verdict = decided.get(record["id"])
            deciding = _turn(intents.get(record.get("turn_uid")), prices)
            confirm_uid = (verdict or {}).get("turn_uid")
            # A turn that both recorded a confirmation and decided the next
            # item is counted once, as that item's deciding turn.
            confirming = (
                _turn(intents.get(confirm_uid), prices)
                if confirm_uid and confirm_uid not in deciding_uids else None
            )
            keg = record.get("keg") or None
            units.append({
                "order": order,
                "item_id": record["item_id"],
                "tier": record.get("tier") or deciding["tier"],
                "by": (f"{keg.get('name')} v{keg.get('version')}" if keg
                       else deciding["model"] or record.get("model")),
                "keg": bool(keg),
                "decision": (verdict or {}).get("decision"),
                "corrected": (verdict or {}).get("decision") == DECISION_CORRECT,
                "deciding": deciding,
                "confirming": confirming,
            })

        by_tier: Dict[str, Dict[str, Any]] = {}
        for tier in sorted({u["tier"] or "?" for u in units}):
            group = [u for u in units if (u["tier"] or "?") == tier]
            turns = [u["deciding"] for u in group]
            by_tier[tier] = {
                "units": len(group),
                "confirmed": sum(1 for u in group if u["decision"] and not u["corrected"]),
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
        goals.append({
            "goal": log_path.stem,
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
            "signatures": _signature_times(base, log_path.stem, run.get("run_id")),
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
        return {"keg": None, "covered": 0, "of": len(units), "share": 0.0}
    spec = keg_mod.keg_of(serving) or {}
    inputs = {r["item_id"]: r.get("inputs") or {} for r in records if r.get("kind") == "proposed"}
    covered = sum(
        1 for u in units if keg_mod.evaluate(spec, inputs.get(u["item_id"], {})) is not None)
    return {
        "keg": f"{spec.get('name')} v{spec.get('version')}",
        "covered": covered, "of": len(units),
        "share": covered / len(units) if units else 0.0,
    }


def _signature_times(home: Path, goal: str, run_id: Optional[str]) -> List[Dict[str, Any]]:
    """For each keg version signed IN THIS RUN: how long from Kaizen's
    proposal to the operator's signature. Read off the ledger; a keg belongs
    to the run whose lineage its cache entry carries."""
    from datetime import datetime

    from grove import keg as keg_mod
    from grove.pattern_cache import PatternCacheStore

    try:
        in_run = {
            entry.pattern_id for entry in PatternCacheStore().all()
            if (keg_mod.keg_record(entry).get("keg") or {}).get("lineage") == run_id
        }
    except Exception:  # noqa: BLE001
        in_run = set()

    proposed: Dict[str, Dict[str, Any]] = {}
    signed: List[Dict[str, Any]] = []
    directory = _ledger_dir(home)
    if not directory.is_dir():
        return signed
    events: List[Dict[str, Any]] = []
    for path in directory.glob("*.jsonl"):
        events += _jsonl(path)
    events.sort(key=lambda e: e.get("timestamp") or "")
    for e in events:
        if e.get("event_type") == "kaizen_proposal" and e.get("proposal_id"):
            proposed[e["proposal_id"]] = e
        elif (e.get("event_type") == "new_standard_work" and e.get("dock_goal") == goal
              and e.get("proposal_id") in proposed and e.get("pattern_id") in in_run):
            start = proposed[e["proposal_id"]]
            try:
                seconds = (datetime.fromisoformat(e["timestamp"])
                           - datetime.fromisoformat(start["timestamp"])).total_seconds()
            except (KeyError, ValueError):
                seconds = None
            signed.append({"version": e.get("version"), "seconds": seconds,
                           "signed_at": e.get("timestamp"), "flag": start.get("flag")})
    return signed


def project(goal_report: Mapping[str, Any], units_per_month: int) -> Dict[str, Any]:
    """Project one goal's measured per-unit figures to a monthly volume.

    Three ways of doing the same volume of work, each from MEASURED per-unit
    averages of this run (the frontier line alone is an estimate):

      ``all_model``    — every unit decided by the model, as before any keg;
      ``with_keg``     — the serving keg takes the share it covers, at T0;
      ``all_frontier`` — every unit at the frontier tier's declared prices.
    """
    n = int(units_per_month)
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
        "avoided_vs_frontier": (
            None if frontier["cost"] is None or with_keg["cost"] is None
            else frontier["cost"] * share   # the covered share never reaches a frontier call
        ),
    }

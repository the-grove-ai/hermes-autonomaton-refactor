"""The audit: integrity and economics, read off the same records.

MESSAGE-TAGGING fixtures — the report is generic over any goal's decision work.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import grove.pattern_cache as pc
from grove import audit
from grove.decision_work import DecisionLog
from grove.intent_store import record_digest
from grove.kaizen_ledger import KaizenLedger

REPO = Path(__file__).resolve().parents[2]
GOAL = "message-triage"


def _intent(home, turn_uid, *, tier, model, calls, tokens, ms, prev=None, session="s1"):
    rec = {
        "session_id": session, "turn_id": f"{session}#{turn_uid}", "turn_uid": turn_uid,
        "tier_selected": tier, "model_used": model, "api_calls": calls,
        "duration_ms": ms, "outcome": "success", "prev_hash": prev,
        "stages": {"execution": {"model_calls": calls, "tokens": tokens}},
    }
    rec["record_hash"] = record_digest(rec)
    with open(home / "intent_records.jsonl", "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec["record_hash"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setattr(pc, "default_pattern_cache_path", lambda: tmp_path / "pattern_cache.db")
    (tmp_path / "routing.operational.yaml").write_text(
        "tier_preferences:\n  T1: {model: small}\n  T3: {model: big}\n"
        "model_facts:\n"
        "  small: {cost_per_mtok_input: 1.0, cost_per_mtok_output: 2.0}\n"
        "  big: {cost_per_mtok_input: 10.0, cost_per_mtok_output: 20.0}\n", encoding="utf-8")
    log = DecisionLog(GOAL, directory=tmp_path / "decisions")
    run = log.start_run("fixture")
    prev = None
    # Two items decided by a model, two by a keg.
    plan = [("m1", "T1", "small", 2, {"input": 1000, "output": 100, "cache_read": 50000}, 8000.0, None),
            ("m2", "T1", "small", 2, {"input": 3000, "output": 300, "cache_read": 50000}, 12000.0, None),
            ("m3", "T0", "pattern_cache", 0, {"input": 0, "output": 0, "cache_read": 0}, 100.0,
             {"name": "Message tagging", "version": 1, "pattern_id": "keg:x:v1:a"}),
            ("m4", "T0", "pattern_cache", 0, {"input": 0, "output": 0, "cache_read": 0}, 100.0,
             {"name": "Message tagging", "version": 1, "pattern_id": "keg:x:v1:a"})]
    for item, tier, model, calls, tokens, ms, keg_ref in plan:
        prev = _intent(tmp_path, f"u-{item}", tier=tier, model=model, calls=calls,
                       tokens=tokens, ms=ms, prev=prev)
        proposed = log.append({
            "kind": "proposed", "run_id": run["run_id"], "item_id": item,
            "inputs": {"channel": "billing"}, "output": {"tag": "finance"},
            "tier": tier, "model": model, "keg": keg_ref, "turn_uid": f"u-{item}"})
        prev = _intent(tmp_path, f"c-{item}", tier="T1", model="small", calls=1,
                       tokens={"input": 10, "output": 5, "cache_read": 50000}, ms=3000.0, prev=prev)
        log.append({"kind": "decided", "run_id": run["run_id"], "ref": proposed["id"],
                    "item_id": item, "decision": "correct" if item == "m4" else "confirm",
                    "output": {"tag": "ops" if item == "m4" else "finance"},
                    "turn_uid": f"c-{item}"})
    KaizenLedger("s1", ledger_dir=tmp_path / ".kaizen_ledger").record(
        "final_response", content_length=1)
    return tmp_path


# ── integrity ─────────────────────────────────────────────────────────


def test_chain_report_is_intact_and_counts_every_decision(home):
    report = audit.chain_report(home)
    assert report["result"] == "intact" and report["problems"] == []
    assert (report["records"], report["chained"]) == (8, 8)
    assert report["ledger"] == {"files": 1, "chained": 1, "unchained": 0}
    assert report["runs"] == [{"goal": GOAL, "run_number": 1, "label": "fixture",
                               "decisions": 4, "with_turn": 4}]
    text = "\n".join(audit.format_chain_report(report))
    assert "RESULT: CHAIN INTACT" in text and "With a turn on record    4 of 4" in text


def test_chain_report_names_every_kind_of_break(home):
    lines = (home / "intent_records.jsonl").read_text().splitlines()
    tampered = json.loads(lines[2])
    tampered["tier_selected"] = "T0"
    # Edit one record, and remove the turn that decided m3 (line 5 of 8).
    (home / "intent_records.jsonl").write_text(
        "\n".join(lines[:2] + [json.dumps(tampered)] + [lines[3]] + lines[5:]) + "\n")
    ledger = home / ".kaizen_ledger" / "s1.jsonl"
    event = json.loads(ledger.read_text().splitlines()[0])
    event["content_length"] = 99
    ledger.write_text(json.dumps(event) + "\n")
    report = audit.chain_report(home)
    assert report["result"] == "broken"
    problems = " | ".join(p["problem"] for p in report["problems"])
    assert "altered" in problems                            # the edited records
    assert "has no turn in the audit trail" in problems     # the decision whose turn was removed
    assert any(p["where"].startswith("ledger s1.jsonl") for p in report["problems"])
    assert "RESULT: CHAIN BROKEN" in "\n".join(audit.format_chain_report(report))


def test_command_line_and_portal_run_the_same_check(home):
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "verify-intent-chain.py")],
        capture_output=True, text=True, env={"GROVE_HOME": str(home), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines() == audit.format_chain_report(audit.chain_report(home))
    script = (REPO / "scripts" / "verify-intent-chain.py").read_text()
    assert "from grove.audit import chain_report, format_chain_report" in script
    assert "def _ledger_report" not in script              # one implementation, not two


def test_missing_store_is_an_error_not_a_pass(tmp_path):
    with pytest.raises(FileNotFoundError):
        audit.chain_report(tmp_path)


# ── economics ─────────────────────────────────────────────────────────


def test_economics_measures_each_tier_from_the_records(home):
    [g] = audit.economics(home)["goals"]
    assert (g["model_units"], g["keg_units"]) == (2, 2)
    t1, t0 = g["by_tier"]["T1"], g["by_tier"]["T0"]
    assert (t1["units"], t1["model_calls"], t1["seconds"]) == (2, 2.0, 10.0)
    assert (t1["fresh_tokens"], t1["cached_tokens"]) == (2200.0, 50000.0)
    # (1000*1 + 100*2)/1e6 and (3000*1 + 300*2)/1e6, averaged. Cached re-read
    # has no declared price, so it is counted and left out of the dollars.
    assert t1["cost"] == pytest.approx((0.0012 + 0.0036) / 2)
    assert g["cache_read_priced"] is False
    assert (t0["units"], t0["model_calls"], t0["cost"], t0["seconds"]) == (2, 0.0, 0.0, 0.1)
    assert (t0["confirmed"], t0["corrected"]) == (1, 1)
    # The deciding turn and the confirming turn are kept apart.
    assert g["units"][0]["confirming"]["model_calls"] == 1
    assert g["totals"]["model_calls"] == 4
    # Frontier: the same measured fresh tokens at the T3 model's prices — an estimate.
    assert g["frontier"]["model"] == "big"
    assert g["frontier"]["cost"] == pytest.approx((2000 * 10 + 200 * 20) / 1e6)


def test_cached_tokens_are_priced_only_when_a_price_is_declared(home):
    cfg = home / "routing.operational.yaml"
    cfg.write_text(cfg.read_text().replace(
        "small: {cost_per_mtok_input: 1.0, cost_per_mtok_output: 2.0}",
        "small: {cost_per_mtok_input: 1.0, cost_per_mtok_output: 2.0, cost_per_mtok_cache_read: 0.1}"))
    [g] = audit.economics(home)["goals"]
    assert g["cache_read_priced"] is True
    assert g["by_tier"]["T1"]["cost"] == pytest.approx((0.0012 + 0.0036) / 2 + 50000 * 0.1 / 1e6)


def test_an_unrecorded_cost_is_listed_as_not_included_never_as_zero(home):
    report = audit.economics(home)
    assert report["included"] == ["model_call"]
    assert "classification" in report["not_included"]
    assert "not yet recorded" in report["not_included"]["classification"]


def test_projection_scales_measured_figures_by_what_the_keg_covers(home):
    [g] = audit.economics(home)["goals"]
    g = {**g, "coverage": {"keg": "Message tagging v1", "covered": 3, "of": 4, "share": 0.75}}
    p = audit.project(g, 1_000_000)
    assert p["all_model"]["cost"] == pytest.approx(0.0024 * 1_000_000)
    assert p["with_keg"]["cost"] == pytest.approx(0.0024 * 1_000_000 * 0.25)
    assert p["avoided"]["cost"] == pytest.approx(0.0024 * 1_000_000 * 0.75)
    assert p["all_model"]["model_calls"] == 2_000_000 and p["with_keg"]["model_calls"] == 500_000
    assert p["all_model"]["hours"] == pytest.approx(10.0 * 1_000_000 / 3600)
    # The keg's own time is counted, not assumed to be zero.
    assert p["with_keg"]["hours"] == pytest.approx((0.25 * 10.0 + 0.75 * 0.1) * 1_000_000 / 3600)
    assert p["all_frontier"]["cost"] == pytest.approx(0.024 * 1_000_000)
    # With no keg serving, nothing is avoided.
    none = audit.project({**g, "coverage": {"keg": None, "covered": 0, "of": 4, "share": 0.0}}, 1000)
    assert none["avoided"]["cost"] == 0.0


def test_a_turn_that_confirmed_one_item_and_decided_the_next_is_counted_once(home):
    log = DecisionLog(GOAL, directory=home / "decisions")
    run = log.current_run()
    last = [r for r in log.run_records() if r["kind"] == "decided"][-1]
    # The next item is decided in the SAME turn that recorded m4's decision.
    log.append({"kind": "proposed", "run_id": run["run_id"], "item_id": "m5",
                "inputs": {}, "output": {"tag": "ops"}, "tier": "T1", "model": "small",
                "keg": None, "turn_uid": last["turn_uid"]})
    [g] = audit.economics(home)["goals"]
    m4, m5 = g["units"][3], g["units"][4]
    assert m5["deciding"]["model_calls"] == 1 and m4["confirming"] is None


# ── the page ──────────────────────────────────────────────────────────


def test_audit_page_shows_the_same_figures_and_its_limits(home):
    from grove.api import fragments

    html = fragments._audit_integrity_html(audit.chain_report(home))
    assert "CHAIN INTACT" in html and "4 decisions, 4 with a turn on record" in html
    assert "does not ask the gateway" in html              # the independence caveat
    eco = fragments._audit_economics_html(audit.economics(home), 1_000_000)
    for text in ("Decided by the keg: 0.10s, 0 tokens, $0 each", "100× faster",
                 "1,000,000 items a month", "Every item decided by a model",
                 "With the keg serving", "Avoided", "estimate:",
                 "NOT priced", "Not included:", "confirmation turn is separate"):
        assert text in eco, text
    assert 'hx-get="/portal/fragments/audit/economics"' in eco
    assert [f"{n:,} items a month" in eco for n in audit.SCALES] == [True] * 4
    assert "Audit</a>" in (REPO / "gateway" / "assets" / "portal" / "index.html").read_text()


def test_audit_page_reads_and_never_writes(home):
    from grove.api import fragments
    before = {p: p.stat().st_mtime_ns for p in home.rglob("*") if p.is_file()}
    fragments._audit_integrity_html(audit.chain_report(home))
    fragments._audit_economics_html(audit.economics(home), 10_000)
    after = {p: p.stat().st_mtime_ns for p in home.rglob("*") if p.is_file()}
    assert set(after) - set(before) <= {home / "pattern_cache.db"}   # opening creates an empty cache
    assert all(after[p] == before[p] for p in before)

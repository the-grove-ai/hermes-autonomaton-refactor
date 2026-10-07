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
    # One link per decision: who decided, corrected or not, turn on record.
    assert [(l["order"], l["keg"], l["corrected"], l["on_record"]) for l in report["links"]] == [
        (1, False, False, True), (2, False, False, True),
        (3, True, False, True), (4, True, True, True)]
    # 2026-10-06: the terminal now prints the panel's own words.
    text = "\n".join(audit.format_chain_report(report))
    assert "AUDIT CHECK · CHAIN INTACT" in text
    assert "Every record accounted for. Nothing altered, removed or reordered." in text
    assert "TURN RECORDS: 8 / 8" in text and "None older or unchained." in text
    assert "DECISIONS THIS RUN (Run 1 · message-triage): 4 / 4" in text
    assert "A record deleted or moved: The next record points to a fingerprint" in text


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
    text = "\n".join(audit.format_chain_report(report))
    assert "AUDIT CHECK · CHAIN BROKEN" in text and "PROBLEMS" in text
    head = audit.check_headline(report)
    first = report["problems"][0]
    assert first["subject"] in head["lead"] and first["where"] in head["lead"]
    assert head["clause"] == f"{len(report['problems'])} problems found."
    assert [l["on_record"] for l in report["links"]] == [True, True, False, True]


def test_command_line_and_portal_run_the_same_check(home):
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "verify-intent-chain.py")],
        capture_output=True, text=True, env={"GROVE_HOME": str(home), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    def _but_the_clock(lines):
        return [l for l in lines if "Last checked" not in l]
    assert _but_the_clock(out.stdout.rstrip().splitlines()) == _but_the_clock(
        audit.format_chain_report(audit.chain_report(home)))
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


# ── the scorecard: the loop as the run saw it ─────────────────────────


def _loop_home(tmp_path, monkeypatch, *, sign_v2=True):
    """Six messages. A keg is signed after the second, serves the third and
    fourth; the operator corrects the fourth, which halts it; v2 is signed
    before the sixth. Every record is written with its own clock time."""
    from grove.pattern_cache import CompiledPattern, PatternCacheStore

    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setattr(pc, "default_pattern_cache_path", lambda: tmp_path / "pattern_cache.db")
    monkeypatch.setattr(audit, "_presentation", lambda goal: {
        "title": "Message tagging", "item_name": ("message", "messages"),
        "label_key": "channel"})
    log = DecisionLog(GOAL, directory=tmp_path / "decisions")
    run = log.append({"kind": "run_started", "run_id": "run-a", "run_number": 3,
                      "label": "", "ts": "2026-01-01T10:00:00+00:00"})

    def keg(version):
        return {"name": "Message tagging", "version": version, "pattern_id": f"keg:mt:v{version}"}

    plan = [("m1", None, 9000.0, "10:01"), ("m2", None, 11000.0, "10:02"),
            ("m3", 1, 100.0, "10:10"), ("m4", 1, 100.0, "10:11"),
            ("m5", None, 61000.0, "10:13"), ("m6", 2 if sign_v2 else None, 100.0, "10:20")]
    for item, version, ms, clock in plan:
        _intent(tmp_path, f"u-{item}", tier="T0" if version else "T1",
                model="pattern_cache" if version else "small", calls=0 if version else 2,
                tokens={"input": 0 if version else 1000, "output": 0, "cache_read": 0}, ms=ms)
        proposed = log.append({
            "kind": "proposed", "run_id": "run-a", "item_id": item,
            "inputs": {"channel": f"chan-{item}"}, "output": {"tag": "finance"},
            "tier": "T0" if version else "T1", "keg": keg(version) if version else None,
            "turn_uid": f"u-{item}", "ts": f"2026-01-01T{clock}:00+00:00"})
        log.append({"kind": "decided", "run_id": "run-a", "ref": proposed["id"], "item_id": item,
                    "decision": "correct" if item == "m4" else "confirm",
                    "output": {"tag": "ops" if item == "m4" else "finance"},
                    "turn_uid": f"c-{item}", "ts": f"2026-01-01T{clock}:30+00:00"})

    store = PatternCacheStore()

    def entry(version, status, rules, feedback=()):
        spec = {"name": "Message tagging", "version": version, "dock_goal": GOAL,
                "scope": "reserved", "reserve": "Any channel with two tags.",
                "inputs": {"channel": {"data_type": "string"}},
                "outputs": {"tag": {"data_type": "string"}}, "conditions": rules}
        store.upsert(CompiledPattern(
            pattern_id=f"keg:mt:v{version}", t0_key=f"keg:mt:v{version}",
            intent_class="conversation", cacheable_type="executable", cached_response=None,
            compiled_invocation=json.dumps({"tool": "tag_message", "args": {"keg": spec}}),
            evidence_hash="e", status=status, created_at="2026-01-01T10:00:00+00:00",
            promotion_evidence=json.dumps({
                "keg": {"dock_goal": GOAL, "lineage": "run-a", "version": version},
                "repetition_count": 2 + version, "feedback": list(feedback),
                "signed": {"by": "operator", "at": "2026-01-01T10:05:00+00:00"}})))

    always = {"if": "channel CONTAINS 'chan'", "then": {"tag": "finance"}}
    events = [
        {"event_type": "kaizen_proposal", "proposal_id": "p1", "pattern_id": "keg:mt:v1",
         "timestamp": "2026-01-01T10:03:00+00:00"},
        {"event_type": "new_standard_work", "proposal_id": "p1", "pattern_id": "keg:mt:v1",
         "dock_goal": GOAL, "version": 1, "signed_by": "operator",
         "timestamp": "2026-01-01T10:05:00+00:00"},
        {"event_type": "andon_event", "goal": GOAL, "detector": "correction",
         "andon_id": "a2", "flag": "anomaly",
         "halted": ["keg:mt:v1"], "timestamp": "2026-01-01T10:11:30+00:00",
         "details": {"item_id": "m4", "served": {"tag": "finance"}, "corrected": {"tag": "ops"}}},
    ]
    if sign_v2:
        entry(1, pc.STATUS_SUPERSEDED, [always])
        entry(2, pc.STATUS_ACTIVE,
              [{"if": "channel == 'chan-m4'", "defer": True}, always], feedback=["narrower"])
        events += [
            {"event_type": "kaizen_proposal", "proposal_id": "p2", "pattern_id": "keg:mt:v2",
             "andon_id": "a2", "version": 2, "replayed": 4, "would_change": 1,
             "timestamp": "2026-01-01T10:14:00+00:00"},
            {"event_type": "new_standard_work", "proposal_id": "p2", "pattern_id": "keg:mt:v2",
             "dock_goal": GOAL, "version": 2, "signed_by": "operator",
             "timestamp": "2026-01-01T10:15:30+00:00"}]
    else:
        entry(1, pc.STATUS_HALTED, [always])
    ledger = tmp_path / ".kaizen_ledger"
    ledger.mkdir()
    (ledger / "s.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    return tmp_path


def test_loop_events_stand_before_the_first_item_each_one_affects(tmp_path, monkeypatch):
    [g] = audit.economics(_loop_home(tmp_path, monkeypatch))["goals"]
    assert [(e["kind"], e["before"]) for e in g["events"]] == [
        ("signed", 3), ("halted", 5), ("signed", 6)]
    assert [(s["version"], s["seconds"]) for s in g["signatures"]] == [(1, 120.0), (2, 90.0)]
    m4 = g["units"][3]
    assert (m4["label"], m4["keg_version"], m4["change"], m4["halted_keg"]) == (
        "chan-m4", 1, "finance → ops", True)
    assert g["brake"]["misses"] == [4] and g["brake"]["resumed_version"] == 2
    assert [(v["version"], v["state"], v["decides"], len(v["hands_back"]))
            for v in g["versions"]] == [(1, "replaced", 1, 0), (2, "serving", 1, 1)]
    assert g["versions"][1]["feedback"] == ["narrower"]
    assert (g["title"], g["item_name"], g["traceable"]) == (
        "Message tagging", ("message", "messages"), 6)
    assert (g["coverage"]["version"], g["coverage"]["covered"]) == (2, 5)   # m4 handed back


def test_a_log_whose_goal_left_the_dock_still_reports_under_plain_names(home):
    [g] = audit.economics(home)["goals"]
    assert g["item_name"] == ("item", "items") and g["title"] == GOAL
    assert g["events"] == [] and g["versions"] == [] and g["units"][0]["label"] == ""


# ── the page ──────────────────────────────────────────────────────────


def test_audit_page_shows_the_same_figures_and_its_limits(home):
    from grove.api import fragments

    chain = audit.chain_report(home)
    html = fragments._audit_integrity_html(chain)
    # The panel and the terminal say the same thing, from the same strings.
    terminal = "\n".join(audit.format_chain_report(chain))
    for text in ("CHAIN INTACT", audit.HEADLINE[0], audit.HEADLINE[1],
                 *(t["text"] for t in audit.check_tiles(chain)),
                 *(sentence for _, sentence in audit.WOULD_CATCH)):
        assert text in terminal and fragments._esc(text) in html, text
    assert "<strong>4 of 4</strong> decisions have their turn on record" in html
    assert html.count('class="sc-link"') == 4 and html.count("sc-node sc-keg") == 2
    assert html.count("sc-node sc-keg sc-ring") == 1 and "sc-missing" not in html
    assert "AUDIT CHECK · MESSAGE-TRIAGE · RUN 1" in html and "sc-pill-ok" in html
    assert "never asks the running system" in html         # the independence caveat
    assert "Run the check again" in html and "Checking…" in html
    assert 'hx-get="/portal/fragments/audit/check"' in html
    eco = fragments._audit_economics_html(audit.economics(home), 1_000_000, chain=chain)
    for text in ("2 of 4 items decided with no model.", "Every one traceable.",
                 "100×", "0.10 s with the keg, 10.0 s with a model",
                 "If your agents make 1,000,000 model calls a month today", "Every item decided by a model",
                 "Avoided", "ESTIMATE · FRONTIER MODEL",
                 "Audit chain intact: 8 intent records, 1 chained ledger events.",
                 "NOT priced", "Not included:", "confirmation turn is separate"):
        assert text in eco, text
    assert eco.count("/portal/fragments/audit/economics?scale=") == len(audit.SCALES) == 3
    assert eco.count('aria-pressed="true"') == 1 and "1M / mo" in eco
    assert "Audit</a>" in (REPO / "gateway" / "assets" / "portal" / "index.html").read_text()


def test_scorecard_reads_the_loop_off_the_records(tmp_path, monkeypatch):
    from grove.api import fragments

    [g] = audit.economics(_loop_home(tmp_path, monkeypatch))["goals"]
    html = fragments._scorecard_html(g, 10_000, "0")
    for text in ("SCORECARD · MESSAGE TAGGING · RUN 3 · 6 MESSAGES",
                 "3 of 6 messages decided with no model.",
                 "Under keg v2, 5 of these 6 would have run with no model at all.",
                 "v1 → v2", "v1 signed 2.0 min after it was proposed. v2 signed 1.5 min after.",
                 "1 miss", "A revision on #4 halted the keg until v2 was signed.",
                 "Seconds to decide each message, in order",
                 "v1 signed</span>", "Revised · keg halted</span>", "v2 signed</span>",
                 "KEG v1</span>", "KEG v2</span>", "↑ 61.0s",
                 "#4 · chan-m4 · keg v1 · no model call · 0.10 s · operator revised "
                 "finance → ops · andon raised · keg halted",
                 "hands back when channel == &#x27;chan-m4&#x27;", "SERVING", "REPLACED",
                 "If your agents make 10,000 model calls a month today</h2>",
                 "Scaled from this run. Keg v2 answers", "of those calls are never made.",
                 "Per-call basis, measured this run:",
                 "SAVINGS · MEASURED", " down to ", "Show the working",
                 "No model</div>", "Fewer model calls</div>", "Less time deciding</div>",
                 "Measured figures use routing-config prices, scaled from this run."):
        assert text in html, text
    assert html.count('<button type="button" class="sc-bar"') == 6      # each bar focusable
    assert html.count("sc-fill sc-keg") == 3 and html.count("sc-ring") == 1
    assert html.count("sc-cut") == 1 and html.count("sc-event-line") == 3
    # A capped bar is drawn at the cap; a keg bar at its fixed visible height.
    assert f'sc-cut" style="height:{fragments._SC_PLOT_PX}px"' in html
    assert f'style="height:{fragments._SC_KEG_BAR_PX}px"' in html
    # The shell carries the one listener that fills the status line.
    assert "data-sc-detail" in (REPO / "gateway" / "assets" / "portal" / "index.html").read_text()


def test_scorecard_says_so_when_the_keg_is_still_halted_or_none_is_signed(
        tmp_path, monkeypatch, home):
    from grove.api import fragments

    bare = fragments._audit_economics_html(audit.economics(home), 10_000)
    assert "No keg version has been signed in this run." in bare
    assert "No keg is serving, so nothing is avoided yet." in bare
    halted_home = tmp_path / "halted"
    halted_home.mkdir()
    [g] = audit.economics(_loop_home(halted_home, monkeypatch, sign_v2=False))["goals"]
    html = fragments._scorecard_html(g, 10_000, "0")
    assert "halted the keg; it is still halted." in html and "HALTED" in html


def test_a_broken_chain_turns_the_panel_red_and_names_the_break(home):
    from grove.api import fragments

    lines = (home / "intent_records.jsonl").read_text().splitlines()
    (home / "intent_records.jsonl").write_text("\n".join(lines[:4] + lines[5:]) + "\n")
    report = audit.chain_report(home)
    html = fragments._audit_integrity_html(report)
    first = report["problems"][0]
    assert "sc-pill-bad" in html and "CHAIN BROKEN" in html and "sc-pill-ok" not in html
    assert fragments._esc(f"The chain breaks at {first['subject']} · {first['where']}.") in html
    assert "Where it breaks" in html and html.count("sc-missing") == 1   # m3's turn is gone
    assert "<strong>3 of 4</strong> decisions have their turn on record" in html


def test_audit_page_reads_and_never_writes(home):
    from grove.api import fragments
    before = {p: p.stat().st_mtime_ns for p in home.rglob("*") if p.is_file()}
    fragments._audit_integrity_html(audit.chain_report(home))
    fragments._audit_economics_html(audit.economics(home), 10_000)
    after = {p: p.stat().st_mtime_ns for p in home.rglob("*") if p.is_file()}
    assert set(after) - set(before) <= {home / "pattern_cache.db"}   # opening creates an empty cache
    assert all(after[p] == before[p] for p in before)


def test_a_batch_shares_its_turn_and_splits_the_scorecard_three_ways(tmp_path, monkeypatch):
    from grove.api import fragments

    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setattr(pc, "default_pattern_cache_path", lambda: tmp_path / "pattern_cache.db")
    monkeypatch.setattr(audit, "_presentation", lambda goal: {
        "title": "Message tagging", "item_name": ("message", "messages"),
        "label_key": "channel", "before_label": "Month 1", "batch_label": "Month 2"})
    log = DecisionLog(GOAL, directory=tmp_path / "decisions")
    log.append({"kind": "run_started", "run_id": "r", "run_number": 1, "label": ""})

    def item(name, uid, decision, batch=None, keg=False, tier="T1"):
        p = log.append({"kind": "proposed", "run_id": "r", "item_id": name,
                        "inputs": {"channel": name}, "output": {"tag": "finance"}, "tier": tier,
                        "keg": {"name": "k", "version": 2, "pattern_id": "keg:k:v2"} if keg else None,
                        "turn_uid": uid, **({"batch": batch} if batch else {})})
        log.append({"kind": "decided", "run_id": "r", "ref": p["id"], "item_id": name,
                    "decision": decision, "output": {"tag": "finance"}, "turn_uid": "c-" + name})
        return p

    _intent(tmp_path, "u-a", tier="T1", model="small", calls=2,
            tokens={"input": 1000, "output": 100, "cache_read": 0}, ms=10000.0)
    item("a", "u-a", "confirm")
    # One batch turn decides four items in 0.4 s; one exception goes to a model.
    _intent(tmp_path, "u-batch", tier="T0", model="session_rule", calls=0,
            tokens={"input": 0, "output": 0, "cache_read": 0}, ms=400.0)
    for name in ("b", "c", "d", "e"):
        item(name, "u-batch", "accepted", batch="B", keg=True, tier="T0")
    _intent(tmp_path, "u-f", tier="T1", model="small", calls=2,
            tokens={"input": 1000, "output": 100, "cache_read": 0}, ms=20000.0)
    f = item("f", "u-f", "confirm", batch="B")
    # The operator later rules on one accepted item: it becomes confirmed.
    b = [r for r in log.run_records() if r["kind"] == "proposed" and r["item_id"] == "b"][0]
    log.append({"kind": "decided", "run_id": "r", "ref": b["id"], "item_id": "b",
                "decision": "confirm", "output": {"tag": "finance"}, "after": "accepted"})

    [g] = audit.economics(tmp_path)["goals"]
    shared = g["units"][2]["deciding"]
    assert (shared["seconds"], shared["shared_with"], shared["model_calls"]) == (0.1, 4, 0.0)
    t0 = g["by_tier"]["T0"]
    assert (t0["units"], t0["confirmed"], t0["accepted"], t0["corrected"]) == (4, 1, 3, 0)
    assert t0["seconds"] == pytest.approx(0.1)               # per item, not the whole turn
    before, batch = g["periods"]
    assert (before["label"], before["units"], before["keg_share"]) == ("Month 1", 1, 0.0)
    assert (batch["label"], batch["units"], batch["keg_units"]) == ("Month 2", 5, 4)
    assert batch["model_calls_per_unit"] == pytest.approx(2 / 5)
    assert (batch["confirmed"], batch["accepted"], batch["revised"]) == (2, 3, 0)
    # Counted per period, never blended: how many needed a model, and the calls.
    assert (before["model_units"], before["model_calls"]) == (1, 2)
    assert (batch["model_units"], batch["model_calls"], batch["calls_per_model_unit"]) == (1, 2, 2.0)
    assert g["headline"]["lead"] == (
        "Month 2: the signed rule handled 4 of 5 messages. No model call.")
    assert g["headline"]["first"] == "Month 1 needed a model on 1 of 1 (100%)."
    assert g["headline"]["second"] == "Month 2 needed one on 1 of 5 (20%)."
    # One computation: the table's rows are the period's own breakdown, and
    # every count adds up to the period's total.
    model, keg = batch["who"]
    assert (model["units"], model["confirmed"], keg["units"], keg["confirmed"],
            keg["accepted"]) == (1, 1, 4, 1, 3)
    for period in (before, batch):
        assert (period["confirmed"] + period["accepted"] + period["revised"]
                + period["awaiting"]) == period["units"]
        assert sum(w["units"] for w in period["who"]) == period["units"]

    html = fragments._scorecard_html(g, 10_000, "0")
    for text in ("CONFIRMED BY YOU", "NOT REVIEWED", "REVISED", "Month 1 against Month 2",
                 "80%", "decided by the keg (4 of 5)",
                 "2 confirmed by you · 3 decided by the keg, not reviewed · 0 revised.",
                 "3 messages were decided by the keg under its signed authority and not "
                 "reviewed; they are not counted as confirmed.",
                 "decided by the keg, not reviewed",
                 "Month 2 · keg, no model", "Month 2 · a model", "AWAITING YOU",
                 "1 of 5</span>", "needed a model (20%)",
                 "Months are counted separately. Keg decisions without review are not "
                 "treated as confirmed."):
        assert text in html, text
    # A run with no batch has one period, and says nothing about periods.
    assert "against" not in fragments._scorecard_html(
        {**g, "periods": []}, 10_000, "0").split("Who decided")[0]


# ── the decision trace ────────────────────────────────────────────────


def test_the_trace_reads_each_decision_step_by_step_from_records(tmp_path, monkeypatch):
    from grove.api import fragments

    home = _loop_home(tmp_path, monkeypatch)
    [g] = audit.trace(home)["goals"]
    assert (g["title"], g["run_number"], len(g["items"])) == ("Message tagging", 3, 6)
    m4 = g["items"][3]
    assert (m4["label"], m4["decided_by"], m4["verdict"], m4["revised"]) == (
        "chan-m4", "keg v1", "revised", True)
    assert m4["inputs"] == {"channel": "chan-m4"} and m4["turn"]["on_record"] is True
    assert [r["word"] for r in m4["rulings"]] == ["revised"]
    # The loop's steps hang on the item that caused them, in order.
    assert [(e["kind"], e.get("version")) for e in m4["loop"]] == [
        ("flagged", None), ("proposed", 2), ("signed", 2)]
    assert m4["loop"][0]["halted"] == ["keg:mt:v1"]
    # One decision as one training example: nothing but what is on record.
    assert m4["example"] == {
        "goal": GOAL, "run": 3, "item_id": "m4", "inputs": {"channel": "chan-m4"},
        "proposed": {"tag": "finance"}, "reasoning": "", "decided_by": "keg",
        "keg_version": 1, "tier": "T0", "model": "pattern_cache", "question_asked": None,
        "operator_said": [],
        "final": {"tag": "ops"}, "verdict": "revised", "reviewed_by_operator": True,
        "turn_uid": "u-m4", "record_hash": m4["turn"]["record_hash"]}
    lines = audit.trace_export(home).splitlines()
    assert len(lines) == 6 and json.loads(lines[3]) == m4["example"]

    html = fragments._trace_html(audit.trace(home))
    for text in ("DECISION TRACE · MESSAGE TAGGING · RUN 3 · 6 MESSAGES",
                 "6 decisions on record.", "Download as JSONL (6 lines)",
                 f"/portal/fragments/trace/export?goal={GOAL}",
                 "revised · was finance", "YOU REVISED", "JIDOKA FLAGGED",
                 "A miss: keg v1 answered finance and you revised it. The keg was halted.",
                 "KAIZEN PROPOSED", "AS ONE TRAINING EXAMPLE", "no model call"):
        assert text in html, text
    assert html.count('<details class="sc-trace') == 6
    assert html.count('sc-trace-revised" open') == 1          # the revised item is open
    assert "Trace</a>" in (REPO / "gateway" / "assets" / "portal" / "index.html").read_text()


def test_an_unreviewed_keg_decision_is_never_exported_as_the_operators_judgment(
        tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setattr(pc, "default_pattern_cache_path", lambda: tmp_path / "pattern_cache.db")
    log = DecisionLog(GOAL, directory=tmp_path / "decisions")
    log.append({"kind": "run_started", "run_id": "r", "run_number": 1, "label": ""})
    _intent(tmp_path, "u-b", tier="T0", model="session_rule", calls=0,
            tokens={"input": 0, "output": 0, "cache_read": 0}, ms=200.0)
    waiting = None
    for name, decision in (("a", "accepted"), ("b", "accepted"), ("c", None)):
        p = log.append({"kind": "proposed", "run_id": "r", "item_id": name,
                        "inputs": {"channel": name}, "output": {"tag": "finance"}, "tier": "T0",
                        "keg": {"name": "k", "version": 2, "pattern_id": "keg:k:v2"},
                        "turn_uid": "u-b", "batch": "B"})
        if decision:
            log.append({"kind": "decided", "run_id": "r", "ref": p["id"], "item_id": name,
                        "decision": decision, "output": {"tag": "finance"}, "by": "keg_authority",
                        "turn_uid": "u-b"})
    [g] = audit.trace(tmp_path)["goals"]
    a = g["items"][0]
    assert (a["verdict"], a["example"]["reviewed_by_operator"]) == ("not reviewed", False)
    assert a["turn"]["shared_with"] == 3 and a["turn"]["seconds"] == pytest.approx(0.2 / 3)
    # An item still waiting has no answer, so it is not a training example.
    assert g["items"][2]["verdict"] == "awaiting the operator"
    assert len(audit.trace_export(tmp_path).splitlines()) == 2


def _period(label, units, model_units, **over):
    return {"label": label, "units": units, "model_units": model_units, **over}


def test_the_headline_says_only_what_the_counts_show():
    names = ("invoice", "invoices")
    month1 = _period("Month 1 · learning", 20, 13, keg_units=7, cost_per_unit=0.004)
    month2 = _period("Month 2 · production", 40, 9, keg_units=31, cost_per_unit=0.0006, of=40)
    all_explained = {"key": "vendor", "new": 5, "judgment": 4, "other": 0}
    h = audit._headline([month1, month2], all_explained, names)
    assert h["lead"] == "Month 2: the signed rule handled 31 of 40 invoices. No model call."
    assert h["first"] == "Month 1 needed a model on 13 of 20 (65%)."
    assert h["second"] == ("Month 2 needed one on 9 of 40 (23%), all new vendors or cases "
                           "the rule handed back.")
    assert (h["cost_before"], h["cost_batch"], h["complete"]) == (0.004, 0.0006, True)
    # One that is neither a new vendor nor handed back: "all" is not claimed.
    h = audit._headline([month1, month2],
                        {"key": "vendor", "new": 5, "judgment": 3, "other": 1}, names)
    assert h["second"].endswith(": 5 new vendors, 3 the rule handed back, 1 neither.")
    assert h["all_explained"] is False
    # Read while the batch is under way: the counts are "so far", and say so.
    partial = _period("Month 2 · production", 38, 7, keg_units=31, cost_per_unit=0.0006, of=40)
    h = audit._headline([month1, partial], {"key": "vendor", "new": 4, "judgment": 3,
                                            "other": 0}, names)
    assert h["lead"] == ("Month 2: the signed rule handled 31 of 38 invoices so far; "
                         "2 of 40 still to come. No model call.")
    assert h["second"].startswith("Month 2 needed one on 7 of 38 (18%)") and not h["complete"]
    # "About the same" is a claim made only inside the band.
    for needed, same in ((13, True), (10, True), (9, False), (17, False)):
        assert audit._headline([month1, _period("M2", 40, needed, keg_units=1,
                                                cost_per_unit=0.0)],
                               all_explained, names)["same"] is same
    assert audit._headline([month1, _period("M2", 40, 40, keg_units=0, cost_per_unit=0.0)],
                           {}, names)["lead"] == "M2: all 40 invoices needed a model."
    assert audit._headline([month1], all_explained, names) is None
    # One rounding, used everywhere, halves up: 31 of 38 is 82%, 9 of 40 is 23%.
    assert (audit._pct(31, 38), audit._pct(9, 40), audit._pct(13, 20)) == ("82%", "23%", "65%")


def test_the_baseline_uses_the_cost_of_a_model_decided_item_not_the_average():
    def unit(keg, cost, batch=None):
        return {"keg": keg, "batch": batch, "confirmed": True, "accepted": False,
                "corrected": False,
                "deciding": {"model_calls": 0 if keg else 2, "cost": cost, "seconds": 1.0,
                             "priced": True}}

    # Month 1: two by a model at $0.10, two by the keg at $0. Month 2: one model, three keg.
    units = ([unit(False, 0.10), unit(False, 0.10), unit(True, 0.0), unit(True, 0.0)]
             + [unit(False, 0.12, "B")] + [unit(True, 0.0, "B")] * 3)
    before, batch = audit._periods(units, {"before_label": "M1", "batch_label": "M2"})
    assert before["baseline_rate"] == pytest.approx(0.10)       # not the $0.05 average
    assert before["baseline"] == pytest.approx(0.40) and before["savings"] == pytest.approx(0.20)
    assert (batch["cost"], batch["baseline"]) == (pytest.approx(0.12), pytest.approx(0.40))
    assert batch["savings"] == pytest.approx(0.28) and batch["cost_per_unit"] == pytest.approx(0.03)


def test_learning_cost_is_read_from_the_ledger_and_never_estimated(tmp_path):
    ledger = tmp_path / ".kaizen_ledger"
    ledger.mkdir()

    def write(name, *events):
        (ledger / name).write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

    run = {"ts": "2026-10-06T10:00:00+00:00"}
    at = "2026-10-06T10:05:00+00:00"
    write("a.jsonl",
          {"event_type": "andon_event", "andon_id": "A1", "goal": GOAL, "timestamp": at,
           "detector": "reference_agreement"},
          {"event_type": "kaizen_answer", "andon_id": "A1", "kind": "standard_work"},
          {"event_type": "andon_event", "andon_id": "A2", "goal": GOAL, "timestamp": at,
           "detector": "correction"},
          {"event_type": "kaizen_answer", "andon_id": "A2", "kind": "standard_work",
           "drafting": [{"tier": "T1", "refused": True,
                         "tokens": {"calls": 1, "input": 2000, "output": 500, "model": "small"}},
                        {"tier": "T2", "refused": False,
                         "tokens": {"calls": 1, "input": 2000, "output": 1000, "model": "big"}}]},
          {"event_type": "andon_event", "andon_id": "A3", "goal": GOAL, "timestamp": at,
           "detector": "correction"},
          {"event_type": "kaizen_answer", "andon_id": "A3", "kind": "standard_work"},
          {"event_type": "andon_event", "andon_id": "A0", "goal": GOAL,
           "timestamp": "2026-10-06T09:00:00+00:00", "detector": "correction"},
          {"event_type": "kaizen_answer", "andon_id": "A0", "kind": "standard_work"})
    prices = {"facts": {"small": {"cost_per_mtok_input": 1, "cost_per_mtok_output": 2},
                        "big": {"cost_per_mtok_input": 10, "cost_per_mtok_output": 20}}}
    out = audit._learning(tmp_path, GOAL, run, prices)
    assert (out["backtests"], out["from_table"], out["drafts"], out["unrecorded"]) == (3, 1, 2, 1)
    assert (out["calls"], out["input"], out["output"]) == (2, 4000, 1500)
    assert out["cost"] == pytest.approx(0.003 + 0.040) and out["priced"] is True


def test_a_decision_whose_turn_is_still_running_is_not_a_break(tmp_path):
    """Live, 2026-10-06: the audit page was opened while a turn was coding an
    item. The decision was on the log; the turn's record is written when the
    turn ends. The panel said the chain was broken, and seconds later it was
    not. A missing turn is a break only once the turn has had time to end."""
    from datetime import datetime, timedelta, timezone

    log = DecisionLog(GOAL, directory=tmp_path / "decisions")
    log.append({"kind": "run_started", "run_id": "r", "run_number": 1, "label": ""})
    now = datetime.now(timezone.utc)
    log.append({"kind": "proposed", "run_id": "r", "item_id": "done", "turn_uid": "u1",
                "ts": (now - timedelta(minutes=30)).isoformat()})
    log.append({"kind": "proposed", "run_id": "r", "item_id": "running", "turn_uid": "u2",
                "ts": (now - timedelta(seconds=5)).isoformat()})
    out = audit._run_check(tmp_path, {"u1"})
    assert out["problems"] == []
    assert (out["runs"][0]["with_turn"], out["runs"][0]["in_flight"]) == (1, 1)
    assert [(l["on_record"], l["in_flight"]) for l in out["links"]] == [(True, False), (False, True)]
    tile = audit.check_tiles({
        "chained_sessions": 1, "unchained": 0, "chained": 1, "records": 1, "live": True,
        "anchored": 1, "ledger": {"chained": 0, "files": 0, "unchained": 0},
        "runs": out["runs"]})[-1]
    assert tile["text"].endswith(
        "1 more is from a turn still running, and will be checked when it ends.")
    # A later decision's turn is on record: the one before it is a hole, however recent.
    log.append({"kind": "proposed", "run_id": "r", "item_id": "after", "turn_uid": "u1",
                "ts": now.isoformat()})
    out = audit._run_check(tmp_path, {"u1"})
    assert [p["problem"] for p in out["problems"]] == [
        "decision running has no turn in the audit trail"]
    assert "in_flight" not in out["runs"][0]
    # Still without its turn after the window: a break.
    old = DecisionLog("other-goal", directory=tmp_path / "decisions")
    old.append({"kind": "run_started", "run_id": "q", "run_number": 1, "label": ""})
    old.append({"kind": "proposed", "run_id": "q", "item_id": "lost", "turn_uid": "u9",
                "ts": (now - timedelta(seconds=audit.IN_FLIGHT_SECONDS + 1)).isoformat()})
    out = audit._run_check(tmp_path, {"u1"})
    assert "decision lost has no turn in the audit trail" in [
        p["problem"] for p in out["problems"]]


def test_the_cost_rate_row_is_computed_and_the_arrow_sits_on_the_change():
    from grove.api import fragments

    def g(then, now, baseline=0.0051):
        return {"item_name": ("invoice", "invoices"), "periods": [
            {"label": "Month 1 · learning", "cost_per_unit": then, "baseline_rate": baseline},
            {"label": "Month 2 · production", "cost_per_unit": now, "baseline_rate": baseline}]}

    html = fragments._rate_row_html(g(0.00369, 0.00061))
    for text in ("MODEL COST · PER 1,000 INVOICES", "$0.61<span class=\"sc-unit\"> / 1,000</span>",
                 "Versus month 1", 'sc-delta-figure sc-down">↓ 83%</div>',
                 "$3.08 less per 1,000", "Month 1 · learning", "$3.69",
                 "Measured model cost this run, not the all-model counterfactual. Keg "
                 "decisions are $0 and are already in the month 2 rate.",
                 "AGAINST ALL-MODEL · PER 1,000 INVOICES",
                 "$5.10 if a model decided every one · $0.61 measured with the keg"):
        assert text in html, text
    assert html.count("↓") == 1                       # one arrow, on the change only
    # A rise flips the arrow and the color; a small move has no arrow at all.
    up = fragments._rate_row_html(g(0.0010, 0.0015))
    assert 'sc-delta-figure sc-up">↑ 50%</div>' in up and "$0.50 more per 1,000" in up
    assert "sc-down" not in up
    flat = fragments._rate_row_html(g(0.00100, 0.00097))
    assert "about the same" in flat and "↓" not in flat and "↑" not in flat
    # Nothing priced, or one period: no row.
    assert fragments._rate_row_html(g(None, 0.001)) == ""
    assert fragments._rate_row_html({"item_name": ("a", "b"), "periods": []}) == ""
    assert fragments._rate(0.00061) == "$0.61" and fragments._rate(0) == "$0"


def test_signed_changes_are_counted_as_tickets_avoided_and_the_estimate_is_declared():
    from types import SimpleNamespace
    from grove.api import fragments

    loop = {
        "signatures": [
            {"version": 1, "seconds": 14.0, "signed_at": "2026-10-06T10:01:00+00:00",
             "flag": "tier_down_pattern", "signed_by": "operator"},
            {"version": 2, "seconds": 10.0, "signed_at": "2026-10-06T10:05:20+00:00",
             "flag": "anomaly", "signed_by": "operator"}],
        "brake": {"halts": [{"item": 12, "at": "2026-10-06T10:05:00+00:00", "corrected": True}]},
    }
    model = SimpleNamespace(hours_per_ticket=12.0, loaded_rate=88.26, price_per_month=1000.0,
                            source="Sokori model: 12 hours per exception; BLS median + 35% load")
    t = audit._tickets(loop, model)
    assert [(c["version"], c["kind"], c["review_seconds"], c["fix_seconds"])
            for c in t["changes"]] == [(1, "enhancement", 14.0, None), (2, "exception", 10.0, 20.0)]
    assert (t["count"], t["exceptions"], t["review_seconds"], t["hours"]) == (2, 1, 24.0, 24.0)
    assert t["dollars"] == pytest.approx(2118.24)
    assert t["break_even"] == pytest.approx(1000 / (12 * 88.26))      # 0.94 tickets a month
    html = fragments._tickets_html({"tickets": t, "versions": [
        {"version": 1, "decides": 6}, {"version": 2, "decides": 6}, {"version": 3, "decides": 8}]})
    assert ("one per signature, not one per rule. v3 is one signature though it adds 2 "
            "rules.") in html
    for text in ("The fixes that usually become engineering tickets",
                 "CHANGES SIGNED · MEASURED", "keg v1, keg v2. v1: an enhancement; v2: an "
                 "exception.", "ENGINEERING TICKETS FILED · MEASURED", "No ticket system is "
                 "connected", "EXPERT REVIEW TIME · MEASURED", "24 s",
                 "v1: signed 14 s after it was proposed.",
                 "v2: signed 10 s after it was proposed; 20 s from the correction to the "
                 "signed fix.", '<span class="sc-event">ESTIMATE</span> · ENGINEERING TIME AVOIDED',
                 "2 × 12 hours a ticket. At $88.26 an hour, $2,118.",
                 "A dock breaks even by avoiding about one exception a month. This run avoided 2: "
                 "one enhancement, one exception.",
                 "Hours and rate from the Sokori model; a dock is $1,000 a month. Inference "
                 "savings are counted separately below."):
        assert text in html, text
    # No declared model: the measured tiles stand, and no estimate is made.
    bare = audit._tickets(loop, None)
    assert (bare["count"], bare["hours"], bare["dollars"], bare["model"]) == (2, None, None, None)
    # The break-even is computed from the declared price, never stated on its own.
    dear = SimpleNamespace(hours_per_ticket=12.0, loaded_rate=88.26, price_per_month=5000.0,
                           source="Sokori model: x")
    assert ("about five exceptions a month. This run avoided 2: one enhancement, one "
            "exception.") in fragments._tickets_html({"tickets": audit._tickets(loop, dear)})
    free = SimpleNamespace(hours_per_ticket=12.0, loaded_rate=88.26, price_per_month=None,
                           source="Sokori model: x")
    quiet = fragments._tickets_html({"tickets": audit._tickets(loop, free)})
    assert "breaks even" not in quiet and "This run avoided 2: one enhancement, one exception." in quiet
    plain = fragments._tickets_html({"tickets": bare})
    assert "No ticket model is declared" in plain and "breaks even" not in plain
    # Nothing signed: no panel.
    assert fragments._tickets_html({"tickets": audit._tickets({"signatures": []}, model)}) == ""


def test_the_ticket_model_is_declared_in_the_dock_and_a_bad_one_is_refused(tmp_path):
    from types import SimpleNamespace
    from grove import decision_work as dw

    (tmp_path / "q").mkdir()

    def goal(model):
        return SimpleNamespace(id="g", root=tmp_path, keywords=(), resolved_sources=lambda: [],
                               extra={"decision_work": {
                                   "tool": "t", "queue": "q", "isolation": "sources_only",
                                   "inputs": {"a": {"data_type": "string", "required": True}},
                                   "outputs": {"b": {"data_type": "string"}},
                                   **({"ticket_model": model} if model is not None else {})}})

    assert dw.load_config(goal(None)).ticket_model is None
    good = dw.load_config(goal({"hours_per_ticket": 12, "loaded_rate": 88.26,
                                "source": "a model"})).ticket_model
    assert (good.hours_per_ticket, good.loaded_rate, good.price_per_month) == (12.0, 88.26, None)
    assert dw.load_config(goal({"hours_per_ticket": 12, "loaded_rate": 88.26, "source": "s",
                                "dock_price": 1000})).ticket_model.price_per_month == 1000.0
    for bad, why in (({"hours_per_ticket": 12, "loaded_rate": 88.26}, "source must say"),
                     ({"hours_per_ticket": 0, "loaded_rate": 1, "source": "s"}, "above zero"),
                     ({"hours_per_ticket": 12, "loaded_rate": "88", "source": "s"}, "above zero"),
                     ("12 hours", "must be a mapping")):
        with pytest.raises(ValueError, match=why):
            dw.load_config(goal(bad))


def test_what_one_dock_returns_is_computed_and_claims_only_what_the_numbers_show():
    from types import SimpleNamespace
    from grove.api import fragments

    loop = {"signatures": [
        {"version": 1, "seconds": 14.0, "signed_at": "2026-10-06T10:01:00+00:00",
         "flag": "tier_down_pattern"},
        {"version": 2, "seconds": 10.0, "signed_at": "2026-10-06T10:05:20+00:00",
         "flag": "anomaly"}], "brake": {"halts": []}}

    def card(price, saved, months=2, frontier=(70277.0, 15227.0), scale=1_000_000):
        model = SimpleNamespace(hours_per_ticket=12.0, loaded_rate=88.26, price_per_month=price,
                                source="Sokori model: x")
        g = {"item_name": ("invoice", "invoices"),
             "tickets": audit._tickets(loop, model, months=months)}
        proj = {"avoided": {"cost": saved},
                "all_frontier": {"cost": frontier[0] if frontier else None},
                "frontier_with_keg": frontier[1] if frontier else None}
        return fragments._returns_html(g, proj, scale, "TOGGLE")

    html = card(1000.0, 3618.0)
    # Two changes in two months is one a month: 1 × 12 × $88.26 = $1,059.
    for text in ("What one dock returns in a month", "<strong>4.7×</strong> the dock's price at ", "at 1,000,000 model calls a month.",
                 "It pays for itself on avoided tickets alone; volume is upside.",
                 "$1,059</div>", "1 signed change a month × 12 hours × $88.26.",
                 "Measured: 2 changes signed in 2 months.", "MODEL COST SAVED · MEASURED",
                 "$3,618</div>", "$4,677<span", "against a dock price of $1,000 a month.",
                 "At this run's pace of 1 signed change a month.", "TOGGLE",
                 "If you run frontier models today: model cost saved would be $55,050, not "
                 "$3,618. That is $51,432 more,"):
        assert text in html, text
    # Frontier pricing is never in the total.
    assert "$4,677" in card(1000.0, 3618.0, frontier=None)
    assert "frontier" not in card(1000.0, 3618.0, frontier=None)
    # Tickets alone do not cover the price: the claim is not made, the numbers are.
    dear = card(3000.0, 3618.0)
    assert "pays for itself" not in dear
    assert ("Avoided tickets cover $1,059 of the $3,000 price; model cost saved covers "
            "the rest.") in dear and "<strong>1.6×</strong>" in dear
    short = card(3000.0, 36.0, scale=10_000)
    assert "come to $1,095 against a $3,000 price." in short and "<strong>0.4×</strong>" in short
    # No price declared, or nothing signed: no card.
    assert card(None, 3618.0) == ""


def test_volume_is_stated_in_model_calls_and_scaled_from_the_measured_per_call_basis():
    g = {"model_avg": {"cost": 0.006, "seconds": 15.0, "model_calls": 3.0, "tokens": 9000.0},
         "keg_avg": {"cost": 0.0, "seconds": 0.1},
         "coverage": {"share": 0.75}, "frontier": {"cost": 0.06}}
    p = audit.project_calls(g, 1_000_000)
    # A million calls is a third of a million model-decided items, at 3 calls each.
    assert p["all_model"]["model_calls"] == pytest.approx(1_000_000)
    assert p["avoided"]["model_calls"] == pytest.approx(750_000)
    assert p["with_keg"]["model_calls"] == pytest.approx(250_000)
    # Measured cost per call: $0.006 / 3 = $0.002; three quarters of the calls avoided.
    assert p["per_call"] == {"calls_per_unit": 3.0, "cost": pytest.approx(0.002),
                             "seconds": pytest.approx(5.0), "share_avoided": 0.75}
    assert p["avoided"]["cost"] == pytest.approx(1_000_000 * 0.75 * 0.002)
    assert p["all_frontier"]["cost"] == pytest.approx(1_000_000 / 3 * 0.06)
    assert audit.SCALES == (100_000, 1_000_000, 10_000_000)
    # No model call measured: nothing can be scaled, and nothing is invented.
    empty = audit.project_calls({**g, "model_avg": {"cost": None, "seconds": None,
                                                    "model_calls": None, "tokens": None}}, 1000)
    assert empty["avoided"]["cost"] is None and empty["per_call"]["cost"] is None


def test_the_scorecard_computes_over_as_many_periods_as_the_run_has():
    from grove.api import fragments

    def unit(order, keg, cost, batch=None):
        return {"order": order, "keg": keg, "batch": batch, "confirmed": not keg,
                "accepted": keg, "corrected": False, "item_id": f"i{order}",
                "deciding": {"model_calls": 0 if keg else 2, "cost": cost, "seconds": 1.0,
                             "priced": True}}

    # Month 1: 4 by a model. Month 2: 2 model, 2 keg. Month 3: 1 model, 3 keg.
    units = ([unit(n, False, 0.010) for n in range(1, 5)]
             + [unit(5, True, 0.0, "B2"), unit(6, True, 0.0, "B2"),
                unit(7, False, 0.010, "B2"), unit(8, False, 0.010, "B2")]
             + [unit(9, True, 0.0, "B3"), unit(10, True, 0.0, "B3"),
                unit(11, True, 0.0, "B3"), unit(12, False, 0.008, "B3")])
    shown = {"before_label": "Month 1 · learning",
             "stage_labels": ["Month 2 · production", "Month 3 · production"]}
    periods = audit._periods(units, shown)
    assert [(p["label"], p["units"], p["model_units"], p["keg_units"]) for p in periods] == [
        ("Month 1 · learning", 4, 4, 0), ("Month 2 · production", 4, 2, 2),
        ("Month 3 · production", 4, 1, 3)]
    assert [p.get("batch") for p in periods] == [None, "B2", "B3"]
    # The all-model rate is still the first period's; every period is priced against it.
    assert all(p["baseline_rate"] == pytest.approx(0.010) for p in periods)
    assert periods[2]["cost_per_unit"] == pytest.approx(0.002)
    # More stages than labels: the rest are numbered, never mislabeled.
    assert [p["label"] for p in audit._periods(units, {"batch_label": "Batch"})] == [
        "Before the batch", "Batch", "Batch 2"]

    h = audit._headline(periods, {"key": "vendor", "new": 1, "judgment": 0, "other": 0},
                        ("invoice", "invoices"))
    assert h["lead"] == "Month 3: the signed rule handled 3 of 4 invoices. No model call."
    assert h["first"] == ("Month 1 needed a model on 4 of 4 (100%). Month 2 needed one on "
                          "2 of 4 (50%).")
    assert h["second"] == "Month 3 needed one on 1 of 4 (25%), all new vendors."
    assert (h["cost_before"], h["cost_batch"]) == (pytest.approx(0.010), pytest.approx(0.002))
    # The rate row compares the latest period with the one before it.
    row = fragments._rate_row_html({"item_name": ("invoice", "invoices"), "periods": periods})
    for text in ("Month 3 · production", "$2.00<span", "Versus month 2", "↓ 60%",
                 "$3.00 less per 1,000", "Month 2 · production", "$5.00"):
        assert text in row, text
    # The chart marks the start of each later period.
    g = {"units": [{**u, "label": "", "tier": "T1", "by": "m", "served": {}, "final": {},
                    "keg_version": 2 if u["keg"] else None, "decision": "confirm",
                    "confirming": None} for u in units],
         "item_name": ("invoice", "invoices"), "events": [], "periods": periods}
    chart = fragments._scorecard_chart_html(g, "0")
    assert chart.count("sc-boundary") == 2
    assert "Month 2 · production</span>" in chart and "Month 3 · production</span>" in chart


# ── what led to each keg version ──────────────────────────────────────


def test_a_version_says_what_led_to_it_from_the_detector_that_fired():
    """2026-10-07: the goal page's evidence line comes from what the ledger
    recorded for the version (the detector and what it saw), never from the
    version's number. One detector's facts read the same way wherever they
    appear."""
    from grove.api import fragments

    by_item = {"m12": {"order": 12, "label": "billing"}}
    cfg = SimpleNamespace(item_name=("message", "messages"),
                          reference=SimpleNamespace(path=Path("channels.csv")))
    said = lambda andon, draft=None: fragments._version_ground(
        audit._trigger(andon, draft, by_item), cfg)

    # Confirmations that agree with the reference table.
    assert said({"detector": "reference_agreement", "details": {
        "confirmations": 5, "rule": {"threshold": 5}}}) == (
        "5 confirmations matched channels.csv (the rule asks for 5).")
    # The operator's correction of a keg decision, with the replay.
    assert said(
        {"detector": "correction", "details": {
            "item_id": "m12", "served": {"tag": "finance"}, "corrected": {"tag": "ops"}}},
        {"replayed": 12, "unchanged": 8, "would_change": 1}) == (
        "Your correction on message 12 billing: finance → ops. Replayed on the 12 "
        "decided so far: 8 unchanged, 1 changed, 3 left to the model.")
    # A key the table does not list, confirmed the same way.
    assert said({"detector": "confirmed_key", "details": {
        "confirmations": 4, "threshold": 4, "key": "press", "output": {"tag": "comms"}}}) == (
        "press is not in channels.csv. You confirmed it 4 times as comms, none revised "
        "(the rule asks for 4).")
    # An existing key under another name: what identified it, then the answer.
    assert said({"detector": "key_alias", "details": {
        "confirmations": 1, "key": "media", "same_as": "press", "output": {"tag": "comms"},
        "identity": [{"input": "sender_account", "kind": "same", "value": "A-17"},
                     {"input": "footer", "kind": "names", "text": "press is now media"}]}}) == (
        "Identified with press: same sender account (A-17); the footer on the message "
        "names press. You confirmed the same answer once.")
    # A detector this page has never heard of still says what the ledger said.
    assert said({"detector": "something_new", "summary": "A new kind of evidence.",
                 "details": {}}) == "A new kind of evidence."
    # No andon on the ledger for the version: no line (the count is shown instead).
    assert audit._trigger(None, None, by_item) is None
    assert fragments._version_ground(None, cfg) == ""
    # The wording never branches on a version.
    import inspect
    assert "version" not in inspect.getsource(fragments._version_ground).split('"""')[2]

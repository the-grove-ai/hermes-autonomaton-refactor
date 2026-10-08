"""gl_coding — the GL invoice-coding adapter. The one test file allowed to use
GL vocabulary: everything generic is tested in tests/grove/test_decision_work.py
against a different domain."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import tools.gl_coding_tool as gl
from grove import decision_work as dw
from grove import turn_provenance

INVOICE = """INVOICE

Alder Cloud Hosting
1 Example Way
Springfield, ST 00000

Bill to:
Example Co.

Invoice number: AC-0001
Invoice date:   2026-01-05
Terms:          Net 30

Description                                           Qty        Unit      Amount
---------------------------------------------------------------------------------
Compute instances - December usage                      1    1,200.00    1,200.00
Object storage - 1.0 TB                                 1      100.50      100.50
---------------------------------------------------------------------------------
TOTAL DUE (USD)                                                          1,300.50

Thank you for your business.
"""
SECOND = INVOICE.replace("Alder Cloud Hosting", "Birch Office Goods").replace("AC-0001", "BO-0002")


@pytest.fixture
def work(tmp_path, monkeypatch):
    queue = tmp_path / "invoices"
    queue.mkdir()
    (queue / "01_AC.txt").write_text(INVOICE, encoding="utf-8")
    (queue / "02_BO.txt").write_text(SECOND, encoding="utf-8")
    (tmp_path / "guide.csv").write_text(
        "Vendor,Default GL Code,Notes\n"
        "Alder Cloud Hosting,6110,Hosting.\n"
        "Birch Office Goods,6300 / 6310,Confirm which applies.\n",
        encoding="utf-8",
    )
    (tmp_path / "chart.csv").write_text(
        "GL Code,Account Name\n6110,Cloud Hosting\n6300,Office Supplies\n6310,Office Furniture\n",
        encoding="utf-8",
    )
    goal = SimpleNamespace(
        id=gl.GOAL_ID, root=tmp_path, keywords=("invoice",), resolved_sources=lambda: [],
        extra={"decision_work": {
            "tool": "gl_coding", "queue": "invoices", "isolation": "sources_only",
            "inputs": {"vendor": {"data_type": "string"}, "description": {"data_type": "string"}},
            "outputs": {"gl_code": {"data_type": "string"}},
            "reference_table": {"path": "guide.csv", "key_column": "Vendor",
                                "value_column": "Default GL Code", "key_input": "vendor",
                                "value_output": "gl_code"},
            "output_domains": [{"output": "gl_code", "path": "chart.csv", "column": "GL Code"}],
            "evidence": {"threshold": 5},
        }},
    )
    w = dw.DecisionWork(dw.load_config(goal),
                        log=dw.DecisionLog(gl.GOAL_ID, directory=tmp_path / "decisions"))
    monkeypatch.setattr(gl, "_work", lambda: w)
    token = turn_provenance.set_current({
        "isolation_goal": gl.GOAL_ID, "sections": ["identity"], "tools_yielded": ["gl_coding"],
        "cellar_hits": 0, "session_id": "s", "turn_id": "s#1", "turn_uid": "u1",
        "tier": "T1", "model": "m",
    })
    yield w
    turn_provenance.reset(token)


_turn = {"n": 100}


def _call(**args):
    # Each call is its own turn unless a test says otherwise: the operator
    # asks for each invoice, and the tool holds the model to that.
    prov = turn_provenance.current()
    if prov is not None and args.get("verb") != "apply_keg" and not args.pop("_same_turn", False):
        _turn["n"] += 1
        turn_provenance.set_current({**prov, "turn_uid": f"t{_turn['n']}",
                                     "turn_id": f"s#{_turn['n']}"})
    args.pop("_same_turn", None)
    return json.loads(gl.gl_coding(args))


def test_parse_invoice_reads_vendor_lines_and_total():
    inv = gl.parse_invoice(INVOICE)
    assert inv["vendor"] == "Alder Cloud Hosting"
    assert inv["invoice_number"] == "AC-0001" and inv["total"] == "1,300.50"
    assert [line["description"] for line in inv["lines"]] == [
        "Compute instances - December usage", "Object storage - 1.0 TB"]
    assert gl.item_inputs(inv) == {
        "vendor": "Alder Cloud Hosting",
        "description": "Compute instances - December usage; Object storage - 1.0 TB",
    }


@pytest.mark.parametrize("text", ["", "RECEIPT\n\nSomeone\n", "INVOICE\n\nVendor Only\n"])
def test_unreadable_invoice_is_surfaced_not_guessed(text):
    with pytest.raises(ValueError):
        gl.parse_invoice(text)


def test_next_returns_the_invoice_the_guide_row_and_the_full_chart(work):
    out = _call(verb="next")
    assert out["status"] == "ready" and out["item_id"] == "01_AC"
    assert out["invoice"]["vendor"] == "Alder Cloud Hosting"
    assert out["vendor_guide_row"]["Default GL Code"] == "6110"
    assert [row["GL Code"] for row in out["chart_of_accounts"]] == ["6110", "6300", "6310"]
    # The tool hands over sources, never an answer.
    assert "gl_code" not in out and "suggested" not in json.dumps(out).lower()


def test_code_confirm_then_correct(work):
    _call(verb="next")
    recorded = _call(verb="record", gl_code="6110", reasoning="hosting")
    assert recorded["status"] == "recorded_awaiting_confirmation" and recorded["tier"] == "T1"
    assert _call(verb="next")["status"] == "awaiting_confirmation"
    confirmed = _call(verb="decide", decision="confirm")
    assert confirmed["status"] == "confirmed" and confirmed["final_gl_code"] == "6110"
    assert confirmed["keg"] is None and confirmed["keg_halted"] is False
    assert confirmed["message"].startswith("No keg was involved in this coding.")

    assert _call(verb="next")["item_id"] == "02_BO"
    _call(verb="record", gl_code="6300", reasoning="supplies")
    corrected = _call(verb="decide", decision="correct", corrected_gl_code="6310")
    assert (corrected["status"], corrected["proposed_gl_code"], corrected["final_gl_code"]) == (
        "revised", "6300", "6310")
    assert _call(verb="next")["status"] == "queue_empty"
    # Inputs came from the adapter's own parse, not from the model.
    first = [r for r in work.log.run_records() if r["kind"] == "proposed"][0]
    assert first["inputs"]["vendor"] == "Alder Cloud Hosting"


def test_refusals_are_returned_in_plain_words(work):
    assert _call(verb="record", gl_code="9999", reasoning="x")["refused"] == "output_not_in_domain"
    _call(verb="record", gl_code="6110", reasoning="x")
    again = _call(verb="record", gl_code="6300", reasoning="x")
    assert again["refused"] == "prior_unconfirmed" and "01_AC" in again["message"]
    assert _call(verb="decide", decision="correct")["refused"] == "missing_correction"
    assert _call(verb="shrug")["success"] is False


def test_session_that_is_not_isolated_is_told_to_start_a_new_one(work):
    turn_provenance.set_current({"isolation_goal": None, "sections": [], "tools_yielded": []})
    for verb in ("next", "record"):
        out = _call(verb=verb, gl_code="6110", reasoning="x")
        assert out["refused"] == "session_not_isolated"
        assert "its context is not clean" in out["message"]
        assert "/new" not in out["message"]
    assert work.log.records() == []


def test_turn_that_read_the_cellar_cannot_code(work):
    turn_provenance.set_current({
        "isolation_goal": gl.GOAL_ID, "sections": ["cellar_knowledge"],
        "tools_yielded": [], "cellar_hits": 1,
    })
    out = _call(verb="record", gl_code="6110", reasoning="x")
    assert out["refused"] == "contaminated_turn" and work.log.records() == []


def test_tool_is_declared_at_every_wiring_point():
    import yaml
    from pathlib import Path

    import toolsets
    from grove.zones import ZoneClassifier

    root = Path(gl.__file__).resolve().parent.parent
    schema = yaml.safe_load((root / "config" / "zones.schema.yaml").read_text())
    entry = schema["tool_effects"]["gl_coding"]
    assert entry["class"] == "contained_write" and entry["containment"]
    record = yaml.safe_load((root / "config" / "capabilities" / "gl_coding.yaml").read_text())
    assert record["bindings"]["tools"] == ["gl_coding"] and record["zone"] == "green"
    assert record["tier_rule"]["eligible"] == [1, 2, 3]
    assert "gl_coding" in inspect_core_tools(toolsets)
    assert ZoneClassifier is not None


def inspect_core_tools(toolsets_module):
    import inspect
    return inspect.getsource(toolsets_module)


# ── T0: a signed keg codes the invoice, with no model ─────────────────

KEG = {
    "protocol": "GRV-004", "name": "Invoice GL coding", "version": 1,
    "inputs": {"vendor": {"data_type": "string"}, "description": {"data_type": "string"}},
    "outputs": {"gl_code": {"data_type": "string"}},
    "conditions": [{"if": "vendor == 'Alder Cloud Hosting'", "then": {"gl_code": "6110"}}],
}


def _t0(pattern="keg:invoice-gl-coding:v1:abc"):
    turn_provenance.set_current({
        "isolation_goal": gl.GOAL_ID, "sections": [], "tools_yielded": [], "cellar_hits": 0,
        "session_id": "s", "turn_id": "s#9", "turn_uid": "u9", "tier": "T0",
        "model": "pattern_cache", "t0_pattern": pattern,
    })


def test_keg_codes_the_invoice_at_t0_and_says_so(work):
    _t0()
    reply = gl.gl_coding({"verb": "apply_keg", "keg": KEG})
    assert reply.startswith("Next invoice: Alder Cloud Hosting AC-0001, $1,300.50 — coded 6110")
    assert "keg Invoice GL coding v1, with no model call" in reply
    record = work.pending()
    assert record["tier"] == "T0" and record["model"] == "pattern_cache"
    assert record["keg"] == {"name": "Invoice GL coding", "version": 1,
                             "pattern_id": "keg:invoice-gl-coding:v1:abc"}
    confirmed = _call(verb="decide", decision="confirm")
    assert confirmed["message"].startswith("Coded by the keg Invoice GL coding v1.")


def test_keg_hands_back_an_invoice_it_does_not_cover(work):
    _t0()
    gl.gl_coding({"verb": "apply_keg", "keg": KEG})
    _call(verb="decide", decision="confirm")
    declined = json.loads(gl.gl_coding({"verb": "apply_keg", "keg": KEG}))   # Birch: two codes
    assert declined["t0_declined"] is True and work.pending() is None
    # 2026-10-07: the handback says why, in a form the turn's record keeps.
    # The reference key and its value on this invoice: "no rule for vendor X".
    why = {"kind": "no_rule", "key": "vendor", "value": "Birch Office Goods"}
    assert declined["handback"] == why
    from grove.dispatcher import _t0_handback_why
    assert _t0_handback_why(json.dumps(declined)) == why
    # A rule that sends the case to the model names itself.
    defer = {"if": "vendor == 'Birch Office Goods'", "defer": True}
    narrowed = {**KEG, "conditions": [defer] + list(KEG["conditions"])}
    deferred = json.loads(gl.gl_coding({"verb": "apply_keg", "keg": narrowed}))
    assert deferred["handback"] == {"kind": "rule_defers", "rule": defer["if"]}
    assert work.pending() is None
    # A tool that gives no reason, or says something else, yields none.
    for raw in ('{"t0_declined": true, "reason": "x"}', "Coded 6110.", "{oops", None,
                '{"t0_declined": true, "handback": {"kind": ""}}'):
        assert _t0_handback_why(raw) is None


def test_the_turn_record_keeps_each_calls_duration_and_the_handback_reason():
    """2026-10-07: two questions the records could not answer (which model
    call of a slow turn was slow; why a keg handed an item back) are extra
    fields on the turn's own record, not a new record type."""
    import inspect

    import run_agent
    from grove import dispatcher
    src = inspect.getsource(dispatcher)
    assert '"call_ms": [int(ms) for ms in' in src and "agent._turn_call_ms = []" in src
    assert '"t0_handback_why": self._current_turn_t0_handback_why' in src
    assert "self._current_turn_t0_handback_why = None" in src
    assert "_calls.append(round(api_duration * 1000))" in inspect.getsource(run_agent)


def test_keg_verb_is_refused_outside_a_t0_serve(work):
    out = _call(verb="apply_keg", keg=KEG)        # the fixture's turn is T1
    assert out["refused"] == "not_t0" and work.log.records() == []
    assert "apply_keg" not in gl.GL_CODING_SCHEMA["parameters"]["properties"]["verb"]["enum"]


def test_keg_refused_at_t0_stops_the_line_on_the_bus(work):
    # Live 2026-10-06: a refusal at T0 reached the operator as raw JSON. It is
    # an abnormality: Jidoka flags it, the andon cord is pulled, the turn
    # stops with a plain message. No model takes over.
    from grove.dispatcher import _t0_declined, _t0_refused
    from grove.kaizen_ledger import default_ledger_dir

    turn_provenance.set_current({
        "isolation_goal": None, "sections": [], "tools_yielded": [], "cellar_hits": 0,
        "session_id": "sess-1", "turn_id": "sess-1#4", "turn_uid": "u4",
        "tier": "T0", "model": "pattern_cache", "t0_pattern": "keg:x:v1:abc",
    })
    raw = gl.gl_coding({"verb": "apply_keg", "keg": KEG})
    stop = _t0_refused(raw)
    assert stop["reason"] == "session_not_isolated"
    assert "its context is not clean" in stop["message"] and "/new" not in stop["message"]
    assert _t0_declined(raw) is False                 # not a handback
    assert work.log.records() == []

    # On the one bus: the session's own ledger carries the flag, the event
    # and Kaizen's answer — a refusal never arrives alone.
    events = [json.loads(l) for l in
              (default_ledger_dir() / "sess-1.jsonl").read_text().splitlines()]
    flag, cord, close = [e for e in events if e.get("loop_step")]
    assert (flag["event_type"], flag["flag"], flag["detector_id"]) == (
        "jidoka_flag", "anomaly", "turn_check")
    assert cord["event_type"] == "andon_event" and cord["stops_line"] is True
    assert cord["andon_id"] == stop["andon_id"] and cord["flag_id"] == flag["flag_id"]
    assert cord["provenance"] == [{"turn_id": "sess-1#4", "turn_uid": "u4"}]
    # The session rule is unsigned, so Kaizen proposes it for signature.
    assert close["event_type"] == "kaizen_answer" and close["closes"] == [cord["andon_id"]]
    assert (close["kind"], close["channel"]) == ("standard_work", "portal")
    assert "Proposed the rule for your signature" in stop["message"]


def test_model_turn_refusal_is_on_the_bus_too(work):
    turn_provenance.set_current({
        "isolation_goal": gl.GOAL_ID, "sections": ["cellar_knowledge"], "tools_yielded": [],
        "cellar_hits": 1, "session_id": "sess-2", "turn_id": "sess-2#1", "turn_uid": "u1",
        "tier": "T1", "model": "m",
    })
    out = _call(verb="record", gl_code="6110", reasoning="x")
    assert out["refused"] == "contaminated_turn" and out["stopped"] is True and out["andon_id"]
    # Ordinary flow is not an abnormality and raises nothing.
    turn_provenance.set_current({
        "isolation_goal": gl.GOAL_ID, "sections": [], "tools_yielded": [], "cellar_hits": 0,
        "session_id": "sess-3", "turn_id": "sess-3#1", "turn_uid": "u1", "tier": "T1", "model": "m",
    })
    flow = _call(verb="decide", decision="confirm")
    assert flow["refused"] == "nothing_pending" and "andon_id" not in flow
    # A code that is not in the chart IS one.
    assert _call(verb="record", gl_code="9999", reasoning="x")["andon_id"]


def test_unreadable_invoice_at_t0_stops_the_line(work, tmp_path):
    from grove.dispatcher import _t0_refused
    (tmp_path / "invoices" / "00_bad.txt").write_text("not an invoice", encoding="utf-8")
    _t0()
    stop = _t0_refused(gl.gl_coding({"verb": "apply_keg", "keg": KEG}))
    assert stop["reason"] == "item_unreadable" and stop["andon_id"]


def test_refusal_message_carries_the_next_step_and_no_manual_instruction(work, monkeypatch, tmp_path):
    # Live 2026-10-06: the clean-session remedy fired, but the reply told the
    # operator to type /new. The tool now hands the model ONE message to relay.
    import grove.grants as grants_mod
    from grove.grant_recognition import GrantToken

    # In the test's own directory: the default store is under the real home.
    monkeypatch.setattr(grants_mod, "_store", grants_mod.GrantStore(tmp_path / "grants.yaml"))
    cfg = work.config
    object.__setattr__(cfg, "on_unclean", dw.ON_UNCLEAN_OPEN_CLEAN)
    grants_mod.get_grant_store().add_standing_grant(GrantToken(
        source="standing", scope=cfg.goal_id, disposition="always",
        write_class=dw.SESSION_RULE_PREFIX + dw.session_rule_digest(cfg)))
    turn_provenance.set_current({
        "isolation_goal": None, "sections": ["cellar_knowledge"], "tools_yielded": [],
        "cellar_hits": 2, "session_id": "chat-9", "turn_id": "chat-9#2", "turn_uid": "u2",
        "tier": "T1", "model": "m", "request": "code the next invoice.",
    })
    out = _call(verb="next")
    assert out["refused"] == "session_not_isolated"
    assert out["message"].endswith("Opening a clean session and re-issuing the request there.")
    assert "/new" not in json.dumps(out)
    assert "Do not add steps of your own" in out["tell_the_operator"]
    from grove import reissue
    assert reissue.take("chat-9")["request"] == "code the next invoice."
    assert "never tell the operator" in gl.GL_CODING_SCHEMA["description"]


def test_one_invoice_per_request_the_model_cannot_run_on_after_a_confirmation(work):
    # Live 2026-10-06: a "confirm" turn recorded the decision and then coded
    # the NEXT invoice with the model in the same turn — so an invoice a signed
    # keg would have answered with no model call was coded by a model instead.
    _call(verb="next")
    _call(verb="record", gl_code="6110", reasoning="hosting")
    confirmed = _call(verb="decide", decision="confirm")
    assert "Do not fetch or code the next invoice in this turn" in confirmed["message"]
    for verb in ("next", "record"):
        out = _call(verb=verb, gl_code="6300", reasoning="x", _same_turn=True)
        assert out["refused"] == "one_step_per_turn", verb
        assert "andon_id" not in out                    # ordinary flow, not an abnormality
    assert work.pending() is None                       # nothing was coded
    # The operator's next request is a new turn, and proceeds.
    assert _call(verb="next")["item_id"] == "02_BO"
    assert "One invoice per request" in gl.GL_CODING_SCHEMA["description"]


def test_the_adapter_reads_the_account_and_a_printed_notice_when_declared():
    from tools import gl_coding_tool as tool

    text = (
        "INVOICE\n\nSwiftline Logistics\n2200 Freightway Blvd\n\n"
        "Invoice number: SWL-1\nInvoice date:   2026-11-26\nTerms:          Net 15\n"
        "Customer account: HF-44817\n\n"
        "Description                                Qty        Unit      Amount\n"
        "----------------------------------------------------------------------\n"
        "Same-day courier deliveries                  4       38.00      152.00\n"
        "----------------------------------------------------------------------\n"
        "TOTAL DUE (USD)                                                 152.00\n\n"
        "NOTICE: Copperline Couriers is now part of Swiftline Logistics. Your account\n"
        "number and service are unchanged.\n\nThank you for your business.\n")
    invoice = tool.parse_invoice(text)
    assert invoice["customer_account"] == "HF-44817"
    assert invoice["notice"] == ("Copperline Couriers is now part of Swiftline Logistics. "
                                 "Your account number and service are unchanged.")
    # Undeclared, the inputs are exactly what they were before.
    assert tool.item_inputs(invoice) == {
        "vendor": "Swiftline Logistics", "description": "Same-day courier deliveries"}
    declared = {"vendor": {}, "description": {}, "customer_account": {}, "notice": {}}
    assert tool.item_inputs(invoice, declared) == {
        "vendor": "Swiftline Logistics", "description": "Same-day courier deliveries",
        "customer_account": "HF-44817", "notice": invoice["notice"]}
    # An invoice with neither reads as empty, never as an error.
    plain = tool.parse_invoice(text.replace("Customer account: HF-44817\n", "").split("NOTICE:")[0])
    assert tool.item_inputs(plain, declared)["customer_account"] == ""
    assert tool.item_inputs(plain, declared)["notice"] == ""


def test_the_goals_own_request_cannot_pause_the_session(work):
    """Found live, 2026-10-07: asked to "code the next invoice", a model called
    the pause step, turn after turn. Whether a message is about something else
    is decided in code: the goal's own request never is."""
    from dataclasses import replace
    from grove.decision_work import KegDeclaration

    asked = "Code the next invoice"
    declared = replace(work.config, keg=KegDeclaration(
        name="Invoice GL coding", request=asked, requests=(), match_threshold=0.8,
        verb_bonus=0.0, scope="reserved", authority_level="green", revision_tiers=("T1",)))
    work.config = declared
    prov = turn_provenance.current()
    turn_provenance.set_current({**prov, "request": asked, "turn_uid": "p1", "turn_id": "s#p1"})
    out = json.loads(gl.gl_coding({"verb": "pause"}))
    assert (out["success"], out["status"]) == (False, "not_paused")
    assert "Call verb='next' now" in out["message"]
    from grove import reissue
    assert reissue.take("s") is None                      # nothing was armed: no pause happened
    # A message that really is about something else is not stopped by this
    # check: the pause goes on to the work session's own handling.
    turn_provenance.set_current({**prov, "request": "what's the weather in Tulsa?",
                                 "turn_uid": "p2", "turn_id": "s#p2"})
    assert json.loads(gl.gl_coding({"verb": "pause"})).get("status") != "not_paused"


def test_an_earlier_invoice_is_revised_by_the_name_the_operator_gave_it(work):
    # "I need to correct invoice 1, it should have been 6300": the operator
    # names an invoice by its place or its number, not by the queue's file id.
    _call(verb="next")
    _call(verb="record", gl_code="6110", reasoning="hosting")
    _call(verb="decide", decision="confirm")
    for said, code in (("1", "6300"), ("AC", "6110"), ("#01_ac", "6300")):
        out = _call(verb="decide", decision="correct", corrected_gl_code=code, item_id=said)
        assert out.get("status") == "revised", (said, out)
        proposed, decided = work._state()
        assert decided[proposed["01_AC"]["id"]]["output"] == {"gl_code": code}
    # A name no decided invoice answers to is refused, and nothing changes.
    refused = _call(verb="decide", decision="correct", corrected_gl_code="6110", item_id="77")
    assert refused["refused"] == "not_decided"

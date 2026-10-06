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


def _call(**args):
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
    assert confirmed["message"] == "No keg was involved in this coding."

    assert _call(verb="next")["item_id"] == "02_BO"
    _call(verb="record", gl_code="6300", reasoning="supplies")
    corrected = _call(verb="decide", decision="correct", corrected_gl_code="6310")
    assert (corrected["status"], corrected["proposed_gl_code"], corrected["final_gl_code"]) == (
        "corrected", "6300", "6310")
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
        assert "Start a new session with /new" in out["message"]
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

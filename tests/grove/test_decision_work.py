"""Decision work: the goal declaration, the append-only log, the turn checks,
the evidence rule and session isolation.

Fixtures are a MESSAGE-TAGGING goal on purpose. This engine is generic — a new
kind of work is a new Dock goal, a skill and a small adapter — so nothing here
may lean on any one domain's fields.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from grove import decision_work as dw
from grove.decision_work import DecisionLog, DecisionRefused, DecisionWork

GOAL = "message-triage"


def _goal(tmp_path, **over):
    root = tmp_path / "dock"
    root.mkdir(exist_ok=True)
    queue = tmp_path / "queue"
    queue.mkdir(exist_ok=True)
    (tmp_path / "channels.csv").write_text(
        "Channel,Default Tag,Notes\n"
        "billing,finance,\n"
        "outage,ops,\n"
        "legal,contracts / compliance,pick one\n"
        "press,comms,\n",
        encoding="utf-8",
    )
    (tmp_path / "tags.csv").write_text(
        "Tag\nfinance\nops\ncontracts\ncompliance\ncomms\nother\n", encoding="utf-8",
    )
    block = {
        "tool": "tag_message",
        "queue": str(queue),
        "isolation": "sources_only",
        "inputs": {"channel": {"data_type": "string", "required": True}},
        "outputs": {"tag": {"data_type": "string"}},
        "reference_table": {
            "path": str(tmp_path / "channels.csv"), "key_column": "Channel",
            "value_column": "Default Tag", "key_input": "channel",
            "value_output": "tag",
        },
        "output_domains": [
            {"output": "tag", "path": str(tmp_path / "tags.csv"), "column": "Tag"},
        ],
        "evidence": {"threshold": 3, "scope": "single_value_keys"},
    }
    block.update(over)
    return SimpleNamespace(
        id=GOAL, root=root, keywords=("message", "triage"),
        extra={"decision_work": block}, resolved_sources=lambda: [],
    )


def _queue(tmp_path, *channels):
    for i, channel in enumerate(channels, 1):
        (tmp_path / "queue" / f"m{i:02d}.txt").write_text(channel, encoding="utf-8")


def _work(tmp_path, *channels, **over):
    goal = _goal(tmp_path, **over)
    _queue(tmp_path, *channels)
    cfg = dw.load_config(goal)
    return DecisionWork(cfg, log=DecisionLog(GOAL, directory=tmp_path / "decisions"))


def _prov(**over):
    base = {
        "session_id": "s1", "turn_id": "s1#1", "turn_uid": "u1", "tier": "T1",
        "model": "m", "cellar_hits": 0, "sections": ["identity", "timestamp"],
        "tools_yielded": ["tag_message"], "isolation_goal": GOAL,
    }
    base.update(over)
    return base


def _code(work, tag, *, decision="confirm", corrected=None, prov=None):
    path = work.next_item()
    work.record(item_id=path.stem, inputs={"channel": path.read_text()},
                output={"tag": tag}, reasoning="because", provenance=prov or _prov())
    return work.decide(
        decision=decision,
        corrected_output={"tag": corrected} if corrected else None,
        provenance=prov or _prov(),
    )


# ── declaration ───────────────────────────────────────────────────────


def test_goal_without_a_declaration_has_no_decision_work(tmp_path):
    goal = _goal(tmp_path)
    goal.extra = {}
    assert dw.load_config(goal) is None


@pytest.mark.parametrize("over", [
    {"tool": ""}, {"inputs": "x"}, {"isolation": "loose"},
    {"evidence": {"threshold": 0}}, {"evidence": {"threshold": 3, "scope": "any"}},
    {"reference_table": {"path": "x.csv", "key_column": "a", "value_column": "b",
                         "key_input": "nope", "value_output": "tag"}},
    {"output_domains": [{"output": "nope", "path": "x.csv", "column": "c"}]},
])
def test_malformed_declaration_fails_loud(tmp_path, over):
    with pytest.raises(ValueError):
        dw.load_config(_goal(tmp_path, **over))


def test_relative_paths_resolve_against_the_dock_root(tmp_path):
    cfg = dw.load_config(_goal(tmp_path, queue="../queue"))
    assert cfg.queue.resolve() == (tmp_path / "queue").resolve()


# ── the log is append-only and attributed ─────────────────────────────


def test_record_then_decide_appends_and_never_edits(tmp_path):
    work = _work(tmp_path, "billing", "outage")
    assert work.next_item().stem == "m01"
    proposed = work.record(item_id="m01", inputs={"channel": "billing"},
                           output={"tag": "finance"}, reasoning="r", provenance=_prov())
    before = work.log.path.read_text()
    decided = work.decide(decision="confirm", provenance=_prov(turn_id="s1#2"))
    after = work.log.path.read_text()
    assert after.startswith(before)                    # nothing rewritten
    assert decided["ref"] == proposed["id"] and decided["output"] == {"tag": "finance"}
    assert (proposed["tier"], proposed["model"], proposed["turn_uid"]) == ("T1", "m", "u1")
    assert proposed["run_id"] == work.log.current_run()["run_id"]
    assert work.next_item().stem == "m02" and work.pending() is None


def test_correction_is_a_new_record_with_the_operators_value(tmp_path):
    work = _work(tmp_path, "billing")
    decided = _code(work, "ops", decision="correct", corrected="finance")
    kinds = [r["kind"] for r in work.log.run_records()]
    assert kinds == ["proposed", "decided"]
    assert decided["decision"] == "correct" and decided["output"] == {"tag": "finance"}
    assert work.log.run_records()[0]["output"] == {"tag": "ops"}   # original kept


def test_damaged_log_is_not_read_around(tmp_path):
    work = _work(tmp_path, "billing")
    _code(work, "finance")
    with open(work.log.path, "a") as fh:
        fh.write("{not json\n")
    with pytest.raises(ValueError):
        work.log.records()


def test_new_run_hides_earlier_records_without_deleting_them(tmp_path):
    work = _work(tmp_path, "billing", "outage")
    _code(work, "finance")
    size = work.log.path.stat().st_size
    run = work.log.start_run("reset")
    assert run["run_number"] == 2
    assert work.log.run_records() == [] and work.next_item().stem == "m01"
    assert work.log.path.stat().st_size > size
    assert len(work.log.records()) == 4   # run 1, proposed, decided, run 2


# ── refusals ──────────────────────────────────────────────────────────


def _refusal(fn):
    with pytest.raises(DecisionRefused) as info:
        fn()
    return info.value


def test_refuses_a_second_item_until_the_first_is_decided(tmp_path):
    work = _work(tmp_path, "billing", "outage")
    work.record(item_id="m01", inputs={}, output={"tag": "finance"},
                reasoning="", provenance=_prov())
    err = _refusal(lambda: work.record(
        item_id="m02", inputs={}, output={"tag": "ops"}, reasoning="", provenance=_prov()))
    assert err.reason == "prior_unconfirmed" and "m01" in str(err)


def test_refuses_out_of_order_and_out_of_domain(tmp_path):
    work = _work(tmp_path, "billing", "outage")
    assert _refusal(lambda: work.record(
        item_id="m02", inputs={}, output={"tag": "ops"}, reasoning="",
        provenance=_prov())).reason == "not_next_item"
    assert _refusal(lambda: work.record(
        item_id="m01", inputs={}, output={"tag": "made-up"}, reasoning="",
        provenance=_prov())).reason == "output_not_in_domain"
    assert _refusal(lambda: work.record(
        item_id="m01", inputs={}, output={}, reasoning="",
        provenance=_prov())).reason == "missing_output"
    assert work.log.records() == []          # a refusal writes nothing


def test_decide_refusals(tmp_path):
    work = _work(tmp_path, "billing")
    assert _refusal(lambda: work.decide(decision="confirm")).reason == "nothing_pending"
    work.record(item_id="m01", inputs={}, output={"tag": "finance"},
                reasoning="", provenance=_prov())
    assert _refusal(lambda: work.decide(decision="maybe")).reason == "unknown_decision"
    assert _refusal(lambda: work.decide(decision="correct")).reason == "missing_correction"
    assert _refusal(lambda: work.decide(
        decision="correct", corrected_output={"tag": "finance"})).reason == "correction_matches"
    assert _refusal(lambda: work.decide(
        decision="correct", corrected_output={"tag": "nope"})).reason == "output_not_in_domain"


@pytest.mark.parametrize("taint", [
    {"sections": ["identity", "cellar_knowledge"]},
    {"sections": ["accumulated_domain_memory"]},
    {"sections": ["external_memory"]},
    {"tools_yielded": ["tag_message", "cellar_search"]},
    {"tools_yielded": ["session_search"]},
    {"cellar_hits": 2},
])
def test_isolated_goal_refuses_a_turn_that_drew_on_recall(tmp_path, taint):
    work = _work(tmp_path, "billing")
    err = _refusal(lambda: work.record(
        item_id="m01", inputs={}, output={"tag": "finance"}, reasoning="",
        provenance=_prov(**taint)))
    assert err.reason == "contaminated_turn" and "/new" in str(err)
    assert work.log.records() == []


def test_isolated_goal_refuses_a_session_that_is_not_its_own(tmp_path):
    work = _work(tmp_path, "billing")
    for prov in (_prov(isolation_goal=None), _prov(isolation_goal="other-goal")):
        err = _refusal(lambda: work.record(
            item_id="m01", inputs={}, output={"tag": "finance"}, reasoning="",
            provenance=prov))
        assert err.reason == "session_not_isolated"
        assert "Start a new session with /new" in str(err)
    assert _refusal(lambda: work.record(
        item_id="m01", inputs={}, output={"tag": "finance"}, reasoning="",
        provenance=None)).reason == "no_provenance"


def test_goal_without_isolation_does_not_check_the_turn(tmp_path):
    work = _work(tmp_path, "billing", isolation=None)
    work.record(item_id="m01", inputs={}, output={"tag": "finance"}, reasoning="",
                provenance=_prov(isolation_goal=None, cellar_hits=3))


# ── reference table and the evidence rule ─────────────────────────────


def test_reference_table_single_and_multi_value_keys(tmp_path):
    table = _work(tmp_path).reference()
    assert table.single_value("  BILLING ") == "finance"
    assert table.values("legal") == ["contracts", "compliance"]
    assert table.single_value("legal") is None and table.single_value("unknown") is None
    assert table.single_value_rows() == [
        ("billing", "finance"), ("outage", "ops"), ("press", "comms")]


def test_evidence_counts_across_keys_and_never_counts_multi_value_keys(tmp_path):
    work = _work(tmp_path, "billing", "legal", "outage", "press")
    _code(work, "finance")
    _code(work, "contracts")            # multi-value key: never evidence
    _code(work, "ops")
    assert work.evidence()["confirmations"] == 2 and not work.evidence()["met"]
    _code(work, "comms")
    ev = work.evidence()
    assert ev["met"] and ev["confirmations"] == 3
    assert [e["item_id"] for e in ev["evidence"]] == ["m01", "m03", "m04"]


def test_confirmation_that_departs_from_the_table_is_not_evidence(tmp_path):
    work = _work(tmp_path, "billing")
    _code(work, "other")                # confirmed, but not the table's value
    assert work.evidence()["confirmations"] == 0


def test_correction_against_the_table_blocks_the_pattern(tmp_path):
    work = _work(tmp_path, "billing", "outage", "press", "billing")
    _code(work, "finance")
    _code(work, "ops")
    _code(work, "comms")
    assert work.evidence()["met"]
    (tmp_path / "queue" / "m05.txt").write_text("billing")
    _code(work, "finance", decision="correct", corrected="other")
    ev = work.evidence()
    assert ev["corrections_against_reference"] == 1 and not ev["met"]


def test_evidence_reads_only_the_current_run(tmp_path):
    work = _work(tmp_path, "billing", "outage", "press")
    for tag in ("finance", "ops", "comms"):
        _code(work, tag)
    assert work.evidence()["met"]
    work.log.start_run("reset")
    assert work.evidence()["confirmations"] == 0


# ── isolation: which session, which sections ──────────────────────────


def test_isolating_goal_is_matched_by_its_declared_keywords(tmp_path):
    dock = SimpleNamespace(goals=(_goal(tmp_path),))
    assert dw.isolating_goal_for("Tag the next message", dock=dock) == GOAL
    assert dw.isolating_goal_for("what's on my calendar", dock=dock) is None
    assert dw.isolating_goal_for("messages", dock=dock) is None    # whole words only
    open_goal = _goal(tmp_path, isolation=None)
    assert dw.isolating_goal_for("Tag the next message",
                                 dock=SimpleNamespace(goals=(open_goal,))) is None


def test_composer_composes_no_isolated_section():
    from grove.prompt.composer import PromptComposer, SectionResult

    composer = PromptComposer()
    for i, name in enumerate(("identity", "cellar_knowledge",
                              "accumulated_domain_memory", "external_memory")):
        composer.register_section(
            name, lambda ctx, n=name: SectionResult(label=n, text=f"<{n}>"),
            order=i, tier="context",
        )
    open_prompt = composer.compose()
    assert set(open_prompt.sections) == {
        "identity", "cellar_knowledge", "accumulated_domain_memory", "external_memory"}
    # The tier allow-list can never switch an isolated section back on.
    isolated = composer.compose(
        isolated_sections=dw.RECALL_SECTIONS,
        tier_context_blocks={"cellar_context"},
    )
    assert set(isolated.sections) == {"identity"}
    assert "<cellar_knowledge>" not in isolated.text


class _Meta:
    def __init__(self):
        self.data = {}

    def get_meta(self, key):
        return self.data.get(key)

    def set_meta(self, key, value):
        self.data[key] = value


def _dispatcher(monkeypatch, tmp_path, session_id, turn):
    from grove.dispatcher import Dispatcher

    dock = SimpleNamespace(goals=(_goal(tmp_path),))
    monkeypatch.setattr(
        dw, "isolating_goal_for",
        lambda message, dock=dock, _real=dw.isolating_goal_for: _real(message, dock=dock),
    )
    d = Dispatcher.__new__(Dispatcher)
    d.session_id = session_id
    d._turn_counter = turn
    d._current_turn_id = f"{session_id}#{turn}"
    d._isolation_by_session = {}
    return d


def test_session_is_isolated_by_its_first_turn_for_its_whole_life(monkeypatch, tmp_path):
    meta = _Meta()
    d = _dispatcher(monkeypatch, tmp_path, "s1", 1)
    d.session = meta
    assert d._resolve_turn_isolation(None, "tag the next message") == GOAL
    assert meta.data[dw.isolation_meta_key("s1")] == GOAL
    # Later turns need no keyword, and the latch survives a rebuilt Dispatcher.
    d2 = _dispatcher(monkeypatch, tmp_path, "s1", 4)
    d2.session = meta
    assert d2._resolve_turn_isolation(None, "yes, confirmed") == GOAL


def test_session_that_began_otherwise_is_never_isolated(monkeypatch, tmp_path):
    meta = _Meta()
    d = _dispatcher(monkeypatch, tmp_path, "s2", 1)
    d.session = meta
    assert d._resolve_turn_isolation(None, "what's on my calendar") is None
    d._turn_counter = 2
    assert d._resolve_turn_isolation(None, "tag the next message") is None
    # A session first seen mid-way (it predates the latch) is not isolated either.
    d3 = _dispatcher(monkeypatch, tmp_path, "s3", 7)
    d3.session = _Meta()
    assert d3._resolve_turn_isolation(None, "tag the next message") is None


def test_isolation_latch_works_without_a_session_database(monkeypatch, tmp_path):
    d = _dispatcher(monkeypatch, tmp_path, "s4", 1)
    d.session = None
    assert d._resolve_turn_isolation(None, "triage this") == GOAL
    d._turn_counter = 2
    assert d._resolve_turn_isolation(None, "ok") == GOAL


def test_isolation_fault_is_logged_and_fails_closed(monkeypatch, tmp_path, caplog):
    d = _dispatcher(monkeypatch, tmp_path, "s5", 1)

    class _Broken:
        def get_meta(self, key):
            raise RuntimeError("db down")

    d.session = _Broken()
    with caplog.at_level("ERROR"):
        assert d._resolve_turn_isolation(None, "tag the next message") is None
    assert "could not resolve goal isolation" in caplog.text
    # Not isolated ⇒ the goal's tool refuses: the fault fails closed.
    work = _work(tmp_path, "billing")
    assert _refusal(lambda: work.record(
        item_id="m01", inputs={}, output={"tag": "finance"}, reasoning="",
        provenance=_prov(isolation_goal=None))).reason == "session_not_isolated"


def test_tools_receive_the_turns_provenance():
    from grove import turn_provenance

    assert turn_provenance.current() is None
    token = turn_provenance.set_current({"turn_id": "s#1"})
    assert turn_provenance.current() == {"turn_id": "s#1"}
    turn_provenance.reset(token)
    assert turn_provenance.current() is None
    import run_agent
    src = inspect.getsource(run_agent.AIAgent._invoke_tool)
    assert src.index("set_current(") < src.index("self._invoke_tool_impl(")
    assert "_turn_provenance.reset(_prov_token)" in src


# ── generality ────────────────────────────────────────────────────────


def test_decision_work_carries_no_domain_vocabulary():
    src = inspect.getsource(dw).lower()
    for word in ("vendor", "invoice", "gl_code", "gl code", "chart of accounts"):
        assert word not in src, word

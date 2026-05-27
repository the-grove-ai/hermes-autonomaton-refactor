"""Integration tests for Sprint 29 Phase 2 — selective tool loading wiring.

Covers AIAgent's per-turn filter behavior (`_tools_for_api` property,
`_maybe_apply_tool_filter` logic, fallback paths) and the Dispatcher's
``tool_selection`` Kaizen Ledger event emission end-to-end via a
synthetic generator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from grove.classify import ClassificationResult
from grove.dispatcher import Dispatcher
from grove.intent_store import IntentStore
from grove.intents import FinalResponse, Observation, ToolIntent


# ── Test helpers (mirrored from test_dispatcher_intent_records.py) ────────


def _synthetic_generator(
    intents_batch: Optional[List[ToolIntent]],
    result: Dict[str, Any],
    *,
    final_text: str = "ok",
):
    def gen():
        if intents_batch:
            obs = yield intents_batch
            assert isinstance(obs, list)
            assert all(isinstance(o, Observation) for o in obs)
        yield FinalResponse(content=final_text)
        return result
    return gen()


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": ""}}


def _bare_agent_with_tools(tools: List[dict]):
    import run_agent
    agent = object.__new__(run_agent.AIAgent)
    agent.tools = tools
    agent._tools_for_turn = None
    agent._last_tool_selection = None
    agent._current_assistant_message = {
        "role": "assistant",
        "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": "{}"}}],
    }
    agent._current_messages = []
    agent._current_effective_task_id = "task_t"
    agent._current_api_call_count = 1
    agent.session_id = "ctx-budget-test"
    agent.model = "claude-sonnet-4-6"
    agent._execute_tool_calls = (
        lambda asst, messages, task_id, api_n: [
            messages.append({
                "role": "tool", "tool_call_id": tc.get("id", ""),
                "content": "stub",
            })
            for tc in (asst.get("tool_calls") or [])
        ]
    )
    return agent


def _set_classification(
    monkeypatch: pytest.MonkeyPatch,
    *,
    intent_class: str = "code_generation",
    complexity_signal: str = "simple",
    goal_alignment: Optional[str] = "direct",
) -> ClassificationResult:
    result = ClassificationResult(
        intent_class=intent_class,
        pattern_hash="abc",
        confidence=0.9,
        register_class="technical",
        complexity_signal=complexity_signal,
        goal_alignment=goal_alignment,
    )
    from grove import providers as _providers_mod
    monkeypatch.setattr(_providers_mod, "_last_classification", result)
    return result


def _patch_classifier_green(monkeypatch: pytest.MonkeyPatch) -> None:
    from grove import zones as _zones
    from grove.zones import ZoneResult
    monkeypatch.setattr(
        _zones, "classify",
        lambda action: ZoneResult(
            zone="green", matched_rule=action, source="test_force_green",
        ),
    )


@pytest.fixture
def tmp_store(tmp_path: Path) -> IntentStore:
    return IntentStore(store_path=tmp_path / "records.jsonl")


# ── AIAgent._tools_for_api property ──────────────────────────────────────


class TestToolsForApiProperty:
    def test_returns_full_tools_when_filter_not_applied(self):
        full = [_tool("clarify"), _tool("write_file")]
        agent = _bare_agent_with_tools(full)
        assert agent._tools_for_api is full

    def test_returns_filtered_when_filter_applied(self):
        full = [_tool("clarify"), _tool("write_file"), _tool("delegate_task")]
        agent = _bare_agent_with_tools(full)
        agent._tools_for_turn = [full[0]]  # only clarify
        result = agent._tools_for_api
        assert result == [full[0]]
        # Underlying full list unchanged.
        assert agent.tools == full

    def test_returns_none_when_both_none(self):
        agent = _bare_agent_with_tools([])
        agent.tools = None
        assert agent._tools_for_api is None


# ── AIAgent._maybe_apply_tool_filter ──────────────────────────────────────


class TestMaybeApplyToolFilter:
    def test_no_op_when_tools_empty(self, monkeypatch: pytest.MonkeyPatch):
        _set_classification(monkeypatch)
        agent = _bare_agent_with_tools([])
        agent._maybe_apply_tool_filter()
        # Nothing to filter — both stay None.
        assert agent._tools_for_turn is None
        assert agent._last_tool_selection is None

    def test_classification_to_filtered_set(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        # code_generation, simple — should keep core + reads +
        # code_generation domain chunk; should NOT add exploratory or
        # writes.
        _set_classification(
            monkeypatch, intent_class="code_generation",
            complexity_signal="simple",
        )
        full = [
            _tool("clarify"),                      # core
            _tool("write_file"),                   # code_generation
            _tool("patch"),                        # code_generation
            _tool("delegate_task"),                # exploratory — excluded
            _tool("mcp_notion_API_post_search"),   # mcp read — included
            _tool("mcp_notion_API_patch_page"),    # mcp write — excluded
        ]
        agent = _bare_agent_with_tools(full)
        agent._maybe_apply_tool_filter()
        assert agent._tools_for_turn is not None
        names = {t["function"]["name"] for t in agent._tools_for_turn}
        assert "clarify" in names
        assert "write_file" in names
        assert "patch" in names
        assert "mcp_notion_API_post_search" in names
        assert "delegate_task" not in names
        assert "mcp_notion_API_patch_page" not in names
        assert agent._last_tool_selection["intent_class"] == "code_generation"
        assert agent._last_tool_selection["fallback"] is False

    def test_unknown_intent_fallback_to_full(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        # Sprint 12 graceful-tier returned None classification → loud
        # fallback. _tools_for_turn stays None (so _tools_for_api
        # returns the full self.tools).
        from grove import providers as _providers_mod
        monkeypatch.setattr(_providers_mod, "_last_classification", None)
        full = [_tool("clarify"), _tool("write_file")]
        agent = _bare_agent_with_tools(full)
        agent._maybe_apply_tool_filter()
        assert agent._tools_for_turn is None
        assert agent._tools_for_api is full
        assert agent._last_tool_selection["fallback"] is True
        assert agent._last_tool_selection["selected_count"] == 2

    def test_complex_intent_adds_exploratory(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        _set_classification(
            monkeypatch, intent_class="code_generation",
            complexity_signal="complex",
        )
        full = [_tool("delegate_task"), _tool("browser_navigate"), _tool("clarify")]
        agent = _bare_agent_with_tools(full)
        agent._maybe_apply_tool_filter()
        names = {t["function"]["name"] for t in agent._tools_for_turn}
        # Exploratory tools are now in the allowed set.
        assert "delegate_task" in names
        assert "browser_navigate" in names

    def test_planning_intent_adds_notion_writes(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        # planning is in write_intents → writes loaded.
        _set_classification(
            monkeypatch, intent_class="planning", complexity_signal="moderate",
        )
        full = [
            _tool("clarify"),
            _tool("mcp_notion_API_post_page"),  # write
            _tool("mcp_notion_API_post_search"),  # read
        ]
        agent = _bare_agent_with_tools(full)
        agent._maybe_apply_tool_filter()
        names = {t["function"]["name"] for t in agent._tools_for_turn}
        assert "mcp_notion_API_post_page" in names
        assert "mcp_notion_API_post_search" in names

    def test_filter_failure_degrades_to_full(
        self, monkeypatch: pytest.MonkeyPatch, caplog,
    ):
        # If the taxonomy loader explodes, the agent must NOT crash
        # the turn — degrade to full registry with a WARNING.
        _set_classification(monkeypatch)
        import grove.context_budget as _cb
        monkeypatch.setattr(
            _cb, "load_taxonomy",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("yaml broken")),
        )
        full = [_tool("clarify"), _tool("write_file")]
        agent = _bare_agent_with_tools(full)
        import logging
        with caplog.at_level(logging.WARNING, logger="run_agent"):
            agent._maybe_apply_tool_filter()
        assert agent._tools_for_turn is None
        assert agent._tools_for_api is full
        assert agent._last_tool_selection["fallback"] is True
        assert "yaml broken" in agent._last_tool_selection["error"]


# ── Dispatcher writes tool_selection ledger event ────────────────────────


class TestDispatcherToolSelectionEvent:
    def test_writes_tool_selection_event_with_metadata(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
        tmp_path: Path,
    ):
        _patch_classifier_green(monkeypatch)
        _set_classification(
            monkeypatch, intent_class="planning",
            complexity_signal="moderate", goal_alignment="direct",
        )
        full = [_tool("clarify"), _tool("write_file"), _tool("search_files")]
        agent = _bare_agent_with_tools(full)
        # Pre-populate _last_tool_selection as if _maybe_apply_tool_filter
        # already ran (the synthetic generator's gen.send is what would
        # normally trigger it; we stub directly to keep the test focused).
        agent._last_tool_selection = {
            "intent_class": "planning",
            "complexity_signal": "moderate",
            "goal_alignment": "direct",
            "fallback": False,
            "selected_count": 2,
            "full_count": 3,
        }
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(
                None, {"final_response": "ok"}, final_text="ok",
            )
        )
        d = Dispatcher(
            intent_store=tmp_store,
            kaizen_ledger_dir=tmp_path / "ledger",
        )
        d.dispatch_turn(agent, user_message="plan it")

        ledger = d.ledger_for(agent)
        assert ledger is not None
        events = ledger.events_by_type("tool_selection")
        assert len(events) == 1
        ev = events[0]
        assert ev["intent_class"] == "planning"
        assert ev["complexity_signal"] == "moderate"
        assert ev["fallback"] is False
        assert ev["selected_count"] == 2
        assert ev["full_count"] == 3

    def test_writes_fallback_event_on_unknown_intent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
        tmp_path: Path,
    ):
        _patch_classifier_green(monkeypatch)
        # No classification → fallback metadata
        full = [_tool("clarify"), _tool("write_file")]
        agent = _bare_agent_with_tools(full)
        agent._last_tool_selection = {
            "intent_class": None,
            "complexity_signal": None,
            "fallback": True,
            "selected_count": 2,
            "full_count": 2,
        }
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(None, {"final_response": "ok"})
        )
        d = Dispatcher(
            intent_store=tmp_store,
            kaizen_ledger_dir=tmp_path / "ledger",
        )
        d.dispatch_turn(agent, user_message="hi")
        events = d.ledger_for(agent).events_by_type("tool_selection")
        assert len(events) == 1
        assert events[0]["fallback"] is True

    def test_no_event_when_agent_has_no_selection_metadata(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
        tmp_path: Path,
    ):
        # An agent without _last_tool_selection (e.g., legacy test path
        # or a turn where the filter hook wasn't reached) must not crash
        # — the Dispatcher silently skips the event.
        _patch_classifier_green(monkeypatch)
        full = [_tool("clarify")]
        agent = _bare_agent_with_tools(full)
        # leave _last_tool_selection as None (default)
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(None, {"final_response": "ok"})
        )
        d = Dispatcher(
            intent_store=tmp_store,
            kaizen_ledger_dir=tmp_path / "ledger",
        )
        d.dispatch_turn(agent, user_message="hi")
        events = d.ledger_for(agent).events_by_type("tool_selection")
        assert events == []

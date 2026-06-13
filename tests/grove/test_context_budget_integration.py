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
from grove.intents import ToolBatchYield, FinalResponse, Observation, ToolIntent


# ── Test helpers (mirrored from test_dispatcher_intent_records.py) ────────


def _synthetic_generator(
    intents_batch: Optional[List[ToolIntent]],
    result: Dict[str, Any],
    *,
    final_text: str = "ok",
):
    def gen():
        if intents_batch:
            obs = yield ToolBatchYield(intents=intents_batch)
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
        # code_generation, simple — should keep core + code_generation
        # domain chunk; should NOT add exploratory. Sprint 74 flip: MCP tools
        # no longer pass through by default — they disclose only on a manifest
        # trigger match. This turn is code_generation with no notion keyword in
        # the message, so the notion MCP unit (intents=[research,retrieval],
        # keywords=[notion,...]) does NOT match and notion is WITHHELD. Native
        # selection (tool_groups.yaml) is unchanged.
        _set_classification(
            monkeypatch, intent_class="code_generation",
            complexity_signal="simple",
        )
        full = [
            _tool("clarify"),                       # core
            _tool("write_file"),                    # code_generation
            _tool("patch"),                         # code_generation
            _tool("delegate_task"),                 # exploratory — excluded
            _tool("mcp_notion_notion_search"),      # mcp read — unmatched
            _tool("mcp_notion_notion_update_page"), # mcp write — unmatched
        ]
        agent = _bare_agent_with_tools(full)
        agent._maybe_apply_tool_filter()
        assert agent._tools_for_turn is not None
        names = {t["function"]["name"] for t in agent._tools_for_turn}
        assert "clarify" in names
        assert "write_file" in names
        assert "patch" in names
        assert "mcp_notion_notion_search" not in names       # unmatched -> withheld
        assert "mcp_notion_notion_update_page" not in names  # unmatched -> withheld
        assert "delegate_task" not in names
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

    def test_matched_mcp_intent_discloses_notion(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        # A turn whose intent is in the notion record's trigger (research)
        # discloses the notion MCP — reads AND writes. GRV-009 E4 C2: gating is
        # registry-driven; notion is eligible only on T3 (tier_rule.eligible:[3]),
        # so pin the tier to T3 — the only tier where notion can disclose.
        _set_classification(
            monkeypatch, intent_class="research", complexity_signal="moderate",
        )
        monkeypatch.setattr("grove.providers._last_routed_tier", "T3")
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

    def test_matched_mcp_keyword_in_message_discloses_notion(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        # The keyword clause: a message mentioning a notion trigger keyword
        # discloses notion even when the intent doesn't match. Exercises the
        # _latest_user_text() seam reading self._current_messages.
        _set_classification(
            monkeypatch, intent_class="code_generation", complexity_signal="simple",
        )
        monkeypatch.setattr("grove.providers._last_routed_tier", "T3")  # notion's eligible tier
        full = [_tool("clarify"), _tool("mcp_notion_API_post_search")]
        agent = _bare_agent_with_tools(full)
        agent._current_messages = [
            {"role": "user", "content": "update the notion database for the sprint"},
        ]
        agent._maybe_apply_tool_filter()
        names = {t["function"]["name"] for t in agent._tools_for_turn}
        assert "mcp_notion_API_post_search" in names  # keyword 'notion' matched

    def test_filter_failure_degrades_to_full(
        self, monkeypatch: pytest.MonkeyPatch, caplog,
    ):
        # If the resolver explodes, the agent must NOT crash the turn — degrade
        # to full registry with a WARNING. GRV-009 E5 C-RETIRE: the resolver path
        # no longer reads tool_groups.yaml (load_taxonomy is off it), so the
        # failure is injected at the resolver itself (the still-present call).
        _set_classification(monkeypatch)
        import grove.context_budget as _cb
        monkeypatch.setattr(
            _cb, "resolve_tools_for_tier",
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


# ── GRV-009 spike C1 — selected names + construction-surface provenance ────


class TestSpikeC1Observability:
    """The three observability signals the spike's GATE-A trace found missing:
    the selected tool NAMES, the capability-hook outcome (covered in
    test_capability_hook.py), and construction-surface provenance — all riding
    the existing ``tool_selection`` event so no new sink is introduced."""

    def test_selected_names_recorded(self, monkeypatch: pytest.MonkeyPatch):
        _set_classification(
            monkeypatch, intent_class="code_generation", complexity_signal="simple",
        )
        full = [
            _tool("clarify"),        # core
            _tool("write_file"),     # code_generation
            _tool("patch"),          # code_generation
            _tool("delegate_task"),  # exploratory — excluded on a simple turn
        ]
        agent = _bare_agent_with_tools(full)
        agent._maybe_apply_tool_filter()
        sel = agent._last_tool_selection
        # The NAMES, not just the count — this is what would have shown
        # calendar_list's absence on the failing turn.
        assert sel["selected_names"] == ["clarify", "patch", "write_file"]
        assert sel["selected_count"] == len(sel["selected_names"])
        assert "delegate_task" not in sel["selected_names"]

    def test_construction_provenance_emitted_once(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        _set_classification(
            monkeypatch, intent_class="code_generation", complexity_signal="simple",
        )
        agent = _bare_agent_with_tools([_tool("clarify"), _tool("write_file")])
        agent._construction_provenance = {
            "enabled_toolsets": ["file"],
            "disabled_toolsets": [],
            "construction_tool_count": 2,
            "construction_tool_names": ["clarify", "write_file"],
        }
        agent._construction_provenance_emitted = False

        agent._maybe_apply_tool_filter()
        first = agent._last_tool_selection
        assert "construction_provenance" in first
        assert first["construction_provenance"]["enabled_toolsets"] == ["file"]
        assert agent._construction_provenance_emitted is True

        # Second turn of the same session: provenance is NOT repeated.
        agent._maybe_apply_tool_filter()
        second = agent._last_tool_selection
        assert "construction_provenance" not in second

    def test_provenance_absent_when_not_captured(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        # Agents built outside __init__ (no provenance captured) never emit a
        # half-formed snapshot — the lazy guard defaults to "already emitted".
        _set_classification(
            monkeypatch, intent_class="code_generation", complexity_signal="simple",
        )
        agent = _bare_agent_with_tools([_tool("clarify")])
        agent._maybe_apply_tool_filter()
        assert "construction_provenance" not in agent._last_tool_selection

"""pull-tool-name-validation-v1 — a tool OFFERED this turn is a real tool.

The JIT pull tools (read_tool_schema / read_goal_context) live only on the
per-turn surface. The hallucinated-name check used to validate against the
construction surface alone, so it told the model an offered pull tool "does not
exist" before the pull interception could run.
"""

from __future__ import annotations

import inspect

import run_agent
from grove.disclosure import PULL_TOOL_NAMES


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": "d"}}


def _agent(construction, turn_surface):
    agent = object.__new__(run_agent.AIAgent)
    agent.valid_tool_names = set(construction)
    agent._tools_for_turn = turn_surface
    return agent


def test_offered_pull_tools_are_callable_names():
    surface = [_tool("terminal"), _tool("web_search")] + [_tool(n) for n in PULL_TOOL_NAMES]
    agent = _agent({"terminal", "web_search", "gmail_send"}, surface)
    callable_names = agent._callable_tool_names()
    assert set(PULL_TOOL_NAMES) <= callable_names          # offered => real
    assert "gmail_send" in callable_names                  # construction tools still real
    assert "totally_made_up_tool" not in callable_names    # hallucinations still caught


def test_offered_names_is_the_turn_surface_not_the_construction_surface():
    surface = [_tool("terminal")] + [_tool(n) for n in PULL_TOOL_NAMES]
    agent = _agent({"terminal", "gmail_send", "calendar_list"}, surface)
    offered = agent._offered_tool_names()
    assert offered == {"terminal", *PULL_TOOL_NAMES}
    assert "gmail_send" not in offered                     # exists, but not offered


def test_no_turn_surface_falls_back_to_construction_surface():
    agent = _agent({"terminal", "web_search"}, None)
    assert agent._offered_tool_names() == {"terminal", "web_search"}
    assert agent._callable_tool_names() == {"terminal", "web_search"}


def test_validator_uses_the_callable_set_and_runs_before_interception():
    # Wiring pin: the name check consults the callable set, and the pull
    # interception still sits AFTER it in the conversation loop.
    src = inspect.getsource(run_agent.AIAgent)
    check = src.index("if tc.function.name not in _callable_names")
    intercept = src.index("_intents = self._intercept_pull_intents(_intents, messages)")
    assert check < intercept
    assert "_callable_names = self._callable_tool_names()" in src
    assert 'available = ", ".join(sorted(self._offered_tool_names()))' in src

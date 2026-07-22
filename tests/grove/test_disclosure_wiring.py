"""Agent-loop wiring for the disclosure round-trip — Sprint 74 Phase 3.

Covers the stateful half the pure resolvers can't: the T2/T3 reduction of the
API surface (core + matched MCP + pull tools, non-core withheld to the index),
T1 staying eager-core (no pull), and the read_tool_schema interception splicing
a pulled schema into the live surface so the model can call it next step.
"""

from __future__ import annotations

import pytest

import run_agent
from grove.context_budget import ToolResolution
from grove.intents import ToolIntent
from grove.manifest import DisclosableUnit, UnitTrigger


def _tool(name, desc="d"):
    return {"type": "function", "function": {"name": name, "description": desc}}


def _bare_agent(tools):
    agent = object.__new__(run_agent.AIAgent)
    agent.tools = tools
    agent._tools_for_turn = None
    agent._disclosure_manifest = None
    return agent


class _Registry:
    """Minimal registry build_manifest can derive tool units from."""

    def __init__(self, names_descs):
        self._nd = names_descs

    def get_all_tool_names(self):
        return [n for n, _ in self._nd]

    def get_definitions(self, names, quiet=True):
        return [
            {"type": "function", "function": {"name": n, "description": d}}
            for n, d in self._nd
            if n in names
        ]


class _DispatcherHolder:
    def __init__(self, registry):
        self.registry = registry


def _res(tools):
    return ToolResolution(
        tools=tuple(tools),
        allowed_names=frozenset(),
        excluded_mcp=frozenset(),
        unparseable_mcp=(),
        fallback=False,
    )


# Real config (config/tool_groups.yaml, routing.config.yaml, manifest.yaml) is
# read by build_manifest; 'terminal'/'clarify' are core, 'web_search' is not.
REGISTRY = _Registry([
    ("terminal", "Run a shell command."),
    ("clarify", "Ask the operator a question."),
    ("web_search", "Search the web."),
    ("execute_code", "Execute a code snippet."),   # intent-gated exemplar (P2)
])


def _names(tools):
    return {t["function"]["name"] for t in (tools or [])}


def test_apply_disclosure_reduces_to_core_plus_pull_tools():
    agent = _bare_agent([])
    agent._dispatcher_singleton = _DispatcherHolder(REGISTRY)
    # The tier-resolved set on a T2 turn: core + a domain tool + a matched MCP.
    res = _res([
        _tool("terminal"), _tool("clarify"),       # core/baseline -> eager
        _tool("web_search"),                        # BASELINE (P2) -> eager, never demoted
        _tool("execute_code"),                      # intent-gated native -> withheld
        _tool("mcp_notion_API_post_page"),          # matched MCP -> eager
    ])
    reduced = agent._apply_disclosure(res)
    names = _names(reduced)
    assert "terminal" in names and "clarify" in names      # core/baseline eager
    assert "mcp_notion_API_post_page" in names             # matched MCP eager
    # retrieval-ambient-class-v1 P2: web_search rides the ambient baseline
    # class — eager at every tier, NEVER pull-demoted.
    assert "web_search" in names
    assert "execute_code" not in names                     # intent-gated -> withheld to index
    assert {"read_tool_schema", "read_goal_context"} <= names  # pull tools present
    # The merged manifest is stashed for the loop interception.
    assert agent._disclosure_manifest is not None
    # execute_code is pullable; eager units are omitted from the index.
    rts = next(t for t in reduced if t["function"]["name"] == "read_tool_schema")
    assert "execute_code" in rts["function"]["description"]
    assert "web_search" not in rts["function"]["description"]  # eager -> omitted
    assert "terminal" not in rts["function"]["description"]    # eager -> omitted


def test_apply_disclosure_no_registry_falls_back_to_eager():
    agent = _bare_agent([])
    agent._dispatcher_singleton = None  # no registry reachable
    res = _res([_tool("terminal"), _tool("web_search")])
    reduced = agent._apply_disclosure(res)
    assert _names(reduced) == {"terminal", "web_search"}    # eager, no disclosure
    assert agent._disclosure_manifest is None


# gateway-disclosure-trigger-v1: a derived native verb's domain-chunk intents
# make it eager on a matched-intent turn and withheld (pull-only) otherwise.
_WS_REGISTRY = _Registry([
    ("terminal", "Run a shell command."),       # core -> always eager
    ("calendar_list", "List calendar events."),  # baseline (workspace_read, P1)
    ("execute_code", "Execute a code snippet."),  # intent-gated -> pull-only off-intent
])


def test_apply_disclosure_eager_on_matched_intent():
    agent = _bare_agent([])
    agent._dispatcher_singleton = _DispatcherHolder(_WS_REGISTRY)
    res = _res([_tool("terminal"), _tool("calendar_list"), _tool("execute_code")])
    reduced = agent._apply_disclosure(res, intent_class="scheduling")
    names = _names(reduced)
    assert "terminal" in names                              # core eager
    # calendar_list rides the BASELINE class (workspace_read, P1) — eager on
    # every turn, scheduling included.
    assert "calendar_list" in names
    assert "execute_code" not in names                      # not a scheduling verb -> withheld
    # No double-exposure: eager verbs are omitted from the pull index.
    rts = next(t for t in reduced if t["function"]["name"] == "read_tool_schema")
    assert "calendar_list" not in rts["function"]["description"]
    assert "execute_code" in rts["function"]["description"]  # still pullable


def test_apply_disclosure_withheld_on_unmatched_intent():
    # JIT intact: a genuinely intent-gated verb stays pull-only on an unmatched
    # turn. NOTE (retrieval-ambient-class-v1 P2): web_search was the prior
    # example, but it joined the ambient BASELINE class (P1 ratification —
    # eager on every turn), so we assert withholding on execute_code instead:
    # intent-gated to code_generation/debugging/system_admin — NOT
    # conversation.
    agent = _bare_agent([])
    agent._dispatcher_singleton = _DispatcherHolder(_WS_REGISTRY)
    res = _res([_tool("terminal"), _tool("execute_code")])
    reduced = agent._apply_disclosure(res, intent_class="conversation")
    names = _names(reduced)
    assert "terminal" in names                              # core still eager
    assert "execute_code" not in names                      # withheld (intent-gated)
    rts = next(t for t in reduced if t["function"]["name"] == "read_tool_schema")
    assert "execute_code" in rts["function"]["description"]  # pullable


def test_intercept_read_tool_schema_splices_pulled_def():
    agent = _bare_agent([_tool("web_search", "Search the web.")])
    # Disclosure active: a manifest with a web_search tool unit.
    agent._disclosure_manifest = [
        DisclosableUnit(
            id="web_search", kind="tool", oneline="Search the web.",
            payload="tool_schema:web_search", tiers=("T2", "T3"),
            trigger=UnitTrigger((), (), None),
            disclosure_mode="eager",
        )
    ]
    agent._tools_for_turn = [_tool("read_tool_schema")]  # reduced surface
    messages = []
    intents = [ToolIntent(tool_name="read_tool_schema",
                          arguments={"id": "web_search"}, call_id="c1")]
    remaining = agent._intercept_pull_intents(intents, messages)
    # The pull tool is consumed (not passed to the executor).
    assert remaining == []
    # Its tool-result message is appended for the model to read.
    assert messages and messages[0]["tool_call_id"] == "c1"
    assert "web_search" in messages[0]["content"]
    # The pulled schema is spliced into the live surface for the next step.
    assert "web_search" in _names(agent._tools_for_turn)


def test_intercept_passes_non_pull_intents_through():
    agent = _bare_agent([_tool("terminal")])
    agent._disclosure_manifest = [
        DisclosableUnit(id="terminal", kind="tool", oneline="x",
                        payload="tool_schema:terminal", tiers=("T2",),
                        trigger=UnitTrigger((), (), None),
                        disclosure_mode="eager")
    ]
    agent._tools_for_turn = [_tool("read_tool_schema")]
    messages = []
    intents = [ToolIntent(tool_name="terminal", arguments={}, call_id="c9")]
    remaining = agent._intercept_pull_intents(intents, messages)
    assert [it.tool_name for it in remaining] == ["terminal"]
    assert messages == []  # nothing handled inline


def test_intercept_noop_when_disclosure_inactive():
    # T1 / no-manifest: every intent passes through; no pull handling.
    agent = _bare_agent([_tool("terminal")])
    agent._disclosure_manifest = None
    intents = [ToolIntent(tool_name="read_tool_schema",
                          arguments={"id": "web_search"}, call_id="c1")]
    messages = []
    remaining = agent._intercept_pull_intents(intents, messages)
    assert [it.tool_name for it in remaining] == ["read_tool_schema"]
    assert messages == []

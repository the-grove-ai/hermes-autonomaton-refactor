"""Pure resolver tests for grove.disclosure — Sprint 74 Phase 3.

The agent-pull round-trip resolves a disclosable unit id to its full payload:
read_tool_schema -> a tool's schema (native) or an MCP server's schemas, plus
the tool defs to add to the live API surface; read_goal_context -> a goal's
full record. These resolvers are pure (no agent, no live model) so the round-
trip's resolution half is unit-tested deterministically; the agent-loop
interception that consumes them is exercised separately.
"""

from __future__ import annotations

import json

from grove.disclosure import (
    PULL_TOOL_NAMES,
    build_pull_tool_defs,
    resolve_pull,
    resolve_goal_record,
)
from grove.manifest import DisclosableUnit, UnitTrigger


def _tool(name, desc="d"):
    return {"type": "function", "function": {"name": name, "description": desc}}


def _unit(uid, kind, **trig):
    return DisclosableUnit(
        id=uid,
        kind=kind,
        oneline=f"{uid} oneline",
        payload=f"{'tool_schema' if kind=='tool' else kind+'_schema' if kind=='mcp' else 'goal_record'}:{uid}",
        tiers=("T2", "T3"),
        trigger=UnitTrigger(
            intents=tuple(trig.get("intents", ())),
            keywords=tuple(trig.get("keywords", ())),
            dock_goal=trig.get("dock_goal"),
        ),
        # P2 (one disclose-on-match rule): a trigger-less synthetic unit maps
        # to the eager class (mirrors proactive-always core natives); units
        # WITH a trigger stay in the strict "triggered" default.
        disclosure_mode=(
            "triggered"
            if (trig.get("intents") or trig.get("keywords") or trig.get("dock_goal"))
            else "eager"
        ),
    )


MANIFEST = [
    _unit("terminal", "tool"),
    _unit("write_file", "tool"),
    _unit("notion", "mcp", intents=("research",)),
    _unit("humanity-ai", "goal", keywords=("funding",), dock_goal="humanity-ai"),
]

REGISTRY_TOOLS = [
    _tool("terminal", "Run a shell command."),
    _tool("write_file", "Write a file."),
    _tool("mcp_notion_API_post_page", "Notion write."),
    _tool("mcp_notion_API_post_search", "Notion read."),
    _tool("mcp_other_thing", "Some other MCP."),
]


# ── read_tool_schema resolution ──────────────────────────────────────────

def test_resolve_pull_native_tool_returns_schema_and_def():
    text, defs = resolve_pull(MANIFEST, REGISTRY_TOOLS, "terminal")
    payload = json.loads(text)
    assert payload["id"] == "terminal" and payload["kind"] == "tool"
    assert payload["schema"]["function"]["name"] == "terminal"
    # The def to add to the live surface is the real registry schema.
    assert len(defs) == 1 and defs[0]["function"]["name"] == "terminal"


def test_resolve_pull_mcp_server_returns_all_its_schemas():
    text, defs = resolve_pull(MANIFEST, REGISTRY_TOOLS, "notion")
    payload = json.loads(text)
    assert payload["kind"] == "mcp"
    names = {d["function"]["name"] for d in defs}
    assert names == {"mcp_notion_API_post_page", "mcp_notion_API_post_search"}
    assert "mcp_other_thing" not in names  # a different server


def test_resolve_pull_unknown_id_is_loud_no_defs():
    text, defs = resolve_pull(MANIFEST, REGISTRY_TOOLS, "does_not_exist")
    assert "error" in json.loads(text)
    assert defs == []


def test_resolve_pull_goal_unit_not_pullable_as_schema():
    # A goal unit is fetched via read_goal_context, not read_tool_schema.
    text, defs = resolve_pull(MANIFEST, REGISTRY_TOOLS, "humanity-ai")
    assert "error" in json.loads(text)
    assert defs == []


def test_resolve_pull_mcp_with_no_connected_tools_is_loud():
    # notion declared in manifest, but no mcp_notion_* tools in the registry.
    text, defs = resolve_pull(MANIFEST, [_tool("terminal")], "notion")
    assert "error" in json.loads(text)
    assert defs == []


# ── read_goal_context resolution ─────────────────────────────────────────

class _FakeGoal:
    def __init__(self, gid):
        self.id = gid
        self.name = "Humanity AI"
        self.context_sources = ()

    def resolved_sources(self):
        return []


class _FakeDock:
    goals = (_FakeGoal("humanity-ai"),)
    context_char_budget = 5000


def test_resolve_goal_record_returns_record():
    text = resolve_goal_record("humanity-ai", dock=_FakeDock())
    payload = json.loads(text)
    assert payload["id"] == "humanity-ai"
    assert payload["name"] == "Humanity AI"
    assert "record" in payload


def test_resolve_goal_record_unknown_goal_is_loud():
    text = resolve_goal_record("nope", dock=_FakeDock())
    assert "error" in json.loads(text)


def test_resolve_goal_record_no_dock_is_loud():
    text = resolve_goal_record("humanity-ai", dock=None, allow_load=False)
    assert "error" in json.loads(text)


# ── pull tool defs (the embedded index) ──────────────────────────────────

def test_build_pull_tool_defs_embeds_index_and_omits_eager():
    defs = build_pull_tool_defs(MANIFEST, eager_names={"terminal"})
    names = {d["function"]["name"] for d in defs}
    assert names == set(PULL_TOOL_NAMES)
    rts = next(d for d in defs if d["function"]["name"] == "read_tool_schema")
    desc = rts["function"]["description"]
    assert "write_file" in desc        # pullable native tool listed
    assert "notion" in desc            # pullable MCP server listed
    assert "terminal" not in desc      # eager -> omitted from the pull index
    # read_tool_schema takes an id; read_goal_context takes a goal_id.
    assert rts["function"]["parameters"]["required"] == ["id"]
    rgc = next(d for d in defs if d["function"]["name"] == "read_goal_context")
    assert "humanity-ai" in rgc["function"]["description"]   # goal indexed
    assert rgc["function"]["parameters"]["required"] == ["goal_id"]

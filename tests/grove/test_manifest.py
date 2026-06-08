"""Unit tests for grove.manifest — Sprint 74 context-jit-disclosure-v1.

Phase 1: the disclosable-unit index — frozen dataclasses with fail-loud cap
validation, plus a YAML loader/validator that mirrors grove.tier_budget's
discipline (every malformed entry raises ValueError at load). No wiring;
manifest.py is import-only until Phase 2. All tests are hermetic: an explicit
config_path (tmp file) so neither ~/.grove nor the repo template is touched.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from grove.manifest import (
    ONELINE_CAP,
    MAX_KEYWORDS,
    VALID_KINDS,
    DisclosableUnit,
    UnitTrigger,
    load_manifest,
    matched_mcp_servers,
)


# A minimal valid manifest exercising all three live kinds: a tool unit
# (no triggers — native selection stays in tool_groups.yaml, the ADDITIVE
# principle), an MCP unit (the NEW disclose-on-match trigger map), and a
# goal unit (dock_goal pointer). Payloads are POINTERS, never inlined schema.
VALID_MANIFEST = {
    "version": 1,
    "units": [
        {
            "id": "terminal",
            "kind": "tool",
            "oneline": "Run a shell command in the operator's terminal.",
            "payload": "tool_schema:terminal",
            "tiers": ["T1", "T2", "T3"],
            "trigger": {"intents": [], "keywords": [], "dock_goal": None},
        },
        {
            "id": "notion",
            "kind": "mcp",
            "oneline": "Notion workspace: search, read, write pages and databases.",
            "payload": "mcp_schema:notion",
            "tiers": ["T2", "T3"],
            "trigger": {
                "intents": ["research", "retrieval"],
                "keywords": ["notion", "page", "database", "workspace"],
                "dock_goal": None,
            },
        },
        {
            "id": "humanity-ai-funding",
            "kind": "goal",
            "oneline": "Apex goal: secure humanity-AI alignment funding.",
            "payload": "goal_record:humanity-ai-funding",
            "tiers": ["T2", "T3"],
            "trigger": {
                "intents": [],
                "keywords": ["humanity", "alignment", "funding"],
                "dock_goal": "humanity-ai-funding",
            },
        },
    ],
}


def _write(tmp_path, data) -> "object":
    """Write a manifest dict to a tmp YAML file; return its Path."""
    p = tmp_path / "manifest.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


# ── 1. caps fail loud at load ────────────────────────────────────────────

def test_oneline_over_cap_fails_loud(tmp_path):
    data = copy.deepcopy(VALID_MANIFEST)
    data["units"][0]["oneline"] = "x" * (ONELINE_CAP + 1)
    with pytest.raises(ValueError, match="oneline"):
        load_manifest(_write(tmp_path, data))


def test_empty_oneline_fails_loud(tmp_path):
    data = copy.deepcopy(VALID_MANIFEST)
    data["units"][0]["oneline"] = "   "
    with pytest.raises(ValueError, match="oneline"):
        load_manifest(_write(tmp_path, data))


def test_too_many_keywords_fails_loud(tmp_path):
    data = copy.deepcopy(VALID_MANIFEST)
    data["units"][1]["trigger"]["keywords"] = [f"k{i}" for i in range(MAX_KEYWORDS + 1)]
    with pytest.raises(ValueError, match="keyword"):
        load_manifest(_write(tmp_path, data))


def test_no_eligible_tier_fails_loud(tmp_path):
    data = copy.deepcopy(VALID_MANIFEST)
    data["units"][0]["tiers"] = []
    with pytest.raises(ValueError, match="tier"):
        load_manifest(_write(tmp_path, data))


def test_unknown_kind_fails_loud(tmp_path):
    data = copy.deepcopy(VALID_MANIFEST)
    data["units"][0]["kind"] = "gadget"
    with pytest.raises(ValueError, match="kind"):
        load_manifest(_write(tmp_path, data))


def test_dataclass_post_init_enforces_oneline_cap():
    """The cap is on the dataclass itself, not only the loader — a unit can
    never exist over-cap regardless of how it is constructed."""
    with pytest.raises(ValueError, match="oneline"):
        DisclosableUnit(
            id="x",
            kind="tool",
            oneline="y" * (ONELINE_CAP + 1),
            payload="tool_schema:x",
            tiers=("T1",),
            trigger=UnitTrigger(intents=(), keywords=(), dock_goal=None),
        )


def test_trigger_post_init_enforces_keyword_cap():
    with pytest.raises(ValueError, match="keyword"):
        UnitTrigger(
            intents=(),
            keywords=tuple(f"k{i}" for i in range(MAX_KEYWORDS + 1)),
            dock_goal=None,
        )


# ── 2. a valid manifest round-trips ──────────────────────────────────────

def test_valid_manifest_round_trips(tmp_path):
    units = load_manifest(_write(tmp_path, VALID_MANIFEST))
    assert len(units) == 3
    by_id = {u.id: u for u in units}

    term = by_id["terminal"]
    assert term.kind == "tool"
    assert term.oneline.startswith("Run a shell command")
    assert term.payload == "tool_schema:terminal"
    assert term.tiers == ("T1", "T2", "T3")
    # Tool unit carries NO triggers — native selection stays in tool_groups.yaml.
    assert term.trigger.intents == ()
    assert term.trigger.keywords == ()
    assert term.trigger.dock_goal is None

    notion = by_id["notion"]
    assert notion.kind == "mcp"
    assert notion.trigger.intents == ("research", "retrieval")
    assert "notion" in notion.trigger.keywords

    goal = by_id["humanity-ai-funding"]
    assert goal.kind == "goal"
    assert goal.trigger.dock_goal == "humanity-ai-funding"
    # Every kind we ship is a recognized kind.
    assert {u.kind for u in units} <= VALID_KINDS


def test_units_are_frozen(tmp_path):
    units = load_manifest(_write(tmp_path, VALID_MANIFEST))
    with pytest.raises(Exception):
        units[0].oneline = "mutated"  # frozen dataclass


# ── 3. payload pointers do not carry inlined schema text ─────────────────

def test_payload_is_a_pointer_not_inlined_schema(tmp_path):
    units = load_manifest(_write(tmp_path, VALID_MANIFEST))
    for u in units:
        # A pointer is "<namespace>:<key>" — short, single-line, no JSON.
        assert ":" in u.payload, u.payload
        assert "{" not in u.payload and "}" not in u.payload, u.payload
        assert "\n" not in u.payload, u.payload
        assert len(u.payload) <= 120, u.payload


def test_inlined_schema_payload_fails_loud(tmp_path):
    """A payload that smuggles a JSON schema instead of a pointer is rejected
    at load — the index must never carry the heavy payload it points at."""
    data = copy.deepcopy(VALID_MANIFEST)
    data["units"][0]["payload"] = '{"type": "function", "function": {"name": "terminal"}}'
    with pytest.raises(ValueError, match="payload"):
        load_manifest(_write(tmp_path, data))


# ── Phase 2: untriggered-MCP policy — fail loud at load ──────────────────

def test_mcp_unit_without_any_trigger_fails_loud(tmp_path):
    """An mcp unit with no intents, no keywords, and no dock_goal can never
    disclose — under disclose-on-match it would silently vanish. Declarative
    discipline: adding a connector = a manifest entry WITH its trigger."""
    data = copy.deepcopy(VALID_MANIFEST)
    data["units"][1]["trigger"] = {"intents": [], "keywords": [], "dock_goal": None}
    with pytest.raises(ValueError, match="trigger"):
        load_manifest(_write(tmp_path, data))


def test_mcp_unit_without_trigger_fails_at_dataclass():
    """The invariant holds on the dataclass itself, not only the loader."""
    with pytest.raises(ValueError, match="trigger"):
        DisclosableUnit(
            id="ghost",
            kind="mcp",
            oneline="A server nobody can reach.",
            payload="mcp_schema:ghost",
            tiers=("T3",),
            trigger=UnitTrigger(intents=(), keywords=(), dock_goal=None),
        )


def test_tool_unit_with_empty_trigger_is_fine():
    """Tool units carry NO trigger by design — native selection owns them.
    The untriggered guard must not fire for kind=='tool'."""
    u = DisclosableUnit(
        id="terminal",
        kind="tool",
        oneline="Run a shell command.",
        payload="tool_schema:terminal",
        tiers=("T1",),
        trigger=UnitTrigger(intents=(), keywords=(), dock_goal=None),
    )
    assert u.trigger.keywords == ()


# ── Phase 2: the pure MCP matcher ────────────────────────────────────────

def _mcp_unit(uid, intents=(), keywords=(), dock_goal=None):
    return DisclosableUnit(
        id=uid,
        kind="mcp",
        oneline=f"{uid} server.",
        payload=f"mcp_schema:{uid}",
        tiers=("T2", "T3"),
        trigger=UnitTrigger(
            intents=tuple(intents), keywords=tuple(keywords), dock_goal=dock_goal
        ),
    )


def test_matched_on_intent():
    units = [_mcp_unit("notion", intents=("research",), keywords=("notion",))]
    assert matched_mcp_servers(
        units, intent_class="research", message="anything"
    ) == frozenset({"notion"})


def test_matched_on_keyword_substring():
    units = [_mcp_unit("notion", intents=("research",), keywords=("notion", "page"))]
    assert matched_mcp_servers(
        units, intent_class="code_generation", message="update the Notion page"
    ) == frozenset({"notion"})


def test_no_match_returns_empty():
    units = [_mcp_unit("notion", intents=("research",), keywords=("notion",))]
    assert matched_mcp_servers(
        units, intent_class="code_generation", message="fix the failing test"
    ) == frozenset()


def test_matched_on_dock_goal():
    units = [_mcp_unit("airtable", keywords=("__nomatch__",), dock_goal="grv-001")]
    assert matched_mcp_servers(
        units, intent_class=None, message="x", resolved_goal_id="grv-001"
    ) == frozenset({"airtable"})
    assert matched_mcp_servers(
        units, intent_class=None, message="x", resolved_goal_id="other-goal"
    ) == frozenset()


def test_matcher_ignores_non_mcp_units():
    tool = DisclosableUnit(
        id="terminal", kind="tool", oneline="run", payload="tool_schema:terminal",
        tiers=("T1",), trigger=UnitTrigger((), (), None),
    )
    units = [tool, _mcp_unit("notion", keywords=("notion",))]
    assert matched_mcp_servers(
        units, intent_class=None, message="open notion"
    ) == frozenset({"notion"})


# ── Phase 3: build_manifest — derive tool units + merge declarative ──────

from grove.manifest import build_manifest
from grove.tier_budget import TierBudget, ToolBudget


def _fake_registry(*tool_descs):
    """A minimal registry stand-in exposing the two methods build_manifest uses."""
    defs = [
        {"type": "function", "function": {"name": n, "description": d}}
        for n, d in tool_descs
    ]

    class _Reg:
        def get_all_tool_names(self):
            return [n for n, _ in tool_descs]

        def get_definitions(self, names, quiet=True):
            return [x for x in defs if x["function"]["name"] in names]

    return _Reg()


DERIVE_TAXONOMY = {
    "version": 1,
    "core": ["terminal", "clarify"],
    "domain_chunks": {"code_generation": ["write_file"], "research": ["web_search"]},
    "exploratory": ["delegate_task"],
}


def _tb(allow):
    return TierBudget(context=(), tools=ToolBudget(allow_groups=tuple(allow), exclude_mcp=()))


# T1 = core only; T2 = core + code_generation; T3 = wildcard (all groups).
FAKE_BUDGETS = {"T1": _tb(["core"]), "T2": _tb(["core", "code_generation"]), "T3": _tb(["*"])}


def _mcp_only_yaml():
    return {
        "version": 1,
        "units": [
            {
                "id": "notion",
                "kind": "mcp",
                "oneline": "Notion workspace.",
                "payload": "mcp_schema:notion",
                "tiers": ["T2", "T3"],
                "trigger": {"intents": ["research"], "keywords": ["notion"], "dock_goal": None},
            }
        ],
    }


def test_build_manifest_derives_tool_units(tmp_path):
    reg = _fake_registry(
        ("terminal", "Run a shell command in the operator's terminal."),
        ("web_search", "Search the web for current information."),
    )
    units = build_manifest(
        reg,
        taxonomy=DERIVE_TAXONOMY,
        tier_budgets=FAKE_BUDGETS,
        manifest_path=_write(tmp_path, _mcp_only_yaml()),
    )
    by = {u.id: u for u in units}
    # Tool units are DERIVED from the registry — oneline, pointer, tiers.
    assert by["terminal"].kind == "tool"
    assert by["terminal"].payload == "tool_schema:terminal"
    assert by["terminal"].oneline.startswith("Run a shell command")
    assert by["terminal"].trigger.intents == ()          # native — no trigger
    assert by["terminal"].tiers == ("T1", "T2", "T3")     # core: every tier
    assert by["web_search"].tiers == ("T3",)              # research only in T3 wildcard
    # The declarative MCP unit is merged in unchanged.
    assert by["notion"].kind == "mcp"
    assert by["notion"].trigger.intents == ("research",)


def test_build_manifest_oneline_capped_from_long_description(tmp_path):
    reg = _fake_registry(("terminal", "X" * 400))
    units = build_manifest(
        reg, taxonomy=DERIVE_TAXONOMY, tier_budgets=FAKE_BUDGETS,
        manifest_path=_write(tmp_path, _mcp_only_yaml()),
    )
    term = {u.id: u for u in units}["terminal"]
    assert len(term.oneline) <= ONELINE_CAP


def test_build_manifest_collision_derived_vs_yaml_fails_loud(tmp_path):
    # A YAML unit whose id collides with a derived tool id -> fail loud.
    reg = _fake_registry(("notion", "A native tool that happens to be named notion."))
    with pytest.raises(ValueError, match="collision|collid|duplicate"):
        build_manifest(
            reg, taxonomy=DERIVE_TAXONOMY, tier_budgets=FAKE_BUDGETS,
            manifest_path=_write(tmp_path, _mcp_only_yaml()),
        )


def test_build_manifest_duplicate_within_yaml_fails_loud(tmp_path):
    reg = _fake_registry(("terminal", "Run shell."))
    data = _mcp_only_yaml()
    data["units"].append(dict(data["units"][0]))  # duplicate notion id
    with pytest.raises(ValueError, match="collision|collid|duplicate"):
        build_manifest(
            reg, taxonomy=DERIVE_TAXONOMY, tier_budgets=FAKE_BUDGETS,
            manifest_path=_write(tmp_path, data),
        )


def test_build_manifest_ids_globally_unique_across_kinds(tmp_path):
    reg = _fake_registry(("terminal", "Run shell."), ("write_file", "Write a file."))
    units = build_manifest(
        reg, taxonomy=DERIVE_TAXONOMY, tier_budgets=FAKE_BUDGETS,
        manifest_path=_write(tmp_path, _mcp_only_yaml()),
    )
    ids = [u.id for u in units]
    assert len(ids) == len(set(ids))           # no dup ids across derived + declarative
    assert {u.kind for u in units} == {"tool", "mcp"}


# ── 4. the committed repo manifest is itself valid ───────────────────────

def test_repo_yaml_loads_and_holds_only_declarative_units():
    """The shipped config/manifest.yaml is the DECLARATIVE half only after
    Phase 3: mcp/goal/contract units, NO derived tool units (those come from
    build_manifest at load)."""
    from pathlib import Path
    import grove.manifest as m

    repo_manifest = Path(m.__file__).resolve().parents[1] / "config" / "manifest.yaml"
    units = load_manifest(repo_manifest)
    assert units, "committed manifest is empty"
    assert all(u.oneline and len(u.oneline) <= ONELINE_CAP for u in units)
    assert all(u.tiers for u in units)
    assert all(":" in u.payload and "{" not in u.payload for u in units)
    kinds = {u.kind for u in units}
    assert "tool" not in kinds                 # tool units are derived, not in YAML
    assert {"mcp", "goal"} <= kinds


def test_repo_build_manifest_merges_derived_tools_with_declarative():
    """build_manifest against the real registry yields the FULL merged index:
    derived tool units + the declarative mcp/goal units, all ids unique."""
    import os
    os.environ.setdefault("GROVE_HOME", os.path.expanduser("~/.grove"))
    from tools.registry import ToolRegistry, register_builtin_tools

    reg = ToolRegistry()
    register_builtin_tools(reg)
    units = build_manifest(reg)
    kinds = {u.kind for u in units}
    assert {"tool", "mcp", "goal"} <= kinds
    ids = [u.id for u in units]
    assert len(ids) == len(set(ids))
    assert all(len(u.oneline) <= ONELINE_CAP for u in units)
    # Tool units point at tool_schema:<name>; never an inlined schema.
    tool_units = [u for u in units if u.kind == "tool"]
    assert tool_units and all(u.payload.startswith("tool_schema:") for u in tool_units)

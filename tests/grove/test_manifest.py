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
            # P2 (one disclose-on-match rule): a trigger-less tool unit must
            # declare its eager class (proactive-always core exemplar).
            "disclosure_mode": "eager",
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
    """retrieval-ambient-class-v1 P2 (one disclose-on-match rule): the
    kind-based native exemption is RETIRED. An EAGER-class tool unit
    (baseline / proactive-always) legitimately carries no trigger; a
    triggered-class tool unit with no trigger fails loud, same as MCP."""
    u = DisclosableUnit(
        id="terminal",
        kind="tool",
        oneline="Run a shell command.",
        payload="tool_schema:terminal",
        tiers=("T1",),
        trigger=UnitTrigger(intents=(), keywords=(), dock_goal=None),
        disclosure_mode="eager",
    )
    import pytest as _pytest
    with _pytest.raises(ValueError, match="triggered unit with no trigger"):
        DisclosableUnit(
            id="terminal2",
            kind="tool",
            oneline="Run a shell command.",
            payload="tool_schema:terminal2",
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


# GRV-009 E4 C4 — the manifest MCP-matching tests (matched_mcp_servers) are
# retired with the function: per-turn MCP disclose-on-match moved onto the
# kind=mcp Capability records and is covered by tests/grove/test_mcp_gating_parity.py
# (run_agent._compute_mcp_allow / _mcp_trigger_reason).



# ── Phase 3: build_manifest — RETIRED (GRV-009 E5b C2) ──────────────────
# build_manifest + matched_tool_units + _tiers_for_tool are gone; native
# disclosure is registry-driven (grove.disclosure.build_disclosure_units +
# disclosure_split_sets), proven in tests/grove/test_disclosure_split_records.py.
# The declarative loader (load_manifest) + DisclosableUnit/UnitTrigger tests
# above remain — they cover the surviving half of the module.

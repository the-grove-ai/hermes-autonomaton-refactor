"""Integration tests for the additive Workspace Capability hook + skill
retirement (GRV-009 E2 C3).

Covers: the hook resolves a Workspace-shaped turn through the committed records
(provenance + eager guidance) on the shared per-turn method both entrypoints
drive; a non-Workspace turn is byte-identical (no-op); the registry tool object
is never mutated across turns; the google-workspace skill is gone from every
scanned path while its scripts/verbs stay intact; and zone routing is unchanged
(yellow still gates through tool_zones).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import yaml

import run_agent
from agent.skill_utils import iter_skill_index_files

REPO = Path(__file__).resolve().parents[2]
WS_DIR = REPO / "skills" / "productivity" / "google-workspace"
WORKSPACE_IDS = ["workspace_destructive", "workspace_read", "workspace_write"]  # sorted


def _tool(name, desc="d"):
    return {"type": "function", "function": {"name": name, "description": desc}}


class _Registry:
    """Minimal registry exposing the google-workspace toolset names."""

    def __init__(self, names):
        self._names = list(names)

    def get_tool_names_for_toolset(self, toolset):
        return list(self._names) if toolset == "google-workspace" else []


class _DispatcherHolder:
    def __init__(self, registry):
        self.registry = registry


def _ws_agent(tools):
    agent = object.__new__(run_agent.AIAgent)
    agent._tools_for_turn = tools
    agent._dispatcher_singleton = _DispatcherHolder(
        _Registry(["gmail_search", "gmail_send", "calendar_list", "drive_search"])
    )
    return agent


# ── The hook resolves a Workspace turn through the records ────────────────────


def test_hook_resolves_through_records_on_workspace_intent():
    carrier = _tool("gmail_search", "Search Gmail; returns matching messages.")
    surface = [_tool("terminal"), carrier, _tool("calendar_list")]
    agent = _ws_agent(surface)

    agent._apply_capability_hook("scheduling")

    # Provenance: all three Workspace records govern the turn.
    assert agent._capability_records_applied == WORKSPACE_IDS
    # Eager guidance composed from the records' payloads.
    assert "GRV-009" in agent._capability_guidance
    assert "workspace_read" in agent._capability_guidance
    assert "Never execute" in agent._capability_guidance or "approval" in agent._capability_guidance
    # Attached to a Workspace-verb carrier in the live surface.
    g = agent._tools_for_turn[1]["function"]["description"]
    assert g.startswith("Google Workspace is delivered through Capability records")
    assert "Search Gmail" in g  # original description preserved after the guidance


def test_hook_noop_outside_workspace_intents():
    surface = [_tool("terminal"), _tool("web_search")]
    before = [dict(t["function"]) for t in surface]
    agent = _ws_agent(surface)

    agent._apply_capability_hook("conversation")

    assert agent._capability_records_applied == []
    assert agent._capability_guidance is None
    # byte-identical: nothing in the surface was touched
    after = [dict(t["function"]) for t in agent._tools_for_turn]
    assert after == before


def test_hook_does_not_mutate_shared_registry_tool_object():
    # The carrier dict is COPIED — the original tool object keeps its description,
    # so guidance never bleeds across turns/sessions.
    carrier = _tool("gmail_search", "Search Gmail.")
    original_fn = carrier["function"]
    agent = _ws_agent([carrier])

    agent._apply_capability_hook("messaging")

    assert original_fn["description"] == "Search Gmail."         # untouched original
    assert agent._tools_for_turn[0]["function"] is not original_fn  # replaced with a copy
    assert "GRV-009" in agent._tools_for_turn[0]["function"]["description"]


def test_hook_idempotent_when_guidance_already_present():
    carrier = _tool("gmail_search", "Search Gmail.")
    agent = _ws_agent([carrier])
    agent._apply_capability_hook("scheduling")
    once = agent._tools_for_turn[0]["function"]["description"]
    # Re-run on the now-augmented surface — guidance is not stacked twice.
    agent._apply_capability_hook("scheduling")
    twice = agent._tools_for_turn[0]["function"]["description"]
    assert once == twice
    assert twice.count("delivered through Capability records") == 1


# ── Wired into the shared per-turn method both entrypoints drive ──────────────


def test_hook_wired_into_shared_per_turn_method():
    src = inspect.getsource(run_agent.AIAgent._maybe_apply_tool_filter)
    assert "self._apply_capability_hook(intent_class)" in src
    # Both entrypoints drive _run_turn_generator -> _maybe_apply_tool_filter;
    # the gateway path is grove/dispatcher.py calling agent._run_turn_generator.
    disp = (REPO / "grove" / "dispatcher.py").read_text(encoding="utf-8")
    assert "agent._run_turn_generator(" in disp


# ── GRV-009 spike C1 — hook outcome observability ─────────────────────────────


def test_hook_stamps_outcome_when_payload_attaches():
    carrier = _tool("gmail_search", "Search Gmail.")
    surface = [_tool("terminal"), carrier, _tool("calendar_list")]
    agent = _ws_agent(surface)
    agent._last_tool_selection = {}

    agent._apply_capability_hook("scheduling")

    sel = agent._last_tool_selection
    assert sel["capability_hook_fired"] is True
    assert sel["capability_records_applied"] == WORKSPACE_IDS
    assert sel["capability_payload_attached"] is True
    # Both Workspace verbs on the surface are reported as carrier candidates.
    assert sel["capability_carrier_verbs_present"] == ["calendar_list", "gmail_search"]


def test_hook_stamps_admission_gate_signature_when_no_verbs_on_surface():
    # The spike's root-cause shape: a Workspace intent fires but the platform
    # admission gate left ZERO Workspace verbs on the surface — fired, yet
    # nothing to carry. This is the telemetry signature that would have named
    # the bug on turn one.
    surface = [_tool("terminal"), _tool("read_file")]
    agent = _ws_agent(surface)
    agent._last_tool_selection = {}

    agent._apply_capability_hook("scheduling")

    sel = agent._last_tool_selection
    assert sel["capability_hook_fired"] is True
    assert sel["capability_carrier_verbs_present"] == []   # admission gate dropped them
    assert sel["capability_payload_attached"] is False


def test_hook_stamps_outcome_non_workspace_intent():
    agent = _ws_agent([_tool("terminal"), _tool("web_search")])
    agent._last_tool_selection = {}

    agent._apply_capability_hook("conversation")

    sel = agent._last_tool_selection
    assert sel["capability_hook_fired"] is False
    assert sel["capability_records_applied"] == []
    assert sel["capability_payload_attached"] is False
    assert sel["capability_carrier_verbs_present"] == []


def test_hook_outcome_stamp_tolerates_absent_selection():
    # The direct/CLI path may invoke the hook before any tool_selection dict
    # exists; the stamp must log-only, never raise. (Residual CLI question.)
    agent = _ws_agent([_tool("gmail_search")])
    # deliberately no _last_tool_selection attribute
    agent._apply_capability_hook("messaging")  # must not raise
    # fallback-retirement-v1: discord migrated to proactive+intents=[messaging], so
    # it now co-governs the messaging intent alongside the Workspace records.
    assert agent._capability_records_applied == sorted(WORKSPACE_IDS + ["discord"])


# ── Skill retirement: gone from every scanned path, verbs/scripts intact ──────


def test_skill_retired_from_every_scanned_path():
    skills_root = REPO / "skills"
    indexed = [str(p) for p in iter_skill_index_files(skills_root, "SKILL.md")
               if "google-workspace" in str(p)]
    assert indexed == []  # gone from the prompt index AND skills_list (same iterator)


def test_skill_relocated_not_deleted_and_scripts_intact():
    assert (WS_DIR / ".archive" / "SKILL.md").is_file()        # nothing deleted
    assert (WS_DIR / ".archive" / "references").is_dir()        # markdown refs archived
    assert (WS_DIR / "scripts" / "google_api.py").is_file()     # verb execution path intact
    assert not (WS_DIR / "SKILL.md").exists()                   # no longer at the scanned path


# ── Zone routing unchanged (parity) ──────────────────────────────────────────


def test_workspace_zone_parity_unchanged():
    schema = yaml.safe_load((REPO / "config" / "zones.schema.yaml").read_text(encoding="utf-8"))
    tz = schema["tool_zones"]
    # Reads still green, mutations still yellow — the records mirror this; the
    # hook does not re-enforce zones, so yellow still routes through approval.
    assert tz["gmail_search"] == "green"
    assert tz["gmail_send"] == "yellow"
    assert tz["drive_delete"] == "yellow"

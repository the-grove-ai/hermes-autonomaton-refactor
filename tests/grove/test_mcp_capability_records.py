"""GRV-009 E4 C1 — MCP Capability records (notion_read, notion_write).

Asserts the two records load through the Amendment-A3 dry-run and that their
fields match the GATE-A lock exactly. Nothing consumes them yet (C2 wires the
gating); this commit only proves the records are valid and faithful.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grove.capability import CapabilityKind, LifecycleState, Provenance, Zone
from grove.capability_registry import load_capabilities

REPO = Path(__file__).resolve().parents[2]
CAPS = REPO / "config" / "capabilities"

# The GATE-A locked trigger (strict parity with config/manifest.yaml notion unit).
LOCKED_INTENTS = ["research", "retrieval"]
LOCKED_KEYWORDS = ["notion", "page", "database", "workspace", "doc"]


@pytest.fixture(scope="module")
def caps():
    return load_capabilities(CAPS)


def test_both_mcp_records_load_via_dry_run(caps):
    # The whole directory dry-run-validates (workspace + notion) with no
    # duplicate id and no field error — a single bad record would have raised.
    assert {"notion_read", "notion_write"} <= set(caps)


@pytest.mark.parametrize("rid", ["notion_read", "notion_write"])
def test_record_is_mcp_kind_with_locked_trigger_and_tier(caps, rid):
    c = caps[rid]
    assert c.kind == CapabilityKind.MCP
    # Mapping (no A2): per-turn allow -> intents + keywords; dock clause ->
    # dock_affinity (empty, mirroring manifest dock_goal: null).
    assert c.trigger.intents == LOCKED_INTENTS
    assert c.trigger.keywords == LOCKED_KEYWORDS
    assert c.trigger.dock_affinity == []
    # exclude_mcp ceiling -> tier_rule.eligible: T3 only (T1+T2 stay MCP-free).
    assert c.tier_rule.eligible == [3]
    assert c.tier_rule.preferred == 3
    # Migrated capability entered at the operator-locked gate.
    assert c.lifecycle.state == LifecycleState.APPROVED
    assert c.lifecycle.provenance == Provenance.MIGRATED
    assert c.telemetry.feed  # non-empty binding
    # GRV-009 E4 C4 — eager-on-match is the live semantics after index retirement.
    assert c.context.disclosure.value == "eager"
    # Server binding the C3 attribution map parses.
    assert c.context.payload.startswith("mcp_schema:notion")


def test_read_record_is_green_write_record_is_yellow(caps):
    # Strict zone parity with zones.schema.yaml::tool_zones.
    assert caps["notion_read"].zone == Zone.GREEN
    assert caps["notion_write"].zone == Zone.YELLOW


def test_records_are_the_sole_mcp_gate_after_c4(caps):
    # C1 wrote this as a "nothing consumes them yet" guardrail; C2/C4 flipped
    # authority to the registry and retired the legacy keys. The exclude_mcp
    # field is gone from the tier budget, and the registry gating helpers exist.
    from grove.tier_budget import ToolBudget
    import run_agent
    assert "exclude_mcp" not in ToolBudget.__dataclass_fields__
    assert hasattr(run_agent.AIAgent, "_compute_mcp_allow")
    assert hasattr(run_agent.AIAgent, "_mcp_server_of_record")

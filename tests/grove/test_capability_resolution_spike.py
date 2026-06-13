"""Regression test for capability-resolution-spike-v1 (GRV-009 spike C2).

Pins the platform-admission fix the spike's GATE-A trace isolated: a
``scheduling``/T1 turn was delivered a Workspace-less surface because
``google-workspace`` was absent from ``CONFIGURABLE_TOOLSETS`` and therefore
from the gateway's per-platform toolset resolution — the verbs never entered
``self.tools``, upstream of the per-turn trimmer and the E2 capability hook.

Static config assertions alone are insufficient (Gemini flag 4): this pins the
CONSTRUCTED surface (``enabled_toolsets`` / ``self.tools`` carry the verbs) AND
the per-turn resolution (the trimmer delivers ``calendar_list`` and the hook
attaches the capability payload) — on BOTH entrypoints (telegram = gateway,
cli = CLI/direct).

The OAuth ``check_fn`` (``_workspace_check``) is forced True so the verbs
register: the gate is orthogonal to admission, which is what regressed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINTS = ["telegram", "cli"]  # gateway + CLI/direct
SAMPLE_VERBS = {"calendar_list", "gmail_search", "drive_search", "calendar_create"}


def _registry_with_workspace(monkeypatch):
    """A real registry where the Workspace toolset is registered (gate forced
    ON). Patched BEFORE register() so the bound ``check_fn`` is the True stub;
    the check_fn TTL cache is cleared so ``get_definitions`` re-evaluates it."""
    monkeypatch.setattr("tools.google_workspace_tool._workspace_check", lambda: True)
    from tools.registry import (
        ToolRegistry,
        register_builtin_tools,
        invalidate_check_fn_cache,
    )

    reg = ToolRegistry()
    register_builtin_tools(reg)
    invalidate_check_fn_cache()
    return reg


def _platform_surface(reg, platform):
    """Mirror the gateway/CLI construction path: per-platform enabled_toolsets
    → ``get_tool_definitions`` → the agent's ``self.tools``."""
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import get_tool_definitions

    enabled = sorted(_get_platform_tools({}, platform))
    tools = get_tool_definitions(
        reg, enabled_toolsets=enabled, disabled_toolsets=[], quiet_mode=True
    )
    return enabled, tools


# ── 1. Constructed surface carries the Workspace verbs (both entrypoints) ──────


@pytest.mark.parametrize("platform", ENTRYPOINTS)
def test_workspace_admitted_to_constructed_surface(monkeypatch, platform):
    reg = _registry_with_workspace(monkeypatch)
    enabled, tools = _platform_surface(reg, platform)

    # enabled_toolsets carries the toolset (the admission fix) ...
    assert "google-workspace" in enabled, (
        f"google-workspace not admitted on {platform}: {sorted(enabled)}"
    )
    # ... and the verbs reach self.tools (the construction surface).
    names = {t["function"]["name"] for t in tools}
    assert SAMPLE_VERBS.issubset(names), (
        f"Workspace verbs missing from {platform} surface: "
        f"{sorted(SAMPLE_VERBS - names)}"
    )


# ── 2. Per-turn resolution delivers calendar_list + attaches payload ──────────


def _delivered_for_scheduling(reg, platform):
    from grove.context_budget import resolve_tools_for_tier
    from grove.tier_budget import load_tier_budgets

    _enabled, tools = _platform_surface(reg, platform)
    tax = None  # GRV-009 E5b C2 — tool_groups.yaml retired; resolver ignores taxonomy
    t1 = load_tier_budgets()["T1"]
    res = resolve_tools_for_tier(tools, "scheduling", "simple", tax, t1)
    return list(res.tools)


def _hook_agent(reg, delivered):
    """A minimal agent that drives the real ``_apply_capability_hook`` over a
    delivered surface, with the real registry (so workspace verb names resolve)
    and a tool_selection dict to receive the C1 outcome stamp."""
    import run_agent

    class _Holder:
        def __init__(self, registry):
            self.registry = registry

    agent = object.__new__(run_agent.AIAgent)
    agent._tools_for_turn = delivered
    agent._dispatcher_singleton = _Holder(reg)
    agent._last_tool_selection = {}
    return agent


@pytest.mark.parametrize("platform", ENTRYPOINTS)
def test_scheduling_t1_delivers_calendar_list(monkeypatch, platform):
    reg = _registry_with_workspace(monkeypatch)
    delivered = _delivered_for_scheduling(reg, platform)
    names = {t["function"]["name"] for t in delivered}
    assert "calendar_list" in names, (
        f"scheduling/T1 did not deliver calendar_list on {platform}: {sorted(names)}"
    )


@pytest.mark.parametrize("platform", ENTRYPOINTS)
def test_scheduling_t1_attaches_capability_payload(monkeypatch, platform):
    reg = _registry_with_workspace(monkeypatch)
    delivered = _delivered_for_scheduling(reg, platform)
    agent = _hook_agent(reg, delivered)

    agent._apply_capability_hook("scheduling")

    # A Workspace carrier on the delivered surface received the GRV-009 payload.
    carriers = [
        t for t in agent._tools_for_turn
        if "GRV-009" in t.get("function", {}).get("description", "")
    ]
    assert len(carriers) == 1, "exactly one carrier should hold the payload"

    # C1 outcome (committed in spike C1) confirms the fix end-to-end.
    sel = agent._last_tool_selection
    assert sel["capability_hook_fired"] is True
    assert "calendar_list" in sel["capability_carrier_verbs_present"]
    assert sel["capability_payload_attached"] is True

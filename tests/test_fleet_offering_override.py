"""fleet-corpus-only-offering-v1 P2 — L1 OFFERING OVERRIDE (enforced control).

The L1 short-circuit at the ABSOLUTE TOP of ``_maybe_apply_tool_filter`` ROUTES on
the agent's structural fleet platform identity (``self.platform == "fleet"`` — the
AGENT's attr, run_agent.py:1452), NOT on config-key presence. For a fleet worker it REPLACES
the whole per-turn offered surface with the per-spawn allow-list read from
``RuntimeContext.config`` (route gate and payload source decoupled — no common-mode
SPOF). REPLACE, not union: it drops disclosure-core write_file / terminal / patch
and offers exactly {read_file, skill_view}.

A fleet worker NEVER falls through to the interactive resolver: a MISSING allow-list
is fatal (fleet_allowlist_absent), not a silent degrade to core (which would re-offer
write_file — the founding confinement bug). Empty and ⊄-self.tools also halt. A
non-fleet / bare agent takes the unchanged none-branch (interactive byte-identical,
guarded by test_offer_parity_snapshot / test_disclosure_split_records).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import run_agent
from grove.capability_registry import load_capabilities
from grove.fleet.errors import FleetWorkerAndon

_ABSENT = object()
_CORE_ISH = ["read_file", "skill_view", "write_file", "terminal", "patch", "web_search"]


def _tool(name):
    return {"type": "function", "function": {"name": name}}


def _agent(allowlist, tools=None, platform="fleet"):
    a = object.__new__(run_agent.AIAgent)
    a.tools = [_tool(n) for n in (_CORE_ISH if tools is None else tools)]
    a._tools_for_turn = None
    a._disclosure_manifest = None
    if platform is not None:
        a.platform = platform  # the AGENT's attr (run_agent.py:1452), not _platform
    cfg = {} if allowlist is _ABSENT else {"fleet_offered_allowlist": allowlist}
    a._runtime_ctx = SimpleNamespace(config=cfg)
    return a


def _offered(agent):
    return {t["function"]["name"] for t in (agent._tools_for_turn or [])}


def test_override_replaces_surface_with_allowlist():
    a = _agent(["read_file", "skill_view"])
    a._maybe_apply_tool_filter("do the job")
    assert _offered(a) == {"read_file", "skill_view"}
    assert a._last_tool_selection.get("fleet_offered_override") is True


def test_override_drops_core_write_tools():
    # REPLACE proven: write_file/terminal/patch are on the construction surface
    # (and are disclosure-core) yet the override drops them.
    a = _agent(["read_file", "skill_view"])
    a._maybe_apply_tool_filter("do the job")
    offered = _offered(a)
    for banned in ("write_file", "terminal", "patch", "web_search"):
        assert banned not in offered


def test_missing_allowlist_andons():
    # THE new coverage (Gemini GATE-B): a fleet worker whose config lacks the key
    # must HALT, not fall through to the interactive resolver (which would re-offer
    # write_file and dissolve the corpus-only surface — the founding bug).
    a = _agent(_ABSENT)  # platform=='fleet' but NO fleet_offered_allowlist key
    with pytest.raises(FleetWorkerAndon) as ei:
        a._maybe_apply_tool_filter("x")
    assert ei.value.check == "fleet_allowlist_absent"


def test_empty_allowlist_andons():
    # Present-but-empty allow-list would emit an empty surface -> SEAM5 admit-all.
    a = _agent([])
    with pytest.raises(FleetWorkerAndon) as ei:
        a._maybe_apply_tool_filter("x")
    assert ei.value.check == "fleet_allowlist_empty"


def test_allowlist_not_subset_andons():
    # skill_view NOT on the construction surface -> L2 floor and L1 allow-list
    # disagree -> Andon, never offer a tool the agent does not hold.
    a = _agent(["read_file", "skill_view"], tools=["read_file", "write_file"])
    with pytest.raises(FleetWorkerAndon) as ei:
        a._maybe_apply_tool_filter("x")
    assert ei.value.check == "fleet_allowlist_unsatisfiable"


def test_non_fleet_platform_takes_none_branch():
    # A non-fleet platform NEVER enters the override — control flows to the
    # unchanged resolver path. With self.tools empty the normal path early-returns;
    # the override marker is never set. Even a fleet-style allow-list in config is
    # IGNORED when the platform is not 'fleet' (routing is structural, not config).
    a = _agent(["read_file", "skill_view"], tools=[], platform="cli")
    a._maybe_apply_tool_filter("x")
    assert not getattr(a, "_last_tool_selection", None)


def test_bare_agent_no_platform_takes_none_branch():
    # A bare agent with no _platform attr must not raise (getattr default) and must
    # not fire the override — the interactive none-branch.
    a = _agent(_ABSENT, tools=[], platform=None)  # no _platform set at all
    a._maybe_apply_tool_filter("x")
    assert not getattr(a, "_last_tool_selection", None)


def test_all_records_load_after_required_tools_retired():
    # The required_tools field was stripped from the schema + the forge record; the
    # registry must still load cleanly (no dangling from_dict/validate reference).
    caps = load_capabilities()
    assert len(caps) > 100
    forge = caps["skill.fleet.forge-jobsearch"]
    assert not hasattr(forge, "required_tools")
    assert "required_tools" not in forge.to_dict()

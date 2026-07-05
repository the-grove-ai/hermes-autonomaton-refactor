"""fleet-corpus-only-offering-v1 P2 — L1 OFFERING OVERRIDE (enforced control).

A per-spawn allow-list on the RuntimeContext config REPLACES the whole per-turn
offered surface at the ABSOLUTE TOP of ``_maybe_apply_tool_filter``, before all
five contributors. REPLACE (not union): it drops disclosure-core write_file /
terminal / patch and offers exactly {read_file, skill_view}. Fail-loud on an empty
or unsatisfiable allow-list — never emit an empty ``_tools_for_turn`` (SEAM5 reads
empty as admit-ALL, run_agent.py:12418). Its trust root is the config key, SEPARATE
from L2's platform-hardcoded floor (no common-mode SPOF).

The interactive none-branch (no allow-list key) is byte-identical: the override is
a pure prepend that returns early ONLY when the key is present; the resolver itself
is untouched, guarded by ``tests/grove/test_offer_parity_snapshot.py``.
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


def _agent(allowlist, tools=None):
    a = object.__new__(run_agent.AIAgent)
    a.tools = [_tool(n) for n in (_CORE_ISH if tools is None else tools)]
    a._tools_for_turn = None
    a._disclosure_manifest = None
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


def test_override_absent_does_not_fire():
    # None-branch: no allow-list key -> override skipped, control flows to the
    # unchanged resolver path. With self.tools empty the normal path early-returns
    # and the override marker is never set.
    a = _agent(_ABSENT, tools=[])
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

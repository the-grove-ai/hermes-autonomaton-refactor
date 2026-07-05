"""fleet-corpus-only-offering-v1 P1 — L2 STRUCTURAL FLOOR (config-blind).

The corpus-only tool surface for a fleet worker is a HARDCODED floor in
``Dispatcher.get_authorized_tools`` (platform=='fleet' -> {read_file, skill_view}),
decoupled from worker_config / capability records (the DECOUPLED TRUST ROOT). This
retires P5's config-derived deny-complement, which keyed on a 'fleet' platform the
Dispatcher never carried (default 'cli') and so silently never applied — the leg-1
write_file escape. Both floor tools are Green + disclosure-core (grant-free under
non_interactive_deny_handler); invoke_skill is Yellow → the sovereign wall, so
skill_view is the sanctioned skill-load door.
"""

from __future__ import annotations

import pytest

from grove.dispatcher import Dispatcher, RuntimeContext
from grove.fleet import worker_entry as we
from grove.fleet.errors import FleetWorkerAndon


def _names(disp):
    return {t["function"]["name"] for t in disp.get_authorized_tools()}


def _ctx(config=None):
    return RuntimeContext(env={}, config=config or {})


def test_fleet_floor_is_exactly_read_file_and_skill_view():
    d = Dispatcher(runtime_ctx=_ctx(), platform="fleet", agent_kwargs=None)
    assert _names(d) == {"read_file", "skill_view"}


def test_fleet_floor_is_config_blind():
    # A config that WOULD narrow (blocked_tools drops the floor tools) AND widen
    # (extra_capabilities / legacy toolsets) under get_admitted_tools must not move
    # the floor at all — the decoupled trust root ignores worker_config entirely.
    hostile = {
        "blocked_tools": {"fleet": ["read_file", "skill_view"]},
        "extra_capabilities": {"fleet": ["invoke_skill", "write_file"]},
        "platform_toolsets": {"fleet": ["hermes-cli"]},
    }
    d = Dispatcher(runtime_ctx=_ctx(hostile), platform="fleet", agent_kwargs=None)
    assert _names(d) == {"read_file", "skill_view"}


def test_fleet_empty_floor_refuses_construct(monkeypatch):
    # Registry missing the floor tools -> empty surface. An empty surface makes
    # SEAM5 admit-ALL (run_agent.py:12418), the fail-OPEN the floor exists to
    # prevent. Must Andon, never return []. This raise propagates through
    # AIAgent.__init__'s get_available_tools call -> refuses Dispatcher construction.
    d = Dispatcher(runtime_ctx=_ctx(), platform="fleet", agent_kwargs=None)
    import model_tools
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda *a, **k: [])
    d._tools_cache.clear()
    with pytest.raises(FleetWorkerAndon) as ei:
        d.get_authorized_tools()
    assert ei.value.check == "fleet_floor_empty"


def test_interactive_surface_unchanged_by_fleet_branch():
    # A non-fleet platform takes the normal get_admitted_tools path — the fleet
    # floor branch never touches it. write_file is admitted; the surface is wide.
    d = Dispatcher(runtime_ctx=_ctx(), platform="cli", agent_kwargs=None)
    names = _names(d)
    assert "write_file" in names
    assert names != {"read_file", "skill_view"}
    assert len(names) > 2


def test_worker_constructs_dispatcher_with_platform_fleet(monkeypatch, tmp_path):
    # Root-cause regression (leg-1): the prior code passed platform ONLY in
    # agent_kwargs, leaving the Dispatcher default 'cli' so the 'fleet'-keyed
    # admission never applied and write_file (core) stayed offered. The worker MUST
    # pass platform='fleet' to the DISPATCHER so self._platform=='fleet' and the L2
    # floor fires.
    captured = {}

    class _Stop(Exception):
        pass

    def _capture(**kwargs):
        captured.update(kwargs)
        raise _Stop()

    # run_worker imports Dispatcher at call time (from grove.dispatcher import
    # Dispatcher), so patch the source attribute.
    monkeypatch.setattr("grove.dispatcher.Dispatcher", _capture)
    monkeypatch.setattr(we, "_resolve_worker_runtime", lambda *a, **k: ("m", 100, {}))
    monkeypatch.setattr(we, "_resolve_declared_sink", lambda *a, **k: tmp_path)
    import hermes_state
    monkeypatch.setattr(hermes_state, "SessionDB", lambda **k: object())

    with pytest.raises(_Stop):
        we.run_worker("forge", "rtest", {"rows": [{"id": "x"}]})
    assert captured.get("platform") == "fleet"
    # agent_kwargs still carries platform='fleet' for AIAgent.platform.
    assert captured.get("agent_kwargs", {}).get("platform") == "fleet"

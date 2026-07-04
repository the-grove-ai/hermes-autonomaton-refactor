"""fleet-pipeline-v1 P5 — corpus-only per-tool admission (sole structural control).

Covers: the required_tools record field (round-trip + structural fail-loud); the
spawn-time deny-complement that cuts the fleet worker to ONLY its declared tools;
the declared-but-unadmitted refuse-spawn Andon; per-spawn recomputation (hot-reload
leak closed); and non-vacuity (declared vs resolved, different sources).
"""

from __future__ import annotations

import pytest

from grove.capability import Capability
from grove.capability_registry import load_capabilities
from grove.fleet import worker_entry as we
from grove.fleet.errors import FleetWorkerAndon
from grove.tool_admission import get_admitted_tools
from tools.registry import ToolRegistry, register_builtin_tools


def _forge():
    return load_capabilities()["skill.fleet.forge-jobsearch"]


def _admitted(config):
    reg = ToolRegistry(); register_builtin_tools(reg)
    return get_admitted_tools(reg, "fleet", config)


# ── required_tools record field ──────────────────────────────────────────────


def test_required_tools_round_trip_present_key():
    f = _forge()
    assert f.required_tools == ["read_file", "invoke_skill"]
    d = f.to_dict()
    assert d["required_tools"] == ["read_file", "invoke_skill"]
    assert Capability.from_yaml(f.to_yaml()).required_tools == ["read_file", "invoke_skill"]


def test_required_tools_empty_not_emitted():
    rec = load_capabilities()["read_file"]  # a plain verb, no required_tools
    assert rec.required_tools == []
    assert "required_tools" not in rec.to_dict()  # byte-identical when empty


def test_required_tools_structural_fail_loud():
    base = _forge().to_dict()
    with pytest.raises(ValueError, match="must not repeat"):
        Capability.from_dict({**base, "required_tools": ["read_file", "read_file"]})
    with pytest.raises(ValueError, match="non-empty strings"):
        Capability.from_dict({**base, "required_tools": ["read_file", ""]})


def test_all_records_still_load():
    assert len(load_capabilities()) > 100


# ── the cut: forge admits ONLY {read_file, invoke_skill} ─────────────────────


def test_forge_cut_to_only_required_tools():
    forge = _forge()
    full = _admitted({})
    # sanity: the un-restricted fleet surface is broad and DOES include the index +
    # external-read tools we intend to cut.
    for t in ("cellar_search", "session_search", "web_search", "web_extract",
              "x_search", "gmail_search", "drive_search", "mcp_notion_notion_search",
              "mcp_grove_browser_browser_read_page"):
        assert t in full, f"{t} expected in the un-restricted admitted set"

    blocked = we._corpus_only_admission(forge, "forge", {})
    final = _admitted({"blocked_tools": {"fleet": blocked}})
    assert final == {"read_file", "invoke_skill"}  # the whole surface -> exactly 2
    # every index + external-read tool is excluded
    for t in ("cellar_search", "session_search", "web_search", "web_extract",
              "x_search", "gmail_search", "drive_search", "mcp_notion_notion_search",
              "mcp_grove_browser_browser_read_page"):
        assert t not in final


def test_no_required_tools_means_no_restriction():
    scout = load_capabilities()["skill.fleet.scout"]  # declares no required_tools
    assert scout.required_tools == []
    assert we._corpus_only_admission(scout, "scout", {}) is None  # full toolset


# ── declared-but-unadmitted -> refuse spawn (fail loud, never strip) ─────────


def test_declared_but_unadmitted_refuses_spawn():
    bad = Capability.from_dict({**_forge().to_dict(),
                                "required_tools": ["read_file", "no_such_tool"]})
    with pytest.raises(FleetWorkerAndon) as ei:
        we._corpus_only_admission(bad, "forge", {})
    assert ei.value.check == "required_tool_unadmitted"


# ── per-spawn recomputation (hot-reload leak closed) ─────────────────────────


def test_hot_reloaded_green_tool_lands_in_complement(monkeypatch):
    # A green tool admitted AFTER a boot snapshot must still be denied — the
    # complement is computed at spawn against the LIVE admitted set, not cached.
    real = _admitted({})
    hot = set(real) | {"hotreload_green_tool"}
    import grove.tool_admission as ta
    monkeypatch.setattr(ta, "get_admitted_tools", lambda *a, **k: hot)
    blocked = we._corpus_only_admission(_forge(), "forge", {})
    assert "hotreload_green_tool" in blocked  # denied, not leaked


# ── non-vacuity: declared vs resolved come from different sources ────────────


def test_assertion_is_non_vacuous():
    forge = _forge()
    # DECLARED source: the record's required_tools — NOT its read_surfaces (they
    # differ), NOT derived from admission.
    assert forge.required_tools == ["read_file", "invoke_skill"]
    assert forge.read_surfaces == ["corpus_file"]
    assert set(forge.required_tools) != set(forge.read_surfaces)
    # RESOLVED source: get_admitted_tools. A tool declared-required but NOT in the
    # resolved admitted set fails — proving the check is against admission, not a
    # tautology over the declaration.
    bad = Capability.from_dict({**forge.to_dict(), "required_tools": ["definitely_not_admitted"]})
    with pytest.raises(FleetWorkerAndon):
        we._corpus_only_admission(bad, "forge", {})

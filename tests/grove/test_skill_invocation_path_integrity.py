"""skill-invocation-path-integrity-v1 P6 — the seven governance pins.

Covers: record zone joins classification via max() (flat + slashed shapes);
ambiguous-slug refusal at invoke; record/disk divergence refusal; recordless
legacy-allow + IntentRecord annotation; success-gated rebind; and the
log-only boot collision scan.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import grove.router as router
from grove.capability import CapabilityKind, LifecycleState
from grove.capability_registry import SkillResolution
from grove.dispatcher import Dispatcher
from grove.zones import ZoneResult

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _init_router():
    router.initialize(REPO / "config" / "routing.config.yaml")


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


def _green() -> ZoneResult:
    return ZoneResult(zone="green", matched_rule="tool_zones", source="tool_zones")


# ── (1) yellow record raises a green class zone ───────────────────────────────


def test_yellow_record_classifies_yellow_under_green_class_zone():
    # forge-jobsearch's record declares zone: yellow; an operator zone rule
    # greening the invoke_skill intent class must not green the record.
    out = Dispatcher._raise_zone_to_skill_record(_green(), "forge-jobsearch")
    assert out.zone == "yellow"
    assert out.source == "invoke_skill_record_zone"
    assert "skill.fleet.forge-jobsearch" in out.matched_rule


# ── (2) slashed yellow-fleet name routes yellow ───────────────────────────────


def test_slashed_yellow_fleet_name_routes_yellow():
    out = Dispatcher._raise_zone_to_skill_record(_green(), "fleet/forge-jobsearch")
    assert out.zone == "yellow"
    assert out.source == "invoke_skill_record_zone"
    # max() is monotonic: a green record leaves the classification alone, and
    # a green record never LOWERS a yellow classification.
    assert Dispatcher._raise_zone_to_skill_record(_green(), "scout").zone == "green"
    yellow = ZoneResult(zone="yellow", matched_rule="x", source="tool_zones")
    assert Dispatcher._raise_zone_to_skill_record(yellow, "scout").zone == "yellow"
    # NONE resolution: classification stands.
    assert Dispatcher._raise_zone_to_skill_record(_green(), "no-such-skill-xyz").zone == "green"


# ── (3) ambiguous slug refuses at invoke ──────────────────────────────────────


def test_ambiguous_slug_refuses_at_invoke(grove_home, monkeypatch):
    active = grove_home / "skills" / "dup" / "SKILL.md"
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text("---\nname: dup\ndescription: d\n---\n\nBody.\n")

    import tools.invoke_skill_tool as ist
    monkeypatch.setattr(
        ist, "_resolve_record",
        lambda name: SkillResolution(
            "ambiguous", None, None, ("skill.alpha.dup", "skill.beta.dup"),
        ),
    )
    result = json.loads(ist.invoke_skill(name="dup"))
    assert result["success"] is False
    assert "ambiguous" in result["error"]
    assert "skill.alpha.dup" in result["error"]
    assert "skill.beta.dup" in result["error"]


# ── (4) record/disk divergence refuses ────────────────────────────────────────


def _resolution_with_state(state):
    record = SimpleNamespace(lifecycle=SimpleNamespace(state=state))
    return SkillResolution("resolved", record, "skill.test.div", ("skill.test.div",))


def test_record_disk_divergence_refuses_both_directions(grove_home, monkeypatch):
    import tools.invoke_skill_tool as ist

    # Direction 1: active tree + non-executable record.
    active = grove_home / "skills" / "div" / "SKILL.md"
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text("---\nname: div\ndescription: d\n---\n\nBody.\n")
    monkeypatch.setattr(
        ist, "_resolve_record",
        lambda name: _resolution_with_state(LifecycleState.DEPRECATED),
    )
    r1 = json.loads(ist.invoke_skill(name="div"))
    assert r1["success"] is False
    assert "divergence" in r1["error"]
    assert "deprecated" in r1["error"]          # record state named
    assert "active tree" in r1["error"]         # disk state named

    # Direction 2: .andon quarantine + non-proposed record.
    q = grove_home / "skills" / ".andon" / "qdiv" / "SKILL.md"
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text("---\nname: qdiv\ndescription: d\n---\n\nBody.\n")
    monkeypatch.setattr(
        ist, "_resolve_record",
        lambda name: _resolution_with_state(LifecycleState.ACTIVE),
    )
    r2 = json.loads(ist.invoke_skill(name="qdiv"))
    assert r2["success"] is False
    assert "divergence" in r2["error"]
    assert "active" in r2["error"]              # record state named
    assert ".andon" in r2["error"]              # disk state named


# ── (5) recordless invocation allows + annotates ──────────────────────────────


def _shell_dispatcher():
    d = object.__new__(Dispatcher)
    d._current_turn_routing_decision = None  # rebind no-ops (vanilla path)
    d._current_turn_skill_bound_tier = None
    d._current_turn_recordless_allow = False
    return d


def _ok_result(name="legacy-xyz"):
    return SimpleNamespace(
        intent_id="c1", success=True,
        content=json.dumps({"success": True, "name": name}),
    )


def test_recordless_invocation_allows_and_annotates(grove_home, monkeypatch):
    # Handler leg: no record -> allow (legacy semantics).
    import tools.invoke_skill_tool as ist
    active = grove_home / "skills" / "legacy-xyz" / "SKILL.md"
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text("---\nname: legacy-xyz\ndescription: d\n---\n\nBody.\n")
    handler = json.loads(ist.invoke_skill(name="legacy-xyz"))
    assert handler["success"] is True

    # Dispatcher leg: the executed intent annotates the turn flag.
    d = _shell_dispatcher()
    batch = [SimpleNamespace(
        tool_name="invoke_skill", arguments={"name": "legacy-xyz"}, call_id="c1",
    )]
    d._apply_skill_tier_binding(SimpleNamespace(), batch, exec_results=[_ok_result()])
    assert d._current_turn_recordless_allow is True

    # A record-governed name does NOT set the flag.
    d2 = _shell_dispatcher()
    batch2 = [SimpleNamespace(
        tool_name="invoke_skill", arguments={"name": "forge-jobsearch"}, call_id="c1",
    )]
    d2._capability_for_skill = lambda name: None  # rebind path inert
    d2._apply_skill_tier_binding(
        SimpleNamespace(), batch2, exec_results=[_ok_result("forge-jobsearch")],
    )
    assert d2._current_turn_recordless_allow is False


# ── (6) failed invocation does not rebind ─────────────────────────────────────


def test_failed_invocation_does_not_rebind():
    d = _shell_dispatcher()
    rebinds = []
    d._rebind_agent_for_skill = lambda agent, name: rebinds.append(name)

    batch = [SimpleNamespace(
        tool_name="invoke_skill", arguments={"name": "ghost-skill"}, call_id="c1",
    )]
    # Failed load (handler envelope success: false) -> no rebind.
    failed = SimpleNamespace(
        intent_id="c1", success=True,
        content=json.dumps({"success": False, "error": "not found"}),
    )
    d._apply_skill_tier_binding(SimpleNamespace(), batch, exec_results=[failed])
    assert rebinds == []
    # Transport failure -> no rebind.
    d._apply_skill_tier_binding(
        SimpleNamespace(), batch,
        exec_results=[SimpleNamespace(intent_id="c1", success=False, content="x")],
    )
    assert rebinds == []
    # Successful load -> rebind fires.
    d._apply_skill_tier_binding(
        SimpleNamespace(), batch, exec_results=[_ok_result("ghost-skill")],
    )
    assert rebinds == ["ghost-skill"]
    # P4 independence (PM ruling): the failed calls above still annotated —
    # a failed recordless invocation is backfill telemetry.
    assert d._current_turn_recordless_allow is True


# ── (7) boot collision scan logs and does not raise ───────────────────────────


def test_boot_collision_scan_logs_and_does_not_raise(monkeypatch, caplog):
    import grove.capability_registry as reg

    fake = {
        "skill.alpha.dup": SimpleNamespace(kind=CapabilityKind.SKILL),
        "skill.beta.dup": SimpleNamespace(kind=CapabilityKind.SKILL),
        "skill.solo.unique": SimpleNamespace(kind=CapabilityKind.SKILL),
        "read_file": SimpleNamespace(kind=CapabilityKind.VERB),
    }
    monkeypatch.setattr(reg, "load_capabilities", lambda *a, **k: fake)
    with caplog.at_level(logging.WARNING, logger="grove.capability_registry"):
        collisions = reg.scan_skill_slug_collisions()
    assert collisions == {"dup": ["skill.alpha.dup", "skill.beta.dup"]}
    assert "skill.alpha.dup" in caplog.text and "skill.beta.dup" in caplog.text

    # Unloadable registry: warn + empty map, never a raise.
    def _boom(*a, **k):
        raise RuntimeError("registry down")
    monkeypatch.setattr(reg, "load_capabilities", _boom)
    with caplog.at_level(logging.WARNING, logger="grove.capability_registry"):
        assert reg.scan_skill_slug_collisions() == {}
    assert "scan skipped" in caplog.text

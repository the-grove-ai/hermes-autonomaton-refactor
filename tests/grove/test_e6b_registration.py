"""GRV-009 E6b C1 — write-path substrate + registration + curator dual-read.

Proves the C1 invariants:

* NO-THROW CONTENTION — ``transition_record`` under a held per-record lock
  returns ``DEFERRED`` (no block, no throw, no ``IllegalTransitionError``); the
  on-disk record is left untouched.
* DUAL-READ — the curator routes a record-backed skill through
  ``transition_record`` and a record-less skill through the unchanged
  ``.usage.json`` fallback.
* ZONE — minted records inherit RED/YELLOW from the SKILL.md frontmatter;
  green / silent / invalid all fall back to YELLOW (never GREEN, never RED).
* DEDUP — a mint over a skill the registry already holds is a no-op.
* MINT SHAPE — installed records are provenance:installed / lifecycle:managed
  (terminal) and land under the ``skill__installed__`` filename prefix.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from grove.capability import (
    Capability,
    CapabilityKind,
    CircuitBreaker,
    Context,
    Disclosure,
    DockComposition,
    Failure,
    Lifecycle,
    LifecycleState,
    Provenance,
    SkillPresentation,
    Telemetry,
    TierRule,
    TierValidation,
    Trigger,
    TriggerDisclosure,
    Zone,
)
from grove import capability_registry as reg


def _skill_cap(cap_id: str, *, state: LifecycleState, payload: str = "---\nname: x\n---\nb") -> Capability:
    return Capability(
        id=cap_id,
        kind=CapabilityKind.SKILL,
        trigger=Trigger(always=True, disclosure=TriggerDisclosure.PROACTIVE),
        tier_rule=TierRule(
            eligible=[1, 2, 3], preferred=1,
            validation=TierValidation(confidence_threshold=0.95, shadow_window=20),
        ),
        zone=Zone.YELLOW,
        telemetry=Telemetry(feed="intent_feed"),
        context=Context(disclosure=Disclosure.PULL, payload=payload, dock_composition=DockComposition.NONE),
        lifecycle=Lifecycle(state=state, provenance=Provenance.MIGRATED),
        failure=Failure(circuit_breaker=CircuitBreaker(threshold=3, window_seconds=300)),
        skill=SkillPresentation(category="demo"),
    )


# ── NO-THROW CONTENTION ───────────────────────────────────────────────────────


def test_transition_record_defers_under_contention(tmp_path):
    """An active turn holds the per-record lock; the curator's transition is
    DEFERRED — no block, no throw, no IllegalTransitionError, record untouched."""
    fcntl = pytest.importorskip("fcntl")
    caps = tmp_path / "caps"
    caps.mkdir()
    cap = _skill_cap("skill.demo.foo", state=LifecycleState.ACTIVE)
    record_path = caps / "skill__demo__foo.yaml"
    record_path.write_text(cap.to_yaml(), encoding="utf-8")

    lock_path = record_path.with_suffix(".yaml.lock")
    holder = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)  # simulate the active turn
        result = reg.transition_record(
            "skill.demo.foo", LifecycleState.DEPRECATED,
            actor="curator", reason="inactive", directory=caps,
        )
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    assert result.status == reg.TRANSITION_DEFERRED
    assert result.record is None
    # The on-disk record is unchanged — still ACTIVE.
    reloaded = Capability.from_yaml(record_path.read_text(encoding="utf-8"))
    assert reloaded.lifecycle.state is LifecycleState.ACTIVE


def test_transition_record_applies_when_uncontended(tmp_path):
    caps = tmp_path / "caps"
    caps.mkdir()
    (caps / "skill__demo__foo.yaml").write_text(
        _skill_cap("skill.demo.foo", state=LifecycleState.ACTIVE).to_yaml(), encoding="utf-8"
    )
    result = reg.transition_record(
        "skill.demo.foo", LifecycleState.DEPRECATED,
        actor="curator", reason="inactive", directory=caps,
    )
    assert result.status == reg.TRANSITION_APPLIED
    reloaded = Capability.from_yaml((caps / "skill__demo__foo.yaml").read_text(encoding="utf-8"))
    assert reloaded.lifecycle.state is LifecycleState.DEPRECATED


def test_transition_record_skips_terminal_managed(tmp_path):
    """A MANAGED (terminal) record cannot transition — SKIPPED, never raises."""
    caps = tmp_path / "caps"
    caps.mkdir()
    (caps / "skill__installed__demo__bar.yaml").write_text(
        _skill_cap("skill.demo.bar", state=LifecycleState.MANAGED).to_yaml(), encoding="utf-8"
    )
    result = reg.transition_record(
        "skill.demo.bar", LifecycleState.DEPRECATED,
        actor="curator", reason="x", directory=caps,
    )
    assert result.status == reg.TRANSITION_SKIPPED
    assert result.record is None


# ── DUAL-READ ─────────────────────────────────────────────────────────────────


def test_curator_dual_read_record_first_and_fallback(monkeypatch):
    """One skill has a record (-> transition_record); one does not (-> .usage.json)."""
    from agent import curator
    from tools import skill_usage

    rec = _skill_cap("skill.demo.has-record", state=LifecycleState.ACTIVE)
    monkeypatch.setattr(reg, "load_capabilities", lambda *a, **k: {rec.id: rec})

    long_ago = (datetime.now(timezone.utc) - timedelta(days=9999)).isoformat()
    rows = [
        {"name": "has-record", "pinned": False, "last_activity_at": long_ago,
         "created_at": long_ago, "state": "active"},
        {"name": "no-record", "pinned": False, "last_activity_at": long_ago,
         "created_at": long_ago, "state": "active"},
    ]
    monkeypatch.setattr(skill_usage, "agent_created_report", lambda: rows)

    transitions = []
    monkeypatch.setattr(
        reg, "transition_record",
        lambda cap_id, to_state, **kw: (
            transitions.append((cap_id, to_state)),
            reg.TransitionResult(reg.TRANSITION_APPLIED, object()),
        )[1],
    )

    usage_writes = []
    monkeypatch.setattr(
        skill_usage, "archive_skill",
        lambda n: (usage_writes.append(("archive", n)), (True, "ok"))[1],
    )
    monkeypatch.setattr(
        skill_usage, "set_state",
        lambda n, s: usage_writes.append(("set_state", n, s)),
    )

    counts = curator.apply_automatic_transitions()

    # record-first: the record-backed skill went through transition_record.
    assert (rec.id, LifecycleState.DEPRECATED) in transitions
    # fallback: the record-less skill went through the .usage.json archive path.
    assert ("archive", "no-record") in usage_writes
    # the record-backed skill never touched .usage.json.
    assert all("has-record" not in w for w in usage_writes)
    assert counts["archived"] == 2  # one via record, one via fallback


# ── ZONE RESOLUTION ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "declared, expected",
    [
        ("zone: red\n", Zone.RED),
        ("zone: yellow\n", Zone.YELLOW),
        ("zone: green\n", Zone.YELLOW),   # never GREEN
        ("", Zone.YELLOW),                # silent
        ("zone: chartreuse\n", Zone.YELLOW),  # invalid
    ],
)
def test_minted_zone_resolution(declared, expected):
    payload = f"---\nname: x\n{declared}---\nbody\n"
    assert reg._resolve_minted_zone(payload) is expected


# ── DEDUP + MINT SHAPE ────────────────────────────────────────────────────────


def test_register_installed_skill_mints_managed_installed(tmp_path):
    caps = tmp_path / "caps"
    payload = "---\nname: New Skill\nzone: red\n---\nbody\n"
    path = reg.register_installed_skill("New Skill", "creative", payload, directory=caps)
    assert path is not None
    assert path.name == "skill__installed__creative__new-skill.yaml"
    cap = Capability.from_yaml(path.read_text(encoding="utf-8"))
    assert cap.id == "skill.creative.new-skill"
    assert cap.lifecycle.provenance is Provenance.INSTALLED
    assert cap.lifecycle.state is LifecycleState.MANAGED
    assert cap.zone is Zone.RED  # inherited from frontmatter


def test_register_installed_skill_dedup_is_noop(tmp_path):
    caps = tmp_path / "caps"
    caps.mkdir()
    # Pre-seed a record for the id (as if migrated/bundled).
    (caps / "skill__creative__ascii.yaml").write_text(
        _skill_cap("skill.creative.ascii", state=LifecycleState.ACTIVE).to_yaml(), encoding="utf-8"
    )
    out = reg.register_installed_skill("ascii", "creative", "---\nname: ascii\n---\nb", directory=caps)
    assert out is None  # dedup — never overwrite an existing record
    assert not (caps / "skill__installed__creative__ascii.yaml").exists()


def test_top_level_skill_id_matches_e6a_convention(tmp_path):
    """skills/<name>/ -> skill.<name>.<name> (category == name)."""
    caps = tmp_path / "caps"
    path = reg.register_installed_skill("yuanbao", "", "---\nname: yuanbao\n---\nb", directory=caps)
    assert path is not None
    cap = Capability.from_yaml(path.read_text(encoding="utf-8"))
    assert cap.id == "skill.yuanbao.yuanbao"


# ── GROVE_HOME OVERLAY + COLLISION RULE ───────────────────────────────────────


def test_load_capabilities_overlays_grove_home(monkeypatch, tmp_path):
    """A non-colliding installed record in the GROVE_HOME overlay joins the
    registry; the repo bundled records remain."""
    overlay = tmp_path / "caps"
    overlay.mkdir()
    inst = _skill_cap("skill.installed.demoz", state=LifecycleState.MANAGED)
    (overlay / "skill__installed__installed__demoz.yaml").write_text(inst.to_yaml(), encoding="utf-8")
    monkeypatch.setattr(reg, "grove_home_capabilities_dir", lambda: overlay)

    merged = reg.load_capabilities()
    assert "skill.installed.demoz" in merged              # overlay record present
    assert "skill.creative.ascii-art" in merged           # repo bundled still present


def test_load_capabilities_collision_raises_loud(monkeypatch, tmp_path):
    """COLLISION RULE: an id in BOTH repo and overlay raises loudly — never a
    silent last-glob-wins shadow."""
    overlay = tmp_path / "caps"
    overlay.mkdir()
    dup = _skill_cap("skill.creative.ascii-art", state=LifecycleState.MANAGED)  # a real repo id
    (overlay / "skill__installed__creative__ascii-art.yaml").write_text(dup.to_yaml(), encoding="utf-8")
    monkeypatch.setattr(reg, "grove_home_capabilities_dir", lambda: overlay)

    with pytest.raises(reg.CapabilityLoadError, match="BOTH"):
        reg.load_capabilities()


def test_mint_targets_grove_home_not_repo(monkeypatch, tmp_path):
    """Isolation: with a tmp GROVE_HOME overlay, a default mint lands there and
    the repo config/capabilities is never written (no pollution, no cleanup)."""
    home_caps = tmp_path / "caps"
    monkeypatch.setattr(reg, "grove_home_capabilities_dir", lambda: home_caps)

    repo = reg.default_capabilities_dir()
    repo_installed_before = set(repo.glob("skill__installed__*.yaml"))

    out = reg.register_installed_skill("Iso Skill", "creative", "---\nname: iso\n---\nb")
    assert out is not None
    assert out.parent == home_caps                        # minted into GROVE_HOME
    assert set(repo.glob("skill__installed__*.yaml")) == repo_installed_before == set()

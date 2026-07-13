"""binding-governance-surfaces-v1 Phase 1 — CapabilityBindingWriter pins.

``set_model_binding`` is the ONE sanctioned model_binding writer. Proves:

* PIN — happy path writes type=model to the record file atomically.
* UNPIN — ``binding=None`` clears the field (present-key-only round-trip:
  the serialized record carries no model_binding key afterwards).
* AMBIGUOUS REFUSAL — a colliding slug refuses before any write.
* IN-LOCK RE-VERIFY — a resolution that shifts between the pre-lock resolve
  and the locked write refuses (no wrong-record write).
* RESTORE ON FAILURE — catalog-membership and validate() failures restore
  the original bytes (backup discipline, RoutingConfigWriter parity).
* AUDIT (R5) — the writer files its own ``capability_binding_mutation``
  ledger event carrying surface + proposal_id + previous/new binding.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import grove.capability_registry as reg
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
    ModelBinding,
    Provenance,
    SkillPresentation,
    Telemetry,
    TierRule,
    TierValidation,
    Trigger,
    TriggerDisclosure,
    Zone,
)
from grove.capability_registry import (
    BindingWriteError,
    set_model_binding,
)

_CATALOG = [{"slug": "z-ai/glm-5.2"}, {"slug": "anthropic/claude-haiku-4.5"}]


def _skill_cap(
    cap_id: str,
    *,
    model_binding: ModelBinding | None = None,
) -> Capability:
    return Capability(
        id=cap_id,
        kind=CapabilityKind.SKILL,
        trigger=Trigger(always=True, disclosure=TriggerDisclosure.PROACTIVE),
        tier_rule=TierRule(
            eligible=[1, 2, 3], preferred=2,
            validation=TierValidation(confidence_threshold=0.95, shadow_window=20),
        ),
        zone=Zone.YELLOW,
        telemetry=Telemetry(feed="intent_feed"),
        context=Context(
            disclosure=Disclosure.PULL, payload="---\nname: x\n---\nb",
            dock_composition=DockComposition.NONE,
        ),
        lifecycle=Lifecycle(state=LifecycleState.ACTIVE, provenance=Provenance.MIGRATED),
        failure=Failure(circuit_breaker=CircuitBreaker(threshold=3, window_seconds=300)),
        skill=SkillPresentation(category="demo"),
        model_binding=model_binding,
    )


@pytest.fixture
def caps_env(tmp_path, monkeypatch):
    """Hermetic registry + ledger home: both capability dirs point at tmp."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    repo_caps = tmp_path / "repo_caps"
    repo_caps.mkdir()
    monkeypatch.setattr(reg, "default_capabilities_dir", lambda: repo_caps)
    monkeypatch.setattr(
        reg, "grove_home_capabilities_dir", lambda: tmp_path / "capabilities"
    )
    monkeypatch.setattr(
        "grove.config.model_catalog.load_catalog", lambda: list(_CATALOG)
    )
    return repo_caps


def _mint(caps_dir: Path, cap: Capability) -> Path:
    path = caps_dir / (cap.id.replace(".", "__") + ".yaml")
    path.write_text(cap.to_yaml(), encoding="utf-8")
    return path


def _ledger_events(tmp_path: Path) -> list[dict]:
    events = []
    ledger_dir = tmp_path / ".kaizen_ledger"
    if not ledger_dir.is_dir():
        return events
    for f in sorted(ledger_dir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            events.append(json.loads(line))
    return [e for e in events if e.get("event_type") == "capability_binding_mutation"]


# ── PIN happy path ────────────────────────────────────────────────────────────


def test_pin_happy_path(caps_env, tmp_path):
    # fleet-hygiene-sweep P2 — the pin lands in the STATE overlay; the bundled
    # DEFINITION stays byte-clean (the deploy-immune post-condition).
    from grove.capability_registry import capability_state_dir, load_capabilities

    path = _mint(caps_env, _skill_cap("skill.demo.bindtest-alpha"))
    defn_before = path.read_bytes()

    result = set_model_binding(
        "bindtest-alpha", {"type": "model", "model": "z-ai/glm-5.2"},
        surface="portal",
    )

    assert result.record_id == "skill.demo.bindtest-alpha"
    assert result.previous_binding is None
    assert result.new_binding == {"type": "model", "model": "z-ai/glm-5.2"}
    # definition untouched — the whole point
    assert path.read_bytes() == defn_before
    # composed load renders the pin from state
    reloaded = load_capabilities()["skill.demo.bindtest-alpha"]
    assert reloaded.model_binding is not None
    assert reloaded.model_binding.model == "z-ai/glm-5.2"
    # state file exists at the overlay
    state_file = capability_state_dir() / "skill__demo__bindtest-alpha.yaml"
    assert state_file.is_file()


# ── UNPIN clears the field ────────────────────────────────────────────────────


def test_unpin_clears_field(caps_env, tmp_path):
    path = _mint(
        caps_env,
        _skill_cap(
            "skill.demo.bindtest-beta",
            model_binding=ModelBinding(type="model", model="z-ai/glm-5.2"),
        ),
    )

    result = set_model_binding("bindtest-beta", None, surface="portal")

    assert result.previous_binding == {"type": "model", "model": "z-ai/glm-5.2"}
    assert result.new_binding is None
    # P2 — the DEFINITION still carries its seed pin (definitions are read-only
    # to the writer); the composed load reflects the state's cleared pin.
    from grove.capability_registry import load_capabilities

    assert "model_binding" in path.read_text(encoding="utf-8")  # defn untouched
    reloaded = load_capabilities()["skill.demo.bindtest-beta"]
    assert reloaded.model_binding is None  # state clears it (model_binding: null)


# ── AMBIGUOUS refusal ─────────────────────────────────────────────────────────


def test_ambiguous_slug_refuses(caps_env, tmp_path):
    p1 = _mint(caps_env, _skill_cap("skill.alpha.bindtest-dup"))
    p2 = _mint(caps_env, _skill_cap("skill.beta.bindtest-dup"))
    orig1, orig2 = p1.read_bytes(), p2.read_bytes()

    with pytest.raises(BindingWriteError, match="ambiguous"):
        set_model_binding(
            "bindtest-dup", {"type": "model", "model": "z-ai/glm-5.2"},
            surface="portal",
        )
    assert p1.read_bytes() == orig1 and p2.read_bytes() == orig2
    assert _ledger_events(tmp_path) == []


def test_unresolved_name_refuses(caps_env, tmp_path):
    _mint(caps_env, _skill_cap("skill.demo.bindtest-alpha"))
    with pytest.raises(BindingWriteError, match="no capability record"):
        set_model_binding(
            "bindtest-nonexistent", {"type": "model", "model": "z-ai/glm-5.2"},
            surface="portal",
        )


# ── IN-LOCK re-verify mismatch refusal ────────────────────────────────────────


def test_inside_lock_reverify_mismatch_refuses(caps_env, tmp_path, monkeypatch):
    path = _mint(caps_env, _skill_cap("skill.demo.bindtest-gamma"))
    original = path.read_bytes()

    real_resolve = reg.resolve_skill_record
    calls = {"n": 0}

    def shifting_resolve(name):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_resolve(name)  # pre-lock: resolved
        # in-lock: the registry shifted — the slug now collides.
        return reg.SkillResolution(
            "ambiguous", None, None,
            ("skill.demo.bindtest-gamma", "skill.other.bindtest-gamma"),
        )

    monkeypatch.setattr(reg, "resolve_skill_record", shifting_resolve)

    with pytest.raises(BindingWriteError, match="resolution changed under the lock"):
        set_model_binding(
            "bindtest-gamma", {"type": "model", "model": "z-ai/glm-5.2"},
            surface="portal",
        )
    assert calls["n"] == 2
    assert path.read_bytes() == original
    assert _ledger_events(tmp_path) == []


# ── RESTORE on validation / catalog failure ──────────────────────────────────


def test_catalog_membership_failure_restores(caps_env, tmp_path):
    path = _mint(caps_env, _skill_cap("skill.demo.bindtest-delta"))
    original = path.read_bytes()

    with pytest.raises(BindingWriteError, match="not in.*catalog"):
        set_model_binding(
            "bindtest-delta", {"type": "model", "model": "fake/not-a-model"},
            surface="portal",
        )
    assert path.read_bytes() == original
    assert _ledger_events(tmp_path) == []


def test_validate_failure_restores(caps_env, tmp_path):
    path = _mint(caps_env, _skill_cap("skill.demo.bindtest-epsilon"))
    original = path.read_bytes()

    # type=model carries no tier — validate() fails loud.
    with pytest.raises(BindingWriteError, match="failed record validation"):
        set_model_binding(
            "bindtest-epsilon",
            {"type": "model", "model": "z-ai/glm-5.2", "tier": "T2"},
            surface="portal",
        )
    assert path.read_bytes() == original
    assert _ledger_events(tmp_path) == []


def test_unknown_binding_key_refuses(caps_env, tmp_path):
    _mint(caps_env, _skill_cap("skill.demo.bindtest-zeta"))
    with pytest.raises(BindingWriteError, match="unknown binding keys"):
        set_model_binding(
            "bindtest-zeta", {"type": "model", "model": "z-ai/glm-5.2", "bogus": 1},
            surface="portal",
        )


# ── AUDIT event (R5) ─────────────────────────────────────────────────────────


def test_ledger_event_carries_surface_and_proposal_id(caps_env, tmp_path):
    _mint(caps_env, _skill_cap("skill.demo.bindtest-eta"))

    set_model_binding(
        "bindtest-eta", {"type": "model", "model": "z-ai/glm-5.2"},
        surface="proposal_apply", proposal_id="prop-1234",
    )

    events = _ledger_events(tmp_path)
    assert len(events) == 1
    ev = events[0]
    assert ev["skill"] == "bindtest-eta"
    assert ev["record_id"] == "skill.demo.bindtest-eta"
    assert ev["previous_binding"] is None
    assert ev["new_binding"] == {"type": "model", "model": "z-ai/glm-5.2"}
    assert ev["surface"] == "proposal_apply"
    assert ev["proposal_id"] == "prop-1234"


def test_previous_binding_captured(caps_env, tmp_path):
    _mint(
        caps_env,
        _skill_cap(
            "skill.demo.bindtest-theta",
            model_binding=ModelBinding(type="tier_override", tier="T2"),
        ),
    )

    result = set_model_binding(
        "bindtest-theta", {"type": "model", "model": "anthropic/claude-haiku-4.5"},
        surface="portal",
    )

    assert result.previous_binding == {"type": "tier_override", "tier": "T2"}
    events = _ledger_events(tmp_path)
    assert len(events) == 1
    assert events[0]["previous_binding"] == {"type": "tier_override", "tier": "T2"}
    assert events[0]["new_binding"] == {
        "type": "model", "model": "anthropic/claude-haiku-4.5",
    }
    assert events[0]["proposal_id"] is None


# ── fleet-hygiene-sweep P2 — state overlay (Option B: uniform) ────────────────


def test_minted_overlay_record_pin_composes(caps_env, tmp_path):
    """Ruling B — a MINTED (whole-file overlay) record's pin also flows to the
    state overlay and composes back correctly (deploy-immunity preserved; the
    superseded 'still whole-file' line becomes this composition pin)."""
    from grove.capability_registry import (
        capability_state_dir,
        grove_home_capabilities_dir,
        load_capabilities,
    )

    # mint the record into the whole-file overlay dir (not the repo bundled dir)
    overlay_dir = grove_home_capabilities_dir()
    overlay_dir.mkdir(parents=True, exist_ok=True)
    cap = _skill_cap("skill.demo.minted-one")
    (overlay_dir / "skill__demo__minted-one.yaml").write_text(
        cap.to_yaml(), encoding="utf-8"
    )

    set_model_binding(
        "minted-one", {"type": "model", "model": "z-ai/glm-5.2"}, surface="portal",
    )

    # state sidecar written; overlay definition byte-clean; composed load pins
    assert (capability_state_dir() / "skill__demo__minted-one.yaml").is_file()
    reloaded = load_capabilities()["skill.demo.minted-one"]
    assert reloaded.model_binding.model == "z-ai/glm-5.2"


def test_mint_gate_refuses_orphaned_state(caps_env, tmp_path):
    """R-B4 — minting an id that already has a state sidecar (no definition)
    refuses: minting onto stale state would silently resurrect it."""
    from grove.capability_registry import (
        CapabilityLoadError,
        _mint_skill_record,
        capability_state_dir,
    )
    from grove.capability import LifecycleState, Provenance

    # a non-empty registry so the dedup load succeeds (real system has 153)
    _mint(caps_env, _skill_cap("skill.demo.seedrec"))

    # plant an orphaned state file at the id the mint would produce
    state_dir = capability_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "skill__demo__ghosttest.yaml").write_text(
        "id: skill.demo.ghosttest\nmodel_binding:\n  type: model\n  model: z-ai/glm-5.2\n",
        encoding="utf-8",
    )

    with pytest.raises(CapabilityLoadError, match="orphaned state"):
        _mint_skill_record(
            "ghosttest", "demo", "---\nname: ghosttest\n---\nbody",
            provenance=Provenance.AGENT_PROPOSED, state=LifecycleState.PROPOSED,
            filename_tag="installed",
        )

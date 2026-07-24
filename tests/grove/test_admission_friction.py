"""operator-mutable-admission-v1 Phase 4 — admission_friction Kaizen producer.

The self-healing loop's DETECT→PROPOSE→(APPROVE|DISMISS) stages. Pins the three
gate-guarantees the operator reserved GO-commit on:

  G1 REPO-WRITE STRUCTURALLY UNREACHABLE — the Stage-04 approval writes ONLY
     ~/.grove via set_admission_overlay; even with the repo record dir made
     read-only the approval succeeds (no code route to config/capabilities/).
  G2 TOMBSTONE grain (record, intent) + ~/.grove location — a dismiss of (A, X)
     never suppresses (A, Y); tombstones live outside the repo tree (git-reset
     durable).
  G3 ADDITIVE + GREEN-SCOPED — proposal verbs are only add_intents / force_always;
     force_always fires ONLY for a GREEN record.

Plus the four take-as-reported confirmations: I7 grep-clean; threshold from
config; registered on the existing RENDER_REGISTRY / PROPOSAL_HANDLERS surface;
proposals render with refusal-recurrence evidence.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import grove.capability_registry as reg
import grove.eval.admission_friction as af
import grove.flywheel_cli as fc
from grove.capability import (
    Bindings, Capability, CapabilityKind, CircuitBreaker, Context, Disclosure,
    DockComposition, Failure, Lifecycle, LifecycleState, Provenance, Telemetry,
    TierRule, TierValidation, Trigger, TriggerDisclosure, Zone,
)
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_ADMISSION_FRICTION, RoutingProposal, _type_offers_approve,
)
from grove.kaizen.rendering import RENDER_REGISTRY


class _Rec:
    def __init__(self, zone):
        self.zone = zone


def _caps(**by_id):
    return dict(by_id)


def _refusal(record, intent, ts="2026-07-15T12:00:00+00:00", session="s1", tier="T2"):
    return {"governing_record": record, "intent": intent, "tier": tier,
            "session_id": session, "reason": "not in the per-turn offered surface",
            "ts": ts}


def _write_refusals(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _build(tmp_path, rows, caps, config_path=None, tombstone_path=None):
    rp = tmp_path / "refusals.jsonl"
    _write_refusals(rp, rows)
    return af.build_admission_friction_proposals(
        refusals_path=rp,
        tombstone_path=tombstone_path or (tmp_path / "tomb.json"),
        config_path=config_path,
        caps=caps,
    )


# ── core producer ───────────────────────────────────────────────────────────

# native-presence-declared-v1 RETIRED test_over_threshold_arm_yields_one_add_intent_proposal:
# the per-intent add_intents proposal path is deleted (adding an intent no longer
# offers a tool). A YELLOW over-threshold arm now yields nothing; the sole lever is
# the GREEN-scoped force_always proposal, covered below.


def test_under_threshold_yields_nothing(tmp_path):
    rows = [_refusal("recY", "intent_a") for _ in range(2)]
    assert _build(tmp_path, rows, _caps(recY=_Rec(Zone.YELLOW))) == []


def test_record_absent_from_registry_is_skipped(tmp_path):
    rows = [_refusal("ghost", "intent_a") for _ in range(3)]
    assert _build(tmp_path, rows, _caps()) == []


# ── G3: additive + green-scoped ──────────────────────────────────────────────

def test_green_record_three_distinct_intents_yields_single_force_always(tmp_path):
    rows = []
    for intent in ("intent_a", "intent_b", "intent_c"):
        rows += [_refusal("recG", intent) for _ in range(3)]
    props = _build(tmp_path, rows, _caps(recG=_Rec(Zone.GREEN)))
    assert len(props) == 1
    assert props[0].payload["verb"] == "force_always"
    assert "add_intents" not in props[0].payload


def test_non_green_record_never_yields_force_always(tmp_path):
    rows = []
    for intent in ("intent_a", "intent_b", "intent_c"):
        rows += [_refusal("recY", intent) for _ in range(3)]
    props = _build(tmp_path, rows, _caps(recY=_Rec(Zone.YELLOW)))
    verbs = [p.payload["verb"] for p in props]
    assert "force_always" not in verbs, "a non-GREEN record must never force_always"
    # native-presence-declared-v1: with add_intents retired, force_always is the
    # only producer verb and it is GREEN-scoped — a non-green record yields NO
    # admission-friction proposals at all.
    assert props == []


def test_all_producer_verbs_are_additive_only(tmp_path):
    # native-presence-declared-v1: force_always is the SOLE producer verb now
    # (add_intents retired). A GREEN record with >=3 distinct over-threshold intents
    # yields one force_always; a non-green record yields nothing.
    rows = []
    for intent in ("intent_a", "intent_b", "intent_c"):
        rows += [_refusal("recG", intent) for _ in range(3)]
    for intent in ("intent_a", "intent_b"):
        rows += [_refusal("recY", intent) for _ in range(3)]
    props = _build(tmp_path, rows, _caps(recG=_Rec(Zone.GREEN), recY=_Rec(Zone.YELLOW)))
    assert props, "expected proposals"
    for p in props:
        assert p.payload["verb"] == "force_always"
        # no removal/shrink verb, ever
        assert not any(k.startswith(("remove", "drop", "strip")) for k in p.payload)


# ── threshold from config (confirmation) ─────────────────────────────────────

def test_threshold_read_from_config_not_constant(tmp_path):
    # native-presence-declared-v1: exercised through the GREEN force_always path
    # (add_intents retired). friction_threshold still gates each arm before the
    # >=3-distinct-intents force_always fires.
    cfg = tmp_path / "flywheel.config.yaml"
    cfg.write_text("admission_friction:\n  friction_threshold: 5\n", encoding="utf-8")
    caps = _caps(recG=_Rec(Zone.GREEN))
    intents = ("intent_a", "intent_b", "intent_c")
    under = []
    for intent in intents:
        under += [_refusal("recG", intent) for _ in range(4)]  # 4 < 5 → no arm arms
    assert _build(tmp_path, under, caps, config_path=cfg) == []
    over = []
    for intent in intents:
        over += [_refusal("recG", intent) for _ in range(5)]  # 5 >= 5 → all arms clear
    props = _build(tmp_path, over, caps, config_path=cfg)
    assert len(props) == 1 and props[0].payload["verb"] == "force_always"


def test_green_distinct_threshold_from_config(tmp_path):
    cfg = tmp_path / "flywheel.config.yaml"
    cfg.write_text(
        "admission_friction:\n  friction_threshold: 3\n"
        "  green_force_always_distinct_intents: 2\n", encoding="utf-8",
    )
    rows = []
    for intent in ("intent_a", "intent_b"):
        rows += [_refusal("recG", intent) for _ in range(3)]
    props = _build(tmp_path, rows, _caps(recG=_Rec(Zone.GREEN)), config_path=cfg)
    assert len(props) == 1 and props[0].payload["verb"] == "force_always"


# ── G2: tombstone grain + location ───────────────────────────────────────────

# native-presence-declared-v1 RETIRED test_tombstone_grain_is_record_intent:
# the per-intent (record, intent) tombstone grain suppressed add_intents proposals,
# which no longer exist. The surviving force_always proposal tombstones on a
# record-level sentinel (__force_always__); the per-intent grain has no consumer.


def test_tombstone_path_is_outside_repo_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "grove_home"))
    from hermes_constants import get_hermes_home

    p = af.default_tombstone_path()
    assert p.name == "admission_friction_tombstones.json"
    assert p.parent == Path(get_hermes_home())          # under ~/.grove
    repo_root = Path(__file__).resolve().parents[2]
    assert repo_root not in p.parents, "tombstones must live OUTSIDE the repo tree"


# ── G1: repo-write structurally unreachable (Stage-04 approval) ──────────────

def _mk_record_yaml(rid, tool):
    return Capability(
        id=rid, kind=CapabilityKind.VERB,
        trigger=Trigger(intents=["research"], always=False,
                        disclosure=TriggerDisclosure.PROACTIVE),
        bindings=Bindings(tools=[tool], toolset_key=None),
        tier_rule=TierRule(eligible=[1, 2, 3], preferred=1,
                           validation=TierValidation(confidence_threshold=0.95,
                                                     shadow_window=20)),
        zone=Zone.GREEN, telemetry=Telemetry(feed="intent_feed"),
        context=Context(disclosure=Disclosure.EAGER, payload="x",
                        dock_composition=DockComposition.NONE),
        lifecycle=Lifecycle(state=LifecycleState.ACTIVE,
                            provenance=Provenance.OPERATOR_AUTHORED,
                            created_at="2026-01-01T00:00:00+00:00"),
        failure=Failure(circuit_breaker=CircuitBreaker(threshold=3, window_seconds=300)),
    ).to_yaml()


def test_approval_writes_only_grove_never_repo(tmp_path, monkeypatch):
    import os
    defn = tmp_path / "defn"
    defn.mkdir()
    rid = "verb.friction.demo"
    defn_file = defn / "verb__friction__demo.yaml"
    defn_file.write_text(_mk_record_yaml(rid, "friction_demo_tool"), encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setattr(reg, "default_capabilities_dir", lambda: defn)
    monkeypatch.setattr(reg, "grove_home_capabilities_dir", lambda: tmp_path / "ov")
    monkeypatch.setattr(reg, "capability_state_dir", lambda: state)

    before = defn_file.read_bytes()
    # Make the REPO record dir READ-ONLY: any write route to config/capabilities/
    # would now raise. The approval must still succeed → repo-write is unreachable.
    os.chmod(defn, 0o500)
    try:
        # native-presence-declared-v1: force_always is the sole apply verb now.
        proposal = RoutingProposal(
            proposal_id="pfx", type=PROPOSAL_TYPE_ADMISSION_FRICTION,
            payload={"record": rid, "verb": "force_always",
                     "evidence_block": {"verb": "force_always"}},
            evidence=(f"{rid}|__force_always__",), eval_hash="", created_at="t",
        )
        target, applied = fc._approve_admission_friction(proposal)
    finally:
        os.chmod(defn, 0o700)

    assert applied["status"] == "applied"
    assert Path(target).parent == state                 # wrote ~/.grove state overlay
    assert Path(target).exists()
    overlay = Path(target).read_text()
    assert "force_always" in overlay
    assert defn_file.read_bytes() == before, "repo definition must be byte-unchanged"


# ── confirmations ────────────────────────────────────────────────────────────

def test_registered_on_existing_kaizen_surface(tmp_path):
    assert PROPOSAL_TYPE_ADMISSION_FRICTION in RENDER_REGISTRY
    assert PROPOSAL_TYPE_ADMISSION_FRICTION in fc.PROPOSAL_HANDLERS
    assert _type_offers_approve(PROPOSAL_TYPE_ADMISSION_FRICTION) is True


def test_proposal_renders_with_recurrence_evidence(tmp_path):
    # native-presence-declared-v1: the sole surviving proposal is the GREEN
    # force_always; its render carries the recurring intents + arm evidence.
    rows = []
    for intent in ("intent_a", "intent_b", "intent_c"):
        rows += [_refusal("recG", intent) for _ in range(4)]
    props = _build(tmp_path, rows, _caps(recG=_Rec(Zone.GREEN)))
    assert len(props) == 1 and props[0].payload["verb"] == "force_always"
    summary = RENDER_REGISTRY[PROPOSAL_TYPE_ADMISSION_FRICTION](props[0])
    assert "recG" in summary and "intent_a" in summary
    diff = fc._admission_friction_to_diff(props[0])
    assert diff["evidence"] and diff["evidence"][0]["count"] == 4
    assert diff["overlay_edit"] == {"force_always": True}


def test_i7_module_has_no_tool_or_intent_literals():
    from grove.classify import INTENT_CLASSES
    src = Path(af.__file__).read_text(encoding="utf-8")
    tool_names = set()
    for c in reg.load_capabilities().values():
        tool_names.update(c.bindings.tools)
    offenders = [s for s in list(INTENT_CLASSES) + sorted(tool_names)
                 if f'"{s}"' in src or f"'{s}'" in src]
    assert not offenders, f"producer must carry no tool/intent literals; found {offenders}"

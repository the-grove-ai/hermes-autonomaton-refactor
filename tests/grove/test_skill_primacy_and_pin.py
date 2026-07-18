"""skill-adoption-v1 C1 + C4 — intent primacy resolution and the approval-time
payload pin.

C1: a record may claim primacy for an intent class it also declares. The loader
resolves the effective {intent -> slug} map RESILIENTLY (out-of-subset claims
dropped, collisions demote all claimants), never bricking boot; the strict
reject lives in the write-path checker. C4: promotion pins sha256 of the approved
SKILL.md bytes into operator state, and verify_payload_hash fails closed on a
missing pin or a mutated payload. F5: an approval-time static size gate that is
inert until the Phase-2 skill_payload_ceiling config key exists.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from grove import kaizen_ledger
from grove import capability_registry as reg
from grove import skills as gskills
from grove import sovereignty as gsov
from grove.capability import Capability
from grove.kaizen_ledger import KaizenLedger

REPO_CAPS = reg.default_capabilities_dir()
_BASE = yaml.safe_load(
    (REPO_CAPS / "skill__fleet__researcher.yaml").read_text(encoding="utf-8")
)


def _cap(
    *,
    cap_id: str,
    intents: list[str],
    primary: list[str] | None,
    state: str = "active",
) -> Capability:
    """A valid kind=skill Capability built from the researcher record, with the
    trigger + lifecycle-state overridden. tools stays empty (no binding
    collisions), so the only collection-level property under test is primacy."""
    d = copy.deepcopy(_BASE)
    d["id"] = cap_id
    d["trigger"]["intents"] = list(intents)
    if primary is None:
        d["trigger"].pop("primary_intents", None)
    else:
        d["trigger"]["primary_intents"] = list(primary)
    d["lifecycle"]["state"] = state
    return Capability.from_dict(d)


def _write_record(directory: Path, cap: Capability) -> None:
    fname = cap.id.replace(".", "__") + ".yaml"
    (directory / fname).write_text(
        yaml.safe_dump(cap.to_dict(), sort_keys=False), encoding="utf-8"
    )


# ── C1 schema round-trip (byte-identical when absent) ────────────────────────


def test_absent_primary_intents_not_serialized():
    cap = _cap(cap_id="skill.fleet.a", intents=["research"], primary=None)
    assert "primary_intents" not in cap.to_dict()["trigger"]


def test_present_primary_intents_roundtrips():
    cap = _cap(cap_id="skill.fleet.a", intents=["research", "analysis"], primary=["research"])
    again = Capability.from_dict(cap.to_dict())
    assert again.trigger.primary_intents == ["research"]


def test_validate_rejects_malformed_primary_intents():
    with pytest.raises(ValueError, match="repeat"):
        _cap(cap_id="skill.fleet.a", intents=["research"], primary=["research", "research"])
    with pytest.raises(ValueError, match="non-empty strings"):
        _cap(cap_id="skill.fleet.a", intents=["research"], primary=["research", ""])


# ── C1 write-side strict reject (primacy_write_violations) ───────────────────


def test_write_reject_subset_violation():
    cand = _cap(cap_id="skill.fleet.y", intents=["research"], primary=["research", "coding"])
    problems = reg.primacy_write_violations({}, cand)
    assert any("subset" in p for p in problems)


def test_write_reject_collision():
    existing = _cap(cap_id="skill.fleet.x", intents=["research"], primary=["research"])
    cand = _cap(cap_id="skill.fleet.y", intents=["research"], primary=["research"])
    problems = reg.primacy_write_violations({existing.id: existing}, cand)
    assert any("collision" in p and "research" in p for p in problems)


def test_write_no_violation_disjoint_claims():
    existing = _cap(cap_id="skill.fleet.x", intents=["research"], primary=["research"])
    cand = _cap(cap_id="skill.fleet.y", intents=["writing"], primary=["writing"])
    assert reg.primacy_write_violations({existing.id: existing}, cand) == []


def test_write_collision_ignores_non_executable_candidate():
    # A proposed (non-enabled) candidate claiming an already-held class is NOT a
    # collision — only ENABLED records contend for primacy.
    existing = _cap(cap_id="skill.fleet.x", intents=["research"], primary=["research"])
    cand = _cap(
        cap_id="skill.fleet.y", intents=["research"], primary=["research"], state="proposed"
    )
    assert reg.primacy_write_violations({existing.id: existing}, cand) == []


# ── C1 load-side resilient resolution (compute_primacy_map, pure) ────────────


def test_compute_happy_path_resolves_primary():
    a = _cap(cap_id="skill.fleet.researcher", intents=["research", "analysis"], primary=["research"])
    primacy, violations = reg.compute_primacy_map({a.id: a})
    assert primacy == {"research": "researcher"}
    assert violations == []


def test_compute_collision_demotes_all():
    a = _cap(cap_id="skill.fleet.alpha", intents=["research"], primary=["research"])
    b = _cap(cap_id="skill.fleet.beta", intents=["research"], primary=["research"])
    primacy, violations = reg.compute_primacy_map({a.id: a, b.id: b})
    assert "research" not in primacy  # both demoted, no tie-break
    coll = [v for v in violations if v["reason"] == "collision"]
    assert len(coll) == 1
    assert coll[0]["intent_class"] == "research"
    assert coll[0]["slugs"] == ["alpha", "beta"]


def test_compute_subset_violation_drops_out_of_set_intent():
    a = _cap(cap_id="skill.fleet.gamma", intents=["research"], primary=["research", "coding"])
    primacy, violations = reg.compute_primacy_map({a.id: a})
    assert primacy == {"research": "gamma"}  # valid claim survives
    sub = [v for v in violations if v["reason"] == "subset_violation"]
    assert len(sub) == 1 and sub[0]["intent_class"] == "coding"


def test_compute_ignores_non_executable_records():
    active = _cap(cap_id="skill.fleet.a", intents=["research"], primary=["research"])
    proposed = _cap(
        cap_id="skill.fleet.b", intents=["research"], primary=["research"], state="proposed"
    )
    primacy, violations = reg.compute_primacy_map({active.id: active, proposed.id: proposed})
    assert primacy == {"research": "a"}  # the proposed claim never contends
    assert violations == []


# ── C1 load integration: boot survives, Andon fires, map cached ──────────────


def test_load_collision_survives_boot_and_fires_andon(tmp_path, monkeypatch):
    captured: list = []
    monkeypatch.setattr(reg, "_file_primacy_violations", lambda v: captured.extend(v))
    a = _cap(cap_id="skill.fleet.alpha", intents=["research"], primary=["research"])
    b = _cap(cap_id="skill.fleet.beta", intents=["research"], primary=["research"])
    _write_record(tmp_path, a)
    _write_record(tmp_path, b)
    caps = reg.load_capabilities(tmp_path)  # MUST NOT raise — boot survives
    assert {a.id, b.id} <= set(caps)
    assert reg.primary_skill_for_intent("research") is None  # demoted this load
    assert any(
        v["reason"] == "collision" and v["intent_class"] == "research" for v in captured
    )


def test_load_happy_path_caches_resolver(tmp_path, monkeypatch):
    monkeypatch.setattr(reg, "_file_primacy_violations", lambda v: None)
    a = _cap(cap_id="skill.fleet.researcher", intents=["research", "analysis"], primary=["research"])
    _write_record(tmp_path, a)
    reg.load_capabilities(tmp_path)
    assert reg.primary_skill_for_intent("research") == "researcher"
    assert reg.primary_skill_for_intent("analysis") is None  # unclaimed


def test_file_primacy_violations_emits_registered_event(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        KaizenLedger, "record",
        lambda self, event_type, **f: calls.append((event_type, f)) or {},
    )
    reg._file_primacy_violations(
        [{"reason": "collision", "intent_class": "research", "record_ids": ["a", "b"], "slugs": ["a", "b"]}]
    )
    assert calls and calls[0][0] == "skill_primacy_collision"
    assert calls[0][1]["intent_class"] == "research"


def test_new_event_type_is_registered():
    assert "skill_primacy_collision" in KaizenLedger.EVENT_TYPES


# ── C4 hash pin ──────────────────────────────────────────────────────────────

_PROPOSAL = """---
name: {name}
description: A test skill for pin verification.
category: productivity
created_by: autonomaton
proposed_at: '2026-05-20T12:00:00Z'
zone: yellow
provenance:
  created_by: autonomaton
  scan_verdict: safe
  scan_findings: []
---
# {name}

Body for {name}.
"""


def test_promote_pins_payload_and_verifies(monkeypatch):
    monkeypatch.setenv("GROVE_OPERATOR_EMAIL", "jim@the-grove.ai")
    gskills.write_proposal("pinskill", _PROPOSAL.format(name="pinskill"))
    gsov.promote("pinskill")

    rid = reg.skill_record_id_for_name("pinskill")
    assert rid is not None
    cap = reg.skill_record_for_name("pinskill")
    assert reg.approved_payload_hash_for(rid) is not None
    assert reg.verify_payload_hash(cap) is True

    # Mutate the active payload → the pin no longer matches (fail-closed).
    active = gskills.active_path("pinskill") / "SKILL.md"
    active.write_text(active.read_text() + "\n# tamper\n", encoding="utf-8")
    assert reg.verify_payload_hash(cap) is False


def test_verify_fails_closed_on_missing_pin():
    # A record with no pin in state never verifies, regardless of payload.
    cap = _cap(cap_id="skill.fleet.unpinned", intents=["research"], primary=None)
    assert reg.approved_payload_hash_for(cap.id) is None
    assert reg.verify_payload_hash(cap) is False


def test_pin_survives_a_later_lifecycle_write(monkeypatch):
    monkeypatch.setenv("GROVE_OPERATOR_EMAIL", "jim@the-grove.ai")
    gskills.write_proposal("pinskill2", _PROPOSAL.format(name="pinskill2"))
    gsov.promote("pinskill2")
    rid = reg.skill_record_id_for_name("pinskill2")
    pin = reg.approved_payload_hash_for(rid)
    assert pin is not None
    # A routine lifecycle write (use_count bump) must NOT drop the pin.
    reg.update_lifecycle_fields(rid, use_count=1)
    assert reg.approved_payload_hash_for(rid) == pin


# ── C4/F5 static payload-size ceiling ────────────────────────────────────────


def test_f5_inert_when_no_ceiling_configured(monkeypatch):
    monkeypatch.setattr(gsov, "_routing_config", lambda: {})
    assert gsov.smallest_skill_payload_ceiling() is None
    gsov._enforce_payload_size_ceiling("x" * 100_000)  # inert — no raise


def test_f5_repo_config_declares_no_ceiling_yet():
    # Phase 1 invariant: the repo routing.config.yaml carries no ceiling, so the
    # gate is dormant until Phase 2 lands the key.
    assert gsov.smallest_skill_payload_ceiling() is None


def test_f5_rejects_oversize_against_smallest_ceiling(monkeypatch):
    monkeypatch.setattr(
        gsov, "_routing_config",
        lambda: {"tier_budgets": {"T1": {"skill_payload_ceiling": 50}, "T2": {"skill_payload_ceiling": 200}}},
    )
    assert gsov.smallest_skill_payload_ceiling() == 50
    gsov._enforce_payload_size_ceiling("x" * 50)  # exactly at ceiling → ok
    with pytest.raises(gsov.SkillPayloadTooLarge):
        gsov._enforce_payload_size_ceiling("x" * 51)


def test_f5_ignores_non_positive_and_bool_ceilings(monkeypatch):
    monkeypatch.setattr(
        gsov, "_routing_config",
        lambda: {"tier_budgets": {"T1": {"skill_payload_ceiling": True}, "T2": {"skill_payload_ceiling": 0}}},
    )
    assert gsov.smallest_skill_payload_ceiling() is None  # neither counts

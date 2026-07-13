"""GRV-009 E6b C2 (4.2+4.3+4.4) — the faucet + the irreversible sovereignty
rewire, against the A6 collapsed graph (proposed is the sole review lock).

Proves:
* end-to-end proposed→promote→executable (no strand);
* a LEGACY pre-C2 .andon proposal (no record) promotes without stranding;
* STATE-FIRST recovery: file move FAILS after the transition APPLIED → the
  record is truth, no crash (the scrutinize-hardest proof);
* transition DEFERRED (lock contended) → nothing moves;
* revoke active→proposed → non-executable;
* in-place edit of an ACTIVE skill REFUSED (immutable, C1b-i wall);
  managed-edit refusal; delete deprecates a deletable record (not hard-
  removed) and is refused in place on a live governed skill;
* the 4.4 ingest mints an ACTIVE, executable record carrying use_count.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grove import capability_registry as reg
from grove.capability import (
    Capability,
    CapabilityKind,
    LifecycleState,
    Provenance,
)


def _mint_active(name: str, category: str, body: str, use_count: int = 0):
    """Mint an ACTIVE, executable skill record directly.

    GRV-010 C2b deleted ``ingest_pre_faucet_skill`` (dead, un-audited minter);
    these tests used it only as a setup helper to plant an ACTIVE record. This
    exercises the same internal it wrapped (``_mint_skill_record``).
    """
    return reg._mint_skill_record(
        name, category, body,
        provenance=Provenance.AGENT_PROPOSED,
        state=LifecycleState.ACTIVE,
        filename_tag="ingested",
        use_count=use_count,
    )


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    """Isolate GROVE_HOME so mints, .andon, and skills land in tmp (no repo or
    real ~/.grove pollution)."""
    home = tmp_path / ".grove"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("GROVE_HOME", str(home))
    # capability overlay -> tmp; both the registry and grove.skills read GROVE_HOME.
    monkeypatch.setattr(reg, "grove_home_capabilities_dir", lambda: home / "capabilities")
    return home


def _write_andon_proposal(home: Path, name: str, body: str) -> Path:
    """Place a SKILL.md proposal in .andon/<name>/ (the file store)."""
    d = home / "skills" / ".andon" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


def _proposed_body(name: str) -> str:
    return f"---\nname: {name}\ndescription: test skill\n---\nDo the thing.\n"


# ── end-to-end promote ────────────────────────────────────────────────────────


def test_proposed_promotes_to_executable_no_strand(grove_home, monkeypatch):
    import grove.skills as skills
    from grove.skill_disclosure import SkillNotExecutableError, resolve_skill_record
    from grove.sovereignty import promote

    name = "demo-skill"
    body = _proposed_body(name)
    # Mint the proposed record + write the .andon body (what _create_skill does).
    reg.register_proposed_skill(name, "creative", body)
    skills.write_proposal(name, body)

    cap_id = reg.skill_record_id_for_name(name)
    assert cap_id == "skill.creative.demo-skill"
    prop = next(c for c in reg.load_capabilities().values() if c.id == cap_id)
    assert prop.lifecycle.state is LifecycleState.PROPOSED
    with pytest.raises(SkillNotExecutableError):
        resolve_skill_record(prop)  # non-executable while proposed

    promote(name)  # state-first: transition ACTIVE then move

    active = next(c for c in reg.load_capabilities().values() if c.id == cap_id)
    assert active.lifecycle.state is LifecycleState.ACTIVE          # promoted
    assert resolve_skill_record(active).startswith("<skill_reference_data>")  # executable
    assert active_skill_moved(grove_home, name)                    # body left .andon


def active_skill_moved(home: Path, name: str) -> bool:
    return not (home / "skills" / ".andon" / name).exists()


# ── legacy pre-C2 proposal (no record) ────────────────────────────────────────


def test_legacy_proposal_promotes_without_strand(grove_home):
    import grove.skills as skills
    from grove.sovereignty import promote

    name = "legacy-skill"
    body = _proposed_body(name)
    # Only the .andon file — NO record (a pre-C2 proposal).
    skills.write_proposal(name, body)
    assert reg.skill_record_id_for_name(name) is None

    promote(name)  # mint-then-transition

    cap_id = reg.skill_record_id_for_name(name)
    assert cap_id is not None
    rec = next(c for c in reg.load_capabilities().values() if c.id == cap_id)
    assert rec.lifecycle.state is LifecycleState.ACTIVE  # legacy reached executable


# ── STATE-FIRST recovery (move fails after APPLIED) ───────────────────────────


def test_move_fails_after_applied_record_is_truth(grove_home, monkeypatch):
    import grove.sovereignty as sov

    name = "recover-skill"
    body = _proposed_body(name)
    reg.register_proposed_skill(name, "creative", body)
    import grove.skills as skills
    skills.write_proposal(name, body)
    cap_id = reg.skill_record_id_for_name(name)

    # Make the physical move fail AFTER the transition has APPLIED.
    def _boom(*a, **k):
        raise OSError("simulated cross-device move failure")
    monkeypatch.setattr(sov.shutil, "move", _boom)

    result = sov.promote(name)  # must NOT raise — record is truth

    rec = next(c for c in reg.load_capabilities().values() if c.id == cap_id)
    assert rec.lifecycle.state is LifecycleState.ACTIVE  # record is truth
    assert "stray file flagged" in (result.get("reason") or "")


def test_transition_deferred_nothing_moves(grove_home, monkeypatch):
    import fcntl

    import grove.skills as skills
    from grove.sovereignty import promote

    name = "contended-skill"
    body = _proposed_body(name)
    rec_path = reg.register_proposed_skill(name, "creative", body)
    skills.write_proposal(name, body)
    cap_id = reg.skill_record_id_for_name(name)

    # fleet-hygiene-sweep P2 — the transition locks the STATE overlay path, not
    # the definition record; hold THAT lock to simulate the concurrent write.
    state_path = reg.capability_state_dir() / f"{cap_id.replace('.', '__')}.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".yaml.lock")
    holder = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with pytest.raises(RuntimeError, match="DEFERRED|locked"):
            promote(name)
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    # Nothing moved: the .andon proposal is still there, the record still PROPOSED.
    assert (grove_home / "skills" / ".andon" / name).exists()
    rec = next(c for c in reg.load_capabilities().values() if c.id == cap_id)
    assert rec.lifecycle.state is LifecycleState.PROPOSED


# ── revoke ────────────────────────────────────────────────────────────────────


def test_revoke_active_to_proposed_non_executable(grove_home):
    import grove.skills as skills
    from grove.skill_disclosure import SkillNotExecutableError, resolve_skill_record
    from grove.sovereignty import revoke

    name = "revoke-skill"
    body = _proposed_body(name)
    # An active skill: mint an ACTIVE record + place the active body on disk.
    _mint_active(name, "creative", body)
    active_dir = skills.active_path(name)
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "SKILL.md").write_text(body, encoding="utf-8")
    cap_id = reg.skill_record_id_for_name(name)

    revoke(name)

    rec = next(c for c in reg.load_capabilities().values() if c.id == cap_id)
    assert rec.lifecycle.state is LifecycleState.PROPOSED  # back under review
    with pytest.raises(SkillNotExecutableError):
        resolve_skill_record(rec)  # non-executable again


# ── ACTIVE record minting (formerly the 4.4 ingest) ───────────────────────────


def test_mint_active_record_is_executable_with_use_count(grove_home):
    # GRV-010 C2b — ingest_pre_faucet_skill was deleted; this proves the
    # underlying ACTIVE-record mint still yields an executable record carrying
    # use_count (the behavior the deleted wrapper relied on).
    from grove.skill_disclosure import resolve_skill_record

    name = "debugging-mcp-credentials"
    body = _proposed_body(name)
    path = _mint_active(name, "", body, use_count=2)
    assert path is not None and path.name.startswith("skill__ingested__")
    cap = Capability.from_yaml(path.read_text(encoding="utf-8"))
    assert cap.lifecycle.state is LifecycleState.ACTIVE          # executable now
    assert cap.lifecycle.provenance is Provenance.AGENT_PROPOSED
    assert cap.lifecycle.use_count == 2                           # carried
    assert resolve_skill_record(cap).startswith("<skill_reference_data>")


# ── edit / patch / delete record transitions ──────────────────────────────────


def _plant_active_skill(home: Path, name: str, body: str) -> str:
    """Place an on-disk skill + mint an ACTIVE record; return the record id."""
    d = home / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    _mint_active(name, name, body)  # state=ACTIVE
    return reg.skill_record_id_for_name(name)


def test_edit_active_skill_is_refused_in_place(grove_home):
    """In-place edits of ACTIVE skills are structurally prohibited (GRV-010 C1b-i).

    The ``is_governed_path`` / ``_require_andon_target`` wall refuses any write
    into the live ``~/.grove/skills`` tree, preserving provenance: an ACTIVE
    skill is immutable in place. Revision is NOT an edit — it is propose-a-
    successor + deprecate-the-predecessor (the operator-governed supersede
    path). A rejected edit must therefore leave the active record's state AND
    body_hash unchanged.
    """
    import tools.skill_manager_tool as smt

    name = "editable"
    old_body = _proposed_body(name)
    cap_id = _plant_active_skill(grove_home, name, old_body)
    new_body = f"---\nname: {name}\ndescription: revised\n---\nRevised steps.\n"

    out = smt._edit_skill(name, new_body)
    assert out["success"] is False                          # wall fires — no in-place edit
    assert "live ~/.grove/skills" in out["error"]           # the governed-tree refusal

    rec = next(c for c in reg.load_capabilities().values() if c.id == cap_id)
    assert rec.lifecycle.state is LifecycleState.ACTIVE              # unchanged (not REFINED)
    assert rec.lifecycle.body_hash == reg._body_hash(old_body)      # body_hash unchanged


def test_managed_skill_edit_is_refused(grove_home):
    import tools.skill_manager_tool as smt

    name = "installed-thing"
    body = _proposed_body(name)
    d = grove_home / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    reg.register_installed_skill(name, name, body)  # state=MANAGED

    out = smt._edit_skill(name, body.replace("test skill", "tampered"))
    assert out["success"] is False
    assert "managed" in out["error"].lower()


def test_delete_deprecates_record_not_hard_removed(grove_home, tmp_path, monkeypatch):
    """Delete physics under the C1b-i wall (ACTIVE skills are immutable in place).

    (a) On an agent-DELETABLE (non-governed) target, delete is terminal-graceful:
        the record PERSISTS as DEPRECATED, never hard-removed.
    (b) In-place delete of a LIVE ACTIVE governed skill is REFUSED by the
        ``is_governed_path`` wall — provenance is preserved; retirement of a
        live skill is the operator-governed supersede path, not an agent rmtree.
    """
    import agent.skill_utils as skill_utils
    import tools.skill_manager_tool as smt

    # (a) deletable, NON-governed target (an external vault outside ~/.grove).
    ext_root = tmp_path / "external_vault"
    ext_dir = ext_root / "extskill"
    ext_dir.mkdir(parents=True)
    ext_body = _proposed_body("extskill")
    (ext_dir / "SKILL.md").write_text(ext_body, encoding="utf-8")
    _mint_active("extskill", "extskill", ext_body)  # ACTIVE record for the external skill
    ext_id = reg.skill_record_id_for_name("extskill")
    # Make the external vault discoverable alongside the (empty) local tree so
    # the real _find_skill resolves it; the wall passes because it is not in
    # ~/.grove.
    local = grove_home / "skills"
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [local, ext_root])

    out = smt._delete_skill("extskill", absorbed_into="")
    assert out["success"] is True
    assert not ext_dir.exists()                                     # body removed from the vault
    rec = next((c for c in reg.load_capabilities().values() if c.id == ext_id), None)
    assert rec is not None
    assert rec.lifecycle.state is LifecycleState.DEPRECATED         # persists, not hard-removed

    # (b) LIVE ACTIVE governed skill → in-place delete REFUSED by the wall.
    cap_id = _plant_active_skill(grove_home, "deletable", _proposed_body("deletable"))
    refused = smt._delete_skill("deletable", absorbed_into="")
    assert refused["success"] is False
    assert "live ~/.grove/skills" in refused["error"]
    rec = next(c for c in reg.load_capabilities().values() if c.id == cap_id)
    assert rec.lifecycle.state is LifecycleState.ACTIVE             # unchanged; still live

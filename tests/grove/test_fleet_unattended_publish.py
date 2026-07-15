"""forge-unattended-publish-v1 P2/P3 — the fire-point and its three pre-arming
mechanisms.

Hermetic end-to-end tests of the manager forge branch: the real hard-AND gate,
the jail-rooted resolver, and — for an ARMED success — the three P3 mechanisms:
  (3) filesystem coherence: canonicalize + archive so the item renders promoted;
  (1) honest-provenance audit: a FleetPublishedUnattended memory event;
  (2) [notify prefix — tested separately in test_notify_prefix.py].

The Drive door is monkeypatched (NO real Drive write). GROVE_HOME is a tmp dir,
so the authorization overlay, the staging jail, the canonical/archive dirs, the
memory log, and the kaizen ledger all resolve inside the fixture.
"""

import json
from pathlib import Path

import pytest

import grove.fleet.manager as manager_mod
from grove.capability_registry import _state_path_for_id

SKILL_ID = "skill.fleet.forge-jobsearch"
WID, RUN = "forge", "run-abc"


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


def _stage_package(home: Path, slug: str, meta: dict) -> Path:
    slug_dir = home / "forge" / "pending_review" / slug
    slug_dir.mkdir(parents=True)
    (slug_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (slug_dir / "resume.md").write_text("# resume", encoding="utf-8")
    (slug_dir / "cover-letter.md").write_text("# cover", encoding="utf-8")
    return slug_dir


def _arm(home: Path, value) -> None:
    state_dir = home / "capabilities" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    p = _state_path_for_id(SKILL_ID, state_dir)
    p.write_text(f"id: {SKILL_ID}\npublication:\n  unattended: {value}\n", encoding="utf-8")


def _event(slug: str, **over) -> dict:
    e = {"status": "success", "skill": SKILL_ID, "slug": slug, "row_id": "ROW-1"}
    e.update(over)
    return e


def _read_memory_events(home: Path) -> list:
    p = home / "memory_records.jsonl"
    if not p.is_file():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _ledger_event_types(home: Path) -> set:
    d = home / ".kaizen_ledger"
    types = set()
    if d.is_dir():
        for f in d.glob("*.jsonl"):
            for ln in f.read_text(encoding="utf-8").splitlines():
                try:
                    types.add(json.loads(ln).get("event_type"))
                except json.JSONDecodeError:
                    pass
    return types


@pytest.fixture
def spies(monkeypatch):
    """Spy on the door, the two outcome surfaces, the proposal filer, and the
    Notion-bearing portal wrapper (must never be reached from the loop)."""
    calls = {"door": [], "event": [], "andon": [], "proposal": [], "portal_core": []}

    def door(row_id, company, role, resume_path, cover_path, **kw):
        calls["door"].append(
            dict(row_id=row_id, company=company, role=role,
                 resume_path=resume_path, cover_path=cover_path, kw=kw)
        )
        return {"status": "published", "created": True, "row_id": row_id,
                "folder_id": "FOLDER-1", "folder_link": "https://drive/FOLDER-1"}

    monkeypatch.setattr("grove.forge.publish_application_package", door)
    monkeypatch.setattr(
        manager_mod, "surface_fleet_event",
        lambda *a, **k: calls["event"].append((a, k)) or {"surfaced": True},
    )
    monkeypatch.setattr(
        manager_mod, "surface_fleet_andon",
        lambda *a, **k: calls["andon"].append((a, k)) or {"surfaced": True},
    )
    monkeypatch.setattr(
        "grove.eval.proposal_queue.file_agentless",
        lambda **k: calls["proposal"].append(k) or ("pid-1", True),
    )
    import grove.api.actions as actions
    monkeypatch.setattr(
        actions, "_forge_publish_core",
        lambda *a, **k: calls["portal_core"].append((a, k)),
    )
    return calls


def _fire(event):
    manager_mod.FleetManager()._maybe_emit_artifact_proposal(WID, RUN, event)


def test_armed_success_full_coherence(grove_home, spies):
    _stage_package(grove_home, "s1", {"row_id": "META-ROW", "company": "Acme", "role": "PM"})
    _arm(grove_home, "true")
    _fire(_event("s1"))

    # Door called with jail-rooted inputs (row_id EVENT-sourced, not meta's).
    assert len(spies["door"]) == 1
    d = spies["door"][0]
    slug_dir = grove_home / "forge" / "pending_review" / "s1"
    assert d["row_id"] == "ROW-1"
    assert (d["company"], d["role"]) == ("Acme", "PM")
    assert d["resume_path"] == str((slug_dir / "resume.md").resolve())
    assert d["kw"] == {}  # door self-acquires token; nothing extra passed

    # Mechanism 3 — canonicalized + staged dir archived → renders promoted (rule 1).
    canon = grove_home / "forge" / "s1"
    assert (canon / "resume.md").is_file() and (canon / "cover-letter.md").is_file()
    assert not slug_dir.exists()  # archived away → staged-gone
    assert list((grove_home / "forge" / ".archive").glob("s1-*"))

    # Mechanism 1 — honest-provenance audit event with folder_link.
    pubs = [e for e in _read_memory_events(grove_home)
            if e.get("__type__") == "FleetPublishedUnattended"]
    assert len(pubs) == 1
    assert pubs[0]["folder_link"] == "https://drive/FOLDER-1"
    assert pubs[0]["provenance"] == "publication.unattended"
    assert pubs[0]["unit_id"] == "ROW-1"

    # Info event carries the door's folder_link (the operator's clickable link).
    assert len(spies["event"]) == 1
    assert spies["event"][0][1]["extra"]["folder_link"] == "https://drive/FOLDER-1"

    # NO kaizen_disposition; NO proposal; NO Notion/portal-core; NO Andon.
    assert "kaizen_disposition" not in _ledger_event_types(grove_home)
    assert spies["proposal"] == []
    assert spies["portal_core"] == []
    assert spies["andon"] == []


def test_armed_publish_ok_canonicalize_fails_andons_with_link(grove_home, spies, monkeypatch):
    _stage_package(grove_home, "s2", {"row_id": "M", "company": "Acme", "role": "PM"})
    _arm(grove_home, "true")

    def boom(*a, **k):
        raise OSError("canonical write refused")

    monkeypatch.setattr(
        manager_mod.FleetManager, "_canonicalize_and_archive",
        staticmethod(boom),
    )
    _fire(_event("s2"))

    # Published on Drive, but local coherence failed → loud Andon carrying the link.
    assert len(spies["door"]) == 1
    assert len(spies["andon"]) == 1
    _a, kw = spies["andon"][0]
    assert kw.get("check") == "publish_canonicalize_failed"
    assert kw.get("extra", {}).get("folder_link") == "https://drive/FOLDER-1"
    # No misleading success info event over stuck local state; no audit either.
    assert spies["event"] == []
    assert [e for e in _read_memory_events(grove_home)
            if e.get("__type__") == "FleetPublishedUnattended"] == []


def test_armed_audit_emit_failure_is_surfaced_not_swallowed(grove_home, spies, monkeypatch):
    _stage_package(grove_home, "s8", {"row_id": "M", "company": "Acme", "role": "PM"})
    _arm(grove_home, "true")

    # The audit append raises AFTER publish + canonicalize have both succeeded.
    from grove.memory.store import MemoryStore

    def boom(self, event):
        raise OSError("memory log write refused")

    monkeypatch.setattr(MemoryStore, "append_event", boom)
    _fire(_event("s8"))

    # Publish + canonicalize STAND — never unwound.
    assert len(spies["door"]) == 1
    canon = grove_home / "forge" / "s8"
    assert (canon / "resume.md").is_file() and (canon / "cover-letter.md").is_file()
    assert not (grove_home / "forge" / "pending_review" / "s8").exists()  # archived

    # Surfaced loudly (Andon-class), carrying folder_link; NOT swallowed.
    assert len(spies["andon"]) == 1
    _a, kw = spies["andon"][0]
    assert kw.get("check") == "publish_audit_emit_failed"
    assert kw.get("extra", {}).get("folder_link") == "https://drive/FOLDER-1"
    # The Andon replaces the success info event for this run.
    assert spies["event"] == []


def test_audit_event_is_unattended_provenance_not_a_disposition(grove_home, spies):
    _stage_package(grove_home, "s3", {"row_id": "M", "company": "Acme", "role": "PM"})
    _arm(grove_home, "true")
    _fire(_event("s3"))

    pubs = [e for e in _read_memory_events(grove_home)
            if e.get("__type__") == "FleetPublishedUnattended"]
    assert len(pubs) == 1
    # Honest provenance: the grant, NOT an operator disposition.
    assert pubs[0]["provenance"] == "publication.unattended"
    # It is a fleet memory event, NOT an operator-acceptance nor a ledger disposition.
    assert pubs[0]["__type__"] != "FleetPromoteAccepted"
    assert "kaizen_disposition" not in _ledger_event_types(grove_home)


def test_armed_publish_error_fires_andon_with_partial_state(grove_home, spies, monkeypatch):
    _stage_package(grove_home, "s4", {"row_id": "M", "company": "Acme", "role": "PM"})
    _arm(grove_home, "true")

    from grove.forge.publish import PublishError

    def boom(*a, **k):
        raise PublishError("drive exploded", {"folder_id": "PARTIAL"})

    monkeypatch.setattr("grove.forge.publish_application_package", boom)
    _fire(_event("s4"))

    assert spies["proposal"] == []
    assert len(spies["andon"]) == 1
    _a, kw = spies["andon"][0]
    assert kw.get("check") == "publish_failed"
    assert kw.get("extra", {}).get("partial_state") == {"folder_id": "PARTIAL"}
    assert spies["event"] == []
    # publish-FIRST: nothing was canonicalized (staged dir untouched).
    assert (grove_home / "forge" / "pending_review" / "s4").exists()
    assert not (grove_home / "forge" / "s4").exists()


def test_armed_out_of_jail_meta_path_ignored(grove_home, spies):
    _stage_package(
        grove_home, "s5",
        {"row_id": "M", "company": "Acme", "role": "PM", "resume_path": "/etc/passwd"},
    )
    _arm(grove_home, "true")
    _fire(_event("s5"))

    slug_dir = grove_home / "forge" / "pending_review" / "s5"
    d = spies["door"][0]
    assert d["resume_path"] == str((slug_dir / "resume.md").resolve())
    assert "/etc/passwd" not in d["resume_path"]


def test_not_armed_absent_takes_proposal_path(grove_home, spies):
    _stage_package(grove_home, "s6", {"row_id": "M", "company": "Acme", "role": "PM"})
    _fire(_event("s6"))

    assert spies["door"] == []
    assert spies["event"] == []
    assert len(spies["proposal"]) == 1
    assert spies["proposal"][0]["type"] == "forge_artifact_pending"
    # not armed → nothing canonicalized, staged dir intact.
    assert (grove_home / "forge" / "pending_review" / "s6").exists()


def test_not_armed_false_takes_proposal_path(grove_home, spies):
    _stage_package(grove_home, "s7", {"row_id": "M", "company": "Acme", "role": "PM"})
    _arm(grove_home, "false")
    _fire(_event("s7"))

    assert spies["door"] == []
    assert len(spies["proposal"]) == 1
    assert spies["proposal"][0]["type"] == "forge_artifact_pending"

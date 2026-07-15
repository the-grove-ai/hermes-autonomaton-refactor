"""forge-unattended-publish-v1 P2 — the fire-point.

Hermetic end-to-end tests of the manager forge branch: the real hard-AND gate
(mode == action_surface_publish AND overlay-armed publication.unattended), the
jail-rooted resolver, and the two outcomes (published-event vs Andon). The Drive
door is monkeypatched — NO real Drive write, NO real OAuth. GROVE_HOME is a tmp
dir, so the authorization overlay, the staging jail, and every path resolve
inside the fixture.
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
    """Write a staged forge package (meta.json + the two fixed drafts)."""
    slug_dir = home / "forge" / "pending_review" / slug
    slug_dir.mkdir(parents=True)
    (slug_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (slug_dir / "resume.md").write_text("# resume", encoding="utf-8")
    (slug_dir / "cover-letter.md").write_text("# cover", encoding="utf-8")
    return slug_dir


def _arm(home: Path, value) -> None:
    """Write the operator STATE overlay that grants publication.unattended."""
    state_dir = home / "capabilities" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    p = _state_path_for_id(SKILL_ID, state_dir)
    p.write_text(f"id: {SKILL_ID}\npublication:\n  unattended: {value}\n", encoding="utf-8")


def _event(slug: str, **over) -> dict:
    e = {"status": "success", "skill": SKILL_ID, "slug": slug, "row_id": "ROW-1"}
    e.update(over)
    return e


@pytest.fixture
def spies(monkeypatch):
    """Spy on the door, the two outcome surfaces, the proposal filer, and the
    portal Notion-bearing wrapper (must never be reached from the loop)."""
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
    # file_agentless is imported lazily inside the branch from grove.eval.proposal_queue.
    monkeypatch.setattr(
        "grove.eval.proposal_queue.file_agentless",
        lambda **k: calls["proposal"].append(k) or ("pid-1", True),
    )
    # Physical isolation guard: the Notion-bearing portal wrapper must not be
    # reachable from the loop branch. Spy so a stray call would be visible.
    import grove.api.actions as actions
    monkeypatch.setattr(
        actions, "_forge_publish_core",
        lambda *a, **k: calls["portal_core"].append((a, k)),
    )
    return calls


def _fire(home, slug, event):
    manager_mod.FleetManager()._maybe_emit_artifact_proposal(WID, RUN, event)


def test_armed_success_publishes_no_proposal_no_notion(grove_home, spies):
    _stage_package(grove_home, "s1", {"row_id": "META-ROW", "company": "Acme", "role": "PM"})
    _arm(grove_home, "true")
    _fire(grove_home, "s1", _event("s1"))

    # Door called once with jail-rooted inputs: row_id EVENT-sourced, company/role
    # LABELS from meta, paths the fixed names inside the staging slug dir.
    assert len(spies["door"]) == 1
    d = spies["door"][0]
    slug_dir = grove_home / "forge" / "pending_review" / "s1"
    assert d["row_id"] == "ROW-1"  # from the event, not meta's "META-ROW"
    assert (d["company"], d["role"]) == ("Acme", "PM")
    assert d["resume_path"] == str((slug_dir / "resume.md").resolve())
    assert d["cover_path"] == str((slug_dir / "cover-letter.md").resolve())
    assert d["kw"] == {}  # no token / no gapi passed — door self-acquires

    # Published event emitted; NO proposal; NO Notion/portal-core call.
    assert len(spies["event"]) == 1
    assert spies["event"][0][1].get("event") == "fleet_published"
    assert spies["proposal"] == []
    assert spies["andon"] == []
    assert spies["portal_core"] == []


def test_armed_publish_error_fires_andon_with_partial_state(grove_home, spies, monkeypatch):
    _stage_package(grove_home, "s2", {"row_id": "M", "company": "Acme", "role": "PM"})
    _arm(grove_home, "true")

    from grove.forge.publish import PublishError

    def boom(*a, **k):
        raise PublishError("drive exploded", {"folder_id": "PARTIAL"})

    monkeypatch.setattr("grove.forge.publish_application_package", boom)
    _fire(grove_home, "s2", _event("s2"))

    assert spies["proposal"] == []  # no proposal on failure
    assert len(spies["andon"]) == 1
    _a, kw = spies["andon"][0]
    assert kw.get("check") == "publish_failed"
    assert kw.get("extra", {}).get("partial_state") == {"folder_id": "PARTIAL"}
    assert spies["event"] == []


def test_armed_out_of_jail_meta_path_ignored(grove_home, spies):
    # meta.json injects an out-of-jail resume path; the resolver must ignore it
    # and resolve the FIXED name inside the slug dir.
    _stage_package(
        grove_home, "s3",
        {"row_id": "M", "company": "Acme", "role": "PM",
         "resume_path": "/etc/passwd", "resume": "../../../../etc/passwd"},
    )
    _arm(grove_home, "true")
    _fire(grove_home, "s3", _event("s3"))

    slug_dir = grove_home / "forge" / "pending_review" / "s3"
    d = spies["door"][0]
    assert d["resume_path"] == str((slug_dir / "resume.md").resolve())
    assert "/etc/passwd" not in d["resume_path"]


def test_not_armed_absent_takes_proposal_path(grove_home, spies):
    # No overlay at all → not armed → existing proposal path, no door call.
    _stage_package(grove_home, "s4", {"row_id": "M", "company": "Acme", "role": "PM"})
    _fire(grove_home, "s4", _event("s4"))

    assert spies["door"] == []
    assert spies["event"] == []
    assert len(spies["proposal"]) == 1
    assert spies["proposal"][0]["type"] == "forge_artifact_pending"


def test_not_armed_false_takes_proposal_path(grove_home, spies):
    # Overlay present but unattended: false → not armed → proposal path.
    _stage_package(grove_home, "s5", {"row_id": "M", "company": "Acme", "role": "PM"})
    _arm(grove_home, "false")
    _fire(grove_home, "s5", _event("s5"))

    assert spies["door"] == []
    assert len(spies["proposal"]) == 1
    assert spies["proposal"][0]["type"] == "forge_artifact_pending"

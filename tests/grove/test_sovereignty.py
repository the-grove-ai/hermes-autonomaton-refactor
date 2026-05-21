"""Tests for grove.sovereignty — list, promote, reject, revoke flows."""

from __future__ import annotations

from pathlib import Path

import pytest

from grove import skills as gskills
from grove import sovereignty as gsov


_PROPOSAL = """---
name: {name}
description: {desc}
created_by: autonomaton
proposed_at: '2026-05-20T12:00:00Z'
zone: yellow
provenance:
  created_by: autonomaton
  scan_verdict: {verdict}
  scan_findings: []
---
# {name}

Body for {name}.
"""


@pytest.fixture
def fake_grove_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "fake_home"
    fake.mkdir()
    monkeypatch.setattr(gskills, "get_hermes_home", lambda: fake)
    monkeypatch.setenv("GROVE_OPERATOR_EMAIL", "jim@the-grove.ai")
    return fake


def _write_proposal(name: str, fake_home: Path, desc: str = "x", verdict: str = "safe") -> Path:
    return gskills.write_proposal(name, _PROPOSAL.format(name=name, desc=desc, verdict=verdict))


# ----- list_proposals --------------------------------------------------------

def test_list_proposals_empty(fake_grove_home: Path) -> None:
    assert gsov.list_proposals() == []


def test_list_proposals_returns_metadata(fake_grove_home: Path) -> None:
    _write_proposal("weekly-team-sync", fake_grove_home, desc="Schedule weekly sync.")
    _write_proposal("backup-photos", fake_grove_home, desc="Nightly photo backup.", verdict="caution")
    proposals = gsov.list_proposals()
    names = {p["name"] for p in proposals}
    assert names == {"weekly-team-sync", "backup-photos"}
    backup = next(p for p in proposals if p["name"] == "backup-photos")
    assert backup["scan_verdict"] == "caution"
    assert backup["description"] == "Nightly photo backup."


# ----- show_diff -------------------------------------------------------------

def test_show_diff_returns_content(fake_grove_home: Path) -> None:
    _write_proposal("weekly", fake_grove_home)
    diff = gsov.show_diff("weekly")
    assert diff is not None
    assert "Body for weekly." in diff


def test_show_diff_missing_returns_none(fake_grove_home: Path) -> None:
    assert gsov.show_diff("totally-fake") is None


def test_show_diff_lists_supporting_files(fake_grove_home: Path) -> None:
    dest = _write_proposal("weekly", fake_grove_home)
    (dest / "references").mkdir()
    (dest / "references" / "notes.md").write_text("notes")
    diff = gsov.show_diff("weekly")
    assert "supporting files" in diff
    assert "references/notes.md" in diff


# ----- promote ---------------------------------------------------------------

def test_promote_moves_dir_and_stamps_frontmatter(fake_grove_home: Path) -> None:
    _write_proposal("weekly", fake_grove_home)
    event = gsov.promote("weekly")
    assert event["action"] == "promote"
    assert event["operator"] == "jim@the-grove.ai"
    # Source no longer exists; dest does.
    assert not (fake_grove_home / "skills" / ".andon" / "weekly").exists()
    active = fake_grove_home / "skills" / "weekly"
    assert active.exists()
    # Frontmatter was stamped.
    fm, _ = gskills.parse_frontmatter((active / "SKILL.md").read_text())
    assert fm["zone"] == "green"
    assert "promoted_at" in fm
    assert fm["provenance"]["approved_by"] == "jim@the-grove.ai"


def test_promote_missing_raises(fake_grove_home: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No proposal"):
        gsov.promote("missing")


def test_promote_collision_without_replace_raises(fake_grove_home: Path) -> None:
    _write_proposal("weekly", fake_grove_home)
    # Pretend an active skill exists already.
    active = fake_grove_home / "skills" / "weekly"
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text("active version")
    with pytest.raises(FileExistsError, match="already exists"):
        gsov.promote("weekly", replace=False)


def test_promote_collision_with_replace_archives(fake_grove_home: Path) -> None:
    _write_proposal("weekly", fake_grove_home)
    active = fake_grove_home / "skills" / "weekly"
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text("active version")

    event = gsov.promote("weekly", replace=True)
    assert event["action"] == "promote"
    archive_root = fake_grove_home / "skills" / ".archive"
    assert archive_root.exists()
    archived_dirs = [d for d in archive_root.iterdir() if d.is_dir()]
    assert len(archived_dirs) == 1
    assert archived_dirs[0].name.startswith("weekly-")
    assert (archived_dirs[0] / "SKILL.md").read_text() == "active version"


# ----- reject ----------------------------------------------------------------

def test_reject_deletes_and_logs(fake_grove_home: Path) -> None:
    _write_proposal("backup-photos", fake_grove_home, verdict="dangerous")
    event = gsov.reject("backup-photos", reason="Scan flagged credential exfil pattern.")
    assert event["action"] == "reject"
    assert event["reason"] == "Scan flagged credential exfil pattern."
    assert event["scan_verdict"] == "dangerous"
    assert not (fake_grove_home / "skills" / ".andon" / "backup-photos").exists()


def test_reject_missing_raises(fake_grove_home: Path) -> None:
    with pytest.raises(FileNotFoundError):
        gsov.reject("nothing-here")


# ----- revoke ----------------------------------------------------------------

def test_revoke_moves_back_and_strips_promotion(fake_grove_home: Path) -> None:
    # First promote a proposal so we have an active skill to revoke.
    _write_proposal("weekly", fake_grove_home)
    gsov.promote("weekly")
    assert (fake_grove_home / "skills" / "weekly").exists()

    event = gsov.revoke("weekly")
    assert event["action"] == "revoke"
    # Active gone, proposal back.
    assert not (fake_grove_home / "skills" / "weekly").exists()
    proposal = fake_grove_home / "skills" / ".andon" / "weekly"
    assert proposal.exists()
    # Promotion frontmatter stripped.
    fm, _ = gskills.parse_frontmatter((proposal / "SKILL.md").read_text())
    assert fm["zone"] == "yellow"
    assert "promoted_at" not in fm
    assert "approved_by" not in fm.get("provenance", {})


def test_revoke_collision_when_andon_already_has_proposal(fake_grove_home: Path) -> None:
    _write_proposal("weekly", fake_grove_home)
    gsov.promote("weekly")
    _write_proposal("weekly", fake_grove_home)  # New proposal under same name
    with pytest.raises(FileExistsError, match="already pending"):
        gsov.revoke("weekly")


def test_revoke_missing_raises(fake_grove_home: Path) -> None:
    with pytest.raises(FileNotFoundError):
        gsov.revoke("never-existed")

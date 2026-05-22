"""Tests for grove.skills — quarantine writes and Grove frontmatter helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from grove import skills as gskills


@pytest.fixture
def fake_grove_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point grove.skills' captured get_hermes_home at a tmp directory."""
    fake = tmp_path / "fake_home"
    fake.mkdir()
    monkeypatch.setattr(gskills, "get_hermes_home", lambda: fake)
    return fake


def test_path_helpers_layout(fake_grove_home: Path) -> None:
    assert gskills.skills_dir() == fake_grove_home / "skills"
    assert gskills.andon_dir() == fake_grove_home / "skills" / ".andon"
    assert gskills.archive_dir() == fake_grove_home / "skills" / ".archive"
    assert gskills.proposal_path("weekly") == fake_grove_home / "skills" / ".andon" / "weekly"
    assert gskills.active_path("weekly") == fake_grove_home / "skills" / "weekly"


def test_operator_email_unset_returns_unknown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("GROVE_OPERATOR_EMAIL", raising=False)
    import logging
    with caplog.at_level(logging.WARNING, logger="grove.skills"):
        result = gskills.operator_email()
    assert result == "unknown"
    assert any("GROVE_OPERATOR_EMAIL" in r.getMessage() for r in caplog.records)


def test_operator_email_set_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_OPERATOR_EMAIL", "jim@the-grove.ai")
    assert gskills.operator_email() == "jim@the-grove.ai"


def test_parse_frontmatter_round_trip() -> None:
    src = "---\nname: foo\ndescription: bar\n---\n# Body\n"
    fm, body = gskills.parse_frontmatter(src)
    assert fm == {"name": "foo", "description": "bar"}
    assert body == "# Body\n"
    re_emitted = gskills.serialize_frontmatter(fm, body)
    assert "name: foo" in re_emitted
    assert "# Body" in re_emitted


def test_parse_frontmatter_no_frontmatter_raises() -> None:
    with pytest.raises(ValueError, match="no YAML frontmatter block"):
        gskills.parse_frontmatter("# just a body\n")


def test_stamp_proposal_frontmatter_adds_grove_fields() -> None:
    src = "---\nname: weekly\ndescription: Schedule weekly sync.\n---\n# body\n"
    stamped = gskills.stamp_proposal_frontmatter(
        src, scan_verdict="safe", scan_findings=[]
    )
    fm, _ = gskills.parse_frontmatter(stamped)
    assert fm["created_by"] == "autonomaton"
    assert fm["zone"] == "yellow"
    assert "proposed_at" in fm
    assert fm["provenance"]["created_by"] == "autonomaton"
    assert fm["provenance"]["scan_verdict"] == "safe"
    assert fm["provenance"]["scan_findings"] == []
    # Original fields preserved
    assert fm["name"] == "weekly"
    assert fm["description"] == "Schedule weekly sync."


def test_stamp_proposal_frontmatter_records_findings() -> None:
    src = "---\nname: weekly\ndescription: x\n---\n"
    findings = [{"pattern_id": "p1", "severity": "high", "category": "x"}]
    stamped = gskills.stamp_proposal_frontmatter(
        src, scan_verdict="caution", scan_findings=findings
    )
    fm, _ = gskills.parse_frontmatter(stamped)
    assert fm["provenance"]["scan_verdict"] == "caution"
    assert fm["provenance"]["scan_findings"] == findings


def test_stamp_promotion_frontmatter() -> None:
    src = "---\nname: weekly\ndescription: x\ncreated_by: autonomaton\nzone: yellow\n---\n"
    promoted = gskills.stamp_promotion_frontmatter(src, operator="jim@the-grove.ai")
    fm, _ = gskills.parse_frontmatter(promoted)
    assert fm["zone"] == "green"
    assert "promoted_at" in fm
    assert fm["provenance"]["approved_by"] == "jim@the-grove.ai"


def test_strip_promotion_frontmatter_reverts() -> None:
    src = (
        "---\nname: weekly\nzone: green\npromoted_at: '2026-01-01T00:00:00Z'\n"
        "provenance:\n  approved_by: jim@the-grove.ai\n  scan_verdict: safe\n---\n"
    )
    reverted = gskills.strip_promotion_frontmatter(src)
    fm, _ = gskills.parse_frontmatter(reverted)
    assert fm["zone"] == "yellow"
    assert "promoted_at" not in fm
    assert "approved_by" not in fm["provenance"]
    # Other provenance fields preserved
    assert fm["provenance"]["scan_verdict"] == "safe"


def test_write_proposal_creates_andon_dir(fake_grove_home: Path) -> None:
    src = "---\nname: weekly\ndescription: x\n---\n# body\n"
    dest = gskills.write_proposal("weekly", src)
    assert dest == fake_grove_home / "skills" / ".andon" / "weekly"
    assert (dest / "SKILL.md").exists()
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == src


def test_write_proposal_overwrites_existing(fake_grove_home: Path) -> None:
    src1 = "---\nname: weekly\ndescription: v1\n---\n# v1\n"
    src2 = "---\nname: weekly\ndescription: v2\n---\n# v2\n"
    gskills.write_proposal("weekly", src1)
    dest = gskills.write_proposal("weekly", src2)
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == src2


# ----- Sprint 15: tier / register / lineage + promotion_history --------------

def test_stamp_proposal_frontmatter_writes_extension_fields() -> None:
    src = "---\nname: weekly\ndescription: x\n---\n# body\n"
    stamped = gskills.stamp_proposal_frontmatter(
        src,
        tier="T2",
        register="strategic-concise",
        lineage=["calendar-check", "weekly-sync"],
    )
    fm, _ = gskills.parse_frontmatter(stamped)
    assert fm["tier"] == "T2"
    assert fm["register"] == "strategic-concise"
    assert fm["lineage"] == ["calendar-check", "weekly-sync"]


def test_stamp_proposal_frontmatter_extension_defaults() -> None:
    src = "---\nname: weekly\ndescription: x\n---\n# body\n"
    fm, _ = gskills.parse_frontmatter(gskills.stamp_proposal_frontmatter(src))
    assert fm["tier"] is None
    assert fm["register"] is None
    assert fm["lineage"] == []


def test_append_promotion_history_creates_list() -> None:
    src = "---\nname: weekly\ndescription: x\n---\n# body\n"
    out = gskills.append_promotion_history(
        src, action="promote", operator="jim@the-grove.ai",
        timestamp="2026-05-20T09:11:44Z",
    )
    fm, _ = gskills.parse_frontmatter(out)
    assert fm["promotion_history"] == [
        {
            "action": "promote",
            "timestamp": "2026-05-20T09:11:44Z",
            "operator": "jim@the-grove.ai",
        }
    ]


def test_append_promotion_history_accumulates() -> None:
    src = "---\nname: weekly\ndescription: x\n---\n# body\n"
    out = gskills.append_promotion_history(
        src, action="promote", operator="jim@the-grove.ai",
        timestamp="2026-05-20T09:00:00Z",
    )
    out = gskills.append_promotion_history(
        out, action="revoke", operator="jim@the-grove.ai",
        timestamp="2026-05-21T14:30:00Z",
    )
    fm, _ = gskills.parse_frontmatter(out)
    history = fm["promotion_history"]
    assert [e["action"] for e in history] == ["promote", "revoke"]
    assert history[1]["timestamp"] == "2026-05-21T14:30:00Z"


def test_unknown_frontmatter_keys_preserved() -> None:
    """The validator is permissive — Grove and upstream fields both survive
    a stamp + history round-trip (Andon A2: keys preserved, not stripped)."""
    src = (
        "---\nname: weekly\ndescription: x\n"
        "license: MIT\nmetadata:\n  custom_upstream_key: keep-me\n"
        "---\n# body\n"
    )
    stamped = gskills.stamp_proposal_frontmatter(src, tier="T2")
    out = gskills.append_promotion_history(
        stamped, action="promote", operator="jim@the-grove.ai",
    )
    fm, _ = gskills.parse_frontmatter(out)
    assert fm["license"] == "MIT"
    assert fm["metadata"] == {"custom_upstream_key": "keep-me"}
    assert fm["tier"] == "T2"
    assert fm["promotion_history"][0]["action"] == "promote"

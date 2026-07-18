"""dock-goal-ref-integrity-v1 M5 — migration sweep tests.

Fixture corpus covers all three targets plus the R3 career-transition
exclusion and idempotency. The script is imported from scripts/ by path
(tests/scripts precedent) and driven through ``main(argv)``.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tarfile
import time
from pathlib import Path

import pytest
import yaml

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "migrate_goal_refs.py"
)
_spec = importlib.util.spec_from_file_location("migrate_goal_refs", _SCRIPT)
migrate = importlib.util.module_from_spec(_spec)
# dataclasses resolves cls.__module__ through sys.modules — register before exec.
sys.modules["migrate_goal_refs"] = migrate
_spec.loader.exec_module(migrate)


# ── fixture corpus ──────────────────────────────────────────────────────


def _page_text(refs, title="A Page"):
    """Render a page the way pipeline._render does — stable key order,
    yaml.safe_dump(sort_keys=False), fenced body."""
    fm = {
        "title": title,
        "source_type": "x",
        "source": "src",
        "created_at": "2026-07-01T00:00:00+00:00",
        "updated_at": "2026-07-01T00:00:00+00:00",
        "confidence": 0.9,
        "dock_goal_refs": refs,
        "topics": ["t1"],
        "key_entities": ["e1"],
    }
    return (
        "---\n"
        + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
        + "---\n\nThe body text.\n"
    )


def _event_line(record_id, dock_goal_ref):
    data = {
        "__type__": "MemoryCreated",
        "event_id": f"ev_{record_id}",
        "timestamp": "2026-07-01T00:00:00+00:00",
        "record_id": record_id,
        "entity_type": "DomainFact",
        "content": f"Content of {record_id}.",
        "confidence": 0.9,
        "dock_goal_ref": dock_goal_ref,
        "sources": [],
        "supersedes": None,
    }
    return json.dumps(data, sort_keys=True, default=str)


@pytest.fixture()
def corpus(tmp_path):
    """Wiki + memory log + dock manifest covering all three targets."""
    home = Path(os.environ["GROVE_HOME"])

    # Dock: one goal, id grow-fleet, name "Grow the Fleet".
    dock_dir = home / "dock"
    dock_dir.mkdir(parents=True, exist_ok=True)
    (dock_dir / "dock.yaml").write_text(
        yaml.safe_dump({
            "version": 1,
            "goals": [{
                "id": "grow-fleet", "name": "Grow the Fleet",
                "vector": "strategic", "status": "accelerating",
                "definition_of_done": "d", "context_sources": [],
                "keywords": [], "unlocked_skills": [],
            }],
        }),
        encoding="utf-8",
    )

    wiki = tmp_path / "wiki"
    # (a) session page poisoned with a category string.
    session_dir = wiki / "pages" / "session_compacted"
    session_dir.mkdir(parents=True)
    (session_dir / "sess-abc123.md").write_text(
        _page_text(["direct"], title="Session Doc"), encoding="utf-8"
    )
    # (b) non-session page: goal NAME + invented slug.
    brief_dir = wiki / "pages" / "researcher_brief"
    brief_dir.mkdir(parents=True)
    (brief_dir / "brief-def456.md").write_text(
        _page_text(["Grow the Fleet", "invented-x"], title="Brief"),
        encoding="utf-8",
    )
    # clean page: valid ref, must not change.
    clean_dir = wiki / "pages" / "dock_goal"
    clean_dir.mkdir(parents=True)
    (clean_dir / "goal-aaa111.md").write_text(
        _page_text(["grow-fleet"], title="Goal Page"), encoding="utf-8"
    )

    # (c) memory log: 'None' string, career-transition (R3), valid, null.
    log = home / "memory_records.jsonl"
    log.write_text(
        "\n".join([
            _event_line("mem_none", "None"),
            _event_line("mem_career", "career-transition"),
            _event_line("mem_valid", "grow-fleet"),
            _event_line("mem_null", None),
        ]) + "\n",
        encoding="utf-8",
    )
    return wiki, home, log


def _run(wiki, home, *extra):
    return migrate.main(
        ["--wiki-root", str(wiki), "--grove-home", str(home), *extra]
    )


def _make_backup(tmp_path, files):
    tar_path = tmp_path / "backup.tar"
    with tarfile.open(tar_path, "w") as tf:
        for f in files:
            tf.add(f, arcname=f"{f.parent.name}/{f.name}")
    # ensure the tar is strictly fresher than every target
    now = time.time() + 5
    os.utime(tar_path, (now, now))
    return tar_path


# ── dry-run ─────────────────────────────────────────────────────────────


def test_dry_run_census_and_writes_nothing(corpus, capsys):
    wiki, home, log = corpus
    before = {
        p: p.read_text(encoding="utf-8") for p in wiki.rglob("*.md")
    }
    before[log] = log.read_text(encoding="utf-8")

    assert _run(wiki, home) == 0

    out = capsys.readouterr().out
    assert "(a)=1 (b)=1 (c)=1" in out
    assert "'None' -> null" in out
    assert "career-transition events left in place: 1" in out
    # nothing written
    for p, text in before.items():
        assert p.read_text(encoding="utf-8") == text


# ── execute refusal guards ──────────────────────────────────────────────


def test_execute_without_backup_refuses(corpus):
    wiki, home, _log = corpus
    with pytest.raises(SystemExit):
        _run(wiki, home, "--execute")


def test_execute_with_stale_backup_refuses(corpus, tmp_path):
    wiki, home, log = corpus
    session_page = next((wiki / "pages" / "session_compacted").glob("*.md"))
    brief_page = next((wiki / "pages" / "researcher_brief").glob("*.md"))
    tar = _make_backup(tmp_path, [session_page, brief_page, log])
    # make the tar STALE: older than the targets
    os.utime(tar, (1, 1))
    assert _run(wiki, home, "--execute", "--backup-tar", str(tar)) == 2
    # nothing was touched
    assert "direct" in session_page.read_text(encoding="utf-8")


def test_execute_with_incomplete_backup_refuses(corpus, tmp_path):
    wiki, home, log = corpus
    session_page = next((wiki / "pages" / "session_compacted").glob("*.md"))
    tar = _make_backup(tmp_path, [session_page])  # missing brief + log
    assert _run(wiki, home, "--execute", "--backup-tar", str(tar)) == 2
    assert "direct" in session_page.read_text(encoding="utf-8")


# ── execute: the three targets ──────────────────────────────────────────


def _execute_ok(corpus, tmp_path):
    wiki, home, log = corpus
    session_page = next((wiki / "pages" / "session_compacted").glob("*.md"))
    brief_page = next((wiki / "pages" / "researcher_brief").glob("*.md"))
    tar = _make_backup(tmp_path, [session_page, brief_page, log])
    assert _run(wiki, home, "--execute", "--backup-tar", str(tar)) == 0
    return wiki, home, log, session_page, brief_page


def test_execute_applies_all_three_targets(corpus, tmp_path):
    wiki, home, log, session_page, brief_page = _execute_ok(corpus, tmp_path)

    # (a) session page: refs -> [], every other byte preserved.
    assert session_page.read_text(encoding="utf-8") == _page_text(
        [], title="Session Doc"
    )
    # (b) brief page: name mapped, invented dropped; rest preserved.
    assert brief_page.read_text(encoding="utf-8") == _page_text(
        ["grow-fleet"], title="Brief"
    )
    # clean page untouched.
    clean = next((wiki / "pages" / "dock_goal").glob("*.md"))
    assert clean.read_text(encoding="utf-8") == _page_text(
        ["grow-fleet"], title="Goal Page"
    )
    # (c) 'None' event nulled; career-transition + valid + null untouched.
    lines = log.read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(ln) for ln in lines]
    by_id = {d["record_id"]: d for d in parsed}
    assert by_id["mem_none"]["dock_goal_ref"] is None
    assert by_id["mem_career"]["dock_goal_ref"] == "career-transition"
    assert by_id["mem_valid"]["dock_goal_ref"] == "grow-fleet"
    assert by_id["mem_null"]["dock_goal_ref"] is None
    # R3 exclusion is byte-level: the career line is EXACTLY as staged.
    assert _event_line("mem_career", "career-transition") in lines
    # projected index rebuilt from the corrected log.
    idx = json.loads((home / "memory_index.json").read_text(encoding="utf-8"))
    assert idx["mem_none"]["dock_goal_ref"] is None
    assert idx["mem_career"]["dock_goal_ref"] == "career-transition"


def test_second_run_is_a_no_op(corpus, tmp_path, capsys):
    wiki, home, log, session_page, brief_page = _execute_ok(corpus, tmp_path)
    after_first = {
        p: p.read_text(encoding="utf-8") for p in wiki.rglob("*.md")
    }
    after_first[log] = log.read_text(encoding="utf-8")
    capsys.readouterr()

    # second EXECUTE without a backup tar: zero changes short-circuits
    # BEFORE the backup guard — idempotent no-op.
    assert _run(wiki, home, "--execute") == 0
    out = capsys.readouterr().out
    assert "(a)=0 (b)=0 (c)=0" in out
    assert "idempotent no-op" in out
    for p, text in after_first.items():
        assert p.read_text(encoding="utf-8") == text

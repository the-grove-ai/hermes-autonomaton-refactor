"""Tests for grove.wiki.pipeline.project — deterministic Dock→cellar projection.

Sprint K2 (dock-cellar-projection-v1) Phase 1. ``project()`` maps a Dock
:class:`grove.dock.Goal` to a canonical wiki page WITHOUT any LLM call — the
deterministic sibling of ``compact()``. It reuses ``_write_page`` for the
source-stable hash/glob idempotency but renders its OWN frontmatter (status +
vector included, which ``_render`` omits) and derives both timestamps from the
``dock.yaml`` mtime (RULING 3 — byte-stable unless the YAML changes; never
wall-clock).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from grove.dock import load_dock
from grove.wiki.index import WikiIndex
from grove.wiki.pipeline import _HASH_LEN, CanonicalPage, project, project_dock

# The full Dock status taxonomy (grove/dock/__init__.py _VALID_STATUSES).
_ALL_STATUSES = [
    "accelerating", "cruising", "staging", "blocked", "parked",
    "paused", "complete",
]


# ── helpers ───────────────────────────────────────────────────────────────


def _goal_dict(**over) -> dict:
    g = {
        "id": "grow-fleet",
        "name": "Grow the Fleet",
        "vector": "strategic",
        "status": "accelerating",
        "definition_of_done": "ten autonomatons in production",
        "context_sources": ["goals/grow-fleet.md"],
        "keywords": ["fleet", "scaling", "autonomaton"],
        "unlocked_skills": [],
    }
    g.update(over)
    return g


def _load_goals(tmp_path, goals, **top):
    p = _write_dock_file(tmp_path, goals, **top)
    dock = load_dock(p)
    assert dock is not None
    return dock.goals


def _write_dock_file(tmp_path, goals, **top):
    """Write a dock.yaml and return its path (project_dock loads it itself)."""
    manifest = {"version": 1, "goals": goals}
    manifest.update(top)
    p = tmp_path / "dock.yaml"
    p.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return p


def _dock_pages(wiki):
    return list((wiki / "pages" / "dock_goal").glob("*.md"))


def _seed_ghost(wiki, name: str) -> Path:
    out = wiki / "pages" / "dock_goal"
    out.mkdir(parents=True, exist_ok=True)
    p = out / name
    p.write_text(
        "---\ntitle: ghost\nsource_type: dock_goal\n---\n\nbody\n",
        encoding="utf-8",
    )
    return p


def _frontmatter(markdown: str) -> dict:
    lines = markdown.splitlines()
    assert lines[0] == "---", "page must open with a frontmatter delimiter line"
    end = lines.index("---", 1)
    return yaml.safe_load("\n".join(lines[1:end]))


def _hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:_HASH_LEN]


# ── deterministic: no LLM call ──────────────────────────────────────────────


def test_project_never_calls_t1(monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise AssertionError("project() must never call the LLM")

    monkeypatch.setattr("grove.wiki.pipeline.call_t1", _boom)
    (goal,) = _load_goals(tmp_path, [_goal_dict()])
    page = project(goal, wiki_root=tmp_path / "wiki")
    assert isinstance(page, CanonicalPage)


# ── mapping correctness ─────────────────────────────────────────────────────


def test_mapping_correctness(tmp_path):
    (goal,) = _load_goals(tmp_path, [_goal_dict()])
    page = project(goal, wiki_root=tmp_path / "wiki")

    assert page.title == "Grow the Fleet"
    assert page.source == "dock.yaml#grow-fleet"
    assert page.source_type == "dock_goal"
    assert page.dock_goal_refs == ["grow-fleet"]
    assert page.topics == ["fleet", "scaling", "autonomaton"]
    assert page.key_entities == ["fleet", "scaling", "autonomaton"]
    assert page.confidence == 1.0
    assert page.editor_ran is False

    fm = _frontmatter(page.markdown)
    assert fm["title"] == "Grow the Fleet"
    assert fm["source"] == "dock.yaml#grow-fleet"
    assert fm["source_type"] == "dock_goal"
    assert fm["status"] == "accelerating"      # RULING 2: status in frontmatter
    assert fm["vector"] == "strategic"         # RULING 2: vector in frontmatter
    assert fm["confidence"] == 1.0
    assert fm["dock_goal_refs"] == ["grow-fleet"]
    assert fm["topics"] == ["fleet", "scaling", "autonomaton"]
    assert fm["key_entities"] == ["fleet", "scaling", "autonomaton"]


def test_body_carries_goal_fields(tmp_path):
    (goal,) = _load_goals(tmp_path, [_goal_dict()])
    body = project(goal, wiki_root=tmp_path / "wiki").body
    assert "Grow the Fleet" in body                       # name
    assert "strategic" in body                            # vector
    assert "accelerating" in body                         # status
    assert "ten autonomatons in production" in body       # definition_of_done
    assert "fleet" in body                                # keywords
    assert "goals/grow-fleet.md" in body                  # context_sources


@pytest.mark.parametrize("status", _ALL_STATUSES)
def test_status_passthrough_for_each_enum_value(tmp_path, status):
    (goal,) = _load_goals(tmp_path, [_goal_dict(status=status)])
    page = project(goal, wiki_root=tmp_path / "wiki")
    assert _frontmatter(page.markdown)["status"] == status


# ── hash identity (source-stable) ───────────────────────────────────────────


def test_hash_stable_for_fixed_id_across_title_drift(tmp_path):
    wiki = tmp_path / "wiki"
    (goal,) = _load_goals(tmp_path, [_goal_dict(id="grow-fleet")])
    p1 = project(goal, wiki_root=wiki).path
    # Same id, drifted title → same source hash, new slug, old slug gone.
    (goal2,) = _load_goals(tmp_path, [_goal_dict(id="grow-fleet", name="Renamed")])
    p2 = project(goal2, wiki_root=wiki).path

    expected = _hash("dock.yaml#grow-fleet")
    assert p1.name.endswith(f"-{expected}.md")
    assert p2.name.endswith(f"-{expected}.md")
    files = list((wiki / "pages" / "dock_goal").glob("*.md"))
    assert len(files) == 1


def test_hash_distinct_across_ids(tmp_path):
    wiki = tmp_path / "wiki"
    goals = _load_goals(tmp_path, [_goal_dict(id="alpha"), _goal_dict(id="beta")])
    pages = [project(g, wiki_root=wiki) for g in goals]
    assert pages[0].path.name.endswith(f"-{_hash('dock.yaml#alpha')}.md")
    assert pages[1].path.name.endswith(f"-{_hash('dock.yaml#beta')}.md")
    assert pages[0].path != pages[1].path
    assert len(list((wiki / "pages" / "dock_goal").glob("*.md"))) == 2


# ── timestamps from dock.yaml mtime (RULING 3) ──────────────────────────────


def test_timestamps_from_dock_yaml_mtime(tmp_path):
    (goal,) = _load_goals(tmp_path, [_goal_dict()])
    dock_yaml = tmp_path / "dock.yaml"
    expected = datetime.fromtimestamp(
        dock_yaml.stat().st_mtime, tz=timezone.utc
    ).isoformat()
    page = project(goal, wiki_root=tmp_path / "wiki")
    assert page.created_at == expected
    assert page.updated_at == expected
    fm = _frontmatter(page.markdown)
    assert fm["created_at"] == expected
    assert fm["updated_at"] == expected


def test_reproject_unchanged_is_byte_identical(tmp_path):
    """No wall-clock leak: re-projecting an unchanged goal yields the same
    bytes (mtime-derived timestamps; deterministic body/frontmatter)."""
    wiki = tmp_path / "wiki"
    (goal,) = _load_goals(tmp_path, [_goal_dict()])
    page1 = project(goal, wiki_root=wiki)
    bytes1 = page1.path.read_bytes()

    # Reload the SAME (untouched) dock.yaml and reproject.
    dock2 = load_dock(tmp_path / "dock.yaml")
    (goal2,) = dock2.goals
    page2 = project(goal2, wiki_root=wiki)
    assert page2.path == page1.path
    assert page2.path.read_bytes() == bytes1


# ── end-to-end: projected page satisfies the index contract ─────────────────


def test_projected_page_is_index_parseable_and_retrievable(tmp_path):
    wiki = tmp_path / "wiki"
    (goal,) = _load_goals(
        tmp_path,
        [_goal_dict(name="Quantum Tunneling Initiative",
                    keywords=["quantum", "tunneling"])],
    )
    project(goal, wiki_root=wiki)
    idx = WikiIndex(wiki_root=wiki)
    idx.build_index()
    results = idx.query("quantum tunneling", k=5)
    assert len(results) == 1
    assert results[0].source_type == "dock_goal"
    assert results[0].dock_goal_refs == ["grow-fleet"]
    assert results[0].confidence == 1.0


# ════════════════════════════════════════════════════════════════════════════
# P2 — project_dock(): reconcile (project all) + set-difference reap.
# ════════════════════════════════════════════════════════════════════════════


def test_project_dock_n_goals_n_pages(tmp_path):
    wiki = tmp_path / "wiki"
    dp = _write_dock_file(
        tmp_path, [_goal_dict(id="a"), _goal_dict(id="b"), _goal_dict(id="c")]
    )
    pages = project_dock(wiki_root=wiki, dock_path=dp)
    assert len(pages) == 3
    assert len(_dock_pages(wiki)) == 3


def test_project_dock_title_edit_updates_same_hash(tmp_path):
    wiki = tmp_path / "wiki"
    dp = _write_dock_file(tmp_path, [_goal_dict(id="a", name="Original")])
    project_dock(wiki_root=wiki, dock_path=dp)
    h = _hash("dock.yaml#a")
    old = wiki / "pages" / "dock_goal" / f"original-{h}.md"
    assert old.exists()

    dp = _write_dock_file(tmp_path, [_goal_dict(id="a", name="Renamed Title")])
    project_dock(wiki_root=wiki, dock_path=dp)

    files = _dock_pages(wiki)
    assert len(files) == 1
    assert files[0].name == f"renamed-title-{h}.md"   # same hash, new slug
    assert not old.exists()                            # old slug gone


def test_project_dock_deleted_goal_is_reaped(tmp_path):
    wiki = tmp_path / "wiki"
    dp = _write_dock_file(tmp_path, [_goal_dict(id="a"), _goal_dict(id="b")])
    project_dock(wiki_root=wiki, dock_path=dp)
    assert len(_dock_pages(wiki)) == 2

    dp = _write_dock_file(tmp_path, [_goal_dict(id="a")])   # b removed
    project_dock(wiki_root=wiki, dock_path=dp)

    files = _dock_pages(wiki)
    assert len(files) == 1
    assert files[0].name.endswith(f"-{_hash('dock.yaml#a')}.md")


def test_project_dock_completed_goal_persists_with_status_flipped(tmp_path):
    """Reap is on ABSENCE only — a goal flipped to complete still has a page."""
    wiki = tmp_path / "wiki"
    dp = _write_dock_file(tmp_path, [_goal_dict(id="a", status="accelerating")])
    project_dock(wiki_root=wiki, dock_path=dp)

    dp = _write_dock_file(tmp_path, [_goal_dict(id="a", status="complete")])
    project_dock(wiki_root=wiki, dock_path=dp)

    files = _dock_pages(wiki)
    assert len(files) == 1
    assert _frontmatter(files[0].read_text())["status"] == "complete"


def test_project_dock_manual_extra_file_self_heals(tmp_path):
    wiki = tmp_path / "wiki"
    dp = _write_dock_file(tmp_path, [_goal_dict(id="a")])
    project_dock(wiki_root=wiki, dock_path=dp)
    ghost = _seed_ghost(wiki, "ghost-deadbeef.md")   # 8 hex, not a live goal

    project_dock(wiki_root=wiki, dock_path=dp)
    assert not ghost.exists()
    assert len(_dock_pages(wiki)) == 1


def test_project_dock_reap_keys_on_hash_not_slug(tmp_path):
    """A decoy sharing a live goal's SLUG but a foreign hash is still reaped."""
    wiki = tmp_path / "wiki"
    dp = _write_dock_file(tmp_path, [_goal_dict(id="a", name="Same Slug")])
    project_dock(wiki_root=wiki, dock_path=dp)
    live_hash = _hash("dock.yaml#a")
    decoy = _seed_ghost(wiki, f"same-slug-{'0' * _HASH_LEN}.md")

    project_dock(wiki_root=wiki, dock_path=dp)
    assert not decoy.exists()                  # reaped by hash mismatch
    files = _dock_pages(wiki)
    assert len(files) == 1
    assert files[0].name.endswith(f"-{live_hash}.md")


def test_project_dock_absent_manifest_is_noop(tmp_path):
    """GUARD P2-b (None path): absent dock.yaml → no-op; reap NOTHING."""
    wiki = tmp_path / "wiki"
    keep = _seed_ghost(wiki, f"keep-{'a' * _HASH_LEN}.md")

    result = project_dock(wiki_root=wiki, dock_path=tmp_path / "nope" / "dock.yaml")
    assert result == []
    assert keep.exists()


def test_project_dock_empty_goals_reaps_all(tmp_path):
    """GUARD P2-b (empty path): present dock.yaml with zero goals → Expected
    empty → reap ALL dock_goal pages (pure projection mirrors the source)."""
    wiki = tmp_path / "wiki"
    dp = _write_dock_file(tmp_path, [_goal_dict(id="a"), _goal_dict(id="b")])
    project_dock(wiki_root=wiki, dock_path=dp)
    assert len(_dock_pages(wiki)) == 2

    dp = _write_dock_file(tmp_path, [])    # present, but empty
    result = project_dock(wiki_root=wiki, dock_path=dp)
    assert result == []
    assert _dock_pages(wiki) == []


def test_project_dock_combined_reconcile_one_pass(tmp_path):
    """GUARD P2-c: in ONE pass — drift updates, deletion reaps, completion
    persists — the project-then-reap axes compose."""
    wiki = tmp_path / "wiki"
    out = wiki / "pages" / "dock_goal"
    dp = _write_dock_file(tmp_path, [
        _goal_dict(id="drift", name="Before Drift"),
        _goal_dict(id="gone", name="To Be Deleted"),
        _goal_dict(id="finish", name="Finishing", status="accelerating"),
    ])
    project_dock(wiki_root=wiki, dock_path=dp)
    assert len(_dock_pages(wiki)) == 3
    drift_old = out / f"before-drift-{_hash('dock.yaml#drift')}.md"
    assert drift_old.exists()

    dp = _write_dock_file(tmp_path, [
        _goal_dict(id="drift", name="After Drift"),
        _goal_dict(id="finish", name="Finishing", status="complete"),
    ])
    project_dock(wiki_root=wiki, dock_path=dp)

    names = {f.name for f in _dock_pages(wiki)}
    assert len(names) == 2
    # drifted: new slug, same hash, old slug gone
    assert not drift_old.exists()
    assert f"after-drift-{_hash('dock.yaml#drift')}.md" in names
    # deleted: reaped
    assert not any(_hash("dock.yaml#gone") in n for n in names)
    # completed: persists with status flipped
    finish = out / f"finishing-{_hash('dock.yaml#finish')}.md"
    assert finish.exists()
    assert _frontmatter(finish.read_text())["status"] == "complete"


def test_project_dock_all_pages_share_one_mtime_stamp(tmp_path):
    """GUARD P2-a: every page in a reconcile carries the SAME dock.yaml-derived
    timestamp (single threaded source), matching the manifest mtime."""
    wiki = tmp_path / "wiki"
    dp = _write_dock_file(tmp_path, [_goal_dict(id="a"), _goal_dict(id="b")])
    expected = datetime.fromtimestamp(
        dp.stat().st_mtime, tz=timezone.utc
    ).isoformat()
    pages = project_dock(wiki_root=wiki, dock_path=dp)
    assert {p.created_at for p in pages} == {expected}
    assert {p.updated_at for p in pages} == {expected}

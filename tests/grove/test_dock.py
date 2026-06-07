"""Tests for grove.dock — Sprint 68 the-dock-v1.

Component 2 coverage: manifest parsing + fail-loud validation, status
filtering, the classifier OPERATOR GOALS block, and the Obsidian-race
``_safe_read`` retry wrapper. Components 4 (budget guard) and 5 (conflict
resolution) extend this file in their own commits.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest
import yaml

from grove import dock as dock_mod
from grove.dock import (
    ACTIVE_STATUSES,
    Dock,
    Goal,
    active_goals,
    build_classifier_goals_block,
    load_dock,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_MANIFEST = _REPO_ROOT / "config" / "dock" / "dock.yaml"


# ── helpers ───────────────────────────────────────────────────────────────


def _minimal_goal(**over) -> dict:
    g = {
        "id": "g1",
        "name": "Goal One",
        "vector": "strategic",
        "status": "accelerating",
        "definition_of_done": "done when shipped",
        "context_sources": ["goals/g1.md"],
        "keywords": ["alpha", "beta"],
        "unlocked_skills": [],
    }
    g.update(over)
    return g


def _write_dock(tmp_path: Path, goals: List[dict], **top) -> Path:
    manifest = {"version": 1, "goals": goals}
    manifest.update(top)
    p = tmp_path / "dock.yaml"
    p.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return p


# ── load_dock: absence + the real seed ──────────────────────────────────


def test_missing_manifest_is_graceful(tmp_path):
    """Absent dock.yaml → None (Dock not installed), no raise."""
    assert load_dock(tmp_path / "nope" / "dock.yaml") is None


def test_no_path_resolves_runtime_and_is_absent_under_hermetic_home():
    """With the per-test GROVE_HOME tempdir empty, load_dock() → None."""
    assert load_dock() is None


def test_seed_template_parses():
    """The committed config/dock/dock.yaml seed parses to three goals."""
    dock = load_dock(_SEED_MANIFEST)
    assert dock is not None
    ids = {g.id for g in dock.goals}
    assert ids == {"grv-001-humanity-ai", "influencer-outreach", "carriage-house"}
    assert dock.context_char_budget == 4000
    by_id = {g.id: g for g in dock.goals}
    assert by_id["grv-001-humanity-ai"].vector == "apex_strategic"
    assert by_id["carriage-house"].vector == "personal"


# ── load_dock: fail-loud validation ─────────────────────────────────────


def test_not_a_mapping_raises(tmp_path):
    p = tmp_path / "dock.yaml"
    p.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a mapping"):
        load_dock(p)


def test_bad_version_raises(tmp_path):
    p = _write_dock(tmp_path, [_minimal_goal()], version=2)
    # rewrite with explicit bad version (helper forces version=1 first)
    p.write_text(yaml.safe_dump({"version": 2, "goals": [_minimal_goal()]}),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported version"):
        load_dock(p)


def test_goals_not_list_raises(tmp_path):
    p = tmp_path / "dock.yaml"
    p.write_text(yaml.safe_dump({"version": 1, "goals": {"a": 1}}),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="goals must be a list"):
        load_dock(p)


def test_missing_goal_keys_raises(tmp_path):
    bad = {"id": "g1", "name": "x"}  # missing the rest
    p = tmp_path / "dock.yaml"
    p.write_text(yaml.safe_dump({"version": 1, "goals": [bad]}),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="missing keys"):
        load_dock(p)


def test_bad_vector_raises(tmp_path):
    p = _write_dock(tmp_path, [_minimal_goal(vector="cosmic")])
    with pytest.raises(ValueError, match="vector"):
        load_dock(p)


def test_bad_status_raises(tmp_path):
    p = _write_dock(tmp_path, [_minimal_goal(status="vibing")])
    with pytest.raises(ValueError, match="status"):
        load_dock(p)


def test_duplicate_id_raises(tmp_path):
    p = _write_dock(tmp_path, [_minimal_goal(), _minimal_goal()])
    with pytest.raises(ValueError, match="duplicate goal id"):
        load_dock(p)


def test_bad_budget_raises(tmp_path):
    p = _write_dock(tmp_path, [_minimal_goal()], context_char_budget=0)
    with pytest.raises(ValueError, match="context_char_budget"):
        load_dock(p)


def test_list_field_must_be_list(tmp_path):
    p = _write_dock(tmp_path, [_minimal_goal(keywords="not-a-list")])
    with pytest.raises(ValueError, match="keywords must be a list"):
        load_dock(p)


# ── active_goals: status filtering ──────────────────────────────────────


def test_active_goals_filters_by_status(tmp_path):
    goals = [
        _minimal_goal(id="a", status="accelerating"),
        _minimal_goal(id="c", status="cruising"),
        _minimal_goal(id="p", status="paused"),
        _minimal_goal(id="d", status="complete"),
    ]
    dock = load_dock(_write_dock(tmp_path, goals))
    active_ids = {g.id for g in active_goals(dock)}
    assert active_ids == {"a", "c"}
    assert ACTIVE_STATUSES == frozenset({"accelerating", "cruising"})


# ── build_classifier_goals_block ────────────────────────────────────────


def test_classifier_block_renders_active_goals(tmp_path):
    goals = [
        _minimal_goal(id="a", name="Alpha", status="accelerating",
                      definition_of_done="alpha is shipped"),
        _minimal_goal(id="p", name="Paused", status="paused"),
    ]
    dock = load_dock(_write_dock(tmp_path, goals))
    block = build_classifier_goals_block(dock)
    assert "Alpha" in block
    assert "alpha is shipped" in block
    assert "Paused" not in block            # paused excluded
    assert "CLASSIFICATION DIRECTIVE" in block
    assert "no_goals_set" in block          # the directive names the trap


def test_classifier_block_empty_when_no_active(tmp_path):
    goals = [_minimal_goal(id="p", status="paused")]
    dock = load_dock(_write_dock(tmp_path, goals))
    assert build_classifier_goals_block(dock) == ""


# ── _safe_read: Obsidian-race retry ─────────────────────────────────────


class _FlakyPath:
    """A path-like that raises FileNotFoundError ``fail_times`` times."""

    def __init__(self, fail_times: int, payload: str = "ok"):
        self._left = fail_times
        self._payload = payload
        self.label = "flaky.md"

    def read_text(self, encoding="utf-8"):
        if self._left > 0:
            self._left -= 1
            raise FileNotFoundError(self.label)
        return self._payload

    def __fspath__(self):
        return self.label


def test_safe_read_success(tmp_path):
    f = tmp_path / "ok.md"
    f.write_text("hello", encoding="utf-8")
    sleeps: List[float] = []
    assert dock_mod._safe_read(f, sleep=sleeps.append) == "hello"
    assert sleeps == []                     # no retries on a clean read


def test_safe_read_retries_then_succeeds():
    sleeps: List[float] = []
    flaky = _FlakyPath(fail_times=2, payload="recovered")
    out = dock_mod._safe_read(flaky, sleep=sleeps.append)
    assert out == "recovered"
    assert sleeps == [0.1, 0.2]             # 100ms, 200ms backoff, then read


def test_safe_read_fails_loud_after_retries():
    sleeps: List[float] = []
    flaky = _FlakyPath(fail_times=99)
    with pytest.raises(OSError, match="could not read"):
        dock_mod._safe_read(flaky, sleep=sleeps.append)
    assert sleeps == [0.1, 0.2, 0.4]        # 3 retries, then fail-loud

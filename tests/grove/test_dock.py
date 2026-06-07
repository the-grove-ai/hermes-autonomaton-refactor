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
    build_turn_goal_context,
    load_dock,
    load_goal_context,
    resolve_goal,
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
    """The committed config/dock/dock.yaml seed parses to nine goals.

    Sprint 69.2 replaced the 3-goal seed with the operator's expanded
    9-goal Dock (version "1.0", new vectors/statuses, no explicit budget).
    """
    dock = load_dock(_SEED_MANIFEST)
    assert dock is not None
    ids = {g.id for g in dock.goals}
    assert ids == {
        "humanity-ai-funding", "hermes-autonomaton", "grove-content-pipeline",
        "influencer-outreach", "advisory-board", "grove-site", "lambda-watch",
        "carriage-house-renovation", "personal-finance",
    }
    # No explicit context_char_budget in the seed → the 5000 default.
    assert dock.context_char_budget == 5000
    by_id = {g.id: g for g in dock.goals}
    assert by_id["humanity-ai-funding"].vector == "apex_strategic"
    assert by_id["grove-site"].vector == "operational"
    assert by_id["lambda-watch"].vector == "product"
    assert by_id["carriage-house-renovation"].vector == "personal"
    # Only accelerating + cruising are active; lambda-watch (staging) is not.
    active = {g.id for g in active_goals(dock)}
    assert len(active) == 8
    assert "lambda-watch" not in active


def test_seed_goal_files_within_budget():
    """Every committed seed goal file fits the 5000-char budget on its own.

    Guards the operator-authored context files against silently blowing the
    per-turn budget — the leading source of every goal loads in full.
    """
    goals_dir = _REPO_ROOT / "config" / "dock" / "goals"
    files = sorted(goals_dir.glob("*.md"))
    assert files, "no seed goal files found"
    for f in files:
        assert len(f.read_text(encoding="utf-8")) <= 5000, f


def test_seed_version_string_accepted():
    """The seed declares version "1.0" (string) — loader coerces, not crashes."""
    raw = yaml.safe_load(_SEED_MANIFEST.read_text(encoding="utf-8"))
    assert raw["version"] == "1.0"
    assert load_dock(_SEED_MANIFEST) is not None


def test_seed_passes_unknown_keys_through():
    """Expanded top-level + per-goal keys are reachable, not dropped."""
    dock = load_dock(_SEED_MANIFEST)
    assert "routing_hints" in dock.raw
    assert "operator_preferences" in dock.raw
    assert "design_system" in dock.raw
    by_id = {g.id: g for g in dock.goals}
    # apex goal carries milestones + why_this_matters in extra
    assert "milestones" in by_id["humanity-ai-funding"].extra
    assert "why_this_matters" in by_id["humanity-ai-funding"].extra


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


# ── Component 3: per-turn goal-context injection (Path A′) ───────────────


def _dock_with_context(
    tmp_path: Path,
    goals: List[dict],
    files: dict,
    *,
    context_char_budget_top: int = None,
) -> Dock:
    """Write a dock.yaml plus its goals/*.md context files; load it."""
    (tmp_path / "goals").mkdir(exist_ok=True)
    for rel, body in files.items():
        (tmp_path / rel).write_text(body, encoding="utf-8")
    top = {}
    if context_char_budget_top is not None:
        top["context_char_budget"] = context_char_budget_top
    return load_dock(_write_dock(tmp_path, goals, **top))


def test_resolve_goal_single_keyword_match(tmp_path):
    dock = load_dock(_write_dock(tmp_path, [
        _minimal_goal(id="house", keywords=["epoxy", "carriage house"]),
        _minimal_goal(id="other", keywords=["calendar"]),
    ]))
    g = resolve_goal(dock, "best options for epoxy flooring?")
    assert g is not None and g.id == "house"


def test_resolve_goal_no_match_returns_none(tmp_path):
    dock = load_dock(_write_dock(tmp_path, [
        _minimal_goal(id="house", keywords=["epoxy"]),
    ]))
    assert resolve_goal(dock, "what's the weather today") is None


def test_resolve_goal_multimatch_picks_highest_vector(tmp_path):
    """Provisional Component 3 behavior — Component 5 adds history tiebreak."""
    dock = load_dock(_write_dock(tmp_path, [
        _minimal_goal(id="apex", vector="apex_strategic", keywords=["doctorow"]),
        _minimal_goal(id="strat", vector="strategic", keywords=["email"]),
    ]))
    g = resolve_goal(dock, "draft an email to doctorow")
    assert g.id == "apex"                   # apex_strategic > strategic


def test_load_goal_context_reads_sources(tmp_path):
    dock = _dock_with_context(
        tmp_path,
        [_minimal_goal(id="house", context_sources=["goals/house.md"])],
        {"goals/house.md": "---\nsummary: x\n---\nIndianapolis, unheated."},
    )
    house = dock.goals[0]
    out = load_goal_context(house, dock.context_char_budget)
    assert "Indianapolis, unheated." in out


def test_build_turn_goal_context_single_match_emits_fenced_block(tmp_path):
    dock = _dock_with_context(
        tmp_path,
        [_minimal_goal(id="carriage-house", name="Carriage House",
                       keywords=["epoxy", "carriage house"],
                       context_sources=["goals/ch.md"])],
        {"goals/ch.md": "Unheated structure, freeze-thaw."},
    )
    tgc = build_turn_goal_context(dock, message="epoxy flooring options?")
    assert tgc is not None
    assert tgc.goal_id == "carriage-house"
    assert tgc.block.startswith('<grove-dock goal="carriage-house">')
    assert tgc.block.endswith("</grove-dock>")
    assert "Do NOT be overbearing" in tgc.block       # Superposition framing
    assert "Unheated structure, freeze-thaw." in tgc.block  # loaded context


def test_build_turn_goal_context_no_match_returns_none(tmp_path):
    dock = _dock_with_context(
        tmp_path,
        [_minimal_goal(id="house", keywords=["epoxy"],
                       context_sources=["goals/h.md"])],
        {"goals/h.md": "body"},
    )
    assert build_turn_goal_context(dock, message="schedule my dentist") is None


def test_build_turn_goal_context_missing_file_fails_loud(tmp_path):
    """A goal whose promised context file is absent → fail-loud in the
    turn path (no graceful empty-string)."""
    dock = load_dock(_write_dock(tmp_path, [
        _minimal_goal(id="house", keywords=["epoxy"],
                      context_sources=["goals/gone.md"]),
    ]))
    with pytest.raises(OSError, match="could not read"):
        build_turn_goal_context(dock, message="epoxy?")


# ── Component 4: context budget guard ────────────────────────────────────


def test_parse_frontmatter_present():
    meta, body = dock_mod._parse_frontmatter(
        "---\nsummary: hi\nlatest_update: now\n---\nThe body."
    )
    assert meta == {"summary": "hi", "latest_update": "now"}
    assert body == "The body."


def test_parse_frontmatter_absent():
    meta, body = dock_mod._parse_frontmatter("No frontmatter here.")
    assert meta == {}
    assert body == "No frontmatter here."


def test_load_goal_context_full_under_budget(tmp_path):
    dock = _dock_with_context(
        tmp_path,
        [_minimal_goal(id="g", context_sources=["goals/g.md"])],
        {"goals/g.md": "---\nsummary: s\n---\nShort body."},
    )
    out = load_goal_context(dock.goals[0], char_budget=4000)
    assert "Short body." in out             # full content path


def test_load_goal_context_truncates_to_frontmatter(tmp_path):
    big_body = "X" * 500
    dock = _dock_with_context(
        tmp_path,
        [_minimal_goal(id="g", context_sources=["goals/g.md"])],
        {"goals/g.md": f"---\nsummary: short summary\nlatest_update: a tick\n---\n{big_body}"},
    )
    out = load_goal_context(dock.goals[0], char_budget=80)
    assert "X" * 500 not in out             # body dropped
    assert "short summary" in out           # digest kept
    assert "Now: a tick" in out


def test_load_goal_context_andon_when_digest_too_big(tmp_path):
    dock = _dock_with_context(
        tmp_path,
        [_minimal_goal(id="g", context_sources=["goals/g.md"])],
        {"goals/g.md": "---\nsummary: " + ("S" * 300) + "\n---\n" + ("B" * 300)},
    )
    with pytest.raises(dock_mod.DockBudgetAndon, match="cannot fit"):
        load_goal_context(dock.goals[0], char_budget=50)


def test_load_goal_context_andon_when_no_frontmatter_over_budget(tmp_path):
    dock = _dock_with_context(
        tmp_path,
        [_minimal_goal(id="g", context_sources=["goals/g.md"])],
        {"goals/g.md": "B" * 500},           # no frontmatter to fall back to
    )
    with pytest.raises(dock_mod.DockBudgetAndon):
        load_goal_context(dock.goals[0], char_budget=50)


def test_budget_andon_propagates_through_turn_context(tmp_path):
    """A budget ANDON in load must surface out of the turn orchestration."""
    dock = _dock_with_context(
        tmp_path,
        [_minimal_goal(id="g", keywords=["epoxy"], context_sources=["goals/g.md"])],
        {"goals/g.md": "B" * 5000},
        context_char_budget_top=80,
    )
    with pytest.raises(dock_mod.DockBudgetAndon):
        build_turn_goal_context(dock, message="epoxy please")


# ── Component 5: conflict resolution (Ghost Active Goal Overlap) ──────────


def test_resolve_vector_priority_wins(tmp_path):
    dock = load_dock(_write_dock(tmp_path, [
        _minimal_goal(id="strat", vector="strategic", keywords=["doctorow"]),
        _minimal_goal(id="apex", vector="apex_strategic", keywords=["doctorow"]),
    ]))
    assert resolve_goal(dock, "email to doctorow").id == "apex"


def test_resolve_vector_priority_beats_history(tmp_path):
    """History only breaks ties WITHIN the top vector — it never overrides
    a higher vector."""
    dock = load_dock(_write_dock(tmp_path, [
        _minimal_goal(id="apex", vector="apex_strategic", keywords=["doctorow"]),
        _minimal_goal(id="strat", vector="strategic", keywords=["doctorow"]),
    ]))
    g = resolve_goal(dock, "email to doctorow", history=["strat", "strat", "strat"])
    assert g.id == "apex"                   # vector priority, not momentum


def test_resolve_tie_uses_history_momentum(tmp_path):
    dock = load_dock(_write_dock(tmp_path, [
        _minimal_goal(id="alpha", vector="strategic", keywords=["shared"]),
        _minimal_goal(id="beta", vector="strategic", keywords=["shared"]),
    ]))
    # both strategic → tie; most-recent history entry among leaders wins
    assert resolve_goal(dock, "the shared topic", history=["alpha", "beta"]).id == "beta"
    assert resolve_goal(dock, "the shared topic", history=["beta", "alpha"]).id == "alpha"


def test_resolve_tie_no_history_falls_to_manifest_order(tmp_path):
    dock = load_dock(_write_dock(tmp_path, [
        _minimal_goal(id="first", vector="strategic", keywords=["shared"]),
        _minimal_goal(id="second", vector="strategic", keywords=["shared"]),
    ]))
    assert resolve_goal(dock, "shared topic", history=[]).id == "first"


def test_resolve_history_window_is_last_three(tmp_path):
    """Only the last 3 history entries count toward momentum."""
    dock = load_dock(_write_dock(tmp_path, [
        _minimal_goal(id="alpha", vector="strategic", keywords=["shared"]),
        _minimal_goal(id="beta", vector="strategic", keywords=["shared"]),
    ]))
    # 'alpha' is 4 back (outside the window); 'beta' is within → beta wins
    g = resolve_goal(dock, "shared", history=["alpha", "beta", "x", "y"])
    assert g.id == "beta"


def test_apex_beats_strategic_against_seed():
    """Worked example against the 9-goal seed: a prompt touching the apex
    goal AND a strategic goal resolves to the apex (vector priority)."""
    dock = load_dock(_SEED_MANIFEST)
    g = resolve_goal(
        dock, "advance humanity ai funding through the hermes pipeline"
    )
    # "funding" → humanity-ai-funding (apex); "hermes"/"pipeline" →
    # hermes-autonomaton (strategic). apex_strategic > strategic.
    assert g.id == "humanity-ai-funding"


# ── Sprint 69.2: expanded-schema permissiveness ──────────────────────────


def test_version_string_and_int_accepted(tmp_path):
    for v in (1, "1", "1.0"):
        p = tmp_path / "dock.yaml"
        p.write_text(yaml.safe_dump({"version": v, "goals": [_minimal_goal()]}),
                     encoding="utf-8")
        assert load_dock(p) is not None


def test_new_vectors_accepted_and_ranked(tmp_path):
    dock = load_dock(_write_dock(tmp_path, [
        _minimal_goal(id="op", vector="operational"),
        _minimal_goal(id="pr", vector="product"),
    ]))
    by_id = {g.id: g for g in dock.goals}
    # apex > strategic > operational > product > personal
    assert by_id["op"].rank > by_id["pr"].rank
    assert by_id["op"].vector == "operational"
    assert by_id["pr"].vector == "product"


def test_new_statuses_accepted_but_not_active(tmp_path):
    goals = [
        _minimal_goal(id="acc", status="accelerating"),
        _minimal_goal(id="stg", status="staging"),
        _minimal_goal(id="blk", status="blocked"),
        _minimal_goal(id="prk", status="parked"),
    ]
    dock = load_dock(_write_dock(tmp_path, goals))
    assert len(dock.goals) == 4                       # all parse, none crash
    assert {g.id for g in active_goals(dock)} == {"acc"}   # only accelerating


def test_default_budget_is_5000(tmp_path):
    dock = load_dock(_write_dock(tmp_path, [_minimal_goal()]))
    assert dock.context_char_budget == 5000


def test_unknown_keys_pass_through(tmp_path):
    goal = _minimal_goal(deadline="2026-08-31",
                         milestones=[{"name": "m1", "status": "pending"}])
    dock = load_dock(_write_dock(tmp_path, [goal],
                                 routing_hints={"patterns": []},
                                 operator_preferences={"voice": "terse"}))
    assert dock.raw["routing_hints"] == {"patterns": []}
    assert dock.raw["operator_preferences"] == {"voice": "terse"}
    g = dock.goals[0]
    assert g.extra["deadline"] == "2026-08-31"
    assert g.extra["milestones"] == [{"name": "m1", "status": "pending"}]
    # required keys are NOT duplicated into extra
    assert "id" not in g.extra and "keywords" not in g.extra


def test_resolved_sources_expands_tilde_and_absolute(tmp_path):
    goal = _minimal_goal(context_sources=[
        "goals/rel.md",                 # relative → root/goals/rel.md
        "~/abs-home.md",                # ~ → expanded home
        "/etc/abs-root.md",             # absolute → as-is
    ])
    dock = load_dock(_write_dock(tmp_path, [goal]))
    resolved = dock.goals[0].resolved_sources()
    assert resolved[0] == tmp_path / "goals" / "rel.md"
    assert resolved[1] == Path("~/abs-home.md").expanduser()
    assert resolved[1].is_absolute()
    assert resolved[2] == Path("/etc/abs-root.md")


# ── Sprint 69.2: multi-source budget loading ─────────────────────────────


def test_load_goal_context_multi_source_all_fit(tmp_path):
    dock = _dock_with_context(
        tmp_path,
        [_minimal_goal(id="g", context_sources=["goals/a.md", "goals/b.md"])],
        {"goals/a.md": "Alpha body.", "goals/b.md": "Beta body."},
    )
    out = load_goal_context(dock.goals[0], char_budget=5000)
    assert "Alpha body." in out and "Beta body." in out


def test_load_goal_context_multi_source_skips_when_budget_exhausted(tmp_path):
    """First source fits and loads; the over-budget second source (and any
    after it) are skipped — NOT an Andon (the hermes-goal shape)."""
    first = "F" * 100
    second = "S" * 5000
    dock = _dock_with_context(
        tmp_path,
        [_minimal_goal(id="g", context_sources=["goals/a.md", "goals/b.md",
                                                 "goals/c.md"])],
        {"goals/a.md": first, "goals/b.md": second, "goals/c.md": "tail"},
    )
    out = load_goal_context(dock.goals[0], char_budget=200)
    assert out == first                # only the first source survived
    assert "S" not in out and "tail" not in out

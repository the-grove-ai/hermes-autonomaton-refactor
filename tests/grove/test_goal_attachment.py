"""goal-spine-v1 P1 — goal-attachment detector (dry-run) tests.

Pins: the cursor watermark, both P3 exclusion seams, the R-5 alignment
prefilter, the R-7 absent-IntentRecord path, the config-valued Stage-2 cap,
the G2 projection-coverage Andon, the G5 single containment implementation
(app-free helper + handler delegation parity), and the P1 inertness
invariant (no ledger write, no proposal append).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

from grove.artifact_identity import artifact_id, canonical_artifact_path
from grove.dock.attachment import (
    AttachmentDryRunReport,
    GoalAttachmentDetector,
    GoalProjectionGapError,
    MalformedAdjudication,
    _load_cursor,
    _validate_adjudication,
    attached_artifact_ids,
    suppressed_goal_pairs,
    verify_goal_projection_coverage,
)
from grove.wiki.pipeline import _DOCK_GOAL_SOURCE_TYPE, _dock_source_hash
from hermes_constants import get_hermes_home


# ── fixtures ────────────────────────────────────────────────────────────────


def _iso(minute: int) -> str:
    return datetime(2026, 7, 18, 12, minute, 0, tzinfo=timezone.utc).isoformat()


def _write_ledger_events(home: Path, events: List[dict]) -> Path:
    ledger_dir = home / ".kaizen_ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / "session_test.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
    return path


def _artifact_event(path: Path, *, minute: int, turn_id="s#1") -> dict:
    canonical = canonical_artifact_path(str(path))
    return {
        "event_type": "artifact_written",
        "session_id": "session_test",
        "timestamp": _iso(minute),
        "path": canonical,
        "artifact_id": artifact_id(canonical),
        "turn_id": turn_id,
        "tool": "write_file",
        "parent_artifact_ids": [],
    }


def _goal(goal_id="goal-alpha", name="Goal Alpha"):
    return SimpleNamespace(
        id=goal_id,
        name=name,
        keywords=("alpha", "spine"),
        definition_of_done="Alpha shipped.",
        is_active=True,
    )


def _dock(*goals):
    return SimpleNamespace(goals=tuple(goals))


class FakeWiki:
    """Stage-1 stand-in: returns one dock_goal hit per query."""

    def __init__(self, wiki_root: Path, goal_id="goal-alpha", score=0.9):
        self._wiki_root = wiki_root
        self._goal_id = goal_id
        self._score = score
        self.queries: List[str] = []

    def query(self, text, k=5, *, source_type=None, dock_goal=None,
              ensure_fresh=True):
        assert source_type == _DOCK_GOAL_SOURCE_TYPE
        self.queries.append(text)
        return [
            SimpleNamespace(
                source_path=f"{_DOCK_GOAL_SOURCE_TYPE}/x.md",
                source_type=_DOCK_GOAL_SOURCE_TYPE,
                title="Goal Alpha",
                snippet="",
                relevance_score=self._score,
                confidence=1.0,
                dock_goal_refs=[self._goal_id],
                topics=[],
            )
        ]


class FakeIntentStore:
    def __init__(self, records):
        self._records = records

    def latest_by_turn(self):
        yield from self._records


class FakeAdjudicator:
    def __init__(self, verdict="advances"):
        self.calls: List[dict] = []
        self._verdict = verdict

    def __call__(self, *, artifact_text, goal):
        self.calls.append({"text": artifact_text, "goal": goal.id})
        return {
            "verdict": self._verdict,
            "excerpt": "quoted evidence",
            "rationale": "moves the goal forward",
        }


@pytest.fixture
def env(tmp_path):
    """One contained artifact + one goal with a projection page."""
    home = Path(get_hermes_home())
    wiki_root = home / "wiki"
    goal = _goal()
    pages_dir = wiki_root / "pages" / _DOCK_GOAL_SOURCE_TYPE
    pages_dir.mkdir(parents=True)
    (pages_dir / f"goal-alpha-{_dock_source_hash(goal.id)}.md").write_text(
        "projection", encoding="utf-8"
    )

    artifact = home / "artifacts" / "note.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("alpha spine progress content", encoding="utf-8")
    event = _artifact_event(artifact, minute=1)
    _write_ledger_events(home, [event])

    root = home.resolve()
    return SimpleNamespace(
        home=home,
        wiki_root=wiki_root,
        goal=goal,
        artifact=artifact,
        event=event,
        roots=[root],
    )


def _detector(env, *, config=None, intent_records=(), adjudicate=None,
              wiki=None):
    return GoalAttachmentDetector(
        home=env.home,
        config=config
        or {
            "adjudicator_tier": "T-GA",
            "stage2_candidate_cap": 5,
            "prefilter_top_k": 3,
        },
        dock=_dock(env.goal),
        wiki_index=wiki or FakeWiki(env.wiki_root),
        intent_store=FakeIntentStore(list(intent_records)),
        adjudicate=adjudicate or FakeAdjudicator(),
        artifact_roots=env.roots,
    )


def _snapshot(home: Path) -> dict:
    """Byte snapshot of every governance-visible write surface."""
    out = {}
    for path in sorted((home / ".kaizen_ledger").glob("*.jsonl")):
        out[str(path)] = path.read_bytes()
    proposals = home / "proposals.jsonl"
    out["proposals"] = (
        proposals.read_bytes() if proposals.exists() else None
    )
    return out


# ── happy path + P1 inertness ───────────────────────────────────────────────


def test_dry_run_adjudicates_and_emits_nothing(env):
    adj = FakeAdjudicator()
    before = _snapshot(env.home)
    report = _detector(env, adjudicate=adj).detect()

    assert len(report.adjudicated) == 1
    ruling = report.adjudicated[0]
    assert ruling.candidate.artifact_id == env.event["artifact_id"]
    assert ruling.candidate.goal_id == "goal-alpha"
    assert ruling.verdict == "advances"
    assert ruling.excerpt and ruling.rationale  # required verdict surface
    assert adj.calls[0]["goal"] == "goal-alpha"

    # P1 inertness: no ledger write, no proposal append.
    assert _snapshot(env.home) == before
    # The rendered report is printable and carries the verdict surface.
    text = report.render()
    assert "quoted evidence" in text and "advances" in text


# ── cursor ──────────────────────────────────────────────────────────────────


def test_cursor_watermark_advances_and_skips_seen_events(env):
    # P3 contract (J2 obligation b): detect() computes the candidate
    # watermark but does NOT persist; advance_cursor() saves it.
    det1 = _detector(env)
    report1 = det1.detect()
    assert report1.watermark_before is None
    assert report1.watermark_after == env.event["timestamp"]
    assert _load_cursor(env.home) is None  # detect alone moves nothing
    det1.advance_cursor(report1)
    assert _load_cursor(env.home) == env.event["timestamp"]
    cursor_file = env.home / "state" / "goal_attachment.cursor.json"
    assert cursor_file.exists()  # persisted under GROVE_HOME state

    adj2 = FakeAdjudicator()
    report2 = _detector(env, adjudicate=adj2).detect()
    assert report2.events_new == 0
    assert report2.adjudicated == []
    assert adj2.calls == []  # nothing re-adjudicated

    # A NEW event past the watermark is picked up.
    later = env.home / "artifacts" / "later.md"
    later.write_text("alpha spine more progress", encoding="utf-8")
    _write_ledger_events(env.home, [_artifact_event(later, minute=7)])
    report3 = _detector(env).detect()
    assert report3.events_new == 1
    assert len(report3.adjudicated) == 1


# ── exclusion seams (P3 fillers; explicit empty-set helpers in P1) ──────────


def test_exclusion_seams_default_empty():
    assert attached_artifact_ids() == set()
    assert suppressed_goal_pairs() == set()


def test_attached_seam_excludes(env, monkeypatch):
    monkeypatch.setattr(
        "grove.dock.attachment.attached_artifact_ids",
        lambda: {env.event["artifact_id"]},
    )
    report = _detector(env).detect()
    assert report.excluded_attached == 1
    assert report.adjudicated == []


def test_suppressed_seam_excludes_per_pair(env, monkeypatch):
    # Pair-scoped (J3): suppression of (aid, goal-alpha) blocks THAT pair…
    monkeypatch.setattr(
        "grove.dock.attachment.suppressed_goal_pairs",
        lambda: {(env.event["artifact_id"], "goal-alpha")},
    )
    report = _detector(env).detect()
    assert report.excluded_suppressed == 1
    assert report.adjudicated == []

    # …but the SAME artifact stays eligible when Stage 1 names another goal.
    beta = _goal(goal_id="goal-beta", name="Goal Beta")
    pages_dir = env.wiki_root / "pages" / _DOCK_GOAL_SOURCE_TYPE
    (pages_dir / f"goal-beta-{_dock_source_hash(beta.id)}.md").write_text(
        "projection", encoding="utf-8"
    )
    detector = GoalAttachmentDetector(
        home=env.home,
        config={
            "adjudicator_tier": "T-GA",
            "stage2_candidate_cap": 5,
            "prefilter_top_k": 3,
        },
        dock=_dock(env.goal, beta),
        wiki_index=FakeWiki(env.wiki_root, goal_id="goal-beta"),
        intent_store=FakeIntentStore([]),
        adjudicate=FakeAdjudicator(),
        artifact_roots=env.roots,
    )
    report2 = detector.detect()
    assert report2.excluded_suppressed == 0
    assert len(report2.adjudicated) == 1
    assert report2.adjudicated[0].candidate.goal_id == "goal-beta"


# ── R-5 / R-7 alignment handling ────────────────────────────────────────────


def test_alignment_prefilter_drops_known_misaligned(env):
    record = SimpleNamespace(turn_id="s#1", goal_alignment="orthogonal")
    report = _detector(env, intent_records=[record]).detect()
    assert report.alignment_filtered == [
        (env.event["artifact_id"], "orthogonal")
    ]
    assert report.adjudicated == []


def test_absent_intent_record_stays_eligible_and_is_recorded(env):
    # No IntentRecord for the turn (R-7): eligible, unknown recorded.
    report = _detector(env, intent_records=[]).detect()
    assert report.alignment_unknown == [env.event["artifact_id"]]
    assert len(report.adjudicated) == 1  # NOT skipped


def test_direct_alignment_passes_prefilter(env):
    record = SimpleNamespace(turn_id="s#1", goal_alignment="direct")
    report = _detector(env, intent_records=[record]).detect()
    assert report.alignment_filtered == []
    assert report.alignment_unknown == []
    assert len(report.adjudicated) == 1


# ── cap ─────────────────────────────────────────────────────────────────────


def test_stage2_cap_is_config_valued_and_loud(env):
    for i, minute in enumerate((2, 3, 4)):
        extra = env.home / "artifacts" / f"extra{i}.md"
        extra.write_text(f"alpha spine content {i}", encoding="utf-8")
        _write_ledger_events(
            env.home, [_artifact_event(extra, minute=minute)]
        )
    adj = FakeAdjudicator()
    report = _detector(
        env,
        config={
            "adjudicator_tier": "T-GA",
            "stage2_candidate_cap": 2,
            "prefilter_top_k": 3,
        },
        adjudicate=adj,
    ).detect()
    assert len(adj.calls) == 2  # cap bound the adjudicator spend
    assert report.cap_dropped == 2  # 4 candidates, cap 2 — dropped loudly
    assert "CAP" in report.render()


# ── G2 projection coverage Andon ────────────────────────────────────────────


def test_projection_gap_fires_loud(env):
    orphan = _goal(goal_id="goal-unprojected", name="No Page")
    detector = GoalAttachmentDetector(
        home=env.home,
        config={
            "adjudicator_tier": "T-GA",
            "stage2_candidate_cap": 5,
            "prefilter_top_k": 3,
        },
        dock=_dock(env.goal, orphan),
        wiki_index=FakeWiki(env.wiki_root),
        intent_store=FakeIntentStore([]),
        adjudicate=FakeAdjudicator(),
        artifact_roots=env.roots,
    )
    with pytest.raises(GoalProjectionGapError, match="goal-unprojected"):
        detector.detect()


def test_projection_coverage_ok_is_silent(env):
    verify_goal_projection_coverage([env.goal], wiki_root=env.wiki_root)


# ── verdict validation ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "not a dict",
        {"verdict": "maybe", "excerpt": "x", "rationale": "y"},
        {"verdict": "advances", "excerpt": "", "rationale": "y"},
        {"verdict": "advances", "excerpt": "x", "rationale": "  "},
        {"verdict": "advances"},
    ],
)
def test_malformed_adjudication_raises(raw):
    with pytest.raises(MalformedAdjudication):
        _validate_adjudication(raw)


def test_valid_adjudication_normalizes():
    out = _validate_adjudication(
        {"verdict": "counter", "excerpt": " q ", "rationale": " r "}
    )
    assert out == {"verdict": "counter", "excerpt": "q", "rationale": "r"}


# ── G5 containment: app-free helper + handler delegation parity ─────────────


def test_resolve_contained_path_contains_and_refuses(tmp_path):
    from grove.api.artifacts import resolve_contained_path

    root = tmp_path / "root"
    root.mkdir()
    inside = root / "a.md"
    inside.write_text("x", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("y", encoding="utf-8")

    aid_in = "a" * 16
    aid_out = "b" * 16
    index = {aid_in: str(inside), aid_out: str(outside)}
    roots = [root.resolve()]

    assert resolve_contained_path(aid_in, index=index, roots=roots) == (
        inside.resolve()
    )
    # Escape refused, unknown refused, malformed refused, vanished refused.
    assert resolve_contained_path(aid_out, index=index, roots=roots) is None
    assert resolve_contained_path("c" * 16, index=index, roots=roots) is None
    assert resolve_contained_path("nothex!", index=index, roots=roots) is None
    inside.unlink()
    assert resolve_contained_path(aid_in, index=index, roots=roots) is None


def test_handler_resolve_delegates_to_same_core(tmp_path):
    """_resolve_contained (request-handler path) and the app-free helper
    agree on every refusal class — one containment implementation (G5)."""
    from grove.api.artifacts import _resolve_contained, resolve_contained_path

    root = tmp_path / "root"
    root.mkdir()
    inside = root / "a.md"
    inside.write_text("x", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("y", encoding="utf-8")

    aid_in = "a" * 16
    aid_out = "b" * 16
    index = {aid_in: str(inside), aid_out: str(outside)}
    roots = [root.resolve()]
    app = {"_artifact_index": dict(index), "artifact_roots": roots}

    for aid in (aid_in, aid_out, "c" * 16, "nothex!"):
        assert _resolve_contained(app, aid) == resolve_contained_path(
            aid, index=index, roots=roots
        )


# ── report rendering sanity ─────────────────────────────────────────────────


def test_render_empty_report():
    report = AttachmentDryRunReport(
        watermark_before=None,
        watermark_after=None,
        events_scanned=0,
        events_new=0,
        excluded_attached=0,
        excluded_suppressed=0,
    )
    text = report.render()
    assert "adjudicated: 0" in text

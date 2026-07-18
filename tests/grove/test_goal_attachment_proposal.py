"""goal-spine-v1 P3 — proposal type + disposition + wiring tests.

Pins: staging identity stability (and membership change → NEW id),
cursor-advance-only-after-staging ordering (J2 obligation b), approve mints
every entry via the P2 writer with the proposal's id, reject files per-pair
suppressions, pair-scoped suppression leaves the artifact eligible for other
goals, the Dispatcher isolation guard contains a raise and files the
registered producer_failure event, and the push slot + frame declarations.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from grove.dock.attachment import (
    AdjudicatedCandidate,
    AttachmentCandidate,
    AttachmentDryRunReport,
    GoalAttachmentDetector,
)
from grove.dock.attachment_store import (
    attached_pairs,
    suppressed_pairs,
)
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_GOAL_ATTACHMENT,
    RoutingProposal,
    compute_proposal_id,
    read_all,
)
from grove.kaizen_ledger import KaizenLedger, default_ledger_dir

AID1 = "a" * 16
AID2 = "b" * 16
AID3 = "c" * 16


def _adjudicated(aid, goal_id="goal-alpha", verdict="advances"):
    return AdjudicatedCandidate(
        candidate=AttachmentCandidate(
            artifact_id=aid,
            path=f"/tmp/{aid}.md",
            goal_id=goal_id,
            relevance_score=0.9,
            turn_id="s#1",
            goal_alignment="direct",
            content="content",
        ),
        verdict=verdict,
        excerpt="quoted evidence",
        rationale="moves the goal forward",
    )


def _report(*adjudicated):
    return AttachmentDryRunReport(
        watermark_before=None,
        watermark_after="2026-07-18T12:09:00+00:00",
        events_scanned=len(adjudicated),
        events_new=len(adjudicated),
        excluded_attached=0,
        excluded_suppressed=0,
        adjudicated=list(adjudicated),
    )


def _detector(tmp_path, **kw):
    kw.setdefault("home", tmp_path / "home")
    kw.setdefault(
        "config",
        {
            "adjudicator_tier": "T-GA",
            "stage2_candidate_cap": 5,
            "prefilter_top_k": 3,
        },
    )
    kw.setdefault(
        "dock",
        SimpleNamespace(
            goals=(
                SimpleNamespace(id="goal-alpha", name="Goal Alpha"),
                SimpleNamespace(id="goal-beta", name="Goal Beta"),
            )
        ),
    )
    return GoalAttachmentDetector(**kw)


def _seed_artifact_written(ledger_dir: Path, *artifact_ids) -> None:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    with (ledger_dir / "seed.jsonl").open("a", encoding="utf-8") as fh:
        for aid in artifact_ids:
            fh.write(
                json.dumps(
                    {
                        "event_type": "artifact_written",
                        "session_id": "seed",
                        "timestamp": "2026-07-18T00:00:00+00:00",
                        "artifact_id": aid,
                        "path": f"/tmp/{aid}.md",
                        "turn_id": "s#1",
                    },
                    sort_keys=True,
                )
                + "\n"
            )


def _proposal(entries, goal_id="goal-alpha"):
    entries = sorted(entries, key=lambda e: e["artifact_id"])
    identity = {
        "goal_id": goal_id,
        "artifact_ids": [e["artifact_id"] for e in entries],
    }
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_GOAL_ATTACHMENT, payload=identity, evidence=(),
        ),
        type=PROPOSAL_TYPE_GOAL_ATTACHMENT,
        payload={"goal_id": goal_id, "goal_name": "Goal Alpha",
                 "entries": entries},
        evidence=(),
        eval_hash="",
        created_at="2026-07-18T12:00:00+00:00",
        proposer="goal_attachment_detector",
    )


def _entry(aid):
    return {
        "artifact_id": aid,
        "excerpt": "quoted evidence",
        "rationale": "moves the goal forward",
        "verdict": "advances",
    }


# ── staging identity ────────────────────────────────────────────────────────


def test_staging_identity_stable_and_deduped(tmp_path):
    queue = tmp_path / "proposals.jsonl"
    det = _detector(tmp_path)
    report = _report(_adjudicated(AID2), _adjudicated(AID1))

    assert det.stage_proposals(report, queue_path=queue) == 1
    # Same logical batch re-staged (input order shuffled) → same id → dedup.
    report_shuffled = _report(_adjudicated(AID1), _adjudicated(AID2))
    assert det.stage_proposals(report_shuffled, queue_path=queue) == 0
    rows = read_all(path=queue)
    assert len(rows) == 1
    # Entries ride the payload sorted by artifact_id.
    assert [e["artifact_id"] for e in rows[0].payload["entries"]] == [
        AID1, AID2,
    ]


def test_membership_change_produces_new_id(tmp_path):
    queue = tmp_path / "proposals.jsonl"
    det = _detector(tmp_path)
    assert det.stage_proposals(_report(_adjudicated(AID1)), queue_path=queue) == 1
    # A newly-found artifact for the SAME goal → different membership →
    # NEW proposal id (J2 ruling: goal-only identity would silently drop it).
    assert (
        det.stage_proposals(
            _report(_adjudicated(AID1), _adjudicated(AID2)), queue_path=queue
        )
        == 1
    )
    assert len(read_all(path=queue)) == 2


def test_non_advancing_verdicts_do_not_stage(tmp_path):
    queue = tmp_path / "proposals.jsonl"
    det = _detector(tmp_path)
    report = _report(
        _adjudicated(AID1, verdict="neutral"),
        _adjudicated(AID2, verdict="counter"),
    )
    assert det.stage_proposals(report, queue_path=queue) == 0
    assert read_all(path=queue) == []


def test_batches_group_per_goal(tmp_path):
    queue = tmp_path / "proposals.jsonl"
    det = _detector(tmp_path)
    report = _report(
        _adjudicated(AID1, goal_id="goal-alpha"),
        _adjudicated(AID2, goal_id="goal-beta"),
    )
    assert det.stage_proposals(report, queue_path=queue) == 2
    rows = read_all(path=queue)
    assert {r.payload["goal_id"] for r in rows} == {"goal-alpha", "goal-beta"}


# ── cursor-after-staging ordering (J2 obligation b) ─────────────────────────


def test_cursor_advances_only_after_staging_succeeds(tmp_path, monkeypatch):
    from grove.dock.attachment import _load_cursor

    home = tmp_path / "home"
    det = _detector(tmp_path, home=home)
    report = _report(_adjudicated(AID1))
    monkeypatch.setattr(det, "detect", lambda: report)

    # Staging raises → run() propagates LOUD and the cursor stays unmoved.
    def _boom(_report, **_kw):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(det, "stage_proposals", _boom)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        det.run()
    assert _load_cursor(home) is None

    # Staging succeeds → the cursor advances to the report watermark.
    monkeypatch.setattr(det, "stage_proposals", lambda _r, **_kw: 1)
    _report_out, staged = det.run()
    assert staged == 1
    assert _load_cursor(home) == report.watermark_after


# ── approve mints all entries ───────────────────────────────────────────────


def test_approve_mints_all_entries_with_proposal_id(monkeypatch):
    from grove.flywheel_cli import _approve_goal_attachment

    _seed_artifact_written(default_ledger_dir(), AID1, AID2)
    monkeypatch.setattr(
        "grove.dock.load_dock",
        lambda: SimpleNamespace(goals=(SimpleNamespace(id="goal-alpha"),)),
    )
    monkeypatch.setattr(
        "grove.dock.attachment.load_goal_attachment_config",
        lambda: {"excerpt_cap_chars": 600},
    )
    proposal = _proposal([_entry(AID1), _entry(AID2)])

    target, applied = _approve_goal_attachment(proposal)
    assert target == "goal-alpha"
    assert sorted(applied["minted"]) == [AID1, AID2]
    assert applied["skipped_existing"] == []

    pairs = attached_pairs()
    assert (AID1, "goal-alpha") in pairs and (AID2, "goal-alpha") in pairs
    for pair in ((AID1, "goal-alpha"), (AID2, "goal-alpha")):
        assert pairs[pair]["proposal_id"] == proposal.proposal_id

    # Re-approve is idempotent: both entries skip, no error, no new rows.
    _target, applied2 = _approve_goal_attachment(proposal)
    assert applied2["minted"] == []
    assert sorted(applied2["skipped_existing"]) == [AID1, AID2]


def test_approve_refuses_malformed_payload():
    from grove.flywheel_cli import _approve_goal_attachment

    bad = RoutingProposal(
        proposal_id="sha256:x", type=PROPOSAL_TYPE_GOAL_ATTACHMENT,
        payload={"goal_id": "goal-alpha", "entries": []}, evidence=(),
        eval_hash="", created_at="2026-07-18T12:00:00+00:00",
    )
    with pytest.raises(ValueError, match="entries"):
        _approve_goal_attachment(bad)


# ── reject suppresses all pairs, pair-scoped ────────────────────────────────


def test_reject_suppresses_all_pairs_pair_scoped():
    from grove.flywheel_cli import _reject_goal_attachment

    proposal = _proposal([_entry(AID1), _entry(AID2)])
    _reject_goal_attachment(proposal)

    pairs = suppressed_pairs()
    assert (AID1, "goal-alpha") in pairs
    assert (AID2, "goal-alpha") in pairs
    # Pair-scoped: the SAME artifacts are NOT suppressed for another goal.
    assert (AID1, "goal-beta") not in pairs
    # And suppression is not attachment state — nothing reads as attached.
    assert attached_pairs() == {}


def test_reject_is_tolerant_of_store_failure(monkeypatch, caplog):
    from grove.flywheel_cli import _reject_goal_attachment

    def _boom(*_a, **_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(
        "grove.dock.attachment_store.record_suppression", _boom
    )
    # Must NOT raise — the operator can always dismiss (J1 contract).
    _reject_goal_attachment(_proposal([_entry(AID1)]))


# ── isolation guard (J4 shape b) + producer_failure ─────────────────────────


def test_producer_failure_event_registered():
    assert "producer_failure" in KaizenLedger.EVENT_TYPES
    assert "artifact_goal_suppressed" in KaizenLedger.EVENT_TYPES


def _guard(dummy=None):
    from grove.dispatcher import Dispatcher

    return Dispatcher._run_goal_attachment_sweep(dummy or SimpleNamespace())


def test_isolation_guard_contains_raise_and_files_event(monkeypatch):
    def _boom():
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(
        "grove.dock.attachment.run_goal_attachment_sweep", _boom
    )
    _guard()  # must NOT propagate — isolation contract

    events = []
    for path in sorted(default_ledger_dir().glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["event_type"] == "producer_failure":
                events.append(event)
    assert len(events) == 1
    assert events[0]["producer"] == "goal_attachment_detector"
    assert "detector exploded" in events[0]["error"]


def test_isolation_guard_invokes_sweep_on_success(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "grove.dock.attachment.run_goal_attachment_sweep",
        lambda: calls.append(1),
    )
    _guard()
    assert calls == [1]
    # No failure event on the success path.
    for path in sorted(default_ledger_dir().glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            assert json.loads(line)["event_type"] != "producer_failure"


# ── push slot + frame + renderers ───────────────────────────────────────────


def test_push_priority_slot_declared():
    from grove.flywheel_cli import _PUSH_PRIORITY

    slot = _PUSH_PRIORITY[PROPOSAL_TYPE_GOAL_ATTACHMENT]
    assert slot == 1.8
    # Below memory (1) and consolidation (1.5), above the contract-tested
    # routing_adjustment integer (==2) — J6 ruling.
    assert _PUSH_PRIORITY["memory_context"] < slot
    assert 1.5 < slot < 2


def test_push_body_frame_is_batched():
    proposal = _proposal([_entry(AID1), _entry(AID2)])
    body = proposal.push_body("2 artifacts adjudicated")
    assert body.startswith("I've matched work to your goals")


def test_renderers_registered_and_card_copy_names_detach():
    from grove.flywheel_cli import _handler_for, get_renderer

    proposal = _proposal([_entry(AID1), _entry(AID2)])
    summary = get_renderer(PROPOSAL_TYPE_GOAL_ATTACHMENT)(proposal)
    # J5 BINDING: the operator is told per-entry removal exists post-approval.
    assert "individual attachments can be removed" in summary
    assert "2 artifacts" in summary

    diff = _handler_for(PROPOSAL_TYPE_GOAL_ATTACHMENT).diff_renderer(proposal)
    (goal_key,) = [k for k in diff if k.startswith("goal:")]
    assert len(diff[goal_key]["attach"]) == 2
    assert diff[goal_key]["attach"][0]["excerpt"] == "quoted evidence"

"""goal-spine-v1 P2 — attachment authority tests.

Pins: EVENT_TYPES registration gates as expected (A3), mint refuses without
proposal_id, write-strict refusals, idempotence (no duplicate row),
visible excerpt truncation, detach round-trip (mint → detach → re-mint),
reader tolerance on malformed events and pruned goals (R-9), the filled P1
exclusion seam, and continued detector inertness.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from grove.dock.attachment_store import (
    AttachmentWriteError,
    attached_artifact_ids,
    attached_pairs,
    attachments_for_artifact,
    attachments_for_goal,
    detach_attachment,
    mint_attachment,
)
from grove.kaizen_ledger import KaizenLedger, default_ledger_dir

AID = "a" * 16
AID2 = "b" * 16


def _goal(goal_id="goal-alpha"):
    return SimpleNamespace(id=goal_id)


def _dock(*goal_ids):
    return SimpleNamespace(goals=tuple(_goal(g) for g in goal_ids))


def _seed_artifact_written(ledger_dir: Path, artifact_id=AID) -> None:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "event_type": "artifact_written",
        "session_id": "seed",
        "timestamp": "2026-07-18T00:00:00+00:00",
        "artifact_id": artifact_id,
        "path": f"/tmp/{artifact_id}.md",
        "turn_id": "s#1",
    }
    with (ledger_dir / "seed.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")


def _mint(ledger_dir, *, artifact_id=AID, goal_id="goal-alpha", **kw):
    kw.setdefault("proposal_id", "prop-1")
    kw.setdefault("rationale", "advances the goal")
    kw.setdefault("excerpt", "quoted evidence")
    kw.setdefault("dock", _dock("goal-alpha", "goal-beta"))
    kw.setdefault("excerpt_cap", 600)
    return mint_attachment(
        artifact_id, goal_id, ledger_dir=ledger_dir, **kw
    )


@pytest.fixture
def ledger(tmp_path):
    d = tmp_path / "ledger"
    _seed_artifact_written(d)
    return d


# ── A3: registration gates as expected ──────────────────────────────────────


def test_event_types_registered():
    assert "artifact_goal_attached" in KaizenLedger.EVENT_TYPES
    assert "artifact_goal_detached" in KaizenLedger.EVENT_TYPES


def test_unregistered_event_type_still_raises(tmp_path):
    # The gate the registration relies on: an unknown type is a loud
    # ValueError, exactly the floor ledger-eventtype-hygiene-v1 closed.
    with pytest.raises(ValueError, match="expected one of"):
        KaizenLedger("t", ledger_dir=tmp_path).record("no_such_event_type")


# ── mint contract ───────────────────────────────────────────────────────────


def test_mint_happy_path_persists_full_payload(ledger):
    event = _mint(ledger)
    assert event is not None
    assert event["event_type"] == "artifact_goal_attached"
    assert event["artifact_id"] == AID
    assert event["goal_id"] == "goal-alpha"
    assert event["proposal_id"] == "prop-1"
    assert event["rationale"] == "advances the goal"
    assert event["excerpt"] == "quoted evidence"
    assert event["excerpt_truncated"] is False
    assert event["excerpt_full_chars"] == len("quoted evidence")
    assert attached_pairs(ledger_dir=ledger) != {}


@pytest.mark.parametrize("missing", ["proposal_id", "rationale", "excerpt"])
def test_mint_refuses_empty_required_field(ledger, missing):
    with pytest.raises(AttachmentWriteError, match=missing):
        _mint(ledger, **{missing: "  "})


def test_mint_refuses_without_proposal_id_type(ledger):
    with pytest.raises(AttachmentWriteError, match="proposal_id"):
        _mint(ledger, proposal_id=None)


def test_mint_refuses_malformed_artifact_id(ledger):
    with pytest.raises(AttachmentWriteError, match="16-hex"):
        _mint(ledger, artifact_id="not-an-id")


def test_mint_refuses_unrecorded_artifact(ledger):
    with pytest.raises(AttachmentWriteError, match="no artifact_written"):
        _mint(ledger, artifact_id="f" * 16)


def test_mint_refuses_unknown_goal(ledger):
    with pytest.raises(AttachmentWriteError, match="not a Dock goal"):
        _mint(ledger, goal_id="goal-nonexistent")


def test_mint_refuses_missing_dock(ledger):
    with pytest.raises(AttachmentWriteError, match="Dock not installed"):
        mint_attachment(
            AID, "goal-alpha", proposal_id="p", rationale="r",
            excerpt="e", ledger_dir=ledger, dock=None, excerpt_cap=600,
        )


def test_refusal_writes_nothing(ledger):
    with pytest.raises(AttachmentWriteError):
        _mint(ledger, goal_id="goal-nonexistent")
    assert attached_pairs(ledger_dir=ledger) == {}


# ── idempotence ─────────────────────────────────────────────────────────────


def test_mint_idempotent_no_duplicate_row(ledger):
    assert _mint(ledger) is not None
    assert _mint(ledger) is None  # no-op, not an error
    events = [
        e
        for p in sorted(ledger.glob("*.jsonl"))
        for line in p.read_text(encoding="utf-8").splitlines()
        if (e := json.loads(line))["event_type"] == "artifact_goal_attached"
    ]
    assert len(events) == 1


def test_same_artifact_different_goal_is_not_deduped(ledger):
    assert _mint(ledger) is not None
    assert _mint(ledger, goal_id="goal-beta") is not None
    assert len(attached_pairs(ledger_dir=ledger)) == 2


# ── visible truncation ──────────────────────────────────────────────────────


def test_excerpt_truncation_is_visible(ledger):
    long_excerpt = "x" * 1000
    event = _mint(ledger, excerpt=long_excerpt, excerpt_cap=100)
    assert len(event["excerpt"]) == 100
    assert event["excerpt_truncated"] is True
    assert event["excerpt_full_chars"] == 1000


def test_excerpt_cap_comes_from_config_when_not_injected(ledger, monkeypatch):
    monkeypatch.setattr(
        "grove.dock.attachment.load_goal_attachment_config",
        lambda: {"excerpt_cap_chars": 5},
    )
    event = _mint(ledger, excerpt="abcdefgh", excerpt_cap=None)
    assert event["excerpt"] == "abcde"
    assert event["excerpt_truncated"] is True


def test_nonpositive_cap_refused(ledger):
    with pytest.raises(AttachmentWriteError, match="positive"):
        _mint(ledger, excerpt_cap=0)


# ── detach round-trip ───────────────────────────────────────────────────────


def test_detach_round_trip_latest_wins(ledger):
    _mint(ledger)
    assert (AID, "goal-alpha") in attached_pairs(ledger_dir=ledger)

    event = detach_attachment(
        AID, "goal-alpha", reason="operator says wrong goal",
        ledger_dir=ledger,
    )
    assert event is not None
    assert event["event_type"] == "artifact_goal_detached"
    assert event["reason"] == "operator says wrong goal"
    assert attached_pairs(ledger_dir=ledger) == {}  # detached → not attached

    # Re-mint re-attaches (H5: latest-wins by timestamp).
    assert _mint(ledger, proposal_id="prop-2") is not None
    assert (AID, "goal-alpha") in attached_pairs(ledger_dir=ledger)


def test_detach_requires_reason(ledger):
    _mint(ledger)
    with pytest.raises(AttachmentWriteError, match="reason"):
        detach_attachment(AID, "goal-alpha", reason="", ledger_dir=ledger)


def test_detach_of_unattached_pair_is_noop(ledger):
    assert (
        detach_attachment(AID, "goal-zzz", reason="r", ledger_dir=ledger)
        is None
    )


def test_detach_works_on_pruned_goal_without_dock(ledger):
    # A pair attached to a since-pruned auto-* goal stays detachable —
    # detach never resolves the Dock.
    _mint(ledger, goal_id="goal-beta")
    event = detach_attachment(
        AID, "goal-beta", reason="goal pruned", ledger_dir=ledger,
    )
    assert event is not None


# ── reader tolerance (R-9 / _lineage_for idiom) ─────────────────────────────


def test_reader_skips_malformed_events_never_raises(ledger):
    _mint(ledger)
    with (ledger / "junk.jsonl").open("w", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write(json.dumps({"event_type": "artifact_goal_attached"}) + "\n")
        fh.write(
            json.dumps(
                {
                    "event_type": "artifact_goal_attached",
                    "artifact_id": 42,  # malformed — contributes nothing
                    "goal_id": "goal-alpha",
                    "timestamp": "2026-07-18T09:00:00+00:00",
                }
            )
            + "\n"
        )
    pairs = attached_pairs(ledger_dir=ledger)
    assert list(pairs) == [(AID, "goal-alpha")]


def test_pruned_goal_contributes_nothing(ledger):
    _mint(ledger)  # goal-alpha
    live = {"goal-other"}  # goal-alpha pruned from the Dock
    assert attached_pairs(ledger_dir=ledger, live_goal_ids=live) == {}
    assert attached_artifact_ids(ledger_dir=ledger, live_goal_ids=live) == set()
    # Pure-ledger view (no filter) still sees it.
    assert (AID, "goal-alpha") in attached_pairs(ledger_dir=ledger)


def test_by_artifact_and_by_goal_views(ledger):
    _seed_artifact_written(ledger, AID2)
    _mint(ledger)
    _mint(ledger, artifact_id=AID2, goal_id="goal-alpha")
    _mint(ledger, goal_id="goal-beta")

    by_goal = attachments_for_goal("goal-alpha", ledger_dir=ledger)
    assert {e["artifact_id"] for e in by_goal} == {AID, AID2}
    by_artifact = attachments_for_artifact(AID, ledger_dir=ledger)
    assert {e["goal_id"] for e in by_artifact} == {"goal-alpha", "goal-beta"}


def test_empty_ledger_dir_yields_empty(tmp_path):
    assert attached_pairs(ledger_dir=tmp_path / "absent") == {}


# ── the filled P1 exclusion seam ────────────────────────────────────────────


def test_p1_seam_returns_real_attached_ids(monkeypatch):
    # Default ledger dir under the hermetic GROVE_HOME.
    ledger_dir = default_ledger_dir()
    _seed_artifact_written(ledger_dir)
    _mint(ledger_dir)

    monkeypatch.setattr(
        "grove.dock.load_dock", lambda: _dock("goal-alpha")
    )
    from grove.dock.attachment import attached_artifact_ids as seam

    assert seam() == {AID}

    # R-9 through the seam: goal pruned from the Dock → not excluded.
    monkeypatch.setattr(
        "grove.dock.load_dock", lambda: _dock("goal-other")
    )
    assert seam() == set()


def test_suppressed_seam_still_empty():
    from grove.dock.attachment import suppressed_artifact_ids

    assert suppressed_artifact_ids() == set()

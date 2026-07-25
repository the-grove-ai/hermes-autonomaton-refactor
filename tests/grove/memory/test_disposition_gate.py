"""disposition-gate-v1 · P2 — the shared suppression gate (grove/memory/
dispositions.py). Behavior + the R-20 anchor pin.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from grove.memory.dispositions import (
    DISPOSED_AT_FIELD,
    NON_TERMINAL_STATUSES,
    PERMANENT,
    TERMINAL_STATUSES,
    disposed_target_ids,
    resolve_suppression_policy,
    session_processed,
    suppressed_target_ids,
)

NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)


def _rec(status, action="graduate", target="mem_x", *, disposed_at=None,
         timestamp="2026-07-01T00:00:00+00:00", session="s1"):
    rec = {
        "session_id": session,
        "status": status,
        "timestamp": timestamp,
        "proposal": {"action": action, "target_id": target},
    }
    if disposed_at is not None:
        rec[DISPOSED_AT_FIELD] = disposed_at
    return rec


# ── R-24 vocabulary ────────────────────────────────────────────────────────
def test_vocabulary_is_the_single_definition():
    assert TERMINAL_STATUSES == frozenset({"approved", "rejected", "dismissed"})
    assert NON_TERMINAL_STATUSES == frozenset({"pending", "processing"})
    assert not (TERMINAL_STATUSES & NON_TERMINAL_STATUSES)


# ── permanent by default ────────────────────────────────────────────────────
def test_dismissed_binds_permanently_by_default():
    """No config → permanent. A dismissed subject is suppressed regardless of
    how long ago it was disposed — the four-dismissals bug, closed."""
    recs = [_rec("dismissed", disposed_at="2020-01-01T00:00:00+00:00")]
    assert suppressed_target_ids(recs, "graduate", now=NOW) == {"mem_x"}


def test_rejected_and_approved_also_bind():
    for status in ("rejected", "approved"):
        recs = [_rec(status)]
        assert suppressed_target_ids(recs, "graduate", now=NOW) == {"mem_x"}


def test_pending_still_suppresses_as_liveness():
    """The widened set keeps the original pending (in-flight) suppression."""
    recs = [_rec("pending")]
    assert suppressed_target_ids(recs, "graduate", now=NOW) == {"mem_x"}


def test_absent_disposition_leaves_subject_free():
    assert suppressed_target_ids([], "graduate", now=NOW) == set()


def test_action_is_scoped():
    """A dismissed GRADUATE does not suppress a DEPRECATE of the same target."""
    recs = [_rec("dismissed", action="graduate", target="mem_x")]
    assert suppressed_target_ids(recs, "deprecate", now=NOW) == set()


def test_disposed_target_ids_is_terminal_only():
    """The supersede sibling gate (R-23) counts dispositions only, never the
    in-flight liveness — that stays the separate :289 check."""
    recs = [_rec("pending", action="supersede"),
            _rec("dismissed", action="supersede", target="mem_y")]
    assert disposed_target_ids(recs, "supersede", now=NOW) == {"mem_y"}


# ── R-20 · anchor on the disposition timestamp ONLY ─────────────────────────
def test_duration_anchors_on_disposition_timestamp_not_staging(tmp_path):
    """R-20: with a duration policy the window is measured from the disposition
    timestamp (disposed_at), NEVER from the staging ``timestamp`` (an emission
    field). Two records prove the anchor: identical staging times, opposite
    dispositions."""
    cfg = tmp_path / "flywheel.config.yaml"
    cfg.write_text("disposition_gate:\n  graduate: 30\n", encoding="utf-8")

    staging = "2026-07-23T00:00:00+00:00"  # yesterday — recent for both
    # Disposed 40 days ago → outside the 30-day window → NOT suppressed,
    # even though its staging timestamp is yesterday. If the code anchored on
    # staging it would (wrongly) still suppress.
    old = _rec("dismissed", target="mem_old", timestamp=staging,
               disposed_at="2026-06-14T00:00:00+00:00")
    # Disposed 10 days ago → inside the window → suppressed, even though its
    # staging timestamp is 60 days ago.
    recent = _rec("dismissed", target="mem_recent",
                  timestamp="2026-05-25T00:00:00+00:00",
                  disposed_at="2026-07-14T00:00:00+00:00")

    got = suppressed_target_ids([old, recent], "graduate", now=NOW, config_path=cfg)
    assert got == {"mem_recent"}, (
        "duration must anchor on disposed_at, not the staging timestamp"
    )


def test_duration_missing_disposition_timestamp_stays_bound(tmp_path):
    """A terminal record with no disposition timestamp cannot be aged out — it
    stays suppressed (safe). A missing anchor never silently frees a subject."""
    cfg = tmp_path / "flywheel.config.yaml"
    cfg.write_text("disposition_gate:\n  graduate: 30\n", encoding="utf-8")
    recs = [_rec("dismissed", disposed_at=None)]
    assert suppressed_target_ids(recs, "graduate", now=NOW, config_path=cfg) == {"mem_x"}


# ── session gate (detector.py:247 widening) ─────────────────────────────────
def test_session_processed_consults_terminal():
    """A session whose only proposal was dismissed is still 'processed' — it
    must not be re-mined (the old pending/processing-only check let it slip)."""
    recs = [_rec("dismissed", session="s9")]
    assert session_processed(recs, "s9") is True


def test_session_processed_pending_and_processing():
    assert session_processed([_rec("processing", session="s2")], "s2") is True
    assert session_processed([_rec("pending", session="s3")], "s3") is True


def test_session_unprocessed_when_absent():
    assert session_processed([], "s4") is False


# ── policy resolution · fail-loud default ───────────────────────────────────
def test_policy_default_permanent_when_absent(tmp_path):
    assert resolve_suppression_policy("graduate",
                                      config_path=tmp_path / "nope.yaml") == PERMANENT


def test_policy_default_permanent_when_block_absent(tmp_path):
    cfg = tmp_path / "flywheel.config.yaml"
    cfg.write_text("disposition_promotion:\n  threshold_count: 3\n", encoding="utf-8")
    assert resolve_suppression_policy("graduate", config_path=cfg) == PERMANENT


def test_policy_duration_parsed(tmp_path):
    cfg = tmp_path / "flywheel.config.yaml"
    cfg.write_text("disposition_gate:\n  deprecate: 14\n", encoding="utf-8")
    assert resolve_suppression_policy("deprecate", config_path=cfg) == timedelta(days=14)


def test_policy_permanent_literal(tmp_path):
    cfg = tmp_path / "flywheel.config.yaml"
    cfg.write_text("disposition_gate:\n  graduate: permanent\n", encoding="utf-8")
    assert resolve_suppression_policy("graduate", config_path=cfg) == PERMANENT


@pytest.mark.parametrize("bad", ["0", "-3", "true", "3.5", "'soon'"])
def test_policy_malformed_fails_loud(tmp_path, bad):
    cfg = tmp_path / "flywheel.config.yaml"
    cfg.write_text(f"disposition_gate:\n  graduate: {bad}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        resolve_suppression_policy("graduate", config_path=cfg)


# ── P3 · the reset verb (R-19) ──────────────────────────────────────────────
import json
from pathlib import Path

from grove.eval.proposal_queue import PROPOSAL_TYPE_MEMORY_CONTEXT, compute_proposal_id
from grove.memory.digest import reset_memory_proposal, run_digest
from grove.memory.store import MemoryStore


def _pid(proposal, session="s1"):
    ev = (session,) if session else ()
    return compute_proposal_id(type=PROPOSAL_TYPE_MEMORY_CONTEXT, payload=proposal, evidence=ev)


def _stage(ppath, proposal, status="dismissed", session="s1", disposed_at="2020-01-01T00:00:00+00:00"):
    rec = {"session_id": session, "status": status,
           "timestamp": "2026-07-01T00:00:00+00:00", "proposal": proposal}
    if disposed_at is not None and status in ("approved", "rejected", "dismissed"):
        rec["disposed_at"] = disposed_at
    ppath.write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _read_one(ppath):
    return json.loads(ppath.read_text(encoding="utf-8").splitlines()[0])


def test_reset_flips_terminal_to_pending_and_clears_anchor(tmp_path):
    ppath = tmp_path / "memory_proposals.jsonl"
    proposal = {"action": "graduate", "target_id": "mem_x", "content": "c"}
    _stage(ppath, proposal, status="dismissed")
    res = reset_memory_proposal(_pid(proposal), proposals_path=ppath, ledger_dir=tmp_path / "led")
    assert res.ok is True and res.status_before == "dismissed"
    rec = _read_one(ppath)
    assert rec["status"] == "pending"
    assert "disposed_at" not in rec  # R-28 — no stale anchor into the next decision


def test_reset_reports_write_result_on_miss(tmp_path):
    """A confirmation reports the WRITE, not the tap: an unmatched id is a
    reported failure, never a fake success."""
    ppath = tmp_path / "memory_proposals.jsonl"
    _stage(ppath, {"action": "graduate", "target_id": "mem_x"}, status="dismissed")
    res = reset_memory_proposal("sha256:does-not-match", proposals_path=ppath)
    assert res.ok is False and "matched" in res.message.lower()


def test_reset_only_targets_terminal_records(tmp_path):
    """A pending record is not a disposition — reset must not match it."""
    ppath = tmp_path / "memory_proposals.jsonl"
    proposal = {"action": "graduate", "target_id": "mem_x"}
    _stage(ppath, proposal, status="pending", disposed_at=None)
    res = reset_memory_proposal(_pid(proposal), proposals_path=ppath)
    assert res.ok is False


def test_reset_emits_ledger_event(tmp_path):
    ppath = tmp_path / "memory_proposals.jsonl"
    led = tmp_path / "led"
    proposal = {"action": "graduate", "target_id": "mem_x", "content": "c"}
    _stage(ppath, proposal, status="rejected")
    reset_memory_proposal(_pid(proposal), proposals_path=ppath, ledger_dir=led)
    events = []
    for p in Path(led).glob("*.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    resets = [e for e in events if e.get("disposition") == "reset"]
    assert len(resets) == 1, f"expected one reset ledger event, got {events}"
    assert resets[0].get("status_before") == "rejected"


def test_reset_then_redispose_anchors_new_timestamp(tmp_path):
    """R-28 end-to-end: dismiss → reset (anchor cleared) → dismiss again stamps
    a FRESH disposed_at. The new decision never inherits the reset one's."""
    store = MemoryStore(base_dir=tmp_path)
    ppath = tmp_path / "memory_proposals.jsonl"
    led = tmp_path / "led"
    proposal = {"action": "graduate", "target_id": "mem_x", "content": "c"}

    # stage pending, dismiss via the digest (stamps disposed_at #1)
    _stage(ppath, proposal, status="pending", disposed_at=None)
    run_digest(store=store, proposals_path=ppath,
               decide=lambda s, p: "dismiss", ledger_dir=led)
    first = _read_one(ppath).get("disposed_at")
    assert first is not None

    # reset → pending, anchor cleared
    res = reset_memory_proposal(_pid(proposal), proposals_path=ppath, ledger_dir=led)
    assert res.ok and "disposed_at" not in _read_one(ppath)

    # dismiss again → a NEW disposed_at is stamped (not the cleared one)
    run_digest(store=store, proposals_path=ppath,
               decide=lambda s, p: "dismiss", ledger_dir=led)
    second = _read_one(ppath).get("disposed_at")
    assert second is not None
    assert second >= first  # fresh stamp from the re-disposition flip


# ── P3 · CLI reset verb ─────────────────────────────────────────────────────
def test_cli_reset_flips_disposed_to_pending(tmp_path, capsys):
    from grove.memory.cli import cli_memory_reset, _disposed, _full_id
    ppath = tmp_path / "memory_proposals.jsonl"
    proposal = {"action": "graduate", "target_id": "mem_x", "content": "c"}
    _stage(ppath, proposal, status="dismissed")
    fid = _full_id(proposal)
    rc = cli_memory_reset(fid[:8], base_dir=tmp_path, ledger_dir=tmp_path / "led")
    assert rc == 0
    out = capsys.readouterr().out
    assert "Reset" in out and "pending" in out.lower()
    assert _read_one(ppath)["status"] == "pending"
    assert not _disposed(tmp_path)  # no longer disposed


def test_cli_reset_unknown_id_reports_failure(tmp_path, capsys):
    from grove.memory.cli import cli_memory_reset
    (tmp_path / "memory_proposals.jsonl").write_text("", encoding="utf-8")
    rc = cli_memory_reset("abcdef", base_dir=tmp_path)
    assert rc == 1
    assert "No disposed" in capsys.readouterr().err


def test_cli_list_disposed_shows_terminal(tmp_path, capsys):
    from grove.memory.cli import cli_memory_list
    ppath = tmp_path / "memory_proposals.jsonl"
    _stage(ppath, {"action": "graduate", "target_id": "mem_x", "content": "c"},
           status="dismissed")
    rc = cli_memory_list(base_dir=tmp_path, disposed=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "disposed" in out.lower() and "[dismissed]" in out


def test_reset_discloses_ledger_failure_in_message(tmp_path, monkeypatch):
    """R-30: the write lands but the ledger append fails → ok stays True AND
    the message tells the operator the reset is not queryable. A warning logged
    where nobody reads is the exact defect this sprint corrects."""
    import grove.flywheel_cli as fc
    monkeypatch.setattr(
        fc, "_record_kaizen_disposition",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ledger down")),
    )
    ppath = tmp_path / "memory_proposals.jsonl"
    proposal = {"action": "graduate", "target_id": "mem_x", "content": "c"}
    _stage(ppath, proposal, status="dismissed")
    res = reset_memory_proposal(_pid(proposal), proposals_path=ppath, ledger_dir=tmp_path / "led")
    assert res.ok is True                                  # the flip landed
    assert _read_one(ppath)["status"] == "pending"
    m = res.message.lower()
    # Assert on the TWO FACTS (R-30 · TASK 0), not exact wording:
    assert "landed" in m                                   # fact 1 — the reset landed
    assert "log" in m                                      # fact 2 — won't show in the log

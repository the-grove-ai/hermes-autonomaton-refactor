"""kaizen-fault-triage-v1 — detector, judgment, and disposition pins.

Real ledger files in a tmp dir, the real proposal queue (explicit tmp path),
the real cli_acknowledge/cli_reject disposition paths — the only fake is the
clock (injectable ``now``, the DispositionPromotionDetector pattern).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grove.eval.fault_triage import (
    FaultTriageDetector,
    FaultTriageThresholds,
    derive_activity,
    error_signature,
    judgment_line,
    load_fault_triage_thresholds,
)

_NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)


def _ts(*, days: float = 0, hours: float = 0) -> str:
    return (_NOW - timedelta(days=days, hours=hours)).isoformat()


def _fleet_halt(*, worker="cultivator", check="resolver_failed",
                detail="RuntimeError: Event loop is closed",
                ts, session):
    return {
        "event_type": "andon_halt", "source": "fleet_worker",
        "worker": worker, "check": check, "detail": detail,
        "go_forward_options": ["opt"],
        "session_id": session, "timestamp": ts,
    }


def _dispatcher_halt(*, tool="terminal", rule="shell.effect.default",
                     ts, session):
    return {
        "event_type": "andon_halt",
        "intents": [{"call_id": "c1", "tool_name": tool}],
        "matched_rule": rule, "zone": "yellow",
        "session_id": session, "timestamp": ts,
    }


def _red(*, tool="terminal", rule="shell.opacity.unparseable", ts, session):
    return {
        "event_type": "red_resolution", "triggering_tool": tool,
        "matched_rule": rule, "resolution": "cancel", "zone": "red",
        "session_id": session, "timestamp": ts,
    }


def _write_ledger(ledger_dir: Path, session: str, events) -> None:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / f"{session}.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


def _detect(ledger_dir: Path, **kw):
    thresholds = kw.pop("thresholds", FaultTriageThresholds())
    detector = FaultTriageDetector(ledger_dir=ledger_dir, thresholds=thresholds)
    return detector.detect(now=kw.pop("now", _NOW))


# ── schema-aware grouping ────────────────────────────────────────────────


def test_fleet_events_group_without_tool_rule(tmp_path):
    """The GATE-A disqualifier: fleet halts carry NO tool/rule — they must
    key on (worker, check, error_signature), never fold into a (?, ?)."""
    ledger = tmp_path / "ledger"
    events = [
        _fleet_halt(
            detail=f"RuntimeError: Event loop is closed "
                   f"0f0e0d0c-0b0a-4909-8807-06050403020{i}",
            ts=_ts(days=i), session="fleet_cultivator_dispatch",
        )
        for i in range(5)
    ]
    _write_ledger(ledger, "fleet_cultivator_dispatch", events)

    proposals = _detect(ledger)
    assert len(proposals) == 1
    payload = proposals[0].payload
    assert payload["source"] == "fleet_worker"
    assert payload["worker"] == "cultivator"
    assert payload["check"] == "resolver_failed"
    assert "<uuid>" in payload["error_signature"]
    assert "Event loop is closed" in payload["error_signature"]


def test_dispatcher_halts_key_on_intent_tool(tmp_path):
    ledger = tmp_path / "ledger"
    for i in range(5):
        _write_ledger(ledger, f"s{i % 3}", [
            _dispatcher_halt(ts=_ts(days=i, hours=1), session=f"s{i % 3}"),
        ])
    proposals = _detect(ledger)
    assert len(proposals) == 1
    payload = proposals[0].payload
    assert payload == {
        "source": "dispatcher_halt", "tool": "terminal",
        "matched_rule": "shell.effect.default", "error_signature": "",
    }


def test_red_resolution_key(tmp_path):
    ledger = tmp_path / "ledger"
    for i in range(5):
        _write_ledger(ledger, f"r{i}", [_red(ts=_ts(days=1), session=f"r{i}")])
    proposals = _detect(ledger)
    assert len(proposals) == 1
    assert proposals[0].payload["source"] == "red_resolution"
    assert proposals[0].payload["matched_rule"] == "shell.opacity.unparseable"


# ── signature extractor ──────────────────────────────────────────────────


def test_signature_stability():
    """Same fault with per-instance noise → ONE signature; distinct faults
    stay distinct."""
    a = error_signature(
        "OSError at /home/hermes/.grove/wiki/pages/x.md "
        "(0x7f3a2b1c) id=0f0e0d0c-0b0a-4909-8807-060504030201 "
        "at 2026-07-10T23:50:22.013665+00:00"
    )
    b = error_signature(
        "OSError at /mnt/grove-data/other/deep/path.md "
        "(0xdeadbeef) id=11111111-2222-4333-8444-555555555555 "
        "at 2026-06-01 01:02:03"
    )
    assert a == b
    assert a == "OSError at <path> (<hex>) id=<uuid> at <ts>"
    assert error_signature("ValueError: bad frontmatter") != a


# ── identity + window + evidence cap ─────────────────────────────────────


def test_identity_stable_across_scans_and_dedups(tmp_path):
    from grove.eval.proposal_queue import append

    ledger = tmp_path / "ledger"
    queue = tmp_path / "proposals.jsonl"
    for i in range(5):
        _write_ledger(ledger, "fleet_x_run", [
            _fleet_halt(ts=_ts(days=i), session="fleet_x_run"),
        ])
    first = _detect(ledger)[0]
    second = _detect(ledger)[0]
    assert first.proposal_id == second.proposal_id
    assert append(first, path=queue) is True
    assert append(second, path=queue) is False  # content-addressed dedup


def test_window_excludes_cold_class(tmp_path):
    """A class that stopped recurring ages out — it must NOT fire forever
    on an un-rotated ledger (GATE-B Q5)."""
    ledger = tmp_path / "ledger"
    for i in range(10):
        _write_ledger(ledger, f"old{i}", [
            _dispatcher_halt(rule="terminal", ts=_ts(days=20 + i),
                             session=f"old{i}"),
        ])
    assert _detect(ledger) == []


def test_evidence_cap_and_audit_fields(tmp_path):
    ledger = tmp_path / "ledger"
    for i in range(10):
        _write_ledger(ledger, "fleet_y_run", [
            _fleet_halt(worker="scout", ts=_ts(days=i, hours=2),
                        session="fleet_y_run"),
        ])
    proposal = _detect(ledger)[0]
    body = proposal.semantic_justification
    # ≤3 sampled raw events (first/middle/last).
    assert body.count('"event_type"') <= 3
    assert "count=10" in proposal.source_patterns
    assert any(p.startswith("first_seen=") for p in proposal.source_patterns)
    assert any(p.startswith("last_seen=") for p in proposal.source_patterns)
    assert proposal.proposer == "fault_triage"


# ── judgment (amendment 3a) ──────────────────────────────────────────────


def test_judgment_line_byte_stable(tmp_path):
    """Fixed group → byte-stable judgment. All events recent + last within
    48h → 'active, worsening'."""
    ledger = tmp_path / "ledger"
    for i in range(5):
        _write_ledger(ledger, "fleet_cultivator_dispatch", [
            _fleet_halt(ts=_ts(hours=1 + i), session="fleet_cultivator_dispatch"),
        ])
    proposal = _detect(ledger)[0]
    expected = (
        "cultivator is hitting the same resolver_failed fault "
        "(RuntimeError: Event loop is closed) repeatedly — "
        "one defect, active, worsening."
    )
    assert proposal.semantic_justification.splitlines()[0] == expected
    # And the pure function agrees byte-for-byte.
    assert judgment_line(
        "cultivator",
        "resolver_failed fault (RuntimeError: Event loop is closed)",
        "active", True,
    ) == expected


def test_judgment_line_pin_fixed_fixture():
    """proposal-card-legibility-v1 Phase 2 PIN — byte-exact current output of
    the pure template for fixed inputs. This is the conformance floor the
    Phase 3 portal recomposition builds on: any drift in the judgment line is
    a regression, not a restyle. Required to exist BEFORE Phase 3 work."""
    assert judgment_line(
        "terminal", "RED shell.effect.red (secret:operand)",
        "recurring", True,
    ) == (
        "terminal is hitting the same RED shell.effect.red (secret:operand) "
        "repeatedly — one defect, recurring, worsening."
    )
    assert judgment_line(
        "cultivator", "resolver_failed fault (RuntimeError: Event loop is closed)",
        "active", False,
    ) == (
        "cultivator is hitting the same resolver_failed fault "
        "(RuntimeError: Event loop is closed) repeatedly — "
        "one defect, active."
    )


def test_worsening_derivation_rate_split():
    window_start = _NOW - timedelta(days=14)
    recent = [_NOW - timedelta(days=1), _NOW - timedelta(days=2),
              _NOW - timedelta(days=3)]
    older = [_NOW - timedelta(days=10), _NOW - timedelta(days=11)]
    activity, worsening = derive_activity(
        recent + older, now=_NOW, window_start=window_start,
    )
    assert (activity, worsening) == ("active", True)
    # Balanced halves → not worsening; stale last event → recurring.
    balanced = [_NOW - timedelta(days=1), _NOW - timedelta(days=4),
                _NOW - timedelta(days=10), _NOW - timedelta(days=11)]
    activity, worsening = derive_activity(
        balanced, now=_NOW, window_start=window_start,
    )
    assert (activity, worsening) == ("active", False)
    stale = [_NOW - timedelta(days=10), _NOW - timedelta(days=11)]
    activity, worsening = derive_activity(
        stale, now=_NOW, window_start=window_start,
    )
    assert (activity, worsening) == ("recurring", False)


# ── dispositions: directions, not receipts (amendment 3b) ────────────────


def _stage_one(tmp_path):
    """Shared arrange: 5-event fleet class staged into a tmp queue."""
    from grove.eval.proposal_queue import append

    ledger = tmp_path / "ledger"
    queue = tmp_path / "proposals.jsonl"
    for i in range(5):
        _write_ledger(ledger, "fleet_cultivator_dispatch", [
            _fleet_halt(ts=_ts(days=i, hours=3),
                        session="fleet_cultivator_dispatch"),
        ])
    proposal = _detect(ledger)[0]
    assert append(proposal, path=queue) is True
    return ledger, queue, proposal


def test_acknowledge_then_quiet(tmp_path):
    """Acked at count 5, unchanged ledger → NOT re-staged (keep watching)."""
    from grove import flywheel_cli

    ledger, queue, proposal = _stage_one(tmp_path)
    rc = flywheel_cli.cli_acknowledge(
        proposal.proposal_id, queue_path=queue, ledger_dir=ledger,
    )
    assert rc == 0
    from grove.eval.proposal_queue import read_all
    assert read_all(path=queue) == []
    # The disposition event carries the baseline count.
    disposition_lines = [
        json.loads(line)
        for f in ledger.glob("cli-*.jsonl")
        for line in f.read_text(encoding="utf-8").splitlines()
    ]
    assert disposition_lines[-1]["disposition"] == "acknowledged"
    assert disposition_lines[-1]["acknowledged_count"] == 5
    # Same ledger state → suppressed.
    assert _detect(ledger) == []


def test_acknowledge_then_growth_restages(tmp_path):
    """Acked at 5; growth past 1.5x (8 > 7.5) → re-staged, SAME identity."""
    from grove import flywheel_cli

    ledger, queue, proposal = _stage_one(tmp_path)
    assert flywheel_cli.cli_acknowledge(
        proposal.proposal_id, queue_path=queue, ledger_dir=ledger,
    ) == 0
    assert _detect(ledger) == []  # quiet at same count
    for i in range(3):
        _write_ledger(ledger, f"fleet_cultivator_run{i}", [
            _fleet_halt(ts=_ts(hours=1 + i), session=f"fleet_cultivator_run{i}"),
        ])
    restaged = _detect(ledger)
    assert len(restaged) == 1
    assert restaged[0].proposal_id == proposal.proposal_id


def test_dismiss_suppresses_for_window(tmp_path):
    """Dismissed → strictly negative feedback: no re-staging in-window,
    even as the class keeps growing."""
    from grove import flywheel_cli

    ledger, queue, proposal = _stage_one(tmp_path)
    assert flywheel_cli.cli_reject(
        proposal.proposal_id, queue_path=queue, ledger_dir=ledger,
    ) == 0
    for i in range(6):
        _write_ledger(ledger, f"fleet_more{i}", [
            _fleet_halt(ts=_ts(hours=1 + i), session=f"fleet_more{i}"),
        ])
    assert _detect(ledger) == []


def test_acknowledge_verb_gated(tmp_path):
    """acknowledge refuses types whose PROPOSAL_VERBS do not declare it."""
    from grove import flywheel_cli
    from grove.eval.proposal_queue import (
        PROPOSAL_TYPE_ZONE_PROMOTION, RoutingProposal, append,
        compute_proposal_id,
    )

    queue = tmp_path / "proposals.jsonl"
    payload = {"tool": "t", "pattern": "p", "zone": "green", "reason": "r"}
    other = RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_ZONE_PROMOTION, payload=payload,
            evidence=("e",),
        ),
        type=PROPOSAL_TYPE_ZONE_PROMOTION, payload=payload, evidence=("e",),
        eval_hash="", created_at=_NOW.isoformat(),
    )
    assert append(other, path=queue) is True
    rc = flywheel_cli.cli_acknowledge(
        other.proposal_id, queue_path=queue, ledger_dir=tmp_path / "ledger",
    )
    assert rc == 1  # refused, still queued
    from grove.eval.proposal_queue import read_all
    assert len(read_all(path=queue)) == 1


# ── declarative thresholds ───────────────────────────────────────────────


def test_thresholds_loader_defaults_and_validation(tmp_path):
    assert load_fault_triage_thresholds(tmp_path / "absent.yaml") == \
        FaultTriageThresholds()
    good = tmp_path / "flywheel.config.yaml"
    good.write_text(
        "fault_triage:\n  min_events: 7\n  window_days: 10\n"
        "  reraise_growth: 2.0\n",
        encoding="utf-8",
    )
    loaded = load_fault_triage_thresholds(good)
    assert loaded == FaultTriageThresholds(
        min_events=7, min_sessions=1, window_days=10, reraise_growth=2.0,
    )
    bad = tmp_path / "bad.yaml"
    bad.write_text("fault_triage:\n  min_events: nope\n", encoding="utf-8")
    with pytest.raises(ValueError, match="min_events"):
        load_fault_triage_thresholds(bad)
    low = tmp_path / "low.yaml"
    low.write_text("fault_triage:\n  reraise_growth: 0.5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="reraise_growth"):
        load_fault_triage_thresholds(low)


# ── structured detail envelope (proposal-card-legibility-v1 Phase 2) ────


def test_detail_normalizes_red_resolution(tmp_path):
    """red_resolution samples → (date, triggering_tool, resolution); the
    envelope round-trips through the registered codec; sj is unchanged as
    the verbatim fallback source."""
    from grove.kaizen.rendering import FaultTriageDetail, decode_detail

    ledger = tmp_path / "ledger"
    for i in range(5):
        _write_ledger(ledger, f"s{i % 3}", [
            _red(ts=_ts(days=1 + i), session=f"s{i % 3}"),
        ])
    proposal = _detect(ledger)[0]
    assert proposal.detail is not None
    decoded = decode_detail(proposal)
    assert isinstance(decoded, FaultTriageDetail)
    # first / middle / last of 5 events → 3 samples, dates ascending.
    assert len(decoded.samples) == 3
    for s in decoded.samples:
        assert s.subject == "terminal"
        assert s.outcome == "cancel"
        # date-only — no time component, no microseconds.
        assert len(s.ts) == 10 and s.ts.count("-") == 2
    assert [s.ts for s in decoded.samples] == sorted(s.ts for s in decoded.samples)
    # sj (the fallback source) still carries judgment + counts + raw samples.
    assert proposal.semantic_justification.splitlines()[0].startswith("terminal is hitting")
    assert "Samples: " in proposal.semantic_justification


def test_detail_normalizes_fleet_and_dispatcher(tmp_path):
    """fleet_worker → (date, worker, check); dispatcher_halt →
    (date, intents[0].tool_name, matched_rule)."""
    from grove.kaizen.rendering import decode_detail

    fleet = tmp_path / "fleet"
    for i in range(5):
        _write_ledger(fleet, f"s{i % 2}", [
            _fleet_halt(ts=_ts(days=1 + i), session=f"s{i % 2}"),
        ])
    decoded = decode_detail(_detect(fleet)[0])
    assert {(s.subject, s.outcome) for s in decoded.samples} == {
        ("cultivator", "resolver_failed"),
    }

    disp = tmp_path / "disp"
    for i in range(5):
        _write_ledger(disp, f"s{i % 2}", [
            _dispatcher_halt(ts=_ts(days=1 + i), session=f"s{i % 2}"),
        ])
    decoded = decode_detail(_detect(disp)[0])
    assert {(s.subject, s.outcome) for s in decoded.samples} == {
        ("terminal", "shell.effect.default"),
    }


def test_decode_detail_absent_and_malformed():
    """Absent detail → None (legacy queue rows keep rendering via sj);
    malformed detail → ValueError (fail loud — the render caller owns the
    fallback, never a silent blank)."""
    from grove.kaizen.rendering import decode_detail

    class _Legacy:
        type = "fault_triage"
        detail = None

    assert decode_detail(_Legacy()) is None

    class _Malformed:
        type = "fault_triage"
        detail = {"samples": [{"ts": "2026-07-11"}]}  # missing subject/outcome

    with pytest.raises(ValueError, match="missing field"):
        decode_detail(_Malformed())

    class _Uncodeced:
        type = "routing_adjustment"
        detail = {"anything": True}

    assert decode_detail(_Uncodeced()) is None

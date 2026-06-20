"""learning-loop-bridge-v1 (Strike 2) — the self-authoring loop.

Three dark paths illuminated:
  1. YELLOW promotion detector  — repeated andon_disposition approvals queue a
     system-initiated zone_promotion proposal (grove.eval.disposition_promotion).
  2. Kaizen outcome recording   — cli_approve / cli_reject write a
     kaizen_disposition ledger event for every proposal type.
  3. Connector remediation      — a verified Retry that followed a real prior
     failure writes a correction IntentRecord (grove.connector_remediation,
     wired at run_agent.AIAgent._apply_connector_disposition).
"""

from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grove.eval.disposition_promotion import (
    DispositionPromotionDetector,
    PromotionThresholds,
    load_promotion_thresholds,
)
from grove.eval.proposal_queue import read_all
from grove import flywheel_cli
from grove.flywheel_cli import run_disposition_promotion_scan


NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


# ── helpers ──────────────────────────────────────────────────────────


def _write_disposition(
    ledger_dir: Path,
    session: str,
    *,
    disposition: str,
    tool: str,
    rule: str,
    days_ago: int,
    zone: str = "yellow",
) -> None:
    """Append one andon_disposition event in the ledger's on-disk shape."""
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ts = (NOW - timedelta(days=days_ago)).isoformat()
    event = {
        "event_type": "andon_disposition",
        "session_id": session,
        "timestamp": ts,
        "disposition": disposition,
        "zone": zone,
        "matched_rule": rule,
        "triggering_tool": tool,
    }
    with open(ledger_dir / f"{session}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def _read_ledger_events(ledger_dir: Path, event_type: str) -> list:
    """All events of ``event_type`` across every ledger file in ``ledger_dir``."""
    out = []
    for path in sorted(Path(ledger_dir).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("event_type") == event_type:
                out.append(event)
    return out


# ════════════════════════════════════════════════════════════════════
# 1) YELLOW promotion detector
# ════════════════════════════════════════════════════════════════════


def test_1_three_approvals_two_sessions_queues_proposal(tmp_path: Path):
    ledger = tmp_path / "ledger"
    queue = tmp_path / "proposals.jsonl"
    _write_disposition(ledger, "s1", disposition="always", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=5)
    _write_disposition(ledger, "s1", disposition="session", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=4)
    _write_disposition(ledger, "s2", disposition="session", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=2)

    new, dup = run_disposition_promotion_scan(
        ledger_dir=ledger, queue_path=queue, now=NOW,
    )
    assert (new, dup) == (1, 0)
    proposals = read_all(path=queue)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.type == "zone_promotion"
    assert p.payload["tool"] == "terminal"
    assert p.payload["pattern"] == "^ffmpeg .*$"
    assert p.payload["zone"] == "green"


def test_2_below_count_no_proposal(tmp_path: Path):
    ledger = tmp_path / "ledger"
    queue = tmp_path / "proposals.jsonl"
    _write_disposition(ledger, "s1", disposition="always", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=5)
    _write_disposition(ledger, "s2", disposition="session", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=2)
    new, dup = run_disposition_promotion_scan(
        ledger_dir=ledger, queue_path=queue, now=NOW,
    )
    assert (new, dup) == (0, 0)
    assert read_all(path=queue) == []


def test_3_three_approvals_one_session_no_proposal(tmp_path: Path):
    ledger = tmp_path / "ledger"
    queue = tmp_path / "proposals.jsonl"
    for d in (6, 4, 2):
        _write_disposition(ledger, "s1", disposition="session", tool="terminal",
                           rule="^ffmpeg .*$", days_ago=d)
    new, dup = run_disposition_promotion_scan(
        ledger_dir=ledger, queue_path=queue, now=NOW,
    )
    assert (new, dup) == (0, 0)


def test_4_deny_in_window_vetoes(tmp_path: Path):
    ledger = tmp_path / "ledger"
    queue = tmp_path / "proposals.jsonl"
    _write_disposition(ledger, "s1", disposition="always", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=6)
    _write_disposition(ledger, "s2", disposition="session", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=4)
    _write_disposition(ledger, "s2", disposition="session", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=3)
    # A single deny for the same key vetoes the promotion.
    _write_disposition(ledger, "s3", disposition="deny", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=1)
    new, dup = run_disposition_promotion_scan(
        ledger_dir=ledger, queue_path=queue, now=NOW,
    )
    assert (new, dup) == (0, 0)


def test_4b_out_of_window_approvals_ignored(tmp_path: Path):
    ledger = tmp_path / "ledger"
    queue = tmp_path / "proposals.jsonl"
    # Two recent, one ancient (outside the 30d window) → below count.
    _write_disposition(ledger, "s1", disposition="always", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=2)
    _write_disposition(ledger, "s2", disposition="session", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=3)
    _write_disposition(ledger, "s3", disposition="session", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=40)
    new, dup = run_disposition_promotion_scan(
        ledger_dir=ledger, queue_path=queue, now=NOW,
    )
    assert (new, dup) == (0, 0)


def test_4c_default_rule_is_not_promotable(tmp_path: Path):
    ledger = tmp_path / "ledger"
    queue = tmp_path / "proposals.jsonl"
    for s, d in (("s1", 6), ("s1", 4), ("s2", 2)):
        _write_disposition(ledger, s, disposition="always", tool="terminal",
                           rule="default", days_ago=d)
    new, dup = run_disposition_promotion_scan(
        ledger_dir=ledger, queue_path=queue, now=NOW,
    )
    assert (new, dup) == (0, 0)


def test_5_dedup_no_duplicate_on_rerun(tmp_path: Path):
    ledger = tmp_path / "ledger"
    queue = tmp_path / "proposals.jsonl"
    _write_disposition(ledger, "s1", disposition="always", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=5)
    _write_disposition(ledger, "s1", disposition="session", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=4)
    _write_disposition(ledger, "s2", disposition="session", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=2)
    first = run_disposition_promotion_scan(
        ledger_dir=ledger, queue_path=queue, now=NOW,
    )
    second = run_disposition_promotion_scan(
        ledger_dir=ledger, queue_path=queue, now=NOW,
    )
    assert first == (1, 0)
    assert second == (0, 1)
    assert len(read_all(path=queue)) == 1


def test_5b_dedup_survives_new_approval(tmp_path: Path):
    # An additional approval for the same (tool, rule) must NOT stack a second
    # proposal — identity is stable per (tool, pattern); the audit grows in
    # source_patterns (which is excluded from the proposal_id).
    ledger = tmp_path / "ledger"
    queue = tmp_path / "proposals.jsonl"
    _write_disposition(ledger, "s1", disposition="always", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=5)
    _write_disposition(ledger, "s1", disposition="session", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=4)
    _write_disposition(ledger, "s2", disposition="session", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=2)
    assert run_disposition_promotion_scan(
        ledger_dir=ledger, queue_path=queue, now=NOW) == (1, 0)
    _write_disposition(ledger, "s3", disposition="session", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=1)
    assert run_disposition_promotion_scan(
        ledger_dir=ledger, queue_path=queue, now=NOW) == (0, 1)
    assert len(read_all(path=queue)) == 1


def test_6_threshold_is_configurable_not_hardcoded(tmp_path: Path):
    ledger = tmp_path / "ledger"
    queue = tmp_path / "proposals.jsonl"
    _write_disposition(ledger, "s1", disposition="session", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=3)
    _write_disposition(ledger, "s2", disposition="session", tool="terminal",
                       rule="^ffmpeg .*$", days_ago=2)
    # Default thresholds (count=3) → no proposal from two approvals.
    assert run_disposition_promotion_scan(
        ledger_dir=ledger, queue_path=queue, now=NOW) == (0, 0)
    # Lowered thresholds (count=2) → the SAME ledger now earns a proposal.
    relaxed = PromotionThresholds(count=2, sessions=2, window_days=30)
    assert run_disposition_promotion_scan(
        ledger_dir=ledger, queue_path=queue, now=NOW, thresholds=relaxed) == (1, 0)


def test_6b_load_thresholds_defaults_when_absent(tmp_path: Path):
    # Absent file → documented defaults (not a failure).
    assert load_promotion_thresholds(tmp_path / "nope.yaml") == PromotionThresholds()


def test_6c_load_thresholds_reads_operator_overrides(tmp_path: Path):
    cfg = tmp_path / "flywheel.config.yaml"
    cfg.write_text(
        "disposition_promotion:\n"
        "  threshold_count: 5\n"
        "  threshold_sessions: 3\n"
        "  window_days: 14\n",
        encoding="utf-8",
    )
    t = load_promotion_thresholds(cfg)
    assert (t.count, t.sessions, t.window_days) == (5, 3, 14)


def test_6d_load_thresholds_fails_loud_on_bad_value(tmp_path: Path):
    cfg = tmp_path / "flywheel.config.yaml"
    cfg.write_text(
        "disposition_promotion:\n  threshold_count: 0\n", encoding="utf-8",
    )
    with pytest.raises(ValueError, match="threshold_count"):
        load_promotion_thresholds(cfg)


def test_6e_load_thresholds_fails_loud_on_inverted_invariant(tmp_path: Path):
    cfg = tmp_path / "flywheel.config.yaml"
    cfg.write_text(
        "disposition_promotion:\n"
        "  threshold_count: 2\n"
        "  threshold_sessions: 5\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cannot exceed"):
        load_promotion_thresholds(cfg)


def test_once_disposition_is_not_an_approval(tmp_path: Path):
    # "once" is a single-turn grant, not a standing promotion signal.
    ledger = tmp_path / "ledger"
    queue = tmp_path / "proposals.jsonl"
    for s, d in (("s1", 6), ("s1", 4), ("s2", 2)):
        _write_disposition(ledger, s, disposition="once", tool="terminal",
                           rule="^ffmpeg .*$", days_ago=d)
    assert run_disposition_promotion_scan(
        ledger_dir=ledger, queue_path=queue, now=NOW) == (0, 0)


# ════════════════════════════════════════════════════════════════════
# 2) Kaizen outcome recording
# ════════════════════════════════════════════════════════════════════


def _queue_zone_proposal(queue_path: Path):
    from grove.kaizen_promotion import build_zone_promotion_proposal
    from grove.eval.proposal_queue import append
    proposal, _payload = build_zone_promotion_proposal(
        tool_name="terminal",
        command_string="ffmpeg -i in.mp4 out.mp3",
        evidence_turn_id="t_evidence",
    )
    append(proposal, path=queue_path)
    return proposal


def test_7_approve_writes_kaizen_disposition_applied(tmp_path: Path, monkeypatch):
    queue = tmp_path / "proposals.jsonl"
    machine = tmp_path / "routing.autonomaton.yaml"
    ledger = tmp_path / "ledger"
    monkeypatch.setattr("grove.zone_rules.save_zone_rule",
                        lambda **kw: None)
    proposal = _queue_zone_proposal(queue)

    rc = flywheel_cli.cli_approve(
        proposal.proposal_id, queue_path=queue, machine_path=machine,
        ledger_dir=ledger,
    )
    assert rc == 0
    events = _read_ledger_events(ledger, "kaizen_disposition")
    assert len(events) == 1
    assert events[0]["disposition"] == "applied"
    assert events[0]["proposal_id"] == proposal.proposal_id
    assert events[0]["proposal_type"] == "zone_promotion"


def test_8_reject_writes_kaizen_disposition_rejected(tmp_path: Path):
    queue = tmp_path / "proposals.jsonl"
    ledger = tmp_path / "ledger"
    proposal = _queue_zone_proposal(queue)

    rc = flywheel_cli.cli_reject(
        proposal.proposal_id, reason="not safe", queue_path=queue,
        ledger_dir=ledger,
    )
    assert rc == 0
    events = _read_ledger_events(ledger, "kaizen_disposition")
    assert len(events) == 1
    assert events[0]["disposition"] == "rejected"
    assert events[0]["reason"] == "not safe"


def test_9_disposition_event_carries_identity_and_evidence(
    tmp_path: Path, monkeypatch,
):
    queue = tmp_path / "proposals.jsonl"
    machine = tmp_path / "routing.autonomaton.yaml"
    ledger = tmp_path / "ledger"
    monkeypatch.setattr("grove.zone_rules.save_zone_rule", lambda **kw: None)
    proposal = _queue_zone_proposal(queue)

    flywheel_cli.cli_approve(
        proposal.proposal_id, queue_path=queue, machine_path=machine,
        ledger_dir=ledger,
    )
    event = _read_ledger_events(ledger, "kaizen_disposition")[0]
    assert event["proposal_id"] == proposal.proposal_id
    assert event["proposal_type"] == "zone_promotion"
    assert event["evidence_count"] == len(proposal.evidence)
    assert "applied_result" in event


# ════════════════════════════════════════════════════════════════════
# 3) Connector remediation recording
# ════════════════════════════════════════════════════════════════════


@pytest.fixture
def _reset_breaker():
    import tools.mcp_tool as mt
    mt._server_connect_failed.clear()
    mt._server_connect_auth_evidence.clear()
    mt._servers.clear()
    yield
    mt._server_connect_failed.clear()
    mt._server_connect_auth_evidence.clear()
    mt._servers.clear()


def _tmp_store(tmp_path: Path):
    from grove.intent_store import IntentStore
    return IntentStore(store_path=tmp_path / "intent_records.jsonl")


def _agent_with_registry(session_id="sess-conn"):
    from run_agent import AIAgent
    from tools.registry import ToolRegistry
    a = AIAgent.__new__(AIAgent)
    a.enabled_toolsets = None
    a.session_id = session_id
    a._dispatcher_singleton = types.SimpleNamespace(registry=ToolRegistry())
    return a


def test_10_retry_success_with_prior_failure_records_correction(
    tmp_path: Path, monkeypatch, _reset_breaker,
):
    import tools.mcp_tool as mt
    store = _tmp_store(tmp_path)
    monkeypatch.setattr("grove.intent_store.get_store", lambda: store)
    mt._bump_connect_failed("notion", "reauth")          # prior failure
    monkeypatch.setattr(mt, "discover_mcp_tools", lambda registry=None: [])

    a = _agent_with_registry()
    msg = a._apply_connector_disposition("retry", "notion")

    assert "Reconnected" in msg
    records = [json.loads(line) for line in
               store.path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["outcome"] == "correction"
    assert records[0]["intent_class"] == "connector_remediation"
    assert records[0]["session_id"] == "sess-conn"


def test_11_retry_success_without_prior_failure_no_record(
    tmp_path: Path, monkeypatch, _reset_breaker,
):
    import tools.mcp_tool as mt
    store = _tmp_store(tmp_path)
    monkeypatch.setattr("grove.intent_store.get_store", lambda: store)
    # No prior failure for notion → a normal connect, not a remediation.
    monkeypatch.setattr(mt, "discover_mcp_tools", lambda registry=None: [])

    a = _agent_with_registry()
    msg = a._apply_connector_disposition("retry", "notion")

    assert "Reconnected" in msg
    assert not store.path.exists() or store.path.read_text() == ""


def test_12_failed_reconnect_writes_no_correction(
    tmp_path: Path, monkeypatch, _reset_breaker,
):
    import tools.mcp_tool as mt
    store = _tmp_store(tmp_path)
    monkeypatch.setattr("grove.intent_store.get_store", lambda: store)
    mt._bump_connect_failed("notion", "reauth")          # prior failure

    def _still_down(registry=None):
        mt._bump_connect_failed("notion", "reauth")      # re-connect fails
        return []

    monkeypatch.setattr(mt, "discover_mcp_tools", _still_down)

    a = _agent_with_registry()
    msg = a._apply_connector_disposition("retry", "notion")

    assert "Still couldn't reach" in msg
    assert not store.path.exists() or store.path.read_text() == ""

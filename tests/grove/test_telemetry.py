"""Tests for grove.telemetry — sovereignty_decision event logging."""

from __future__ import annotations

import json
import logging

import pytest

from grove.telemetry import log_sovereignty_decision


def test_event_shape_promote(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="grove.telemetry"):
        event = log_sovereignty_decision(
            action="promote",
            skill_name="weekly-team-sync",
            skill_hash="sha256:deadbeefcafebabe",
            scan_verdict="safe",
            operator="jim@the-grove.ai",
            source_path="/tmp/.andon/weekly-team-sync",
            dest_path="/tmp/skills/weekly-team-sync",
        )
    assert event["event_type"] == "sovereignty_decision"
    assert event["action"] == "promote"
    assert event["skill_name"] == "weekly-team-sync"
    assert event["scan_verdict"] == "safe"
    assert event["operator"] == "jim@the-grove.ai"
    assert event["reason"] is None
    assert "timestamp" in event and event["timestamp"].endswith("Z")

    # The logger emitted a JSON-bearing record.
    records = [r for r in caplog.records if r.name == "grove.telemetry"]
    assert records, "no grove.telemetry log records captured"
    last = records[-1]
    payload = json.loads(last.getMessage().split(" ", 1)[1])
    assert payload["action"] == "promote"
    assert payload["skill_name"] == "weekly-team-sync"


def test_event_shape_reject_with_reason(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="grove.telemetry"):
        event = log_sovereignty_decision(
            action="reject",
            skill_name="suspicious-skill",
            scan_verdict="dangerous",
            operator="jim@the-grove.ai",
            source_path="/tmp/.andon/suspicious-skill",
            reason="Scan flagged credential exfiltration pattern.",
        )
    assert event["action"] == "reject"
    assert event["reason"] == "Scan flagged credential exfiltration pattern."
    assert event["dest_path"] is None


def test_event_shape_revoke() -> None:
    event = log_sovereignty_decision(
        action="revoke",
        skill_name="weekly-team-sync",
        operator="jim@the-grove.ai",
        source_path="/tmp/skills/weekly-team-sync",
        dest_path="/tmp/.andon/weekly-team-sync",
    )
    assert event["action"] == "revoke"
    assert event["source_path"].endswith("skills/weekly-team-sync")
    assert event["dest_path"].endswith(".andon/weekly-team-sync")


def test_required_fields_present() -> None:
    """Every event must carry the keys downstream tooling expects."""
    event = log_sovereignty_decision(action="promote", skill_name="x")
    expected_keys = {
        "event_type", "action", "skill_name", "skill_hash",
        "scan_verdict", "operator", "reason", "timestamp",
        "source_path", "dest_path",
    }
    assert expected_keys.issubset(event.keys())

"""artifact-review-v1 P4 — verdict feed writer pins (Family 2 idiom).

Append-only, one record per evaluation, keyed (run_id, attempt) with a
redraft as a SEPARATE record (R-3a), fail-loud on a malformed record.
"""
from __future__ import annotations

import json

import pytest

from grove.fleet.verdict_ledger import VerdictLedger


def _fields(**over):
    base = {
        "artifact_id": "draft-u1.md",
        "rubric_key": "resume-package@1",
        "criteria_ids": ["c1", "c2"],
        "effective_threshold": 0.9,
        "threshold_source": "rubric_default",
        "status": "pass",
        "complete": True,
        "accurate": True,
        "quality_score": 0.93,
        "issues": [],
        "evaluator_tier": "T-QA",
        "evaluator_model": "stub/eval-model",
    }
    base.update(over)
    return base


def _read(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_record_appends_and_keys_on_run_attempt(tmp_path):
    led = VerdictLedger("rid1", ledger_dir=tmp_path)
    rec = led.record(0, **_fields())
    assert rec["run_id"] == "rid1"
    assert rec["attempt"] == 0
    assert "timestamp" in rec
    assert rec["rubric_key"] == "resume-package@1"
    rows = _read(led.path)
    assert len(rows) == 1 and rows[0]["attempt"] == 0


def test_redraft_is_separate_appended_record(tmp_path):
    """R-3a — a redraft verdict is a new record at attempt=1, never an
    amendment; both are retrievable from the run's file."""
    led = VerdictLedger("rid2", ledger_dir=tmp_path)
    led.record(0, **_fields(status="fail", quality_score=0.4, complete=False))
    led.record(1, **_fields(status="pass", quality_score=0.91))
    rows = _read(led.path)
    assert [r["attempt"] for r in rows] == [0, 1]
    assert rows[0]["status"] == "fail" and rows[1]["status"] == "pass"
    assert rows[0]["quality_score"] == 0.4  # first record untouched by the redraft


def test_missing_required_field_fails_loud(tmp_path):
    led = VerdictLedger("rid3", ledger_dir=tmp_path)
    bad = _fields()
    del bad["rubric_key"]
    with pytest.raises(ValueError, match="missing required field"):
        led.record(0, **bad)


def test_reserved_field_collision_fails_loud(tmp_path):
    led = VerdictLedger("rid4", ledger_dir=tmp_path)
    with pytest.raises(ValueError, match="reserved fields"):
        led.record(0, timestamp="nope", **_fields())


def test_unknown_field_fails_loud(tmp_path):
    led = VerdictLedger("rid5", ledger_dir=tmp_path)
    with pytest.raises(ValueError, match="unknown field"):
        led.record(0, surprise=1, **_fields())


def test_invalid_threshold_source_fails_loud(tmp_path):
    led = VerdictLedger("rid6", ledger_dir=tmp_path)
    with pytest.raises(ValueError, match="threshold_source"):
        led.record(0, **_fields(threshold_source="made_up"))


def test_negative_attempt_fails_loud(tmp_path):
    led = VerdictLedger("rid7", ledger_dir=tmp_path)
    with pytest.raises(ValueError, match="attempt"):
        led.record(-1, **_fields())

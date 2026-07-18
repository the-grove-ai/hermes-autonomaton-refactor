"""Tests for grove.kaizen_ledger — Sprint 26 Phase 6.

The Kaizen Ledger is the persistent, structured, async-queryable
backend of the foreground/background split per GRV-005 § IX(4).
Tests cover:

* per-session file creation + sanitized filename
* event_type validation (fail loud on unknown types)
* reserved-field protection (event_type, session_id, timestamp)
* JSON-line append semantics + read-back
* events_by_type filter
* malformed-line tolerance during reads
* thread-safety of concurrent appends
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from grove.kaizen_ledger import KaizenLedger


class TestKaizenLedgerConstruction:
    def test_creates_ledger_dir(self, tmp_path: Path):
        ledger_dir = tmp_path / "kaizen"
        assert not ledger_dir.exists()
        KaizenLedger(session_id="s1", ledger_dir=ledger_dir)
        assert ledger_dir.is_dir()

    def test_path_is_session_id_jsonl(self, tmp_path: Path):
        led = KaizenLedger(session_id="s1", ledger_dir=tmp_path)
        assert led.path == tmp_path / "s1.jsonl"

    def test_sanitizes_unsafe_filename_chars(self, tmp_path: Path):
        led = KaizenLedger(
            session_id="s1/../../../etc/passwd", ledger_dir=tmp_path,
        )
        # All slashes / dots sanitized to underscore
        assert "/" not in led.path.name
        assert led.path.parent == tmp_path

    def test_truncates_long_session_id(self, tmp_path: Path):
        led = KaizenLedger(session_id="x" * 500, ledger_dir=tmp_path)
        # 128-char cap + .jsonl extension
        assert len(led.path.stem) == 128

    def test_default_dir_uses_hermes_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
        led = KaizenLedger(session_id="s1")
        assert led.path.parent == tmp_path / ".kaizen_ledger"


class TestKaizenLedgerRecord:
    def test_record_appends_jsonl_event(self, tmp_path: Path):
        led = KaizenLedger(session_id="s1", ledger_dir=tmp_path)
        led.record("tier_override", batch_size=2, latency_ms=42.5)
        content = led.path.read_text(encoding="utf-8")
        # One line, parseable JSON
        line = content.strip()
        event = json.loads(line)
        assert event["event_type"] == "tier_override"
        assert event["session_id"] == "s1"
        assert event["batch_size"] == 2
        assert event["latency_ms"] == 42.5
        # Auto-populated timestamp present
        assert "timestamp" in event

    def test_record_returns_event_dict(self, tmp_path: Path):
        led = KaizenLedger(session_id="s1", ledger_dir=tmp_path)
        event = led.record("final_response", content_length=42)
        assert event["event_type"] == "final_response"
        assert event["content_length"] == 42

    def test_record_appends_multiple_events(self, tmp_path: Path):
        led = KaizenLedger(session_id="s1", ledger_dir=tmp_path)
        led.record("tier_override", batch_size=1)
        led.record("andon_halt", zone="red", matched_rule="r")
        led.record("final_response", content_length=10)
        lines = led.path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3

    def test_record_rejects_unknown_event_type(self, tmp_path: Path):
        led = KaizenLedger(session_id="s1", ledger_dir=tmp_path)
        with pytest.raises(ValueError, match="unknown kaizen event_type"):
            led.record("invented_event", x=1)

    def test_record_rejects_reserved_field_override(self, tmp_path: Path):
        # event_type collision is caught by Python's call-binding (it's
        # the positional arg name on `record`); session_id and timestamp
        # are auto-populated reserved fields the ledger guards explicitly.
        led = KaizenLedger(session_id="s1", ledger_dir=tmp_path)
        with pytest.raises(ValueError, match="reserved"):
            led.record("final_response", session_id="other")
        with pytest.raises(ValueError, match="reserved"):
            led.record("final_response", timestamp="fake")

    def test_record_rejects_event_type_via_kwargs(self, tmp_path: Path):
        # Python catches the kwarg collision before our reserved check.
        led = KaizenLedger(session_id="s1", ledger_dir=tmp_path)
        with pytest.raises(TypeError, match="event_type"):
            led.record("final_response", event_type="lying")


class TestKaizenLedgerRead:
    def test_events_returns_empty_when_no_file(self, tmp_path: Path):
        led = KaizenLedger(session_id="s1", ledger_dir=tmp_path)
        assert list(led.events()) == []

    def test_events_streams_back_in_append_order(self, tmp_path: Path):
        led = KaizenLedger(session_id="s1", ledger_dir=tmp_path)
        led.record("tier_override", n=1)
        led.record("andon_halt", n=2)
        led.record("final_response", n=3)
        events = list(led.events())
        assert [e["n"] for e in events] == [1, 2, 3]
        assert [e["event_type"] for e in events] == [
            "tier_override", "andon_halt", "final_response",
        ]

    def test_events_skips_malformed_lines(self, tmp_path: Path):
        led = KaizenLedger(session_id="s1", ledger_dir=tmp_path)
        led.record("final_response", content_length=1)
        # Corrupt the file: append an invalid line
        with open(led.path, "a", encoding="utf-8") as fh:
            fh.write("this is not json\n")
        led.record("final_response", content_length=2)
        events = list(led.events())
        # Two valid events; malformed line dropped silently
        assert len(events) == 2
        assert [e["content_length"] for e in events] == [1, 2]

    def test_events_by_type_filters(self, tmp_path: Path):
        led = KaizenLedger(session_id="s1", ledger_dir=tmp_path)
        led.record("tier_override", n=1)
        led.record("andon_halt", n=2)
        led.record("tier_override", n=3)
        halts = led.events_by_type("andon_halt")
        batches = led.events_by_type("tier_override")
        assert len(halts) == 1
        assert len(batches) == 2
        assert halts[0]["n"] == 2
        assert [b["n"] for b in batches] == [1, 3]


class TestKaizenLedgerThreadSafety:
    def test_concurrent_appends_do_not_interleave(self, tmp_path: Path):
        led = KaizenLedger(session_id="s1", ledger_dir=tmp_path)

        def _write_n(start: int, n: int) -> None:
            for i in range(n):
                led.record("tier_override", thread_id=start, seq=i)

        threads = [
            threading.Thread(target=_write_n, args=(t, 25))
            for t in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 100 events total, all valid JSON, no torn writes
        events = list(led.events())
        assert len(events) == 100
        # Every event has the right fields
        assert all("thread_id" in e and "seq" in e for e in events)

"""detector-sweep-resilience-v1 P2 — producer pause writer/reader/seam.

Pins: writer round-trip (pause → reader sees it → unpause → gone), the
set_publication_state discipline (contention defers, ``.bak`` written,
atomic file), write-strict validation, the registered ``producer_paused``
audit event filed with proposal_id (and its error-log floor), the
read-resilient reader (missing → empty, malformed → WARNING + empty and
the SWEEP PROCEEDS), and the stub-replacement pin: a paused producer is
skipped through the REAL reader — INFO logged, zero invocation.

GROVE_HOME is per-test isolated (autouse conftest), so the pause file,
the ledger, and the sweep collaborators all land in a tempdir.
"""

from __future__ import annotations

import json
import logging

import pytest
import yaml

from grove.eval.producer_pauses import (
    default_pauses_path,
    read_producer_pauses,
    set_producer_pause,
)
from grove.kaizen_ledger import KaizenLedger, default_ledger_dir


def _audit_events():
    events = []
    for path in sorted(default_ledger_dir().glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["event_type"] == "producer_paused":
                events.append(event)
    return events


# ── writer round-trip + discipline ──────────────────────────────────────────


def test_pause_roundtrip_and_unpause():
    assert read_producer_pauses() == frozenset()
    assert set_producer_pause(
        "freshness_detector", True,
        proposal_id="sha256:abc", reason="3 failures in 7d",
    ) == "applied"
    assert read_producer_pauses() == frozenset({"freshness_detector"})
    assert set_producer_pause("freshness_detector", False) == "applied"
    assert read_producer_pauses() == frozenset()
    # unpause keeps the entry (audit-legible), only flips the bool
    data = yaml.safe_load(default_pauses_path().read_text(encoding="utf-8"))
    assert data["producers"]["freshness_detector"]["paused"] is False


def test_pause_preserves_sibling_entries():
    set_producer_pause("freshness_detector", True)
    set_producer_pause("dock_mutation_detector", True)
    assert read_producer_pauses() == frozenset(
        {"freshness_detector", "dock_mutation_detector"}
    )
    set_producer_pause("freshness_detector", False)
    assert read_producer_pauses() == frozenset({"dock_mutation_detector"})


def test_bak_written_on_second_write():
    set_producer_pause("a_detector", True)
    bak = default_pauses_path().with_suffix(".yaml.bak")
    assert not bak.exists()  # first write had no prior bytes
    set_producer_pause("b_detector", True)
    assert bak.exists()
    prior = yaml.safe_load(bak.read_text(encoding="utf-8"))
    assert "b_detector" not in prior["producers"]  # .bak holds the PRIOR state


def test_contention_defers():
    fcntl = pytest.importorskip("fcntl")
    p = default_pauses_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_suffix(".yaml.lock")
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert set_producer_pause("x_detector", True) == "deferred"
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()
    assert read_producer_pauses() == frozenset()  # nothing landed


def test_write_strict_validation():
    with pytest.raises(ValueError):
        set_producer_pause("", True)
    with pytest.raises(ValueError):
        set_producer_pause("x", 1)  # int is not a real bool


# ── audit event ─────────────────────────────────────────────────────────────


def test_audit_event_registered_and_filed():
    assert "producer_paused" in KaizenLedger.EVENT_TYPES
    set_producer_pause(
        "graduation_detector", True,
        proposal_id="sha256:card1", reason="recurring T1 timeouts",
    )
    events = _audit_events()
    assert len(events) == 1
    assert events[0]["producer"] == "graduation_detector"
    assert events[0]["paused"] is True
    assert events[0]["proposal_id"] == "sha256:card1"
    assert events[0]["reason"] == "recurring T1 timeouts"


def test_audit_filing_failure_floors_file_mutation_stands(monkeypatch, caplog):
    # File-backed writer: the pause landed atomically; audit failure floors
    # to logger.error without re-raise (set_model_binding precedent).
    import grove.eval.producer_pauses as pp

    def _boom(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(
        "grove.kaizen_ledger.KaizenLedger.record", _boom
    )
    with caplog.at_level(logging.ERROR):
        assert set_producer_pause("x_detector", True) == "applied"
    assert "audit filing failed" in caplog.text
    assert pp.read_producer_pauses() == frozenset({"x_detector"})


# ── reader resilience ───────────────────────────────────────────────────────


def test_missing_file_empty():
    assert read_producer_pauses() == frozenset()


def test_malformed_file_warns_empty_and_sweep_proceeds(
    monkeypatch, tmp_path, caplog
):
    p = default_pauses_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not yaml: [", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert read_producer_pauses() == frozenset()
    assert "unreadable" in caplog.text
    # The sweep proceeds on the empty set: every producer still invoked.
    # (_stub_sweep repoints GROVE_HOME at tmp_path — plant the malformed
    # file there too so the SWEEP's own read hits it.)
    from tests.grove.test_detector_sweep_resilience import _shell, _stub_sweep

    calls: list = []
    _stub_sweep(monkeypatch, tmp_path, calls)
    bad = tmp_path / "flywheel" / "producer_pauses.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not yaml: [", encoding="utf-8")
    _shell()._extract_memory_from_dormant_sessions(["sess-1"])
    for tag in ("context_persistence", "freshness", "graduation",
                "consolidation", "dock_mutation", "compaction"):
        assert tag in calls


def test_non_mapping_producers_key_warns_empty(caplog):
    p = default_pauses_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("producers: [a, b]\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert read_producer_pauses() == frozenset()
    assert "no 'producers' mapping" in caplog.text


# ── stub replacement: pause-skip through the REAL reader ───────────────────


def test_paused_producer_skipped_via_real_reader(monkeypatch, tmp_path, caplog):
    from tests.grove.test_detector_sweep_resilience import _shell, _stub_sweep

    calls: list = []
    # Stub FIRST (repoints get_hermes_home at tmp_path), then write the
    # pause through the REAL writer — path resolution and the sweep's read
    # now agree on tmp_path/flywheel/producer_pauses.yaml by construction.
    _stub_sweep(monkeypatch, tmp_path, calls)
    set_producer_pause(
        "dock_mutation_detector", True, proposal_id="sha256:card1",
    )

    with caplog.at_level(logging.INFO):
        _shell()._extract_memory_from_dormant_sessions(["sess-1"])

    # ZERO invocation of the paused producer; siblings all ran.
    assert "dock_mutation" not in calls
    for tag in ("context_persistence", "freshness", "graduation",
                "consolidation", "compaction"):
        assert tag in calls
    assert "paused by operator" in caplog.text
    # No producer_failure events — a pause is a skip, not a soft-fail.
    for path in sorted(default_ledger_dir().glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            assert json.loads(line)["event_type"] != "producer_failure"

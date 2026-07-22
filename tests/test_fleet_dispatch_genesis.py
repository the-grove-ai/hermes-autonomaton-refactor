"""fleet-receipt-custody-v1 P2 Commit C1 — genesis dispatch record, runner-owned.

Every dispatch mints a durable genesis record BEFORE the worker exists, keyed
identically to the terminal event (one record per run_id). The runner closes
the record with a terminal receipt on every path that aborts after the record
exists but before a live process, so no unit can be Working without a record
and no record can sit open because dispatch failed.

The record's on-disk shape carries a forensics-only timestamp, but readers get
a typed ``DispatchRecord`` projection with NO timestamp attribute (Andon A5):
a field absent from memory cannot be read, so no future implementer can build a
lease/timeout on dispatch time.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from grove.fleet import paths, runner
from grove.fleet.errors import FleetWorkerAndon
from grove.fleet.runner import DispatchRecord


# ── the record path is keyed identically to the terminal event ──────────────


def test_dispatch_path_keyed_like_event_path():
    dp = paths.dispatch_path("forge", "run-abc")
    ep = paths.event_path("forge", "run-abc")
    # Same key (run_id stem), sibling of events/ under the worker subtree.
    assert dp.stem == "run-abc" == ep.stem
    assert dp.parent.name == "dispatch"
    assert dp.parent.parent == ep.parent.parent == paths.worker_dir("forge")


# ── the projection (A5 wall): typed, no timestamp attribute ─────────────────


def test_dispatch_record_projection_has_no_timestamp_field():
    field_names = {f.name for f in dataclasses.fields(DispatchRecord)}
    # Structural pin — adding a timestamp field later breaks this equality, so
    # a timeout can never be built on a field that does not exist in memory.
    assert field_names == {"run_id", "unit_id", "worker_id"}
    assert not any(
        ("time" in n) or (n == "ts") or ("stamp" in n) for n in field_names
    )


def test_dispatch_record_instance_cannot_carry_a_timestamp():
    rec = DispatchRecord(run_id="r", unit_id="u", worker_id="w")
    # slots => no __dict__ => no reader can smuggle a ts onto an instance.
    assert not hasattr(rec, "__dict__")
    # frozen => the assignment is blocked. (frozen+slots raises TypeError from
    # the generated __setattr__ rather than FrozenInstanceError — a CPython
    # quirk; immutability holds either way, which is all the A5 wall needs.)
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError, TypeError)):
        rec.ts = "2026-07-22T00:00:00+00:00"  # type: ignore[attr-defined]
    assert not hasattr(rec, "ts")


# ── write mints the on-disk record; read projects it (dropping ts) ──────────


def test_write_then_read_roundtrips_and_read_drops_ts():
    runner.write_dispatch_record("forge", "rid1", "unit-38f")
    # On disk: the forensics ts is present.
    raw = json.loads(paths.dispatch_path("forge", "rid1").read_text(encoding="utf-8"))
    assert raw["run_id"] == "rid1"
    assert raw["unit_id"] == "unit-38f"
    assert raw["worker_id"] == "forge"
    assert "ts" in raw and raw["ts"]  # forensics-only, present on disk

    # Projected: a typed DispatchRecord, ts nowhere in reach.
    rec = runner.read_dispatch_record("forge", "rid1")
    assert isinstance(rec, DispatchRecord)
    assert rec.run_id == "rid1"
    assert rec.unit_id == "unit-38f"
    assert rec.worker_id == "forge"
    assert not hasattr(rec, "ts")


def test_read_missing_record_returns_none():
    assert runner.read_dispatch_record("forge", "never-dispatched") is None


def test_write_dispatch_record_none_unit_id_is_preserved():
    # A payload that predates the unit_id seam dispatches with unit_id=None.
    runner.write_dispatch_record("forge", "rid2", None)
    rec = runner.read_dispatch_record("forge", "rid2")
    assert rec.unit_id is None


# ── dispatch ordering: config-validate → mint record → inbox → spawn ────────

from types import SimpleNamespace  # noqa: E402

from grove.fleet import config  # noqa: E402

_DISPATCHED = "u-38f780a7"
_PAYLOAD = {"rows": [{"id": _DISPATCHED}], "unit_id": _DISPATCHED}


def _wc(**over):
    base = dict(
        id="forge",
        skill="skill.fleet.forge-jobsearch",
        enabled=True,
        limits={"wall_clock_secs": 900, "mem_mb": 512},
    )
    base.update(over)
    return config.WorkerConfig(**base)


def _fake_proc(pid=4242):
    return SimpleNamespace(pid=pid, poll=lambda: None)


def test_wall_clock_invalid_mints_no_record(monkeypatch):
    """Step 2 (config validation) is above step 3 (mint) deliberately — an
    invalid config never mints a genesis record."""
    called = {"spawn": False}
    monkeypatch.setattr(
        runner.KanbanRunner, "_spawn",
        lambda self, w, r, limits=None: called.__setitem__("spawn", True),
    )
    with pytest.raises(FleetWorkerAndon) as ei:
        runner.dispatch(_wc(limits={}), _PAYLOAD, run_id="rid")
    assert ei.value.check == "missing_wall_clock"
    assert called["spawn"] is False
    # No record was minted — the dispatch dir stays empty.
    assert list(paths.dispatch_dir("forge").glob("*.json")) == []


def test_genesis_record_exists_before_spawn(monkeypatch):
    seen = {}

    def _fake_spawn(self, worker_id, run_id, limits=None):
        # At spawn time the genesis record MUST already be on disk, keyed by rid.
        seen["record_at_spawn"] = paths.dispatch_path(worker_id, run_id).exists()
        return _fake_proc()

    monkeypatch.setattr(runner.KanbanRunner, "_spawn", _fake_spawn)
    runner.dispatch(_wc(), _PAYLOAD, run_id="rid")
    assert seen["record_at_spawn"] is True
    rec = runner.read_dispatch_record("forge", "rid")
    assert (rec.run_id, rec.unit_id, rec.worker_id) == ("rid", _DISPATCHED, "forge")


def test_single_dispatch_writes_exactly_one_record(monkeypatch):
    monkeypatch.setattr(
        runner.KanbanRunner, "_spawn", lambda self, w, r, limits=None: _fake_proc()
    )
    runner.dispatch(_wc(), _PAYLOAD, run_id="rid")
    assert [p.name for p in paths.dispatch_dir("forge").glob("*.json")] == ["rid.json"]


def test_record_write_failure_aborts_without_spawn(monkeypatch):
    called = {"spawn": False}
    monkeypatch.setattr(
        runner.KanbanRunner, "_spawn",
        lambda self, w, r, limits=None: called.__setitem__("spawn", True),
    )

    def _boom(worker_id, run_id, unit_id):
        raise FleetWorkerAndon(
            "disk full", worker_id=worker_id, check="dispatch_record_unwritable"
        )

    monkeypatch.setattr(runner, "write_dispatch_record", _boom)
    with pytest.raises(FleetWorkerAndon) as ei:
        runner.dispatch(_wc(), _PAYLOAD, run_id="rid")
    assert ei.value.check == "dispatch_record_unwritable"
    assert called["spawn"] is False
    # A unit never legally dispatched reaches no terminal state.
    assert not paths.event_path("forge", "rid").exists()


def test_inbox_failure_writes_receipt_with_identity_then_raises(monkeypatch):
    called = {"spawn": False}
    monkeypatch.setattr(
        runner.KanbanRunner, "_spawn",
        lambda self, w, r, limits=None: called.__setitem__("spawn", True),
    )
    # Wedge the inbox dir: a FILE where inbox/ should be makes mkdir raise OSError.
    wd = paths.worker_dir("forge")
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "inbox").write_text("x", encoding="utf-8")

    with pytest.raises(FleetWorkerAndon) as ei:
        runner.dispatch(_wc(), _PAYLOAD, run_id="rid")
    assert ei.value.check == "inbox_unwritable"
    assert called["spawn"] is False  # aborted before a live process
    # The genesis record is closed by a terminal receipt keyed by the same rid.
    ev = json.loads(paths.event_path("forge", "rid").read_text(encoding="utf-8"))
    assert ev["status"] == "failed"
    assert ev["check"] == "inbox_unwritable"
    assert ev["unit_id"] == _DISPATCHED  # carries the dispatched identity


def test_spawn_failure_writes_receipt_with_identity_then_raises(monkeypatch):
    def _boom_spawn(self, worker_id, run_id, limits=None):
        raise OSError("fork: cannot allocate memory")

    monkeypatch.setattr(runner.KanbanRunner, "_spawn", _boom_spawn)
    with pytest.raises(OSError):
        runner.dispatch(_wc(), _PAYLOAD, run_id="rid")
    ev = json.loads(paths.event_path("forge", "rid").read_text(encoding="utf-8"))
    assert ev["status"] == "failed"
    assert ev["check"] == "spawn_failed"
    assert ev["unit_id"] == _DISPATCHED


def test_redraft_mints_no_second_record_structural():
    """A redraft re-arms the emit tool INSIDE the same worker process, reusing
    the same run_id — one Popen, one dispatch. It must never mint a second
    genesis record, so worker_entry may not reach the record writer at all."""
    import inspect

    from grove.fleet import worker_entry

    src = inspect.getsource(worker_entry)
    assert "write_dispatch_record" not in src
    assert "dispatch_path" not in src

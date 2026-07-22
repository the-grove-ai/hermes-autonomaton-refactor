"""fleet-receipt-custody-v1 P2 C2b — the synthetic-receipt writer.

Three kill/crash owners (runner aborts, manager poll, boot sweep) terminate a
unit without the worker writing its own receipt. Each routes through ONE helper
that mints a terminal receipt carrying the dispatched unit_id, keyed by the same
run_id as the C1 genesis record — so every terminal outcome is unit-attributable
and the P3 retry counter can see it.

No-clobber: the receipt shares the event path with a worker-written one, so the
helper NEVER overwrites an existing receipt — a worker that wrote its own richer
record (with P1.2C identity) wins.
"""

from __future__ import annotations

import json

from grove.fleet import paths, runner
from grove.fleet.staging import write_synthetic_receipt


def _write_inbox(worker_id, run_id, payload):
    inbox = paths.inbox_path(worker_id, run_id)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(
        json.dumps({"worker_id": worker_id, "run_id": run_id, "payload": payload}),
        encoding="utf-8",
    )


def _read_event(worker_id, run_id):
    return json.loads(
        paths.event_path(worker_id, run_id).read_text(encoding="utf-8")
    )


# ── identity: unit_id from the C1 dispatch record, keyed by run_id ──────────


def test_receipt_carries_unit_id_from_dispatch_record():
    runner.write_dispatch_record("forge", "rid1", "unit-38f")
    out = write_synthetic_receipt(
        "forge", "rid1", check="wall_clock_exceeded", detail="killed at 900s"
    )
    assert out == paths.event_path("forge", "rid1")  # keyed by the same run_id
    ev = _read_event("forge", "rid1")
    assert ev["status"] == "failed"
    assert ev["check"] == "wall_clock_exceeded"
    assert ev["unit_id"] == "unit-38f"
    assert ev["run_id"] == "rid1"


# ── no-clobber: a worker-written receipt is never overwritten ────────────────


def test_no_clobber_when_a_receipt_already_exists():
    runner.write_dispatch_record("forge", "rid2", "unit-x")
    # A worker already wrote its own richer terminal event.
    real = {"worker_id": "forge", "run_id": "rid2", "status": "failed",
            "check": "no_package", "row_id": "unit-x", "detail": "the worker's own"}
    ep = paths.event_path("forge", "rid2")
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.write_text(json.dumps(real), encoding="utf-8")

    out = write_synthetic_receipt(
        "forge", "rid2", check="nonzero_exit", detail="synthetic must not win"
    )
    assert out is None  # skipped — the real receipt stands
    ev = _read_event("forge", "rid2")
    assert ev["detail"] == "the worker's own"  # untouched
    assert ev["check"] == "no_package"


# ── fallback: the inbox, only for a pre-C1 orphan (no dispatch record) ───────


def test_falls_back_to_inbox_when_no_dispatch_record():
    # No dispatch record (pre-C1 orphan); the inbox still carries the payload.
    _write_inbox("forge", "rid3", {"unit_id": "unit-inbox", "rows": [{"id": "unit-inbox"}]})
    assert runner.read_dispatch_record("forge", "rid3") is None
    write_synthetic_receipt("forge", "rid3", check="reaped_at_restart", detail="d")
    ev = _read_event("forge", "rid3")
    assert ev["unit_id"] == "unit-inbox"


# ── neither record nor inbox: receipt still written, unit_id null ────────────


def test_unit_id_null_when_no_record_and_no_inbox():
    write_synthetic_receipt("forge", "rid4", check="catastrophic_no_event", detail="d")
    ev = _read_event("forge", "rid4")
    assert ev["status"] == "failed"
    assert ev["check"] == "catastrophic_no_event"
    assert ev.get("unit_id") is None  # keyed by run, identity null — still countable


# ── manager poll wiring: kill/crash sites route through the ONE helper ───────

from types import SimpleNamespace  # noqa: E402

from grove.fleet import manager as manager_mod  # noqa: E402


def _classify(monkeypatch, wid, run_id, rc, event, killed, wall_clock_secs=900):
    andons = []
    monkeypatch.setattr(
        manager_mod, "surface_fleet_andon",
        lambda w, r, m, *, check=None, loop=None, **k: andons.append(
            {"wid": w, "run_id": r, "check": check}
        ),
    )
    handle = SimpleNamespace(
        run_id=run_id,
        wall_clock_secs=wall_clock_secs,
        event_path=paths.event_path(wid, run_id),
    )
    manager_mod.FleetManager._classify_terminal(
        SimpleNamespace(_loop=None), wid, handle, rc, event, killed
    )
    return andons


def test_classify_killed_writes_wall_clock_exceeded_receipt(monkeypatch):
    runner.write_dispatch_record("forge", "k1", "unit-k")
    andons = _classify(monkeypatch, "forge", "k1", rc=-9, event=None, killed=True)
    ev = _read_event("forge", "k1")
    assert ev["check"] == "wall_clock_exceeded"
    assert ev["unit_id"] == "unit-k"
    assert [a["check"] for a in andons] == ["wall_clock_exceeded"]  # Andon still fires


def test_classify_no_event_writes_catastrophic_receipt(monkeypatch):
    runner.write_dispatch_record("forge", "c1", "unit-c")
    andons = _classify(monkeypatch, "forge", "c1", rc=0, event=None, killed=False)
    ev = _read_event("forge", "c1")
    assert ev["check"] == "catastrophic_no_event"
    assert ev["unit_id"] == "unit-c"
    assert [a["check"] for a in andons] == ["catastrophic_no_event"]


def test_classify_nonzero_no_event_writes_nonzero_exit_receipt(monkeypatch):
    runner.write_dispatch_record("forge", "n1", "unit-n")
    andons = _classify(monkeypatch, "forge", "n1", rc=1, event=None, killed=False)
    ev = _read_event("forge", "n1")
    assert ev["check"] == "nonzero_exit"
    assert ev["unit_id"] == "unit-n"
    assert andons and andons[0]["check"] == "nonzero_exit"


def test_classify_nonzero_with_worker_event_writes_no_synthetic(monkeypatch):
    # The worker wrote its OWN failure receipt (carries P1.2C identity). The
    # manager Andons for visibility but must NOT overwrite the richer record.
    runner.write_dispatch_record("forge", "n2", "unit-n2")
    real = {"worker_id": "forge", "run_id": "n2", "status": "failed",
            "check": "oom_killed", "row_id": "unit-n2", "detail": "worker OOM"}
    ep = paths.event_path("forge", "n2")
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.write_text(json.dumps(real), encoding="utf-8")

    andons = _classify(monkeypatch, "forge", "n2", rc=1, event=real, killed=False)
    ev = _read_event("forge", "n2")
    assert ev["detail"] == "worker OOM"  # untouched — worker receipt stands
    assert ev["check"] == "oom_killed"
    assert andons  # Andon still fires

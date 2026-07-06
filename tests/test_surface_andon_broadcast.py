"""fleet-mcp-warm-unification-v1 P2(a) / LOCK-1 — the ``broadcast`` gate on
``surface_fleet_andon``.

``broadcast=False`` must suppress ONLY the operator-broadcast leg; the local log
floor AND the governed Kaizen ``andon_halt`` filing must still fire (a suppressed
Andon is muted-to-operator but never silent-in-logs and never unrecorded). The
default (``broadcast=True``) must be byte-identical to the prior behavior for every
existing caller.
"""

import agent.async_utils as au
import grove.kaizen_ledger as kl
import grove.notify as notify
from grove.fleet import observability


def _stub_legs(monkeypatch):
    """Intercept the operator-broadcast scheduler + the Kaizen filing so the test is
    hermetic. Returns (schedule_calls, ledger_record_calls)."""
    sched_calls = []
    monkeypatch.setattr(au, "safe_schedule_threadsafe", lambda *a, **k: sched_calls.append(a))
    # broadcast_to_operator is only *constructed* as the coro arg to the scheduler;
    # a plain object avoids an un-awaited-coroutine warning.
    monkeypatch.setattr(notify, "broadcast_to_operator", lambda *a, **k: object())

    record_calls = []

    class _StubLedger:
        def __init__(self, *a, **k):
            pass

        def record(self, *a, **k):
            record_calls.append((a, k))

    monkeypatch.setattr(kl, "KaizenLedger", _StubLedger)
    return sched_calls, record_calls


def test_broadcast_false_suppresses_only_operator_broadcast(monkeypatch, caplog):
    sched_calls, record_calls = _stub_legs(monkeypatch)
    sentinel_loop = object()

    with caplog.at_level("ERROR"):
        res = observability.surface_fleet_andon(
            "worker-x", "run-1", "cold MCP", check="resolver_cold_mcp",
            loop=sentinel_loop, broadcast=False,
        )

    # Operator broadcast SUPPRESSED (the whole point).
    assert sched_calls == []
    assert res["broadcast_scheduled"] is False
    # Local log floor STILL fired — muted-to-operator, not silent-in-logs.
    assert any("[fleet.observe]" in rec.message for rec in caplog.records)
    # Kaizen andon_halt filing STILL fired — the halt is always recorded.
    assert len(record_calls) == 1
    assert record_calls[0][0][0] == "andon_halt"


def test_broadcast_default_true_schedules_operator_broadcast(monkeypatch):
    sched_calls, record_calls = _stub_legs(monkeypatch)
    sentinel_loop = object()

    # broadcast omitted -> default True -> existing behavior unchanged.
    res = observability.surface_fleet_andon(
        "worker-x", "run-1", "cold MCP", check="resolver_cold_mcp", loop=sentinel_loop,
    )

    assert len(sched_calls) == 1                  # operator broadcast scheduled
    assert res["broadcast_scheduled"] is True
    assert len(record_calls) == 1                 # Kaizen filing also fires

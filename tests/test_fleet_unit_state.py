"""fleet-receipt-custody-v1 P3a — the unit-state derivation + policy surface.

One pure function computes unit state from durable records. Five states, no
timestamp read anywhere (the derivation takes disposition MEMBERSHIP as a bool,
never a timestamped ledger, so it structurally cannot read one). Policy is
config. This phase binds nothing — P4 binds the reader, P3b fires the breaker.
"""

from __future__ import annotations

import inspect

import pytest

from grove.fleet import unit_state
from grove.fleet.unit_state import (
    DEAD_LETTERED,
    DONE,
    NEEDS_YOU,
    WAITING,
    WORKING,
    FailurePolicy,
    derive_unit_state,
    load_failure_policy,
)


# ── the policy surface loads exactly as specced ─────────────────────────────


def test_policy_loads_repo_config():
    p = load_failure_policy()
    assert p.default_cap == 3
    assert p.per_producer == {}
    assert p.default_disposition == "retry"
    assert p.disposition("no_package") == "retry"
    assert p.disposition("approval_deferred") == "dead_letter"
    assert p.disposition("declarative_config_missing") == "pause_producer"
    assert p.disposition("reaped_at_restart") == "ignore"
    # unmapped → default retry (the P3b proposal is out of scope here)
    assert p.disposition("some_brand_new_class") == "retry"
    assert p.disposition(None) == "retry"


def test_cap_for_prefers_per_producer_override():
    p = FailurePolicy(
        default_cap=3, per_producer={"slowpoke": 7},
        default_disposition="retry", failure_policy={},
    )
    assert p.cap_for("anyone") == 3
    assert p.cap_for("slowpoke") == 7  # override wins over the default


# ── shared fixture builder ──────────────────────────────────────────────────

_POL = FailurePolicy(
    default_cap=3,
    per_producer={},
    default_disposition="retry",
    failure_policy={
        "reaped_at_restart": "ignore",
        "no_package": "retry",
        "approval_deferred": "dead_letter",
        "declarative_config_missing": "pause_producer",
    },
)


def _fail(check):
    return {"status": "failed", "check": check}


def _success():
    return {"status": "success", "check": None}


def _derive(**over):
    kw = dict(
        unit_runs=[], dispatched=set(), received=set(), forgiven=set(),
        events={}, disposed=False, producer="forge", policy=_POL,
    )
    kw.update(over)
    return derive_unit_state(**kw)


# ── the five states, from constructed fixtures ──────────────────────────────


def test_working_a_dispatch_without_a_receipt():
    assert _derive(unit_runs=["r1"], dispatched={"r1"}, received=set()) == WORKING


def test_done_a_terminal_disposition_exists():
    # Done wins over any receipt content (precedence 2).
    assert _derive(
        unit_runs=["r1"], dispatched={"r1"}, received={"r1"},
        events={"r1": _success()}, disposed=True,
    ) == DONE


def test_needs_you_success_receipt_without_disposition():
    assert _derive(
        unit_runs=["r1"], dispatched={"r1"}, received={"r1"},
        events={"r1": _success()}, disposed=False,
    ) == NEEDS_YOU


def test_dead_lettered_when_retry_failures_reach_cap():
    runs = ["r1", "r2", "r3"]
    assert _derive(
        unit_runs=runs, dispatched=set(runs), received=set(runs),
        events={r: _fail("no_package") for r in runs},
    ) == DEAD_LETTERED


def test_below_cap_is_waiting():
    runs = ["r1", "r2"]  # 2 retry failures, cap 3
    assert _derive(
        unit_runs=runs, dispatched=set(runs), received=set(runs),
        events={r: _fail("no_package") for r in runs},
    ) == WAITING


def test_waiting_when_nothing_dispatched():
    assert _derive() == WAITING


# ── counting rules ──────────────────────────────────────────────────────────


def test_dead_letter_class_fires_on_first_occurrence_regardless_of_cap():
    # ONE approval_deferred (dead_letter) — dead-lettered immediately, not at cap.
    assert _derive(
        unit_runs=["r1"], dispatched={"r1"}, received={"r1"},
        events={"r1": _fail("approval_deferred")},
    ) == DEAD_LETTERED


def test_pause_producer_failures_do_not_count_and_leave_waiting():
    runs = ["r1", "r2", "r3", "r4", "r5"]  # 5 pause_producer failures
    assert _derive(
        unit_runs=runs, dispatched=set(runs), received=set(runs),
        events={r: _fail("declarative_config_missing") for r in runs},
    ) == WAITING


def test_ignore_class_does_not_count():
    runs = ["r1", "r2", "r3", "r4", "r5"]  # 5 ignore failures
    assert _derive(
        unit_runs=runs, dispatched=set(runs), received=set(runs),
        events={r: _fail("reaped_at_restart") for r in runs},
    ) == WAITING


def test_reset_marker_removes_its_run_from_the_count():
    runs = ["r1", "r2", "r3"]  # 3 retry failures, but r3 forgiven -> 2 < cap
    assert _derive(
        unit_runs=runs, dispatched=set(runs), received=set(runs),
        forgiven={"r3"},
        events={r: _fail("no_package") for r in runs},
    ) == WAITING


def test_unmapped_class_counts_as_retry():
    runs = ["r1", "r2", "r3"]  # 3 unmapped -> default retry -> reaches cap
    assert _derive(
        unit_runs=runs, dispatched=set(runs), received=set(runs),
        events={r: _fail("a_class_not_in_the_map") for r in runs},
    ) == DEAD_LETTERED


def test_per_producer_cap_override_takes_precedence():
    pol = FailurePolicy(
        default_cap=3, per_producer={"patient": 5},
        default_disposition="retry", failure_policy={"no_package": "retry"},
    )
    runs = ["r1", "r2", "r3"]  # 3 retry failures
    ev = {r: _fail("no_package") for r in runs}
    base = dict(unit_runs=runs, dispatched=set(runs), received=set(runs), events=ev)
    # default cap 3 -> dead-lettered; producer 'patient' cap 5 -> still Waiting
    assert derive_unit_state(**base, forgiven=set(), disposed=False,
                             producer="forge", policy=pol) == DEAD_LETTERED
    assert derive_unit_state(**base, forgiven=set(), disposed=False,
                             producer="patient", policy=pol) == WAITING


# ── structural pins ─────────────────────────────────────────────────────────


class _BoobyTrap(dict):
    """A receipt map that screams if Working touches event CONTENTS — proving
    the Working determination is filename-only, zero json.loads."""

    def __getitem__(self, k):
        raise AssertionError("Working read event contents — parsing leaked in")

    def get(self, *a, **k):
        raise AssertionError("Working read event contents — parsing leaked in")


def test_working_derives_with_zero_event_reads_structurally():
    # A booby-trapped events map: if Working reaches into it, the test fails.
    assert _derive(
        unit_runs=["r1"], dispatched={"r1"}, received=set(),
        events=_BoobyTrap(),
    ) == WORKING


def test_derivation_reads_no_timestamp_structurally():
    import ast
    import textwrap

    # Scan EXECUTABLE code only — strip the docstring (which legitimately says
    # "never a timestamp") so the pin fires on a real read, not on prose.
    fn = ast.parse(textwrap.dedent(inspect.getsource(derive_unit_state))).body[0]
    if ast.get_docstring(fn):
        fn.body = fn.body[1:]
    code = ast.unparse(fn)
    for tok in ("timestamp", "updated_at", "created_at", "mtime", "_ts"):
        assert tok not in code, f"derivation reads a time token {tok!r} — lease leak"
    # And the disposition input is MEMBERSHIP (a bool), never a timestamped ledger.
    sig = inspect.signature(derive_unit_state)
    assert sig.parameters["disposed"].annotation in (bool, "bool")

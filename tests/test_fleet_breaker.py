"""fleet-receipt-custody-v1 P3b — the auto-pause breaker + the unmapped card.

pause_producer classes do not count toward the retry cap by design; without a
breaker they would retry forever. When a receipt whose check maps to
pause_producer lands, the fleet manager pauses that producer at classification
time (N=1), surfaces it with an unpause action, and the dispatch loop skips it.
An unmapped class defaults to retry and raises exactly one classify-me card.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from grove import flywheel_cli
from grove.eval.producer_pauses import read_producer_pauses, set_producer_pause
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_PRODUCER_AUTO_PAUSED,
    PROPOSAL_TYPE_UNMAPPED_FAILURE_CLASS,
    RoutingProposal,
    read_all,
)
from grove.fleet import manager as manager_mod
from grove.fleet.manager import apply_failure_policy


def _fail(check):
    return {"status": "failed", "check": check, "worker_id": "forge"}


# ── the breaker: pause_producer receipts ────────────────────────────────────


def test_pause_producer_receipt_pauses_that_producer_first_occurrence():
    apply_failure_policy("forge", "r1", _fail("declarative_config_missing"))
    assert read_producer_pauses() == frozenset({"forge"})


def test_pause_does_not_touch_any_other_producer():
    apply_failure_policy("forge", "r1", _fail("worker_not_registered"))
    paused = read_producer_pauses()
    assert "forge" in paused
    assert "drafter" not in paused
    assert len(paused) == 1


@pytest.mark.parametrize("check", ["no_package", "approval_deferred", "reaped_at_restart"])
def test_retry_dead_letter_ignore_never_pause(check):
    # retry / dead_letter / ignore act on nothing in the breaker.
    apply_failure_policy("forge", "r1", _fail(check))
    assert read_producer_pauses() == frozenset()
    assert [p for p in read_all() if p.type == PROPOSAL_TYPE_PRODUCER_AUTO_PAUSED] == []


def test_a_success_receipt_never_pauses():
    apply_failure_policy("forge", "r1", {"status": "success", "check": None})
    assert read_producer_pauses() == frozenset()


# ── the surface: an unpause card ────────────────────────────────────────────


def test_auto_pause_raises_a_card_carrying_an_unpause_action():
    apply_failure_policy("forge", "r9", _fail("dead_pinned_slug"))
    cards = [p for p in read_all() if p.type == PROPOSAL_TYPE_PRODUCER_AUTO_PAUSED]
    assert len(cards) == 1
    card = cards[0]
    assert card.payload["producer"] == "forge"
    assert card.detail["check"] == "dead_pinned_slug"
    assert card.detail["run_id"] == "r9"
    # the card renders an unpause action, naming producer + check + run
    summary = flywheel_cli._summary_producer_auto_paused(card)
    assert "unpause" in summary.lower()
    assert "forge" in summary and "dead_pinned_slug" in summary
    assert flywheel_cli._producer_auto_paused_to_diff(card)["action"] == "unpause"


def test_auto_pause_is_idempotent_one_card_per_producer():
    apply_failure_policy("forge", "r1", _fail("no_declared_sink"))
    apply_failure_policy("forge", "r2", _fail("no_declared_sink"))  # already paused
    cards = [p for p in read_all() if p.type == PROPOSAL_TYPE_PRODUCER_AUTO_PAUSED]
    assert len(cards) == 1  # N=1 — no duplicate card


# ── unpause is operator-only and the sole unpause path ──────────────────────


def test_approve_unpauses_and_no_auto_unpause_exists():
    set_producer_pause("forge", True, reason="auto")
    assert "forge" in read_producer_pauses()
    proposal = RoutingProposal(
        proposal_id="pid", type=PROPOSAL_TYPE_PRODUCER_AUTO_PAUSED,
        payload={"producer": "forge"}, evidence=("forge",), eval_hash="",
        created_at="t", detail={"check": "x", "run_id": "r"},
    )
    flywheel_cli._approve_producer_auto_paused(proposal)
    assert "forge" not in read_producer_pauses()  # unpaused

    # STRUCTURAL: every producer-UNPAUSE site (set_producer_pause(..., False))
    # in grove/ lives in the approve handler function — nothing auto-unpauses.
    import ast
    import pathlib

    root = pathlib.Path(flywheel_cli.__file__).resolve().parents[0]  # grove/ only
    enclosing = set()
    for py in root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        funcs = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "set_producer_pause"):
                continue
            false_arg = any(
                isinstance(a, ast.Constant) and a.value is False for a in node.args
            )
            if not false_arg:
                continue
            owner = min(
                (f for f in funcs if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)),
                key=lambda f: node.lineno - f.lineno, default=None,
            )
            enclosing.add(owner.name if owner else "<module>")
    assert enclosing <= {"_approve_producer_auto_paused"}, (
        f"a producer-unpause exists outside the operator approve handler: {enclosing}"
    )


# ── the unmapped card: one per class, dismiss-only ──────────────────────────


def test_unmapped_check_raises_exactly_one_card_across_receipts():
    apply_failure_policy("forge", "r1", _fail("a_totally_new_class"))
    apply_failure_policy("drafter", "r2", _fail("a_totally_new_class"))  # same class
    cards = [p for p in read_all() if p.type == PROPOSAL_TYPE_UNMAPPED_FAILURE_CLASS]
    assert len(cards) == 1  # deduped by class, not per receipt
    assert cards[0].payload["check"] == "a_totally_new_class"
    assert read_producer_pauses() == frozenset()  # unmapped never pauses


def test_unmapped_card_states_approve_does_not_write_the_mapping():
    proposal = RoutingProposal(
        proposal_id="pid", type=PROPOSAL_TYPE_UNMAPPED_FAILURE_CLASS,
        payload={"check": "new_x"}, evidence=("new_x",), eval_hash="", created_at="t",
    )
    summary = flywheel_cli._summary_unmapped_failure_class(proposal)
    assert "config/fleet_failure_policy.yaml" in summary
    assert "does not write" in summary.lower() or "dismiss" in summary.lower()
    # approve returns dismiss-only — no config mutation
    _key, applied = flywheel_cli._approve_unmapped_failure_class(proposal)
    assert applied["dismissed"] is True


# ── does not bind the derivation (P4) ───────────────────────────────────────


def test_breaker_does_not_bind_derive_unit_state():
    src = inspect.getsource(manager_mod)
    assert "derive_unit_state" not in src, "P3b must not bind the derivation — that is P4"


# ── the pause consult: the dispatch loop skips a paused producer ────────────


def test_dispatch_loop_skips_a_paused_producer(monkeypatch):
    set_producer_pause("forge", True, reason="auto")
    dispatched = []
    wc = SimpleNamespace(enabled=True)
    monkeypatch.setattr(manager_mod, "load_fleet_workers", lambda *a, **k: {"forge": wc})
    # override_health / fleet_workers_override_path are imported inside the method.
    monkeypatch.setattr("grove.fleet.config.override_health", lambda p: None)
    monkeypatch.setattr(
        "grove.fleet.config.fleet_workers_override_path", lambda: None
    )
    self = SimpleNamespace(
        _override_path=None, _override_fail_reason=None, _workers_path=None,
        _running={}, _loop=None,
        _maybe_dispatch_one=lambda wid, cfg, now: dispatched.append(wid),
    )
    manager_mod.FleetManager._maybe_dispatch(self, now=SimpleNamespace())
    assert dispatched == []  # paused producer never dispatched


# ── flapping: re-pause after an operator unpause must NOT go silent ──────────


def test_re_pause_after_unpause_raises_a_fresh_card():
    """A producer paused, unpaused by the operator, then paused again must
    raise a SECOND card — not dedupe against the disposed first one. append
    dedups queue-only and cli_approve dequeues, so the re-pause re-queues; a
    flapping producer stops loudly, which is the failure this phase closes."""
    apply_failure_policy("forge", "r1", _fail("no_declared_sink"))
    c1 = [p for p in read_all() if p.type == PROPOSAL_TYPE_PRODUCER_AUTO_PAUSED]
    assert len(c1) == 1

    # Operator approves: unpause + dequeue + kaizen_disposition (all real).
    flywheel_cli.cli_approve(c1[0].proposal_id)
    assert "forge" not in read_producer_pauses()

    # It flaps: the underlying fault recurs and pauses it again.
    apply_failure_policy("forge", "r2", _fail("no_declared_sink"))
    c2 = [p for p in read_all() if p.type == PROPOSAL_TYPE_PRODUCER_AUTO_PAUSED]
    assert len(c2) == 1  # re-queued despite the same pid + a prior disposition
    assert read_producer_pauses() == frozenset({"forge"})

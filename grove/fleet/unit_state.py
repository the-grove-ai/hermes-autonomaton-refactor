"""fleet-receipt-custody-v1 P3a — the unit-state derivation + policy surface.

One pure function, :func:`derive_unit_state`, computes a fleet unit's state from
durable records alone. Five states, and **no timestamp is read anywhere**: the
function takes disposition MEMBERSHIP as a bool, never a timestamped ledger, so
it structurally cannot read one (that is the lease sprint, kept out).

This phase COMPUTES; it binds nothing. No reader is wired (P4), no breaker fires
and no proposal is raised (P3b), no reset record is written (P5). Producer pause
is NOT a unit state — a unit whose producer is paused is mathematically Waiting;
that join happens at presentation, never here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, Any, Dict, Mapping, Optional, Sequence

# The five states. Working, Waiting and Done are status (rendered as state);
# Needs you and Dead-lettered are queues. Order below is the derivation
# precedence — the spec.
WORKING = "Working"
DONE = "Done"
NEEDS_YOU = "Needs you"
DEAD_LETTERED = "Dead-lettered"
WAITING = "Waiting"


@dataclass(frozen=True)
class FailurePolicy:
    """The loaded ``config/fleet_failure_policy.yaml`` decision surface.

    ``failure_policy`` maps a receipt ``check`` to one of four dispositions
    (ignore / retry / dead_letter / pause_producer); an unmapped class falls to
    ``default_disposition``. ``per_producer`` overrides ``default_cap`` per
    producer name.
    """

    default_cap: int
    per_producer: Dict[str, int]
    default_disposition: str
    failure_policy: Dict[str, str]

    def disposition(self, check: Optional[str]) -> str:
        """The disposition for a receipt ``check`` — mapped, or the default."""
        if check is None:
            return self.default_disposition
        return self.failure_policy.get(check, self.default_disposition)

    def cap_for(self, producer: str) -> int:
        """The retry cap for *producer* — its override, else the default."""
        return self.per_producer.get(producer, self.default_cap)


def default_failure_policy_path() -> Path:
    """The repo-default policy: ``<repo>/config/fleet_failure_policy.yaml``."""
    return Path(__file__).resolve().parents[2] / "config" / "fleet_failure_policy.yaml"


def load_failure_policy(path: Optional[Path] = None) -> FailurePolicy:
    """Parse the failure-policy config into a :class:`FailurePolicy`."""
    import yaml

    target = Path(path) if path is not None else default_failure_policy_path()
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    cap = raw.get("retry_cap") or {}
    return FailurePolicy(
        default_cap=int(cap.get("default", 3)),
        per_producer=dict(cap.get("per_producer") or {}),
        default_disposition=str(raw.get("default", "retry")),
        failure_policy=dict(raw.get("failure_policy") or {}),
    )


def derive_unit_state(
    *,
    unit_runs: Sequence[str],
    dispatched: AbstractSet[str],
    received: AbstractSet[str],
    forgiven: AbstractSet[str],
    events: Mapping[str, Dict[str, Any]],
    disposed: bool,
    producer: str,
    policy: FailurePolicy,
) -> str:
    """Derive one unit's state from durable records. Precedence IS the spec.

    ``unit_runs`` are this unit's run_ids. ``dispatched`` / ``received`` /
    ``forgiven`` are the run_id stems from ``scandir`` of ``dispatch/`` /
    ``events/`` / ``reset/`` (filenames — no parsing). ``events`` maps a
    received run_id to its receipt dict. ``disposed`` is ledger MEMBERSHIP — a
    terminal disposition (applied/rejected) exists for this unit — a bool, never
    a timestamp.
    """
    runs = set(unit_runs)

    # 1. Working — a dispatch with no matching receipt. Set subtraction on
    #    filenames ONLY; the event map is never touched on this path.
    if any((r in dispatched) and (r not in received) for r in runs):
        return WORKING

    # 2. Done — a terminal disposition exists (membership, no timestamp).
    if disposed:
        return DONE

    terminal = [r for r in runs if r in received]

    # 3. Needs you — a success receipt awaiting operator disposition (there is
    #    none, or step 2 would have returned).
    if any(events[r].get("status") == "success" for r in terminal):
        return NEEDS_YOU

    # 4. Dead-lettered — count unforgiven failure receipts, classified by policy.
    #    dead_letter is immediate; retry accrues to the cap; ignore and
    #    pause_producer do not count (pause leaves the unit Waiting, the breaker
    #    is P3b).
    cap = policy.cap_for(producer)
    retry_count = 0
    for r in terminal:
        if r in forgiven:
            continue
        ev = events[r]
        if ev.get("status") != "failed":
            continue
        disp = policy.disposition(ev.get("check"))
        if disp == "dead_letter":
            return DEAD_LETTERED
        if disp == "retry":
            retry_count += 1
    if retry_count >= cap:
        return DEAD_LETTERED

    # 5. Waiting — everything else.
    return WAITING

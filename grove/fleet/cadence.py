"""Cadence-due and quiet-hours evaluation (Phase 3).

``cadence`` is a 5-field cron expression read as the MINIMUM spacing between
spawns: a worker is due when the cron schedule's next fire after its last
dispatch has passed. ``quiet_hours`` is an operator-LOCAL window during which a
worker never spawns; a window that wraps midnight (start > end) is handled.

Both are pure functions of (config, clock) so the manager and the tests share
one implementation.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timezone
from typing import Any, Dict, Optional


def cadence_due(
    cadence: Optional[str],
    last_dispatch: Optional[datetime],
    now: Optional[datetime] = None,
) -> bool:
    """True if the worker may spawn now.

    Never dispatched -> due immediately. Otherwise the cron schedule's next fire
    strictly after ``last_dispatch`` must be <= ``now``. A missing cadence means
    "no minimum spacing" -> always due. An invalid cron expression raises
    (croniter) — the manager surfaces it as an Andon rather than silently
    treating a mistyped schedule as never/always due.
    """
    if not cadence:
        return True
    now = now or datetime.now(timezone.utc)
    if last_dispatch is None:
        return True
    from croniter import croniter

    nxt = croniter(cadence, last_dispatch).get_next(datetime)
    return nxt <= now


def _parse_hm(value: Any) -> Optional[dtime]:
    if not isinstance(value, str) or ":" not in value:
        return None
    hh, mm = value.split(":", 1)
    return dtime(int(hh), int(mm))


def in_quiet_hours(
    quiet_hours: Optional[Dict[str, Any]], now: Optional[datetime] = None
) -> bool:
    """True if ``now`` (operator-local) falls within [start, end).

    Absent/partial quiet_hours -> never quiet. A window with start > end wraps
    past midnight (e.g. 22:00-07:00).
    """
    if not quiet_hours:
        return False
    start = _parse_hm(quiet_hours.get("start"))
    end = _parse_hm(quiet_hours.get("end"))
    if start is None or end is None:
        return False
    now = now or datetime.now().astimezone()
    t = now.timetz().replace(tzinfo=None) if now.tzinfo else now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # wraps midnight

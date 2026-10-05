"""disposition-gate-v1 · shared disposition vocabulary + suppression gate.

One place, consumed by the three memory detectors AND the AST conformance
guard (tests/grove/test_disposition_gate_conformance.py):

  R-24  ONE definition of the status vocabulary. NON_TERMINAL is the liveness
        of an in-flight proposal; TERMINAL is the operator's disposition. The
        vocabulary is code, never operator config.

  R-16  A terminal disposition binds the producer against re-emitting the
        subject. Permanent by default; a per-action duration (days) is
        available via ``flywheel.config.yaml`` — a missing config never
        silently disables suppression.

  R-20  The duration anchors on the disposition timestamp ONLY
        (:data:`DISPOSED_AT_FIELD`, stamped at the flip), never on the
        record's staging ``timestamp`` (an emission field). A background retry
        must not slide the lock.

This module imports nothing from the detectors — a leaf, so the detectors and
the guard can import it with no circular import or layering inversion (E1).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Union

# ── R-24 · the vocabulary (code, not config) ──────────────────────────────
# NON_TERMINAL: an in-flight proposal — liveness. A guard reading ONLY these
# goes blind the instant the operator disposes of the subject.
# TERMINAL: the operator's disposition. Binding at least one of these is the
# wall the AST guard pins.
NON_TERMINAL_STATUSES = frozenset({"pending", "processing"})
TERMINAL_STATUSES = frozenset({"approved", "rejected", "dismissed"})

# ── R-20 · the disposition-timestamp field ────────────────────────────────
# Stamped at the moment status flips to a terminal value (grove/memory/
# digest.py, grove/api/actions.py). DISTINCT from the record's ``timestamp``
# (staging/emission time). Duration suppression reads THIS, never that.
DISPOSED_AT_FIELD = "disposed_at"

# permanent-suppression sentinel (the default).
PERMANENT = "permanent"

Policy = Union[str, timedelta]  # PERMANENT or a duration


# ── policy resolution (flywheel.config.yaml · fail-loud default) ───────────
def _default_config_path() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "flywheel.config.yaml"


def resolve_suppression_policy(
    action: str, *, config_path: Optional[Path] = None
) -> Policy:
    """Suppression policy for ``action`` from ``flywheel.config.yaml``.

    Resolution follows the fail-loud contract: an absent file,
    absent ``disposition_gate`` block, or absent per-action key → ``PERMANENT``
    (a missing config never disables suppression). A present value must be
    the literal ``"permanent"`` or an int ``>= 1`` (days); anything else
    raises LOUD naming the key and the constraint.
    """
    if config_path is None:
        config_path = _default_config_path()
    if not config_path.exists():
        return PERMANENT

    import yaml

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if raw is None:
        return PERMANENT
    if not isinstance(raw, dict):
        raise ValueError(
            f"{config_path} must be a YAML mapping, got {type(raw).__name__}."
        )
    block = raw.get("disposition_gate")
    if block is None:
        return PERMANENT
    if not isinstance(block, dict):
        raise ValueError(
            f"{config_path} disposition_gate must be a mapping, got "
            f"{type(block).__name__}."
        )
    if action not in block:
        return PERMANENT
    value = block[action]
    if value == PERMANENT:
        return PERMANENT
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"flywheel.config.yaml disposition_gate.{action} must be "
            f"'permanent' or an integer number of days, got {value!r} "
            f"({type(value).__name__})."
        )
    if value < 1:
        raise ValueError(
            f"flywheel.config.yaml disposition_gate.{action} must be >= 1 "
            f"days, got {value}."
        )
    return timedelta(days=value)


def _parse_iso(ts: object) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _still_binding(rec: Dict, policy: Policy, now: datetime) -> bool:
    """Whether a terminal-disposition record still suppresses. Permanent →
    always. Duration → the disposition (R-20: :data:`DISPOSED_AT_FIELD`, never
    the staging ``timestamp``) is within the window. A terminal record with no
    disposition timestamp cannot be aged out, so it stays bound (safe)."""
    if policy == PERMANENT:
        return True
    disposed = _parse_iso(rec.get(DISPOSED_AT_FIELD))
    if disposed is None:
        return True
    return (now - disposed) < policy


def _iter_action_records(records: Iterable[Dict], action: str):
    for rec in records:
        if not isinstance(rec, dict):
            continue
        proposal = rec.get("proposal")
        if not isinstance(proposal, dict) or proposal.get("action") != action:
            continue
        target_id = proposal.get("target_id")
        if not target_id:
            continue
        yield rec, target_id


def disposed_target_ids(
    records: Iterable[Dict], action: str, *,
    now: Optional[datetime] = None, config_path: Optional[Path] = None,
) -> Set[str]:
    """Target ids for ``action`` carrying a TERMINAL disposition that still
    binds under the policy. Terminal only — the disposition gate (R-23's
    sibling for supersede)."""
    records = list(records)
    policy = resolve_suppression_policy(action, config_path=config_path)
    now = now or datetime.now(timezone.utc)
    out: Set[str] = set()
    for rec, target_id in _iter_action_records(records, action):
        if rec.get("status") in TERMINAL_STATUSES and _still_binding(rec, policy, now):
            out.add(target_id)
    return out


def suppressed_target_ids(
    records: Iterable[Dict], action: str, *,
    now: Optional[datetime] = None, config_path: Optional[Path] = None,
) -> Set[str]:
    """Target ids for ``action`` whose re-emission is suppressed: an in-flight
    proposal (liveness — NON_TERMINAL) OR a terminal disposition still binding
    under policy. One set; the caller skips them all. This is the widened
    guard for the graduate/deprecate detectors — it consults TERMINAL, so a
    disposed subject no longer re-emits on the next sweep."""
    records = list(records)
    policy = resolve_suppression_policy(action, config_path=config_path)
    now = now or datetime.now(timezone.utc)
    out: Set[str] = set()
    for rec, target_id in _iter_action_records(records, action):
        status = rec.get("status")
        if status in NON_TERMINAL_STATUSES:
            out.add(target_id)
        elif status in TERMINAL_STATUSES and _still_binding(rec, policy, now):
            out.add(target_id)
    return out


# memory-extraction-retry-v1 — a RELEASE marker, not a disposition. The detector
# writes a ``processing`` lock before its model call; when the reply cannot be
# parsed it now appends a ``failed`` record, which releases that lock so the
# session can be mined again on a later sweep. Deliberately in NEITHER status
# set above: it is not in-flight and it is not an operator decision.
EXTRACTION_FAILED_STATUS = "failed"
DEFAULT_MAX_EXTRACTION_ATTEMPTS = 3


def session_processed(
    records: Iterable[Dict],
    session_id: str,
    *,
    max_attempts: int = DEFAULT_MAX_EXTRACTION_ATTEMPTS,
) -> bool:
    """detector.py idempotency, widened (R-13 note: SESSION-keyed). A session
    with a staged proposal OR a terminal disposition has been processed and
    must not be re-mined. Consulting TERMINAL closes the re-mining of a session
    whose proposals were all disposed (which the old pending/processing-only
    check let slip). Permanent by construction: a transcript, once mined, is
    never re-mined.

    memory-extraction-retry-v1 — a bare ``processing`` lock still means
    processed (a valid extraction that found nothing leaves exactly that), BUT
    a lock RELEASED by a later ``failed`` record does not: the model reply was
    unusable, nothing was mined, and the session stays eligible until
    ``max_attempts`` failures — then it is given up on, loudly, by the
    detector. Without the release, one malformed reply lost the session
    forever."""
    locks = 0
    failures = 0
    for rec in records:
        if not isinstance(rec, dict) or rec.get("session_id") != session_id:
            continue
        status = rec.get("status")
        if status == "processing":
            locks += 1
        elif status == EXTRACTION_FAILED_STATUS:
            failures += 1
        elif status in NON_TERMINAL_STATUSES or status in TERMINAL_STATUSES:
            return True  # a staged proposal or an operator disposition
    if failures >= max_attempts:
        return True  # gave up — never an unbounded retry loop
    return locks > failures

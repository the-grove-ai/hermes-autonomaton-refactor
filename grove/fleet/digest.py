"""Fleet unattended-publish digest — the operator-facing READER of the durable
``FleetPublishedUnattended`` event feed (unattended-publish-legibility-v1 I1).

Telemetry is the lifeblood of the self-evolving loop: the ``FleetPublishedUnattended``
log is the feed the Flywheel reads, and this module only READS it. It never drops,
gates, or writes back into that log — the per-item shown-state lives in a
node-local sidecar (the self-reference bar). ONE deduplicated INFO line per fleet
tick replaces the per-publish ping: report-on-change over the window (a new item →
``published``; a genuine re-publish → ``updated``; an unchanged ``exists`` no-op →
suppressed).

Structure (mesh-primitive invariant): the windowed-dedup CORE
(:func:`classify_window`) carries NO fleet/forge/publish knowledge — it is a
generic first-seen + change-flag primitive over ``(key, changed)`` pairs.
Fleet-ness lives ONLY in the source adapter (:func:`_read_new_unattended`) and the
line template (:func:`_compose_line`).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

_SCHEMA = 1
_EVENT_TYPE = "FleetPublishedUnattended"


# ── generic windowed-dedup CORE (no domain knowledge) ────────────────────────
def classify_window(
    items: Iterable[Tuple[str, bool]], seen: set
) -> Tuple[List[str], List[str], set]:
    """Windowed first-seen + change-flag dedup — the mesh primitive.

    ``items`` is an ordered iterable of ``(key, changed)`` pairs observed this
    window (a key may repeat — the flags OR together). ``seen`` is the set of keys
    reported in a prior window. Returns ``(new_keys, changed_keys, next_seen)``:

      * key not in ``seen``             → NEW       (first sighting; reported once)
      * key in ``seen`` and changed     → CHANGED   (a real change this window)
      * key in ``seen`` and not changed → suppressed (no line)

    ``next_seen`` is ``seen`` plus every key observed this window. Pure — no I/O,
    no domain branch; the caller decides what a key and the ``changed`` flag mean.
    """
    order: List[str] = []
    changed_flag: Dict[str, bool] = {}
    for key, changed in items:
        if key not in changed_flag:
            order.append(key)
            changed_flag[key] = False
        changed_flag[key] = changed_flag[key] or bool(changed)
    new_keys: List[str] = []
    changed_keys: List[str] = []
    next_seen = set(seen)
    for key in order:
        if key not in seen:
            new_keys.append(key)
        elif changed_flag[key]:
            changed_keys.append(key)
        next_seen.add(key)
    return new_keys, changed_keys, next_seen


# ── fleet source adapter (durable log, READ-ONLY) ────────────────────────────
def _memory_log_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "memory_records.jsonl"


def _read_new_unattended(processed_count: int) -> Tuple[List[dict], int]:
    """Read ``FleetPublishedUnattended`` events from the durable memory log,
    returning ``(new_events, total_count)`` where ``new_events`` are those past the
    ``processed_count`` count-watermark.

    READ-ONLY direct parse — deliberately NOT via ``MemoryStore`` (whose
    ``__init__`` folds the whole index every construction) and NEVER a write to the
    log. A malformed line is skipped (the append-only log is replayed, not
    rejected — mirrors ``store.read_events``); a torn trailing line is simply not
    counted and re-read next window (no drop). The log is append-only, so the first
    ``processed_count`` events are stable and processing from there is exactly-once.
    """
    path = _memory_log_path()
    if not path.is_file():
        return [], 0
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"unreadable memory log {path}: {exc}") from exc
    total = 0
    new: List[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("__type__") != _EVENT_TYPE:
            continue
        total += 1
        if total > processed_count:
            new.append(ev)
    return new, total


# ── fleet line template ──────────────────────────────────────────────────────
def _fleet_worker(events: List[dict]) -> Optional[str]:
    """The DOMINANT worker (most-published producer) as its fleet-URL segment — the
    ``producer`` skill_id's last dotted segment
    (``skill.fleet.forge-jobsearch`` → ``forge-jobsearch``), which is exactly what
    ``/portal/fragments/fleet/{skill_name}/`` resolves. Ties break deterministically
    by name. Returns ``None`` when NO event carries a usable producer — so the caller
    OMITS the link rather than ever emit a bare, dead ``fleet/`` (the I1 link bug)."""
    counts: Dict[str, int] = {}
    for ev in events:
        producer = (ev.get("producer") or "").strip()
        if not producer:
            continue
        worker = producer.rsplit(".", 1)[-1].strip()
        if worker:
            counts[worker] = counts.get(worker, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda w: (counts[w], w))


def _fleet_portal_link(worker: str) -> str:
    """The per-worker fleet-inbox deep link ``{base}/portal#fragments/fleet/<worker>/``
    — where the just-published units render with their chips + Drive links. NEVER the
    bare ``fleet/`` trailing-slash form, which dead-ends on the cold-load Knowledge
    default (the I1 link bug). ``worker`` is always non-empty (the caller omits the
    link entirely when there is none), so the result is always a resolvable segment."""
    from grove.prompt.portal_links import resolve_portal_base_url

    base = (resolve_portal_base_url() or "").rstrip("/")
    return f"{base}/portal#fragments/fleet/{worker}/"


def _sink_label(events: List[dict]) -> str:
    """The producer-surface label, DERIVED from the events' ``sink`` (never a
    hardcoded producer branch). One sink → its title-cased name ("forge" →
    "Forge"); a mixed window falls back to the neutral "Fleet"."""
    sinks = {(e.get("sink") or "").strip() for e in events if e.get("sink")}
    if len(sinks) == 1:
        return next(iter(sinks)).title()
    return "Fleet"


def _compose_line(label: str, m: int, k: int, link: Optional[str] = None) -> str:
    def _items(n: int) -> str:
        return f"{n} item" if n == 1 else f"{n} items"

    if m and k:
        body = f"published {_items(m)} to Drive unattended, {k} updated"
    elif m:
        body = f"published {_items(m)} to Drive unattended"
    else:  # k only
        body = f"updated {_items(k)} on Drive unattended"
    line = f"{label} {body}"
    # Link is OPTIONAL: appended only when a resolvable per-worker link exists.
    # A missing worker → NO link (never the bare, dead ``fleet/``).
    return f"{line} → {link}" if link else line


# ── node-local shown-state + watermark (sidecar; NEVER the durable log) ───────
def _state_path() -> Path:
    from grove.fleet.paths import publish_digest_state_path

    return publish_digest_state_path()


def _load_state() -> Dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return {"schema": _SCHEMA, "watermark": 0, "shown": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")
        data.setdefault("watermark", 0)
        data.setdefault("shown", {})
        return data
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        # A corrupt sidecar must not wedge the digest: re-baseline (shown-state
        # lost → items report once again, deduped thereafter). Loud, not silent.
        logger.warning(
            "[fleet.digest] shown-state at %s unreadable (%r) — re-baselining",
            path, exc,
        )
        return {"schema": _SCHEMA, "watermark": 0, "shown": {}}


def _save_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    tmp.replace(path)  # atomic within the one ~/.grove mount


def _emit_fail_notice(loop: Optional[Any], exc: Exception) -> None:
    """ONE loud operator notice on a digest failure — NOT an Andon line-halt, NOT a
    fall-back to the per-event flood. The durable events are safe (already
    appended); only this window's presentation failed, and the watermark stays put
    so the next window reports them deduped."""
    msg = (
        "Fleet-publish digest failed this window — unattended publishes are "
        "unreported to the operator surface for now; the durable audit is intact "
        "and the next window will report them. See the Operator Portal › Fleet."
    )
    try:
        from agent.async_utils import safe_schedule_threadsafe
        from grove.notify import broadcast_to_operator

        if loop is not None:
            safe_schedule_threadsafe(
                broadcast_to_operator(
                    msg, severity="warning",
                    metadata={"event": "fleet_publish_digest_failed"},
                ),
                loop, logger=logger,
                log_message="fleet digest fail-notice scheduling failed",
            )
        else:
            logger.warning("[fleet.digest] %s", msg)
    except Exception as exc2:  # noqa: BLE001 — the fail-notice must never itself raise
        logger.error(
            "[fleet.digest] fail-notice leg ALSO failed: %r (original %r)", exc2, exc
        )


# ── orchestrator (tick-tail windowed emit) ───────────────────────────────────
def emit_publish_digest(*, loop: Optional[Any] = None) -> Dict[str, Any]:
    """The tick-tail windowed emit. Reads the durable feed past the watermark,
    applies report-on-change, emits ONE info line for this window's new/updated
    items (suppressing unchanged ``exists`` no-ops), and advances the node-local
    state atomically.

    Telemetry-first failure posture: any read/compose error is caught HERE (never
    into the tick), surfaces ONE loud fail-notice, and leaves the watermark
    UNADVANCED so the next window catches the missed events deduped. Returns a
    small status dict (for tests/logs)."""
    from grove.fleet.observability import surface_fleet_digest

    state = _load_state()
    processed = int(state.get("watermark") or 0)
    shown: Dict[str, Any] = dict(state.get("shown") or {})
    try:
        events, total = _read_new_unattended(processed)
        if not events:
            # Quiet window — advance the watermark to the current total (so a grown
            # log is not re-scanned) but touch nothing else.
            if total != processed:
                _save_state({"schema": _SCHEMA, "watermark": total, "shown": shown})
            return {"emitted": False, "new": 0, "updated": 0, "window": 0}

        items = [
            (
                (ev.get("unit_id") or ev.get("slug") or ev.get("event_id")),
                (ev.get("status") == "published"),
            )
            for ev in events
        ]
        new_keys, changed_keys, next_seen = classify_window(items, set(shown))
        m, k = len(new_keys), len(changed_keys)

        if m or k:
            worker = _fleet_worker(events)
            link = _fleet_portal_link(worker) if worker else None
            line = _compose_line(_sink_label(events), m, k, link)
            surface_fleet_digest(
                line, loop=loop,
                extra={
                    "new": m, "updated": k,
                    "window_events": len(events), "portal_link": link,
                },
            )

        # Record a shown marker for every newly-seen key (the suppressed/changed
        # keys already sit in shown). Latest event per key wins.
        latest: Dict[str, dict] = {}
        for ev in events:
            key = ev.get("unit_id") or ev.get("slug") or ev.get("event_id")
            latest[key] = ev
        for key in next_seen:
            if key not in shown:
                ev = latest.get(key, {})
                shown[key] = {"status": ev.get("status"), "ts": ev.get("timestamp")}

        _save_state({"schema": _SCHEMA, "watermark": total, "shown": shown})
        return {"emitted": bool(m or k), "new": m, "updated": k, "window": len(events)}
    except Exception as exc:  # noqa: BLE001 — telemetry-first: caught here, never into the tick
        logger.error("[fleet.digest] window read/compose FAILED: %r", exc)
        _emit_fail_notice(loop, exc)
        # Watermark LEFT UNADVANCED (state not saved) → next window retries deduped.
        return {"emitted": False, "error": repr(exc)}

"""Lazy auto-ingest — scan the fleet sinks and compact new/changed docs.

Sprint K1 (living-cellar-v1) Phase 5. :func:`scan_and_ingest` walks the four
fleet sink directories (derived explicitly from ``FLEET_ADAPTERS``) under the
hermes home, glob-matches each dir with its adapter, skips files unchanged by
mtime (mirroring the WikiIndex meta-table pattern via a small JSON ledger), and
compacts new/changed sources through the pipeline.

Every actual ingestion — scanner, CLI, or endpoint — funnels through
:func:`ingest_file`, the single per-file idempotency gatekeeper (Sprint R1,
compaction-ingest-contract-v1). The scanner is a directory walker over the same
per-file body (:func:`_ingest_one`); there is no second ingest path, so the
mtime-ledger idempotency is uniform for every caller.

Discipline:

* **Lazy/poll only** — there is NO inotify/watchdog event watcher and NO
  write_file hook. The CLI, the ingest endpoint, and any future cron drive this
  on demand.
* **Tolerate absent dirs** — a missing sink (e.g. cultivator, which has never
  run) is skipped silently by design. An absent dir is not an error; a
  present-but-malformed file IS — loud PER FILE (quarantined with a WARNING,
  P3 GATE-B F2), never a scan abort.
* **Strict glob** — each dir is globbed with only its adapter's pattern, so
  off-contract residue (e.g. ``thinkpiece-*.md`` in the researcher sink) is
  never picked up.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from grove.wiki.adapters import (
    ADAPTERS,
    FLEET_ADAPTERS,
    Adapter,
    GenericPackageAdapter,
    fleet_adapter_for,
)
from grove.wiki.pipeline import CanonicalPage, compact, project_dock

logger = logging.getLogger(__name__)

_LEDGER_REL = Path(".index") / "ingest_state.json"

# ── attended-session ambient surfaces (notes-research-ingest-v1) ─────────
#
# RULING (banked): attended-session artifacts may auto-ingest into ambient
# context with honest provenance — approval happened in-loop at creation.
# Unattended-run artifacts always gate. ~/.grove/notes/ and ~/.grove/research/
# are attended-session surfaces BY DEFINITION (an agent writes them during an
# operator-present session), so the poll walks them like a fleet sink — but
# under the ``agent_session`` label, NEVER operator_curated: that label would
# assert the operator vetted the document, corrupting Cellar provenance for
# agent-authored research (the binding-governance audit-chain principle). Each
# dir is a flat ambient surface — no pending_review concept applies. Strict
# glob: ``.md``/``.txt`` only (Layer 2 / research-routing-coherence-v1 fixes
# format at the source; off-glob residue is ignored, never errored).
_AGENT_SESSION_DIRS = ("notes", "research")
_AGENT_SESSION_GLOBS = ("*.md", "*.txt")

# ── per-file quarantine (wiki-writer-structured-output-v1 P3, GATE-B F2) ──
#
# No single file may abort the scan. A candidate that fails to compact is
# QUARANTINED: skipped + ledger-marked + the walk CONTINUES, so every other
# file in the same scan keeps its ledger entry. The quarantine record is an
# ADDITIVE ledger value shape — a healthy entry stays a float mtime; a
# quarantined/parked entry is a dict (derive-on-read; no migration, existing
# ledgers stay valid).
#
# Retry policy (ruling): NOT the 60s identical-walk hammer. Post-quarantine
# retries back off at ~1 → ~10 → ~60 poll-cycle multiples (the poller's
# default cadence is 60s); after the 3rd retry fails the file is PARKED —
# permanently skipped for that mtime. An mtime change on a quarantined or
# parked file resets everything (the file changed; it is a new candidate) —
# operator touch is the designed re-ingest path, live-proven in the P2 bake.

_QUARANTINE_BACKOFF_SECONDS = (60.0, 600.0, 3600.0)  # ~1 / ~10 / ~60 cycles
_MAX_QUARANTINE_RETRIES = 3


def _quarantine_entry(ledger: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    """The quarantine record for *key*, or None (healthy float / absent)."""
    entry = ledger.get(key)
    return entry if isinstance(entry, dict) else None


def _mark_failed(
    ledger: Dict[str, Any], key: str, mtime: float, exc: BaseException, now: float
) -> None:
    """Record one compaction failure for *key* IN PLACE (F2 state machine).

    Fresh failure → ``quarantined`` (attempts=0, first backoff) with ONE
    WARNING — the journald-visible line. Retry failure → attempts advance at
    the next backoff step, quiet at INFO. Retry cap reached → ``parked``
    with ONE terminal WARNING; a parked file is never retried for that
    mtime. A prior record for a DIFFERENT mtime never carries over — the
    changed file already re-entered as a new candidate.
    """
    reason = f"{type(exc).__name__}: {exc}"
    prev = _quarantine_entry(ledger, key)
    if prev is not None and prev.get("mtime") != mtime:
        prev = None  # different bytes — a fresh candidate's failure history
    if prev is None:
        ledger[key] = {
            "state": "quarantined",
            "reason": reason,
            "mtime": mtime,
            "first_failed_at": datetime.fromtimestamp(
                now, tz=timezone.utc
            ).isoformat(),
            "attempts": 0,
            "next_retry_at": now + _QUARANTINE_BACKOFF_SECONDS[0],
        }
        logger.warning(
            "[wiki] QUARANTINED %s — %s (scan continues; retry in ~%ds)",
            key, reason, int(_QUARANTINE_BACKOFF_SECONDS[0]),
        )
        return
    attempts = int(prev.get("attempts", 0)) + 1
    if attempts >= _MAX_QUARANTINE_RETRIES:
        entry = {**prev, "state": "parked", "attempts": attempts, "reason": reason}
        entry.pop("next_retry_at", None)
        ledger[key] = entry
        logger.warning(
            "[wiki] PARKED %s after %d failed retries — %s; skipped until the "
            "file changes (touch to re-candidate).", key, attempts, reason,
        )
        return
    ledger[key] = {
        **prev,
        "attempts": attempts,
        "reason": reason,
        "next_retry_at": now + _QUARANTINE_BACKOFF_SECONDS[
            min(attempts, len(_QUARANTINE_BACKOFF_SECONDS) - 1)
        ],
    }
    logger.info(
        "[wiki] quarantine retry %d/%d failed for %s — %s",
        attempts, _MAX_QUARANTINE_RETRIES, key, reason,
    )


def _quarantine_should_skip(
    ledger: Dict[str, Any], key: str, mtime: float, now: float
) -> bool:
    """True when *key* is quarantined/parked for THIS mtime and not yet due.

    An mtime mismatch clears the record (new candidate, attempts reset) and
    returns False — the caller tries immediately. Parked at same mtime is a
    terminal silent skip; quarantined at same mtime skips quietly until its
    ``next_retry_at`` elapses.
    """
    entry = _quarantine_entry(ledger, key)
    if entry is None:
        return False
    if entry.get("mtime") != mtime:
        ledger.pop(key, None)  # the file changed — fresh candidate
        return False
    if entry.get("state") == "parked":
        return True  # terminal for this mtime; no per-cycle log
    if now < float(entry.get("next_retry_at", 0)):
        logger.debug("[wiki] %s quarantined; backoff pending.", key)
        return True
    return False

# The Dock manifest, relative to the hermes home. The watcher treats it as one
# more observed target alongside the fleet sinks (Sprint K2).
_DOCK_REL = Path("dock") / "dock.yaml"


# ── record-driven enumeration (promoted-artifact-persistence-v1 P2) ──────
#
# Declarative poller coverage ALONGSIDE the FLEET_ADAPTERS glob walk (staged
# migration, GATE-B Q1 — the four existing adapters are untouched). A
# capability record opting in via ``write_zone.ingest: {surface:
# canonical_subdirs, source_type: ...}`` gets its per-unit canonical subdirs
# (``<sink>/<unit>/<file>``, the P1 promote layout) walked with a
# declaration-fed :class:`GenericPackageAdapter`. Zero producer names — the
# record, not code, opts a sink in.


def _record_ingest_adapters(home: Path) -> "List[tuple]":
    """``[(GenericPackageAdapter, sink_abs_path)]`` for every kind=skill
    capability declaring ``write_zone.ingest`` with the ``canonical_subdirs``
    surface. A declaration missing ``source_type`` or ``canonical_dir`` fails
    loud (a half-declared coverage would silently ingest nothing)."""
    from grove.capability import CapabilityKind
    from grove.capability_registry import load_capabilities

    out: "List[tuple]" = []
    for cid, cap in load_capabilities().items():
        if cap.kind != CapabilityKind.SKILL or not cap.governance:
            continue
        wz = (cap.governance or {}).get("write_zone") or {}
        ingest = wz.get("ingest") or {}
        if not isinstance(ingest, dict) or ingest.get("surface") != "canonical_subdirs":
            continue
        source_type = ingest.get("source_type")
        canonical = wz.get("canonical_dir")
        if not source_type or not canonical:
            raise ValueError(
                f"capability {cid} declares write_zone.ingest with surface="
                f"canonical_subdirs but is missing "
                f"{'source_type' if not source_type else 'canonical_dir'} — "
                f"coverage cannot be derived"
            )
        out.append(
            (GenericPackageAdapter(sink_dir=canonical, source_type=source_type),
             home / canonical)
        )
    return out


def _iter_package_files(sink: Path):
    """Yield content files one level inside a canonical sink's per-unit
    subdirs: ``<sink>/<unit>/<file>``. Excludes the staging subtree
    (``pending_review``), dot-dirs (``.archive`` / ``.feedback``), dotfiles,
    and ``meta.json`` (never promoted; excluded defensively)."""
    for unit_dir in sorted(sink.iterdir()):
        if (not unit_dir.is_dir() or unit_dir.name == "pending_review"
                or unit_dir.name.startswith(".")):
            continue
        for f in sorted(unit_dir.iterdir()):
            if f.is_file() and f.name != "meta.json" and not f.name.startswith("."):
                yield f


def _record_adapter_for(source: Path) -> Optional[Adapter]:
    """Path-aware record-adapter resolution for the explicit-path caller
    (:func:`ingest_file`) — the no-second-ingest-path symmetry (P2 S1). A file
    one level inside a declared ``canonical_subdirs`` sink resolves the SAME
    adapter the scanner uses; the fleet adapters' filename-only matching is
    untouched. Returns None for anything outside a declared sink."""
    from hermes_constants import get_hermes_home

    src = Path(source).resolve()
    unit_dir = src.parent
    if (unit_dir.name == "pending_review" or unit_dir.name.startswith(".")
            or src.name == "meta.json"):
        return None
    for adapter, sink in _record_ingest_adapters(Path(get_hermes_home())):
        if unit_dir.parent == sink.resolve():
            return adapter
    return None


def _agent_session_adapter_for(source: Path) -> Optional[Adapter]:
    """The agent_session adapter for a file DIRECTLY inside an attended-session
    ambient dir (``$GROVE_HOME/notes/`` or ``research/``), else None — the
    explicit-path caller's mirror of the scanner's ambient walk (the
    _record_adapter_for symmetry). Filename extension is NOT re-checked here:
    the scanner constrains extensions via its glob; a manual ingest of any
    text file the operator points at these dirs earns the honest agent_session
    label rather than the mislabel operator_curated. Nested paths (a subdir
    under research/) do NOT match — only files one level in, matching the
    flat-surface scan."""
    from hermes_constants import get_hermes_home

    src = Path(source).resolve()
    try:
        home = Path(get_hermes_home()).resolve()
    except (OSError, ValueError):
        return None
    for rel_dir in _AGENT_SESSION_DIRS:
        if src.parent == (home / rel_dir).resolve():
            return ADAPTERS["agent_session"]
    return None


def ingest_file(
    path,
    *,
    wiki_root: Optional[Path] = None,
    hermes_home: Optional[Path] = None,
) -> Optional[CanonicalPage]:
    """Compact ONE source file into the cellar — the universal idempotency
    gatekeeper (Sprint R1, compaction-ingest-contract-v1).

    Resolves the adapter from the declarative map (a fleet glob match, else
    ``operator_curated``), honors the mtime ledger (a file unchanged since its
    last ingest is a no-op returning ``None``), compacts through the pipeline,
    and records the new mtime. The CLI file-branch and the
    ``POST /api/substrate/ingest`` endpoint both call this — there is no
    parallel ingest path, so idempotency is uniform across every caller.

    Idempotency is the mtime ledger keyed on the source path, NOT a content
    hash. ``hermes_home`` is accepted for caller symmetry with
    :func:`scan_and_ingest`; a single file carries its own absolute path, so it
    is not used to relocate the source.
    """
    from hermes_constants import get_wiki_path

    root = Path(wiki_root) if wiki_root else get_wiki_path()
    ledger_path = root / _LEDGER_REL
    ledger = _load_ledger(ledger_path)

    source = Path(path)
    page = _ingest_one(source, adapter=None, wiki_root=root, ledger=ledger)
    if page is not None:
        _save_ledger(ledger_path, ledger)
        logger.info("[wiki] ingested %s -> %s", source.name, page.path.name)
    return page


def _ingest_one(
    source: Path,
    *,
    adapter: Optional[Adapter],
    wiki_root: Path,
    ledger: Dict[str, Any],
) -> Optional[CanonicalPage]:
    """The shared per-file body: mtime-ledger short-circuit -> adapter.parse ->
    compact -> ledger write. Mutates ``ledger`` in place; the caller owns its
    load/save (one save per scan for the walker, one per file for
    :func:`ingest_file`). ``adapter`` is supplied by the scanner (already known
    from the glob loop) or resolved here from the declarative map for a
    single-file caller. No skill name is branched on — the map is glob-keyed.
    """
    if adapter is None:
        # Resolution order: fleet glob (filename-only, unchanged) → declared
        # canonical_subdirs sink (path-aware, P2 S1) → attended-session ambient
        # dir (path-aware, notes-research-ingest-v1 — the no-second-ingest-path
        # symmetry: a manual `hermes wiki ingest ~/.grove/research/x.md` labels
        # agent_session, NEVER operator_curated) → operator_curated.
        adapter = (fleet_adapter_for(source) or _record_adapter_for(source)
                   or _agent_session_adapter_for(source)
                   or ADAPTERS["operator_curated"])
    mtime = source.stat().st_mtime
    if ledger.get(str(source)) == mtime:
        return None  # unchanged since last ingest
    doc = adapter.parse(source)  # A2: fail loud on glob-match shape mismatch
    page = compact(doc, wiki_root=wiki_root)
    ledger[str(source)] = mtime
    return page


def scan_and_ingest(
    *,
    wiki_root: Optional[Path] = None,
    hermes_home: Optional[Path] = None,
    debounce_seconds: float = 0.0,
) -> List[CanonicalPage]:
    """Scan the fleet sinks and compact new/changed docs. Return the pages
    written this scan (empty when nothing changed).

    The directory walker over :func:`_ingest_one` — the same per-file body
    :func:`ingest_file` funnels through, so the scanner shares the one
    idempotency gate rather than forking it. A malformed file that matches
    its adapter's glob is loud PER FILE (P3, GATE-B F2): it is quarantined
    with a WARNING and the walk continues — no single file aborts the scan,
    and every successfully compacted file keeps its ledger entry.

    ``debounce_seconds`` is a partial-write guard for the autonomous poller
    (cellar-link-resolution-v1 Scope 2): a fleet sink file whose mtime is
    younger than the window is DEFERRED to a later scan, so a file caught
    mid-write is never compacted as a torn read. It defaults to ``0.0`` (off) so
    every explicit caller — the CLI, the ingest endpoint — ingests a freshly
    written file immediately; only the background poller passes a non-zero
    window. The Dock manifest is exempt: it is written atomically, so it is not
    subject to the partial-write guard.
    """
    from hermes_constants import get_hermes_home, get_wiki_path

    root = Path(wiki_root) if wiki_root else get_wiki_path()
    home = Path(hermes_home) if hermes_home else get_hermes_home()

    ledger_path = root / _LEDGER_REL
    ledger = _load_ledger(ledger_path)
    now = time.time()

    pages: List[CanonicalPage] = []

    def _scan_source(source: Path, adapter: Adapter) -> None:
        """Per-file scan body shared by the fleet glob walk and the
        record-driven package walk: quarantine gate → debounce guard →
        _ingest_one. Tolerates a file that vanishes between enumeration and
        read (P2 Mitigation 1 — e.g. a future purge racing the scan): a
        FileNotFoundError is a graceful skip, never a crashed scan.

        P3 (GATE-B F2): a PRESENT-but-malformed file is still LOUD — but per
        FILE, not per scan. Any per-file failure (adapter parse A2,
        MalformedWriterOutput, transport errors) quarantines that file and
        the walk CONTINUES; every other file this scan keeps its ledger
        entry. The pre-P3 scan-abort-before-_save_ledger behavior is dead.
        """
        try:
            st_mtime = source.stat().st_mtime
        except FileNotFoundError:
            logger.debug(
                "[wiki] %s vanished mid-scan; skipping (purge race).", source,
            )
            return
        if _quarantine_should_skip(ledger, str(source), st_mtime, now):
            return
        try:
            if debounce_seconds > 0:
                age = now - st_mtime
                if age < debounce_seconds:
                    # Younger than the debounce window — likely still being
                    # written. Defer to the next scan rather than compact a torn
                    # read. (No ledger write; it is reconsidered next cycle.)
                    logger.debug(
                        "[wiki] %s age %.1fs < debounce %.0fs; deferring.",
                        source.name, age, debounce_seconds,
                    )
                    return
            page = _ingest_one(
                source, adapter=adapter, wiki_root=root, ledger=ledger
            )
        except FileNotFoundError:
            logger.debug(
                "[wiki] %s vanished mid-scan; skipping (purge race).",
                source,
            )
            return
        except Exception as exc:  # noqa: BLE001 — F2: isolate, mark, continue
            _mark_failed(ledger, str(source), st_mtime, exc, now)
            return
        if page is not None:
            pages.append(page)
            logger.info(
                "[wiki] ingested %s -> %s", source.name, page.path.name
            )

    for adapter in FLEET_ADAPTERS:
        sink = home / adapter.sink_dir
        if not sink.is_dir():
            # Absent sink (e.g. cultivator never ran) — skip by design.
            logger.debug("[wiki] sink %s absent; skipping.", sink)
            continue
        for source in sorted(sink.glob(adapter.glob)):
            _scan_source(source, adapter)

    # Record-driven package walk (promoted-artifact-persistence-v1 P2) —
    # capabilities declaring write_zone.ingest {surface: canonical_subdirs}
    # get their per-unit canonical subdirs ingested via the declaration-fed
    # generic adapter. Same ledger, same debounce, same per-file body.
    for adapter, sink in _record_ingest_adapters(home):
        if not sink.is_dir():
            logger.debug("[wiki] declared sink %s absent; skipping.", sink)
            continue
        for source in _iter_package_files(sink):
            _scan_source(source, adapter)

    # Attended-session ambient walk (notes-research-ingest-v1) — parallel to
    # the fleet + record loops, riding the SAME ledger + debounce + per-file
    # quarantine via _scan_source. Each declared dir is globbed .md/.txt only
    # and ingested under the agent_session adapter (honest provenance; see the
    # _AGENT_SESSION_DIRS ruling above). Absent dirs are skipped by design.
    _agent_session_adapter = ADAPTERS["agent_session"]
    for rel_dir in _AGENT_SESSION_DIRS:
        ambient = home / rel_dir
        if not ambient.is_dir():
            logger.debug("[wiki] ambient dir %s absent; skipping.", ambient)
            continue
        seen: set = set()
        for pattern in _AGENT_SESSION_GLOBS:
            for source in sorted(ambient.glob(pattern)):
                # A dir globbed by two patterns never double-scans a file (no
                # overlap between *.md and *.txt, but guard anyway).
                if source in seen:
                    continue
                seen.add(source)
                _scan_source(source, _agent_session_adapter)

    # Dock observed-target branch (Sprint K2) — parallel to the fleet loop,
    # riding the SAME ledger dict + single save. Acts ONLY when the manifest
    # exists: an absent dock.yaml is "Dock not installed" — no trigger, no reap
    # (the existing dock_goal pages remain last-known-good). An emptied-but-
    # present manifest is a real mtime change and routes to project_dock, whose
    # reap-all mirrors the now-empty Dock.
    dock_path = home / _DOCK_REL
    if dock_path.is_file():
        mtime = dock_path.stat().st_mtime
        if ledger.get(str(dock_path)) != mtime and not _quarantine_should_skip(
            ledger, str(dock_path), mtime, now
        ):
            # P3: the manifest is one more candidate — a malformed dock.yaml
            # quarantines like any file instead of aborting the scan (F2).
            # An operator edit is an mtime change → immediate re-candidate.
            try:
                dock_pages = project_dock(wiki_root=root, dock_path=dock_path)
            except Exception as exc:  # noqa: BLE001 — F2
                _mark_failed(ledger, str(dock_path), mtime, exc, now)
            else:
                ledger[str(dock_path)] = mtime
                pages.extend(dock_pages)
                logger.info(
                    "[wiki] dock reconcile -> %d page(s)", len(dock_pages)
                )

    _save_ledger(ledger_path, ledger)
    quarantined = sum(
        1 for v in ledger.values()
        if isinstance(v, dict) and v.get("state") == "quarantined"
    )
    parked = sum(
        1 for v in ledger.values()
        if isinstance(v, dict) and v.get("state") == "parked"
    )
    if quarantined or parked:
        # One INFO summary per scan (never per-file lines for parked files).
        logger.info(
            "[wiki] scan summary: %d ingested, %d quarantined, %d parked.",
            len(pages), quarantined, parked,
        )
    return pages


async def poll_forever(
    *,
    interval_seconds: float = 60.0,
    debounce_seconds: float = 30.0,
    wiki_root: Optional[Path] = None,
    hermes_home: Optional[Path] = None,
) -> None:
    """Background poll loop (cellar-link-resolution-v1 Scope 2) — the autonomous
    trigger that makes a capability's sink write discoverable without a manual
    ``hermes wiki ingest``. Every ``interval_seconds`` it runs
    :func:`scan_and_ingest` with the partial-write ``debounce_seconds`` guard.

    The scan drives the compaction pipeline (model calls, blocking I/O), so it
    runs in a thread executor — a slow compaction never stalls the gateway event
    loop.

    Resilience vs. fail-loud: a malformed sink file no longer aborts a scan
    (P3 per-file quarantine handles it inside :func:`scan_and_ingest`); this
    catch now guards only NON-per-file failures (e.g. a half-declared
    capability in the record enumeration). The loop logs those loudly
    (``logger.exception``) and continues to the next cycle — letting the
    exception kill the task would silently end ALL future ingests, the worse
    failure. ``CancelledError`` (a ``BaseException``) is not caught, so the
    loop tears down cleanly on shutdown.
    """
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            pages = await loop.run_in_executor(
                None,
                functools.partial(
                    scan_and_ingest,
                    wiki_root=wiki_root,
                    hermes_home=hermes_home,
                    debounce_seconds=debounce_seconds,
                ),
            )
            if pages:
                logger.info("[wiki] poller compacted %d page(s).", len(pages))
        except Exception:
            logger.exception(
                "[wiki] poller scan failed; continuing next cycle."
            )


# ── ledger (mtime state; mirrors the index meta-table intent) ───────────


def _load_ledger(path: Path) -> Dict[str, Any]:
    """Ledger values: float mtime (healthy ingested) OR a quarantine record
    dict (P3, derive-on-read — the two shapes coexist, no migration)."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_ledger(path: Path, ledger: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")

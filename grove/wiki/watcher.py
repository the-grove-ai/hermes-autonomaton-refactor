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
  present-but-malformed file IS (the adapter raises, A2).
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
from pathlib import Path
from typing import Dict, List, Optional

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
    ledger: Dict[str, float],
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
        # canonical_subdirs sink (path-aware, P2 S1) → operator_curated.
        adapter = (fleet_adapter_for(source) or _record_adapter_for(source)
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
    idempotency gate rather than forking it. A malformed file that matches its
    adapter's glob raises (A2) — the scan fails loud rather than skipping it.

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
        record-driven package walk: debounce guard → _ingest_one. Tolerates a
        file that vanishes between enumeration and read (P2 Mitigation 1 —
        e.g. a future purge racing the scan): a FileNotFoundError is a
        graceful skip, never a crashed scan. Adapter parse failures (A2) still
        raise — a PRESENT-but-malformed file stays loud."""
        try:
            if debounce_seconds > 0:
                age = now - source.stat().st_mtime
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

    # Dock observed-target branch (Sprint K2) — parallel to the fleet loop,
    # riding the SAME ledger dict + single save. Acts ONLY when the manifest
    # exists: an absent dock.yaml is "Dock not installed" — no trigger, no reap
    # (the existing dock_goal pages remain last-known-good). An emptied-but-
    # present manifest is a real mtime change and routes to project_dock, whose
    # reap-all mirrors the now-empty Dock.
    dock_path = home / _DOCK_REL
    if dock_path.is_file():
        mtime = dock_path.stat().st_mtime
        if ledger.get(str(dock_path)) != mtime:
            dock_pages = project_dock(wiki_root=root, dock_path=dock_path)
            ledger[str(dock_path)] = mtime
            pages.extend(dock_pages)
            logger.info(
                "[wiki] dock reconcile -> %d page(s)", len(dock_pages)
            )

    _save_ledger(ledger_path, ledger)
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

    Resilience vs. fail-loud: a malformed sink file makes one scan raise. The
    loop logs it loudly (``logger.exception``) and continues to the next cycle —
    letting the exception kill the task would silently end ALL future ingests,
    the worse failure. ``CancelledError`` (a ``BaseException``) is not caught, so
    the loop tears down cleanly on shutdown.
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


def _load_ledger(path: Path) -> Dict[str, float]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_ledger(path: Path, ledger: Dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")

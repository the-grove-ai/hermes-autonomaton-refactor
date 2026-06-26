"""Lazy auto-ingest — scan the fleet sinks and compact new/changed docs.

Sprint K1 (living-cellar-v1) Phase 5. :func:`scan_and_ingest` walks the four
fleet sink directories (derived explicitly from ``FLEET_ADAPTERS``) under the
hermes home, glob-matches each dir with its adapter, skips files unchanged by
mtime (mirroring the WikiIndex meta-table pattern via a small JSON ledger), and
compacts new/changed sources through the pipeline.

Discipline:

* **Lazy/poll only** — there is NO inotify/watchdog event watcher and NO
  write_file hook. The CLI (and any future cron) drives this on demand.
* **Tolerate absent dirs** — a missing sink (e.g. cultivator, which has never
  run) is skipped silently by design. An absent dir is not an error; a
  present-but-malformed file IS (the adapter raises, A2).
* **Strict glob** — each dir is globbed with only its adapter's pattern, so
  off-contract residue (e.g. ``thinkpiece-*.md`` in the researcher sink) is
  never picked up.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from grove.wiki.adapters import FLEET_ADAPTERS
from grove.wiki.pipeline import CanonicalPage, compact

logger = logging.getLogger(__name__)

_LEDGER_REL = Path(".index") / "ingest_state.json"


def scan_and_ingest(
    *,
    wiki_root: Optional[Path] = None,
    hermes_home: Optional[Path] = None,
) -> List[CanonicalPage]:
    """Scan the fleet sinks and compact new/changed docs. Return the pages
    written this scan (empty when nothing changed).

    A malformed file that matches its adapter's glob raises (A2) — the scan
    fails loud rather than skipping it.
    """
    from hermes_constants import get_hermes_home, get_wiki_path

    root = Path(wiki_root) if wiki_root else get_wiki_path()
    home = Path(hermes_home) if hermes_home else get_hermes_home()

    ledger_path = root / _LEDGER_REL
    ledger = _load_ledger(ledger_path)

    pages: List[CanonicalPage] = []
    for adapter in FLEET_ADAPTERS:
        sink = home / adapter.sink_dir
        if not sink.is_dir():
            # Absent sink (e.g. cultivator never ran) — skip by design.
            logger.debug("[wiki] sink %s absent; skipping.", sink)
            continue
        for source in sorted(sink.glob(adapter.glob)):
            mtime = source.stat().st_mtime
            if ledger.get(str(source)) == mtime:
                continue  # unchanged since last ingest
            doc = adapter.parse(source)  # A2: fail loud on glob-match shape mismatch
            page = compact(doc, wiki_root=root)
            ledger[str(source)] = mtime
            pages.append(page)
            logger.info("[wiki] ingested %s -> %s", source.name, page.path.name)

    _save_ledger(ledger_path, ledger)
    return pages


# ── ledger (mtime state; mirrors the index meta-table intent) ───────────


def _load_ledger(path: Path) -> Dict[str, float]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_ledger(path: Path, ledger: Dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")

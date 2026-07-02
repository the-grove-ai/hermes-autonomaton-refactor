"""R6 (browser-read-surface-v1) — stage a browser extraction into the cellar.

grove-browser is read-only: it RETURNS extracted data; the autonomaton persists
it. This thin writer stages an extraction as a Yellow-staged RAW source file under
a substrate-indexed workspace's ``pending_review/`` subdir, carrying
``source: grove-browser/<domain>/<strategy>`` frontmatter.

The substrate CellarIndex (grove/cellar.py) recursively globs the ``research``
workspace (``research/**/*.md``), so the staged file is BM25-retrievable with its
source attribution intact — the frontmatter is kept verbatim in the indexed body
and surfaced in the query snippet. It is invisible to the wiki-compaction poller
(whose flat glob skips ``pending_review/``), so this is NOT the canonical
compaction path: canonicalization is deferred to downstream skill synthesis.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# A substrate-indexed workspace (research) + a pending_review/ staging subdir:
# indexed by CellarIndex's recursive **/*.md glob, invisible to the wiki poller's
# flat glob (Yellow staging). Not a new sink — research is already scanned.
_WORKSPACE = "research"
_STAGING = "pending_review"


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "x"


def _yaml_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_source(domain: str, strategy: str) -> str:
    """The canonical source-attribution string for a browser extraction."""
    return f"grove-browser/{domain}/{strategy}"


def stage_browser_extraction(
    *,
    content: str,
    domain: str,
    strategy: str,
    title: Optional[str] = None,
    cellar_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Path:
    """Write a browser extraction to the cellar staging area; return its path.

    ``source: grove-browser/<domain>/<strategy>`` is written verbatim into the
    frontmatter — it is the provenance the substrate CellarIndex surfaces on
    retrieval. Fails loud on an empty domain/strategy/content (a staged file with
    no attribution or no body is malformed).
    """
    if not domain or not domain.strip():
        raise ValueError("stage_browser_extraction: domain must be non-empty")
    if not strategy or not strategy.strip():
        raise ValueError("stage_browser_extraction: strategy must be non-empty")
    if not content or not content.strip():
        raise ValueError("stage_browser_extraction: content must be non-empty")

    domain = domain.strip()
    strategy = strategy.strip()
    source = build_source(domain, strategy)
    ts = now or datetime.now(timezone.utc)
    root = Path(cellar_dir) if cellar_dir else Path.home() / ".grove"
    staging = root / _WORKSPACE / _STAGING
    staging.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]
    fname = f"extract-{ts.strftime('%Y-%m-%d')}-{_slug(domain)}-{_slug(strategy)}-{digest}.md"
    path = staging / fname

    doc_title = title.strip() if title and title.strip() else f"Browser extraction — {domain} ({strategy})"
    frontmatter = (
        "---\n"
        f"title: {_yaml_quote(doc_title)}\n"
        # source is unquoted on purpose: [a-z0-9./_-] is YAML-safe, so the
        # attribution appears clean in the retrieval snippet.
        f"source: {source}\n"
        "source_type: browser_extraction\n"
        f"domain: {domain}\n"
        f"strategy: {strategy}\n"
        f"extracted_at: {ts.isoformat()}\n"
        "status: pending_review\n"
        "---\n\n"
    )
    path.write_text(frontmatter + content.rstrip() + "\n", encoding="utf-8")
    return path

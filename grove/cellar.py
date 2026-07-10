"""Cellar retrieval index for the Grove Autonomaton.

Sprint 13 (rag-substrate-v1). FTS5 full-text search over the operator's
cellar (~/.grove/) — promoted and proposed skills, the identity files,
the zone and routing config, and memory. Retrieval enriches each turn's
message with the most relevant cellar context, so the Autonomaton
answers from the operator's actual files, not from training data.

No embeddings — SQLite FTS5 is fast enough for the cellar's scale
(hundreds of files) and keeps the dependency graph clean. The index is
local (~/.grove/index/cellar.db); retrieval content never leaves the
process (Sprint 13 D8).

The index builds lazily on first query and refreshes incrementally by
file mtime; ``build_index()`` forces a full rebuild.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

logger = logging.getLogger(__name__)

# Per-result snippet cap — roughly 500 tokens (D7).
_SNIPPET_CHARS = 2000

# GATE-B: a hit must score at least this (0.0-1.0, relative within the
# result set) to enter the <cellar_context> block — keeps weak
# common-word matches out of the prompt.
_RELEVANCE_FLOOR = 0.1
# Total <cellar_context> budget — roughly 2000 tokens (D7).
_CONTEXT_CHAR_BUDGET = 8000

_FTS_SCHEMA = (
    "CREATE VIRTUAL TABLE cellar_fts USING fts5("
    "source_path, content_type, title, body, "
    "tokenize='porter unicode61')"
)
_META_SCHEMA = (
    "CREATE TABLE cellar_meta ("
    "source_path TEXT PRIMARY KEY, mtime REAL NOT NULL)"
)


@dataclass(frozen=True)
class CellarResult:
    """One retrieval hit from the cellar index."""

    source_path: str        # path relative to the cellar root
    content_type: str       # skill | skill_proposed | identity | config | memory
                            # | research | scout | drafter | dock | notes
    title: str
    snippet: str
    relevance_score: float  # 0.0-1.0, relative to the strongest hit in the set


class CellarIndex:
    """FTS5 full-text index over the operator's cellar."""

    def __init__(
        self,
        cellar_dir: Optional[Path] = None,
        index_path: Optional[Path] = None,
    ):
        self._cellar_dir = Path(cellar_dir) if cellar_dir else Path.home() / ".grove"
        self._index_path = (
            Path(index_path)
            if index_path
            else self._cellar_dir / "index" / "cellar.db"
        )

    @property
    def index_path(self) -> Path:
        """Filesystem path of the FTS5 index database."""
        return self._index_path

    # ----- public API ---------------------------------------------------------

    def query(self, text: str, k: int = 5) -> List[CellarResult]:
        """Return up to ``k`` cellar entries ranked by relevance to ``text``.

        Builds the index on first use and refreshes it incrementally by
        file mtime. Returns an empty list when the cellar is empty or
        nothing matches — a normal outcome, not an error.
        """
        if not text or not text.strip():
            return []
        self._ensure_fresh()
        match = _sanitize_fts_query(text)
        if not match:
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT source_path, content_type, title, body, rank "
                "FROM cellar_fts WHERE cellar_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (match, k),
            ).fetchall()
        return _rank_results(rows)

    def build_index(self) -> int:
        """Full rebuild — drop any existing index and re-scan the cellar.

        Returns the number of files indexed.
        """
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with closing(self._connect()) as conn:
            conn.execute("DROP TABLE IF EXISTS cellar_fts")
            conn.execute("DROP TABLE IF EXISTS cellar_meta")
            conn.execute(_FTS_SCHEMA)
            conn.execute(_META_SCHEMA)
            for path, content_type in self._iter_sources():
                self._index_file(conn, path, content_type)
                count += 1
            conn.commit()
        return count

    def update_index(self) -> None:
        """Incremental refresh — re-index files newer than their indexed
        copy and drop entries for files no longer present (D4)."""
        with closing(self._connect()) as conn:
            if not _index_ready(conn):
                needs_rebuild = True
            else:
                needs_rebuild = False
                indexed = {
                    row[0]: row[1]
                    for row in conn.execute(
                        "SELECT source_path, mtime FROM cellar_meta"
                    ).fetchall()
                }
                seen: set[str] = set()
                for path, content_type in self._iter_sources():
                    rel = self._rel(path)
                    seen.add(rel)
                    if rel not in indexed or path.stat().st_mtime > indexed[rel]:
                        self._index_file(conn, path, content_type)
                for stale in set(indexed) - seen:
                    conn.execute(
                        "DELETE FROM cellar_fts WHERE source_path = ?", (stale,)
                    )
                    conn.execute(
                        "DELETE FROM cellar_meta WHERE source_path = ?", (stale,)
                    )
                conn.commit()
        if needs_rebuild:
            # Index file exists but is empty or partial — start clean.
            self.build_index()

    # ----- internals ----------------------------------------------------------

    def _ensure_fresh(self) -> None:
        """Lazy build on first use; incremental refresh otherwise."""
        if not self._index_path.exists():
            logger.info("[cellar] building search index (~2s)...")
            self.build_index()
        else:
            self.update_index()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._index_path)

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self._cellar_dir))

    def _iter_sources(self) -> Iterator[tuple]:
        """Yield (path, content_type) for every cellar file to index (D2)."""
        cellar = self._cellar_dir
        skills_dir = cellar / "skills"
        if skills_dir.is_dir():
            for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
                if ".andon" in skill_md.parts:
                    continue
                yield skill_md, "skill"
            for skill_md in sorted((skills_dir / ".andon").glob("*/SKILL.md")):
                yield skill_md, "skill_proposed"
        for name in ("constitution.md", "soul.md", "operator.md", "goals.md"):
            p = cellar / name
            if p.is_file():
                yield p, "identity"
        for name in ("zones.schema.yaml", "routing.config.yaml"):
            p = cellar / name
            if p.is_file():
                yield p, "config"
        memory = cellar / "memory.md"
        if memory.is_file():
            yield memory, "memory"
        # Fleet + operator workspaces — each a directory of Markdown the
        # autonomaton produces or curates. Walked recursively; each subtree is
        # tagged with its own content_type. content_type matches the directory
        # name so retrieval provenance is self-describing.
        #
        # promoted-artifact-persistence-v1 P4 — CANONICAL-ONLY corpus. pathlib
        # glob descends into dot-dirs and staging subtrees, so an unfiltered
        # **/*.md walk leaks STAGED (pending_review/, unapproved) and
        # REJECTED/residue (.archive/) content into the automatic turn-start
        # context — the agent's ambient sense of operator standards polluted
        # by discards. The filter is STRUCTURAL (path segments, uniform for
        # every workspace, zero producer names): any dir segment that is
        # pending_review or dot-prefixed (.archive, .feedback, .andon — the
        # prior explicit .andon skip is subsumed) excludes the file. Only
        # operator-approved (promoted/canonical) and operator-authored
        # content enters the corpus.
        for subdir, content_type in (
            ("research", "research"),
            ("scout", "scout"),
            ("drafter", "drafter"),
            ("dock", "dock"),
            ("notes", "notes"),
        ):
            workspace = cellar / subdir
            if workspace.is_dir():
                for md in sorted(workspace.glob("**/*.md")):
                    rel_dirs = md.relative_to(workspace).parts[:-1]
                    if any(p == "pending_review" or p.startswith(".")
                           for p in rel_dirs):
                        continue
                    yield md, content_type

    def _index_file(
        self, conn: sqlite3.Connection, path: Path, content_type: str
    ) -> None:
        """(Re-)index one file: replace its FTS row and record its mtime."""
        rel = self._rel(path)
        body = path.read_text(encoding="utf-8")
        conn.execute("DELETE FROM cellar_fts WHERE source_path = ?", (rel,))
        conn.execute(
            "INSERT INTO cellar_fts (source_path, content_type, title, body) "
            "VALUES (?, ?, ?, ?)",
            (rel, content_type, _extract_title(body, path), body),
        )
        conn.execute(
            "INSERT INTO cellar_meta (source_path, mtime) VALUES (?, ?) "
            "ON CONFLICT(source_path) DO UPDATE SET mtime = excluded.mtime",
            (rel, path.stat().st_mtime),
        )


# ----- module-level helpers ---------------------------------------------------


def _index_ready(conn: sqlite3.Connection) -> bool:
    """True when both index tables exist."""
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }
    return "cellar_fts" in names and "cellar_meta" in names


def _extract_title(body: str, path: Path) -> str:
    """Title from YAML frontmatter ``name:``/``title:``, else the first
    markdown heading, else the filename stem."""
    stripped = body.lstrip()
    if stripped.startswith("---"):
        end = stripped.find("\n---", 3)
        if end != -1:
            for line in stripped[3:end].splitlines():
                m = re.match(r"\s*(name|title)\s*:\s*(.+)", line)
                if m:
                    return m.group(2).strip().strip("\"'")
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def _sanitize_fts_query(text: str) -> str:
    """Build a safe FTS5 MATCH query from the operator's message.

    Each alphanumeric token (length > 1, capped at 32) is quoted — so a
    word like ``and`` cannot be parsed as the FTS5 AND operator — and the
    tokens are OR-joined. OR keeps recall forgiving: any word can match,
    and bm25 ranks documents matching more (and rarer) words higher. No
    query expansion or synonyms (D6) — just the operator's own words.
    """
    tokens = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
    tokens = [t for t in tokens if len(t) > 1][:32]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def _rank_results(rows: list) -> List[CellarResult]:
    """Turn FTS5 rows into CellarResults with a 0.0-1.0 relevance score.

    FTS5 ``rank`` (bm25) is negative, more-negative = better. It is
    flipped positive and normalized against the strongest hit in this
    result set — so relevance is relative within a query, not absolute.
    """
    if not rows:
        return []
    scores = [-float(row[4]) for row in rows]
    best = max(scores) or 1.0
    results: List[CellarResult] = []
    for row, score in zip(rows, scores):
        source_path, content_type, title, body, _rank = row
        results.append(
            CellarResult(
                source_path=source_path,
                content_type=content_type,
                title=title,
                snippet=body[:_SNIPPET_CHARS],
                relevance_score=round(min(1.0, score / best), 4),
            )
        )
    return results


# ----- per-turn retrieval (Sprint 13 Phase 2) ---------------------------------


def retrieve_cellar_context(message: str) -> str:
    """Retrieve cellar context for one operator turn as a
    ``<cellar_context>`` block, ready to ride that turn's API call.

    Returns "" when there is nothing to add — a non-text message, an
    empty message, an empty cellar, no FTS match, or every hit below the
    relevance floor. The caller then sends the turn with no block.

    Retrieval failure is the commanded graceful degradation (Sprint 13
    GATE-B): any error logs loudly and returns "". RAG enriches the
    interaction; it never gates it. A corrupt index must not stop the
    operator from talking to their system.
    """
    if not isinstance(message, str) or not message.strip():
        return ""
    try:
        results = CellarIndex().query(message, k=5)
    except Exception as exc:
        logger.error(
            "[cellar] retrieval failed; interaction proceeds without "
            "cellar context: %r",
            exc,
        )
        return ""
    results = [r for r in results if r.relevance_score >= _RELEVANCE_FLOOR]
    if not results:
        return ""
    # D8 telemetry — record that retrieval occurred: source paths, content
    # types, and relevance scores, never the retrieved content itself.
    from grove.telemetry import log_retrieval  # local: keep telemetry decoupled

    log_retrieval(
        sources=[r.source_path for r in results],
        content_types=[r.content_type for r in results],
        scores=[r.relevance_score for r in results],
    )
    return _format_cellar_context(results)


def _format_cellar_context(results: List[CellarResult]) -> str:
    """Render ranked results as a ``<cellar_context>`` block within the
    token budget (D7). Lower-ranked results are dropped once the budget
    is reached; the strongest hit is always kept."""
    blocks: List[str] = []
    used = 0
    for r in results:
        block = (
            f'<result source="{r.source_path}" type="{r.content_type}" '
            f'relevance="{r.relevance_score}">\n'
            f"{r.snippet.strip()}\n"
            f"</result>"
        )
        if blocks and used + len(block) > _CONTEXT_CHAR_BUDGET:
            break
        blocks.append(block)
        used += len(block)
    return "<cellar_context>\n" + "\n".join(blocks) + "\n</cellar_context>"

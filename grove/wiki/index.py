"""WikiIndex — FTS5 retrieval over the living cellar's canonical pages.

Sprint K1 (living-cellar-v1) Phase 2. Modeled on ``grove/cellar.py``'s
CellarIndex pattern (FTS5 + an mtime meta-table + bm25 0-1 normalization) but
a SEPARATE index — cellar.py is untouched. Unlike the cellar's flat schema,
the wiki index carries DEDICATED columns for the canonical page frontmatter
(``source_type``, ``dock_goal_refs``, ``topics``, ``key_entities``,
``confidence``) so retrieval can filter by source type and boost ranking by
Dock-goal match and confidence.

Pages are markdown files with YAML frontmatter under ``$GROVE_WIKI_PATH/pages/``;
the index db lives on the persistent data disk at
``$GROVE_WIKI_PATH/.index/wiki.db``. A malformed page (no/unparseable
frontmatter, or a missing required field) FAILS LOUD during indexing — it is
never silently skipped into a partial index (Digital Jidoka).

Path resolution: the wiki root comes from ``hermes_constants.get_wiki_path()``
(``GROVE_WIKI_PATH`` or ``get_hermes_home()/"wiki"`` — never bare
``Path.home()``, the cellar.py:70 anti-pattern). Phase 5 consolidated the
Phase 2 local resolver onto this single helper.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Per-result snippet cap — roughly 500 tokens (mirrors cellar's _SNIPPET_CHARS).
_SNIPPET_CHARS = 2000

# Boost weights applied to the normalized bm25 base score.
_CONFIDENCE_WEIGHT = 0.5  # final *= 1 + WEIGHT * confidence
_DOCK_GOAL_WEIGHT = 0.5   # final *= 1 + WEIGHT  when the query dock_goal matches

# Indexed (searchable) columns: title, topics, key_entities, body.
# UNINDEXED (stored for filter/boost only): source_path, source_type,
# dock_goal_refs, confidence.
_FTS_SCHEMA = (
    "CREATE VIRTUAL TABLE wiki_fts USING fts5("
    "source_path UNINDEXED, source_type UNINDEXED, "
    "title, topics, key_entities, body, "
    "dock_goal_refs UNINDEXED, confidence UNINDEXED, "
    "tokenize='porter unicode61')"
)
_META_SCHEMA = (
    "CREATE TABLE wiki_meta ("
    "source_path TEXT PRIMARY KEY, mtime REAL NOT NULL)"
)


class MalformedWikiPage(ValueError):
    """A page file cannot be parsed into a canonical record — fail loud."""


@dataclass(frozen=True)
class WikiResult:
    """One retrieval hit from the wiki index."""

    source_path: str          # path relative to the pages root
    source_type: str
    title: str
    snippet: str
    relevance_score: float    # boosted score, relative within the result set
    confidence: Optional[float]
    dock_goal_refs: List[str]
    topics: List[str]


@dataclass(frozen=True)
class _PageRecord:
    """A parsed page ready to index."""

    rel_path: str
    source_type: str
    title: str
    body: str
    topics: List[str]
    key_entities: List[str]
    dock_goal_refs: List[str]
    confidence: Optional[float]


class WikiIndex:
    """FTS5 full-text index over the living cellar's canonical pages."""

    def __init__(
        self,
        wiki_root: Optional[Path] = None,
        index_path: Optional[Path] = None,
    ):
        from hermes_constants import get_wiki_path

        self._wiki_root = Path(wiki_root) if wiki_root else get_wiki_path()
        self._pages_dir = self._wiki_root / "pages"
        self._index_path = (
            Path(index_path)
            if index_path
            else self._wiki_root / ".index" / "wiki.db"
        )

    @property
    def index_path(self) -> Path:
        return self._index_path

    # ----- public API ---------------------------------------------------------

    def query(
        self,
        text: str,
        k: int = 5,
        *,
        source_type: Optional[str] = None,
        dock_goal: Optional[str] = None,
    ) -> List[WikiResult]:
        """Return up to ``k`` pages ranked by relevance to ``text``.

        ``source_type`` is a hard filter. ``dock_goal`` is a soft boost — pages
        whose ``dock_goal_refs`` contain it rank higher, but non-matching pages
        still appear. Confidence always boosts. Builds the index on first use
        and refreshes incrementally by mtime.
        """
        if not text or not text.strip():
            return []
        self._ensure_fresh()
        match = _sanitize_fts_query(text)
        if not match:
            return []
        sql = (
            "SELECT source_path, source_type, title, topics, key_entities, "
            "body, dock_goal_refs, confidence, rank "
            "FROM wiki_fts WHERE wiki_fts MATCH ?"
        )
        params: list = [match]
        if source_type is not None:
            sql += " AND source_type = ?"
            params.append(source_type)
        sql += " ORDER BY rank"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return _rank_results(rows, dock_goal=dock_goal, k=k)

    def build_index(self) -> int:
        """Full rebuild — drop any existing index and re-scan the pages tree.

        Returns the number of pages indexed. A malformed page raises and
        aborts the build (no partial index).
        """
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with closing(self._connect()) as conn:
            conn.execute("DROP TABLE IF EXISTS wiki_fts")
            conn.execute("DROP TABLE IF EXISTS wiki_meta")
            conn.execute(_FTS_SCHEMA)
            conn.execute(_META_SCHEMA)
            for path in self._iter_pages():
                self._index_file(conn, path)
                count += 1
            conn.commit()
        return count

    def update_index(self) -> None:
        """Incremental refresh — re-index pages newer than their indexed copy
        and drop entries for pages no longer present. A malformed page raises."""
        with closing(self._connect()) as conn:
            if not _index_ready(conn):
                needs_rebuild = True
            else:
                needs_rebuild = False
                indexed = {
                    row[0]: row[1]
                    for row in conn.execute(
                        "SELECT source_path, mtime FROM wiki_meta"
                    ).fetchall()
                }
                seen: set[str] = set()
                for path in self._iter_pages():
                    rel = self._rel(path)
                    seen.add(rel)
                    if rel not in indexed or path.stat().st_mtime > indexed[rel]:
                        self._index_file(conn, path)
                for stale in set(indexed) - seen:
                    conn.execute(
                        "DELETE FROM wiki_fts WHERE source_path = ?", (stale,)
                    )
                    conn.execute(
                        "DELETE FROM wiki_meta WHERE source_path = ?", (stale,)
                    )
                conn.commit()
        if needs_rebuild:
            self.build_index()

    # ----- internals ----------------------------------------------------------

    def _ensure_fresh(self) -> None:
        if not self._index_path.exists():
            logger.info("[wiki] building search index...")
            self.build_index()
        else:
            self.update_index()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._index_path)

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self._pages_dir))

    def _iter_pages(self) -> Iterator[Path]:
        if self._pages_dir.is_dir():
            yield from sorted(self._pages_dir.glob("**/*.md"))

    def _index_file(self, conn: sqlite3.Connection, path: Path) -> None:
        rec = _parse_page(path, self._rel(path))
        conn.execute(
            "DELETE FROM wiki_fts WHERE source_path = ?", (rec.rel_path,)
        )
        conn.execute(
            "INSERT INTO wiki_fts (source_path, source_type, title, topics, "
            "key_entities, body, dock_goal_refs, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec.rel_path,
                rec.source_type,
                rec.title,
                " ".join(rec.topics),
                " ".join(rec.key_entities),
                rec.body,
                " ".join(rec.dock_goal_refs),
                "" if rec.confidence is None else repr(rec.confidence),
            ),
        )
        conn.execute(
            "INSERT INTO wiki_meta (source_path, mtime) VALUES (?, ?) "
            "ON CONFLICT(source_path) DO UPDATE SET mtime = excluded.mtime",
            (rec.rel_path, path.stat().st_mtime),
        )


# ----- module-level helpers ---------------------------------------------------


def _index_ready(conn: sqlite3.Connection) -> bool:
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }
    return "wiki_fts" in names and "wiki_meta" in names


def _split_frontmatter(text: str) -> tuple:
    """Return (frontmatter_str, body_str). Raise MalformedWikiPage if the file
    does not open with a ``---`` frontmatter block."""
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        raise MalformedWikiPage("page has no YAML frontmatter block")
    end = stripped.find("\n---", 3)
    if end == -1:
        raise MalformedWikiPage("page frontmatter block is not terminated")
    fm = stripped[3:end]
    body = stripped[end + 4:].lstrip("\n")
    return fm, body


def _as_str_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    raise MalformedWikiPage(f"expected a list of strings, got {type(value).__name__}")


def _parse_page(path: Path, rel_path: str) -> _PageRecord:
    """Parse one page file into a _PageRecord. Fail loud on any defect."""
    text = path.read_text(encoding="utf-8")
    fm_str, body = _split_frontmatter(text)
    try:
        meta = yaml.safe_load(fm_str)
    except yaml.YAMLError as exc:
        raise MalformedWikiPage(f"unparseable frontmatter in {rel_path}: {exc}") from exc
    if not isinstance(meta, dict):
        raise MalformedWikiPage(f"frontmatter in {rel_path} is not a mapping")

    source_type = meta.get("source_type")
    title = meta.get("title")
    if not isinstance(source_type, str) or not source_type.strip():
        raise MalformedWikiPage(f"{rel_path}: missing required 'source_type'")
    if not isinstance(title, str) or not title.strip():
        raise MalformedWikiPage(f"{rel_path}: missing required 'title'")

    confidence = meta.get("confidence")
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError) as exc:
            raise MalformedWikiPage(
                f"{rel_path}: 'confidence' is not a number: {meta.get('confidence')!r}"
            ) from exc

    return _PageRecord(
        rel_path=rel_path,
        source_type=source_type,
        title=title,
        body=body,
        topics=_as_str_list(meta.get("topics")),
        key_entities=_as_str_list(meta.get("key_entities")),
        dock_goal_refs=_as_str_list(meta.get("dock_goal_refs")),
        confidence=confidence,
    )


def _sanitize_fts_query(text: str) -> str:
    """Build a safe FTS5 MATCH query: alnum tokens (len>1, capped 32), each
    quoted so reserved words can't parse as operators, OR-joined for recall.
    Mirrors cellar._sanitize_fts_query."""
    tokens = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
    tokens = [t for t in tokens if len(t) > 1][:32]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def _parse_confidence(raw) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _rank_results(rows: list, *, dock_goal: Optional[str], k: int) -> List[WikiResult]:
    """Normalize bm25 to 0-1, apply confidence + dock_goal boosts, re-sort,
    and return the top ``k``.

    FTS5 ``rank`` (bm25) is negative; more-negative = better. It is flipped
    positive and normalized against the strongest hit, then each result's
    score is multiplied by a confidence factor and (when the query carries a
    dock_goal that the page references) a dock-goal factor.
    """
    if not rows:
        return []
    base_scores = [-float(row[8]) for row in rows]
    best = max(base_scores) or 1.0

    scored: List[tuple] = []
    for row, base in zip(rows, base_scores):
        (
            source_path,
            source_type,
            title,
            topics_str,
            _key_entities_str,
            body,
            dock_goal_refs_str,
            confidence_str,
            _rank,
        ) = row
        norm = base / best
        confidence = _parse_confidence(confidence_str)
        dock_goal_refs = dock_goal_refs_str.split() if dock_goal_refs_str else []
        topics = topics_str.split() if topics_str else []

        factor = 1.0
        if confidence is not None:
            factor *= 1.0 + _CONFIDENCE_WEIGHT * confidence
        if dock_goal and dock_goal in dock_goal_refs:
            factor *= 1.0 + _DOCK_GOAL_WEIGHT
        final = norm * factor

        scored.append(
            (
                final,
                WikiResult(
                    source_path=source_path,
                    source_type=source_type,
                    title=title,
                    snippet=body[:_SNIPPET_CHARS],
                    relevance_score=round(final, 4),
                    confidence=confidence,
                    dock_goal_refs=dock_goal_refs,
                    topics=topics,
                ),
            )
        )

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [result for _score, result in scored[:k]]

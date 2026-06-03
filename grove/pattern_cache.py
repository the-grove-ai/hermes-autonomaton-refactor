"""T0 Pattern Cache substrate (Sprint 48 — pattern-compiler-pt1).

The deterministic key + (Sprint 49) store the Cognitive Router's T0 tier
matches against. T0 is DETERMINISTIC: a T0 hit returns a compiled pattern
with NO model call.

``t0_normalize`` / ``t0_key`` are the shared normalizer used at BOTH compile
time (the scanner / compiler, this sprint) AND execution time (Sprint 49).
Per GATE-A decision 1 the normalization is conservative — it expands a small
safe contraction set and strips cosmetic punctuation + whitespace, but does
NOT stem, remove stopwords, or expand abbreviations. The bias is toward false
negatives (T0 simply doesn't fire) over false positives (a wrong T0 hit would
return a canned answer with no model to catch it). Any residual lexical
collisions a slightly-coarse key produces are caught downstream by the
compiler's response-variance gate: a key whose evidence responses differ is
never promoted as static.

The stored ``pattern_hash`` on ``ClassificationResult`` / ``IntentRecord`` is
left untouched (it is live in the classifier and keys existing records);
``t0_key`` is a separate, more robust grouping key derived from the message.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Compiled-pattern lifecycle states (GATE-A decision 4 — Sprint 49 demotion).
STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"   # compiled, not yet operator-approved
STATUS_DEMOTED = "demoted"       # was active, demoted back to T1
STATUS_REJECTED = "rejected"     # operator rejected — never re-propose

# Small, unambiguous contraction expansions. Each preserves meaning exactly —
# no abbreviation guessing ("fav" → "favorite" is intentionally NOT here).
_CONTRACTIONS = {
    "what's": "what is", "whats": "what is", "who's": "who is",
    "how's": "how is", "where's": "where is", "when's": "when is",
    "why's": "why is", "that's": "that is", "there's": "there is",
    "here's": "here is", "it's": "it is", "let's": "let us",
    "i'm": "i am", "you're": "you are", "we're": "we are",
    "they're": "they are", "i've": "i have", "you've": "you have",
    "we've": "we have", "they've": "they have", "i'll": "i will",
    "you'll": "you will", "we'll": "we will", "i'd": "i would",
    "don't": "do not", "doesn't": "does not", "didn't": "did not",
    "can't": "can not", "won't": "will not", "wouldn't": "would not",
    "shouldn't": "should not", "couldn't": "could not", "isn't": "is not",
    "aren't": "are not", "wasn't": "was not", "weren't": "were not",
    "hasn't": "has not", "haven't": "have not", "hadn't": "had not",
}

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def t0_normalize(message: str) -> str:
    """Conservative, deterministic normalization for T0 pattern matching.

    Lowercase + Unicode NFKC + safe contraction expansion + punctuation strip
    + whitespace collapse. Contractions are expanded token-wise BEFORE the
    punctuation strip (the apostrophe is the contraction signal).
    """
    if not message:
        return ""
    text = unicodedata.normalize("NFKC", message).lower()
    text = " ".join(_CONTRACTIONS.get(tok, tok) for tok in text.split())
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def t0_key(intent_class: str, message: str) -> str:
    """The deterministic T0 cache key: SHA-256 of ``intent_class`` + the
    t0-normalized message. Identical intent + normalized message → identical
    key. ``intent_class`` is part of the key so cross-intent collisions are
    structurally impossible."""
    seed = f"{intent_class}:{t0_normalize(message)}"
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


# ── Compiled pattern schema (GATE-A decision D3) ──────────────────────


@dataclass(frozen=True)
class CompiledPattern:
    """One compiled T0 cache entry.

    ``pattern_id`` == ``t0_key``. STATIC patterns carry ``cached_response``
    (the string to return); EXECUTABLE patterns carry ``compiled_invocation``
    (JSON ``{"tool", "args"}`` the T0 path executes, model-free). ``status``
    moves active → suspended → demoted (Sprint 49). All timestamps ISO-8601.
    """
    pattern_id: str
    t0_key: str
    intent_class: str
    cacheable_type: str                       # static | executable
    cached_response: Optional[str]            # static
    compiled_invocation: Optional[str]        # executable: JSON {tool, args}
    evidence_hash: str
    status: str                               # active | suspended | demoted
    created_at: str
    promoted_at: Optional[str] = None
    last_hit_at: Optional[str] = None
    hit_count: int = 0
    promotion_evidence: Optional[str] = None  # JSON {repetition_count, ...}


# ── SQLite store (GATE-A decision 2 — NOT the FTS5 cellar) ────────────


def default_pattern_cache_path() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "pattern_cache.db"


_COLUMNS = (
    "pattern_id", "t0_key", "intent_class", "cacheable_type",
    "cached_response", "compiled_invocation", "evidence_hash", "status",
    "created_at", "promoted_at", "last_hit_at", "hit_count",
    "promotion_evidence",
)


class PatternCacheStore:
    """SQLite store for compiled T0 patterns at ``~/.grove/pattern_cache.db``.

    Keyed by ``pattern_id`` for O(1) exact lookup on Sprint 49's hot path,
    with atomic status / hit_count updates. NOT the FTS5 cellar (that is
    fuzzy full-text retrieval over markdown — the wrong primitive for
    deterministic exact-key matching)."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._path = Path(db_path) if db_path is not None else default_pattern_cache_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self._path), timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def _ensure_schema(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS t0_patterns (
                    pattern_id         TEXT PRIMARY KEY,
                    t0_key             TEXT NOT NULL,
                    intent_class       TEXT NOT NULL,
                    cacheable_type     TEXT NOT NULL,
                    cached_response    TEXT,
                    compiled_invocation TEXT,
                    evidence_hash      TEXT NOT NULL,
                    status             TEXT NOT NULL,
                    created_at         TEXT NOT NULL,
                    promoted_at        TEXT,
                    last_hit_at        TEXT,
                    hit_count          INTEGER NOT NULL DEFAULT 0,
                    promotion_evidence TEXT
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_t0_key_status "
                "ON t0_patterns (t0_key, status)"
            )

    def upsert(self, pattern: CompiledPattern) -> None:
        """Insert or replace a compiled pattern by ``pattern_id``."""
        data = asdict(pattern)
        cols = ", ".join(_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _COLUMNS)
        with self._connect() as con:
            con.execute(
                f"INSERT OR REPLACE INTO t0_patterns ({cols}) VALUES ({placeholders})",
                data,
            )

    def get(self, pattern_id: str) -> Optional[CompiledPattern]:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM t0_patterns WHERE pattern_id = ?", (pattern_id,),
            ).fetchone()
        return self._row_to_pattern(row) if row else None

    def get_active(self, t0_key: str) -> Optional[CompiledPattern]:
        """Active pattern for a t0_key, or None. Sprint 49's hot-path lookup."""
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM t0_patterns WHERE t0_key = ? AND status = ? LIMIT 1",
                (t0_key, STATUS_ACTIVE),
            ).fetchone()
        return self._row_to_pattern(row) if row else None

    def all(self) -> List[CompiledPattern]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM t0_patterns ORDER BY created_at"
            ).fetchall()
        return [self._row_to_pattern(r) for r in rows]

    def set_status(
        self, pattern_id: str, status: str, *, promoted_at: Optional[str] = None,
    ) -> bool:
        with self._connect() as con:
            if promoted_at is not None:
                cur = con.execute(
                    "UPDATE t0_patterns SET status = ?, promoted_at = ? "
                    "WHERE pattern_id = ?",
                    (status, promoted_at, pattern_id),
                )
            else:
                cur = con.execute(
                    "UPDATE t0_patterns SET status = ? WHERE pattern_id = ?",
                    (status, pattern_id),
                )
            return cur.rowcount > 0

    @staticmethod
    def _row_to_pattern(row: sqlite3.Row) -> CompiledPattern:
        return CompiledPattern(**{c: row[c] for c in _COLUMNS})

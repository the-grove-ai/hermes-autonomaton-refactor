"""Sprint 48 — T0 pattern cache substrate (t0_normalize / t0_key + store)."""

from __future__ import annotations

from pathlib import Path

import pytest

from grove.pattern_cache import (
    CompiledPattern,
    PatternCacheStore,
    STATUS_ACTIVE,
    STATUS_SUSPENDED,
    t0_key,
    t0_normalize,
)


# ── t0_normalize / t0_key (the GATE-A pairs) ──────────────────────────


@pytest.mark.parametrize(
    "a,b,intent,should_match",
    [
        # contraction + punctuation/whitespace → MATCH (safe)
        ("What's on my calendar?", "What is on my calendar?", "factual_lookup", True),
        ("what is 2+2", "what is 2 + 2", "factual_lookup", True),
        # abbreviation / stopword / paraphrase → DIFFER (false-negative, safe)
        ("remember my favorite color", "Remember my fav color", "memory_operation", False),
        ("translate this to Portuguese", "Translate to Portuguese", "transformation", False),
        ("pull up yesterday's architecture", "show me the architecture from yesterday", "factual_lookup", False),
    ],
)
def test_t0_key_pairs(a, b, intent, should_match):
    assert (t0_key(intent, a) == t0_key(intent, b)) is should_match


def test_t0_key_intent_class_prevents_cross_intent_collision():
    # Same normalized message, different intent → different key.
    assert t0_key("factual_lookup", "status") != t0_key("memory_operation", "status")


def test_t0_normalize_basics():
    assert t0_normalize("  What's   UP? ") == "what is up"
    assert t0_normalize("") == ""


# ── PatternCacheStore ─────────────────────────────────────────────────


def _pattern(pid="sha256:abc", status=STATUS_SUSPENDED, response="answer"):
    return CompiledPattern(
        pattern_id=pid, t0_key=pid, intent_class="factual_lookup",
        cacheable_type="static", cached_response=response, compiled_invocation=None,
        evidence_hash="sha256:e", status=status, created_at="2026-06-01T00:00:00+00:00",
        promotion_evidence='{"repetition_count": 5}',
    )


def test_store_upsert_get_roundtrip(tmp_path: Path):
    store = PatternCacheStore(tmp_path / "pc.db")
    p = _pattern()
    store.upsert(p)
    got = store.get(p.pattern_id)
    assert got is not None
    assert got.cached_response == "answer"
    assert got.status == STATUS_SUSPENDED


def test_store_get_active_and_set_status(tmp_path: Path):
    store = PatternCacheStore(tmp_path / "pc.db")
    p = _pattern()
    store.upsert(p)
    assert store.get_active(p.t0_key) is None  # suspended, not active
    assert store.set_status(p.pattern_id, STATUS_ACTIVE, promoted_at="2026-06-03T00:00:00+00:00")
    active = store.get_active(p.t0_key)
    assert active is not None and active.status == STATUS_ACTIVE
    assert active.promoted_at == "2026-06-03T00:00:00+00:00"


def test_store_upsert_replaces_by_pattern_id(tmp_path: Path):
    store = PatternCacheStore(tmp_path / "pc.db")
    store.upsert(_pattern(response="v1"))
    store.upsert(_pattern(response="v2"))
    assert len(store.all()) == 1
    assert store.get("sha256:abc").cached_response == "v2"


def test_store_set_status_missing_returns_false(tmp_path: Path):
    store = PatternCacheStore(tmp_path / "pc.db")
    assert store.set_status("sha256:nope", STATUS_ACTIVE) is False

"""Sprint 37 — Compositional Preamble provider (GRV-006).

The 8 mandatory tests cover:

* render with matching synthetic intents
* return None with empty store
* top_k limits respected
* pending outcomes excluded
* recency weighting orders results
* token cost < 500 tokens with top_k=3
* pattern_hash predicate takes priority over intent_class
* disabled via config

Each test builds an isolated ``IntentStore`` against a ``tmp_path``
JSONL file, populates synthetic ``IntentRecord``s, and injects the
store into the provider via the ``store_factory`` kwarg. No live
``~/.grove`` writes; no Dispatcher round-trip.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import pytest

from grove.intent_store import IntentRecord, IntentStore
from grove.prompt.composer import build_default_composer
from grove.prompt.preamble import build_contextual_preamble_provider


# ── Helpers ───────────────────────────────────────────────────────────


_PATTERN_A = "a" * 64
_PATTERN_B = "b" * 64
_BASE_TIME = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


def _record(
    *,
    pattern_hash: str = _PATTERN_A,
    intent_class: str = "planning",
    outcome: str = "success",
    stem: str = "sample request",
    age_minutes: int = 0,
    turn_id: Optional[str] = None,
) -> IntentRecord:
    ts = _BASE_TIME - timedelta(minutes=age_minutes)
    return IntentRecord(
        timestamp=ts.isoformat(),
        session_id="s_test",
        turn_id=turn_id or f"t_{age_minutes:06d}",
        user_message_stem=stem,
        pattern_hash=pattern_hash,
        intent_class=intent_class,
        register_class="casual",
        complexity_signal="simple",
        confidence=0.75,
        outcome=outcome,
    )


def _store(tmp_path: Path, records: Iterable[IntentRecord]) -> IntentStore:
    store = IntentStore(store_path=tmp_path / "intent_records.jsonl")
    for r in records:
        store.append(r)
    return store


def _provider_with(store: IntentStore, **kwargs):
    return build_contextual_preamble_provider(
        store_factory=lambda: store, **kwargs,
    )


# ── The mandatory eight ───────────────────────────────────────────────


def test_preamble_renders_with_matching_intents(tmp_path: Path) -> None:
    store = _store(tmp_path, [
        _record(stem="sprint kickoff", age_minutes=10),
        _record(stem="sprint review", age_minutes=20),
        _record(stem="sprint retro", age_minutes=30),
    ])
    provider = _provider_with(store)
    result = provider({"pattern_hash": _PATTERN_A, "intent_class": "planning"})

    assert result is not None
    assert result.label == "contextual_preamble"
    assert "## Compositional Context" in result.text
    assert "### Contextual Anchor" in result.text
    assert "### Historical State" in result.text
    assert "### Outcome Signal" in result.text
    assert "Intent class: planning" in result.text
    assert "Pattern: aaaaaaaa" in result.text
    assert "sprint kickoff" in result.text
    assert "sprint review" in result.text
    assert "sprint retro" in result.text


def test_preamble_returns_none_with_empty_store(tmp_path: Path) -> None:
    store = _store(tmp_path, [])
    provider = _provider_with(store)
    result = provider({"pattern_hash": _PATTERN_A, "intent_class": "planning"})
    assert result is None


def test_top_k_limits_respected(tmp_path: Path) -> None:
    store = _store(tmp_path, [
        _record(stem=f"item {i}", age_minutes=i * 10) for i in range(5)
    ])
    provider = _provider_with(store, top_k=2)
    result = provider({"pattern_hash": _PATTERN_A, "intent_class": "planning"})

    assert result is not None
    historical_block = result.text.split("### Historical State")[1].split("### Outcome Signal")[0]
    rendered_rows = [line for line in historical_block.splitlines() if line.startswith("- ")]
    assert len(rendered_rows) == 2


def test_pending_outcomes_excluded(tmp_path: Path) -> None:
    store = _store(tmp_path, [
        _record(stem="finished work", outcome="success", age_minutes=10, turn_id="t_a"),
        _record(stem="finished other", outcome="success", age_minutes=20, turn_id="t_b"),
        _record(stem="still in flight", outcome="pending", age_minutes=30, turn_id="t_c"),
    ])
    provider = _provider_with(store)
    result = provider({"pattern_hash": _PATTERN_A, "intent_class": "planning"})

    assert result is not None
    assert "still in flight" not in result.text
    assert "pending" not in result.text
    assert "finished work" in result.text
    assert "finished other" in result.text


def test_recency_weighting_orders_results(tmp_path: Path) -> None:
    store = _store(tmp_path, [
        _record(stem="oldest item", age_minutes=30 * 24 * 60),
        _record(stem="middle item", age_minutes=10 * 24 * 60),
        _record(stem="newest item", age_minutes=1 * 24 * 60),
    ])
    provider = _provider_with(store)
    result = provider({"pattern_hash": _PATTERN_A, "intent_class": "planning"})

    assert result is not None
    historical_block = result.text.split("### Historical State")[1].split("### Outcome Signal")[0]
    newest_idx = historical_block.find("newest item")
    middle_idx = historical_block.find("middle item")
    oldest_idx = historical_block.find("oldest item")
    assert newest_idx != -1 and middle_idx != -1 and oldest_idx != -1
    assert newest_idx < middle_idx < oldest_idx


def test_token_budget_under_500_with_top_k_3(tmp_path: Path) -> None:
    long_stem = "x" * 100
    store = _store(tmp_path, [
        _record(stem=long_stem, age_minutes=10),
        _record(stem=long_stem, age_minutes=20),
        _record(stem=long_stem, age_minutes=30),
    ])
    provider = _provider_with(store, top_k=3)
    result = provider({"pattern_hash": _PATTERN_A, "intent_class": "planning"})

    assert result is not None
    estimated_tokens = len(result.text) // 4
    assert estimated_tokens < 500, (
        f"preamble at top_k=3 exceeds 500-token budget: "
        f"{estimated_tokens} tokens ({len(result.text)} chars)"
    )


def test_pattern_hash_predicate_takes_priority_over_intent_class(tmp_path: Path) -> None:
    store = _store(tmp_path, [
        _record(
            stem="exact-pattern match",
            pattern_hash=_PATTERN_A,
            intent_class="planning",
            age_minutes=120,
            turn_id="t_pattern",
        ),
        _record(
            stem="class-only A",
            pattern_hash=_PATTERN_B,
            intent_class="planning",
            age_minutes=10,
            turn_id="t_class_a",
        ),
        _record(
            stem="class-only B",
            pattern_hash=_PATTERN_B,
            intent_class="planning",
            age_minutes=20,
            turn_id="t_class_b",
        ),
    ])
    provider = _provider_with(store, top_k=3, recency_decay=1.0)
    result = provider({"pattern_hash": _PATTERN_A, "intent_class": "planning"})

    assert result is not None
    historical_block = result.text.split("### Historical State")[1].split("### Outcome Signal")[0]
    pattern_idx = historical_block.find("exact-pattern match")
    class_a_idx = historical_block.find("class-only A")
    assert pattern_idx != -1 and class_a_idx != -1
    assert pattern_idx < class_a_idx


def test_disabled_via_config_skips_preamble(tmp_path: Path) -> None:
    store = _store(tmp_path, [
        _record(stem="would have rendered", age_minutes=10),
    ])
    import grove.intent_store as _is

    config = {
        "sections": {
            "contextual_preamble": {"enabled": False},
        }
    }
    composer = build_default_composer(config=config)
    output = composer.compose(
        valid_tool_names=set(),
        model="claude-test",
        provider="anthropic",
        platform="cli",
        session_id="s_test",
        skip_context_files=True,
        load_soul_identity=False,
        memory_enabled=False,
        user_profile_enabled=False,
        pass_session_id=False,
        system_message=None,
        session_register=None,
        memory_store=None,
        memory_manager=None,
        terminal_cwd=None,
        pattern_hash=_PATTERN_A,
        intent_class="planning",
    )
    assert "contextual_preamble" not in output.sections
    assert "## Compositional Context" not in output.text


# ── Carry-over coverage ──────────────────────────────────────────────


def test_returns_none_when_no_classification(tmp_path: Path) -> None:
    store = _store(tmp_path, [_record()])
    provider = _provider_with(store)
    assert provider({}) is None
    assert provider({"pattern_hash": "", "intent_class": ""}) is None


def test_outcome_filter_whitelist_overrides_default(tmp_path: Path) -> None:
    store = _store(tmp_path, [
        _record(stem="ok turn", outcome="success", age_minutes=10, turn_id="t_ok"),
        _record(stem="bad turn", outcome="correction", age_minutes=20, turn_id="t_bad"),
        _record(stem="dropped turn", outcome="drop", age_minutes=30, turn_id="t_drop"),
    ])
    provider = _provider_with(store, outcome_filter={"correction"})
    result = provider({"pattern_hash": _PATTERN_A, "intent_class": "planning"})

    assert result is not None
    assert "bad turn" in result.text
    assert "ok turn" not in result.text
    assert "dropped turn" not in result.text


def test_intent_class_fallback_fills_when_pattern_short(tmp_path: Path) -> None:
    store = _store(tmp_path, [
        _record(
            stem="pattern hit",
            pattern_hash=_PATTERN_A,
            age_minutes=10,
            turn_id="t_p",
        ),
        _record(
            stem="class fallback",
            pattern_hash=_PATTERN_B,
            intent_class="planning",
            age_minutes=20,
            turn_id="t_c",
        ),
    ])
    provider = _provider_with(store, top_k=3, recency_decay=1.0)
    result = provider({"pattern_hash": _PATTERN_A, "intent_class": "planning"})

    assert result is not None
    assert "pattern hit" in result.text
    assert "class fallback" in result.text

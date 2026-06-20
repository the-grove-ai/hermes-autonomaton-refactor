"""Phase 4 tests — accumulated_domain_memory PromptComposer provider.

The provider surfaces the operator's accumulated memory, weighted toward
active Dock goals, capped at a token budget, recording a MemoryAccessed
event per served record. The Composer context carries no user_message (see
SPEC amendment), so relevance is goal-boost + confidence, not turn keywords.
"""

from __future__ import annotations

from grove.memory.events import MemoryCreated, MemoryDeprecated
from grove.memory.provider import create_memory_provider
from grove.memory.store import MemoryStore

_TS = "2026-06-01T00:00:00+00:00"


def _seed(store, record_id, content, *, entity_type="DomainFact",
          confidence=0.9, dock_goal_ref=None):
    store.append_event(MemoryCreated(
        event_id="evt_" + record_id, timestamp=_TS, record_id=record_id,
        entity_type=entity_type, content=content, confidence=confidence,
        dock_goal_ref=dock_goal_ref, sources=[], supersedes=None,
    ))
    store.rebuild_index()


def _provider(store, *, dock_slugs=None, token_budget=500):
    return create_memory_provider(
        store=store,
        dock_goals_loader=lambda: [{"slug": s} for s in (dock_slugs or [])],
        token_budget=token_budget,
    )


def _ctx():
    return {"session_id": "sess-1", "intent_class": "conversation"}


# 1. Empty store → None

def test_empty_store_returns_none(tmp_path):
    store = MemoryStore(base_dir=tmp_path)
    assert _provider(store)(_ctx()) is None


# 2. Matching records → SectionResult with formatted text

def test_matching_records_returns_section(tmp_path):
    store = MemoryStore(base_dir=tmp_path)
    _seed(store, "mem_a", "Take Flight Advisors uses Notion.", confidence=0.95)
    result = _provider(store)(_ctx())
    assert result is not None
    assert result.label == "accumulated_domain_memory"
    assert "## Accumulated Domain Memory" in result.text
    assert "[DomainFact] Take Flight Advisors uses Notion. (0.95)" in result.text


# 3. Token budget caps served records

def test_token_budget_caps_records(tmp_path):
    store = MemoryStore(base_dir=tmp_path)
    # Each line ~100 tokens (~400 chars); budget 500 → 5 fit.
    for i in range(10):
        _seed(store, f"mem_{i}", "x" * 378, confidence=0.90)
    result = _provider(store, token_budget=500)(_ctx())
    assert result is not None
    served_lines = [ln for ln in result.text.splitlines() if ln.startswith("- ")]
    assert len(served_lines) == 5


# 4. Dock boost ranks goal-tagged records higher

def test_dock_boost_ranks_higher(tmp_path):
    store = MemoryStore(base_dir=tmp_path)
    _seed(store, "mem_plain", "Alpha general fact.", confidence=0.9)
    _seed(store, "mem_goal", "Beta goal fact.", confidence=0.7,
          dock_goal_ref="g1")
    result = _provider(store, dock_slugs=["g1"])(_ctx())
    assert result is not None
    # goal record (lower confidence) ranks above the general one (boost)
    assert result.text.index("Beta goal fact.") < result.text.index("Alpha general fact.")
    assert "[g1]" in result.text


# 5. MemoryAccessed appended for each served record

def test_memory_accessed_appended_per_served(tmp_path):
    store = MemoryStore(base_dir=tmp_path)
    _seed(store, "mem_a", "Fact A.")
    _seed(store, "mem_b", "Fact B.")
    _provider(store)(_ctx())
    from grove.memory.events import MemoryAccessed
    accesses = [e for e in store.read_events() if isinstance(e, MemoryAccessed)]
    assert len(accesses) == 2
    assert {a.record_id for a in accesses} == {"mem_a", "mem_b"}


# 6. No active records (all deprecated) → None, no MemoryAccessed events

def test_no_active_records_returns_none(tmp_path):
    store = MemoryStore(base_dir=tmp_path)
    store.append_event(MemoryCreated(
        event_id="evt_1", timestamp=_TS, record_id="mem_x",
        entity_type="DomainFact", content="Gone.", confidence=0.9,
        dock_goal_ref=None, sources=[], supersedes=None,
    ))
    store.append_event(MemoryDeprecated(
        event_id="evt_2", timestamp=_TS, record_id="mem_x", reason="retired",
    ))
    store.rebuild_index()

    assert _provider(store)(_ctx()) is None
    from grove.memory.events import MemoryAccessed
    assert not [e for e in store.read_events() if isinstance(e, MemoryAccessed)]


# 7. Registered in the default composer at context:15

def test_registered_in_default_composer():
    from grove.prompt.composer import build_default_composer

    composer = build_default_composer()
    reg = composer._sections.get("accumulated_domain_memory")
    assert reg is not None
    assert reg.tier == "context"
    assert reg.order == 15

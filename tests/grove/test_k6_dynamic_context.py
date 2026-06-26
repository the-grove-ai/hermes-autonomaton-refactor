"""K6 dynamic-context-assembly-v1 — tests for the unified-knowledge-substrate.

Covers the three K6 moves:
  * cellar_context as a tier-gated block with per-tier ceilings (Phase 1).
  * goal_record retired from the gating surface (Phase 2).
  * graduated-record sanity check: P1 + JSONL fallback on a missing cellar
    page (Phase 3 / D5).

Hermetic: tmp wiki roots, a fake BM25 index for arg/ceiling assertions, real
pipeline graduation/projection where faithful. No API calls.
"""

from __future__ import annotations

import logging

import yaml

from grove.wiki.index import WikiResult
from grove.wiki.provider import _SECTION_LABEL, create_cellar_provider


# ── helpers ─────────────────────────────────────────────────────────────────


class _FakeIndex:
    """Returns canned WikiResults; records query kwargs."""

    def __init__(self, results=None):
        self.results = results or []
        self.calls = []

    def query(self, text, k=5, *, source_type=None, dock_goal=None, ensure_fresh=True):
        self.calls.append({"text": text, "dock_goal": dock_goal})
        return self.results


def _result(title="X", snippet="body", st="researcher_brief"):
    return WikiResult(
        source_path=f"{st}/{title}.md", source_type=st, title=title,
        snippet=snippet, relevance_score=1.0, confidence=0.7,
        dock_goal_refs=[], topics=[],
    )


def _cellar_composer(provider):
    """A PromptComposer with only the cellar provider, registered under the
    name the gate keys on (``cellar_knowledge`` → block ``cellar_context``)."""
    from grove.prompt.composer import PromptComposer

    composer = PromptComposer()
    composer.register_section("cellar_knowledge", provider, order=11, tier="context")
    return composer


_TS = "2026-06-01T12:00:00+00:00"


def _created(record_id, content, *, confidence=0.9):
    from grove.memory.events import MemoryCreated

    return MemoryCreated(
        event_id="evt_" + record_id[-8:], timestamp=_TS, record_id=record_id,
        entity_type="DomainFact", content=content, confidence=confidence,
        dock_goal_ref=None, sources=[], supersedes=None,
    )


# ══════════════════════════════════════════════════════════════════════════
# cellar_context gating (the composer central gate via _PROVIDER_GATEABLE_BLOCK)
# ══════════════════════════════════════════════════════════════════════════


def test_cellar_context_gated_in():
    provider = create_cellar_provider(
        index_factory=lambda: _FakeIndex(results=[_result(snippet="quantum")]),
        dock_goals_loader=lambda: [],
    )
    composer = _cellar_composer(provider)
    result = composer.compose(
        user_message="quantum",
        tier_context_blocks=frozenset({"cellar_context"}),  # admitted
    )
    assert "cellar_knowledge" in result.sections
    assert "quantum" in result.sections["cellar_knowledge"]


def test_cellar_context_gated_out():
    provider = create_cellar_provider(
        index_factory=lambda: _FakeIndex(results=[_result(snippet="quantum")]),
        dock_goals_loader=lambda: [],
    )
    composer = _cellar_composer(provider)
    result = composer.compose(
        user_message="quantum",
        tier_context_blocks=frozenset({"claude_contract"}),  # cellar_context absent
    )
    # Central gate drops the section BEFORE the provider runs.
    assert "cellar_knowledge" not in result.sections


# ══════════════════════════════════════════════════════════════════════════
# per-tier ceilings (the cellar_context_ceiling threaded into compose context)
# ══════════════════════════════════════════════════════════════════════════

# Five results, each block ≈ 456 approx-tokens (snippet 1800 chars + header).
_CEILING_RESULTS = [_result(title=f"P{i}", snippet="x" * 1800) for i in range(5)]


def _approx_tokens(text):
    return max(1, len(text) // 4)


def test_cellar_context_ceiling_t1():
    provider = create_cellar_provider(
        index_factory=lambda: _FakeIndex(results=_CEILING_RESULTS),
        dock_goals_loader=lambda: [],
    )
    res = provider({"user_message": "x", "cellar_context_ceiling": 1000})
    assert res is not None
    # T1 ceiling respected: served content stays within the 1000-token budget.
    assert _approx_tokens(res.text) <= 1000
    t1_blocks = res.text.count("### ")
    # And it admits strictly fewer blocks than T3 (cap engaged, content truncated).
    res_t3 = provider({"user_message": "x", "cellar_context_ceiling": 2000})
    assert t1_blocks < res_t3.text.count("### ")


def test_cellar_context_ceiling_t3():
    provider = create_cellar_provider(
        index_factory=lambda: _FakeIndex(results=_CEILING_RESULTS),
        dock_goals_loader=lambda: [],
    )
    res = provider({"user_message": "x", "cellar_context_ceiling": 2000})
    assert res is not None
    assert _approx_tokens(res.text) <= 2000


def test_cellar_context_ceiling_absent_falls_back_to_default():
    # No ceiling threaded (construction-time / legacy compose) → constructor
    # token_budget (default 1500) governs, NOT a crash.
    provider = create_cellar_provider(
        index_factory=lambda: _FakeIndex(results=_CEILING_RESULTS),
        dock_goals_loader=lambda: [],
    )
    res = provider({"user_message": "x"})
    assert res is not None
    assert _approx_tokens(res.text) <= 1500


# ══════════════════════════════════════════════════════════════════════════
# goal_record removal (Phase 2 / D1)
# ══════════════════════════════════════════════════════════════════════════


def test_no_goal_record_injection():
    """goal_record is retired from the gating surface — no block, no provider
    mapping. (The Dock per-goal text seam in run_agent is gone; goals serve via
    the cellar.)"""
    from grove.prompt.composer import _PROVIDER_GATEABLE_BLOCK
    from grove.tier_budget import GATEABLE_CONTEXT_BLOCKS

    assert "goal_record" not in GATEABLE_CONTEXT_BLOCKS
    assert "goal_record" not in _PROVIDER_GATEABLE_BLOCK.values()
    # The repo template budget no longer lists goal_record on any tier.
    from pathlib import Path

    from grove.tier_budget import load_tier_budgets

    budgets = load_tier_budgets(config_path=Path("config/routing.config.yaml"))
    for tier in budgets.values():
        assert "goal_record" not in tier.context


def test_dock_goals_served_via_cellar(tmp_path, monkeypatch):
    """A Dock goal, projected to a canonical cellar page (K2), is surfaced by
    the cellar provider's BM25 retrieval — the single path that replaces the
    removed goal_record injection."""
    from grove.wiki.pipeline import project_dock

    wiki = tmp_path / "wiki"
    monkeypatch.setattr("grove.wiki.pipeline.get_wiki_path", lambda: wiki)
    dock_yaml = tmp_path / "dock.yaml"
    dock_yaml.write_text(yaml.safe_dump({"version": 1, "goals": [{
        "id": "humanity-ai-funding", "name": "HumanityAI Grant Submission",
        "vector": "strategic", "status": "accelerating",
        "definition_of_done": "submit the photovoltaics grant",
        "context_sources": [], "keywords": ["photovoltaics"], "unlocked_skills": [],
    }]}), encoding="utf-8")
    project_dock(wiki_root=wiki, dock_path=dock_yaml)

    provider = create_cellar_provider(
        wiki_root=wiki,
        dock_goals_loader=lambda: [{
            "slug": "humanity-ai-funding", "name": "HumanityAI Grant Submission",
            "status": "accelerating", "vector": "strategic",
        }],
    )
    result = provider({"user_message": "photovoltaics grant"})
    assert result is not None
    assert "photovoltaics" in result.text


# ══════════════════════════════════════════════════════════════════════════
# graduated-record sanity check (Phase 3 / D5)
# ══════════════════════════════════════════════════════════════════════════


def _graduated_store(tmp_path, monkeypatch):
    """A real store + cellar: one active (ungraduated) record, one graduated
    record (graduation writes its cellar page). Returns (store, wiki)."""
    from grove.memory.digest import MemoryProposalHandler
    from grove.memory.store import MemoryStore

    wiki = tmp_path / "wiki"
    monkeypatch.setattr("grove.wiki.pipeline.get_wiki_path", lambda: wiki)

    store = MemoryStore(base_dir=tmp_path / "store")
    store.append_event(_created("mem_ungrad", "Ungraduated fact about turbines."))
    store.append_event(_created("mem_grad", "Graduated fact about photons.",
                                confidence=0.95))
    store.rebuild_index()
    MemoryProposalHandler(store).apply({
        "action": "graduate", "target_id": "mem_grad", "content": "x",
        "entity_type": "DomainFact", "confidence": 0.95, "access_count": 3,
    })
    return store, wiki


def test_graduated_with_cellar_page(tmp_path, monkeypatch):
    """Graduated record WITH a cellar page → query() suppresses it from JSONL
    (K4 closure holds); not re-served as an orphan."""
    from grove.memory.provider import create_memory_provider

    store, _ = _graduated_store(tmp_path, monkeypatch)
    provider = create_memory_provider(store=store, dock_goals_loader=lambda: [])
    res = provider({"user_message": "photons turbines", "session_id": "s"})
    text = res.text if res else ""
    assert "photons" not in text          # graduated + page → suppressed
    assert "turbines" in text             # ungraduated → served


def test_graduated_without_cellar_page(tmp_path, monkeypatch, caplog):
    """Graduated record whose cellar page is MISSING → P1 warning + served from
    JSONL (D5 fail-safe; knowledge must not go dark)."""
    from grove.memory.provider import create_memory_provider

    store, wiki = _graduated_store(tmp_path, monkeypatch)
    # Simulate cellar-page loss: delete the graduated page on disk.
    for stale in (wiki / "pages" / "memory_graduated").glob("*.md"):
        stale.unlink()

    # Fresh provider → the once-only orphan scan runs against the now-empty dir.
    provider = create_memory_provider(store=store, dock_goals_loader=lambda: [])
    with caplog.at_level(logging.WARNING, logger="grove.memory.provider"):
        res = provider({"user_message": "anything", "session_id": "s"})
    text = res.text if res else ""
    assert "photons" in text              # orphan served from JSONL (override K4)
    assert any("has no cellar page" in r.getMessage() for r in caplog.records)


def test_ungraduated_always_served(tmp_path, monkeypatch):
    """Ungraduated (active) records are always served from JSONL regardless of
    cellar state."""
    from grove.memory.provider import create_memory_provider
    from grove.memory.store import MemoryStore

    wiki = tmp_path / "wiki"            # empty cellar
    monkeypatch.setattr("grove.wiki.pipeline.get_wiki_path", lambda: wiki)
    store = MemoryStore(base_dir=tmp_path / "store")
    store.append_event(_created("mem_a", "Active fact about turbines."))
    store.rebuild_index()

    provider = create_memory_provider(store=store, dock_goals_loader=lambda: [])
    res = provider({"user_message": "turbines", "session_id": "s"})
    assert res is not None
    assert "turbines" in res.text


# ══════════════════════════════════════════════════════════════════════════
# integration — full compose cycle
# ══════════════════════════════════════════════════════════════════════════


def test_compose_with_cellar_content(tmp_path, monkeypatch):
    """Full compose with cellar pages present and cellar_context admitted →
    the block composes with tier-appropriate content under the ceiling."""
    wiki = tmp_path / "wiki"
    monkeypatch.setattr("grove.wiki.pipeline.get_wiki_path", lambda: wiki)
    page = wiki / "pages" / "researcher_brief" / "alpha.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\n" + yaml.safe_dump(
            {"source_type": "researcher_brief", "title": "Alpha", "confidence": 0.8},
            sort_keys=False) + "---\n\nquantum tunneling diodes\n",
        encoding="utf-8",
    )
    provider = create_cellar_provider(wiki_root=wiki, dock_goals_loader=lambda: [])
    composer = _cellar_composer(provider)
    result = composer.compose(
        user_message="quantum tunneling",
        tier_context_blocks=frozenset({"cellar_context"}),
        cellar_context_ceiling=2000,
    )
    assert "cellar_knowledge" in result.sections
    assert "quantum tunneling diodes" in result.sections["cellar_knowledge"]
    assert _approx_tokens(result.sections["cellar_knowledge"]) <= 2000


def test_compose_empty_cellar(tmp_path, monkeypatch):
    """Empty cellar → cellar_context block absent, no crash."""
    wiki = tmp_path / "wiki"           # never populated
    monkeypatch.setattr("grove.wiki.pipeline.get_wiki_path", lambda: wiki)
    provider = create_cellar_provider(wiki_root=wiki, dock_goals_loader=lambda: [])
    composer = _cellar_composer(provider)
    result = composer.compose(
        user_message="anything",
        tier_context_blocks=frozenset({"cellar_context"}),
        cellar_context_ceiling=1000,
    )
    # Provider returns None (nothing to serve) → composer omits the section.
    assert "cellar_knowledge" not in result.sections
    assert _SECTION_LABEL == "cellar_knowledge"   # label unchanged (Resolution A)

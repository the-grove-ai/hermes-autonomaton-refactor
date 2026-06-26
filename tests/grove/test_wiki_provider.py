"""Tests for grove.wiki.provider — the cellar_knowledge PromptComposer provider.

unified-retrieval-provider-v1 P2. The cellar provider queries the K1 WikiIndex
at turn start and injects relevant canonical pages, ADDITIVE alongside
accumulated_domain_memory (never replacing it). Three guards:
  P2-a — dock_goals_loader is injectable (not read from context).
  P2-b — empty-message dock fallback actually retrieves (3 scenarios).
  P2-c — TTL-gated _ensure_fresh (monotonic clock, monkeypatched).
"""

from __future__ import annotations

import yaml

from grove.wiki.index import WikiResult
from grove.wiki.provider import (
    _CELLAR_TOKEN_BUDGET,
    _SECTION_LABEL,
    create_cellar_provider,
)


# ── helpers ───────────────────────────────────────────────────────────────


def _write_page(root, rel, *, source_type="researcher_brief", title="A Page",
                body="placeholder body", dock_goal_refs=None, confidence=0.7):
    path = root / "pages" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = {"source_type": source_type, "title": title, "confidence": confidence}
    if dock_goal_refs is not None:
        fm["dock_goal_refs"] = dock_goal_refs
    path.write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body + "\n",
        encoding="utf-8",
    )
    return path


def _goals(*specs):
    """specs: (slug, name) or (slug, name, [keywords])."""
    out = []
    for s in specs:
        g = {"slug": s[0], "name": s[1], "status": "accelerating", "vector": "v"}
        if len(s) > 2:
            g["keywords"] = s[2]
        out.append(g)
    return out


class _FakeIndex:
    """Records query kwargs; returns canned results. For TTL/arg assertions."""

    def __init__(self, results=None):
        self.results = results or []
        self.calls = []

    def query(self, text, k=5, *, source_type=None, dock_goal=None, ensure_fresh=True):
        self.calls.append({
            "text": text, "k": k, "source_type": source_type,
            "dock_goal": dock_goal, "ensure_fresh": ensure_fresh,
        })
        return self.results


def _result(path="researcher_brief/x.md", title="X", st="researcher_brief",
            snippet="body", score=1.0):
    return WikiResult(
        source_path=path, source_type=st, title=title, snippet=snippet,
        relevance_score=score, confidence=0.7, dock_goal_refs=[], topics=[],
    )


# ── P2-b(i): populated message → BM25 over message, dock_goal boost ─────────


def test_populated_message_queries_message_text(tmp_path):
    _write_page(tmp_path, "researcher_brief/alpha.md", body="quantum tunneling diodes")
    fake = _FakeIndex(results=[_result()])
    provider = create_cellar_provider(
        index_factory=lambda: fake,
        dock_goals_loader=lambda: _goals(("grow-fleet", "Grow the Fleet")),
    )
    result = provider({"user_message": "explain quantum tunneling"})
    assert fake.calls[0]["text"] == "explain quantum tunneling"
    assert fake.calls[0]["dock_goal"] == "grow-fleet"   # highest-priority slug
    assert result is not None
    assert result.label == _SECTION_LABEL


def test_populated_message_retrieves_real_page(tmp_path):
    _write_page(tmp_path, "researcher_brief/alpha.md", body="quantum tunneling diodes")
    _write_page(tmp_path, "researcher_brief/beta.md", body="garden composting soil")
    provider = create_cellar_provider(
        wiki_root=tmp_path, dock_goals_loader=lambda: [],
    )
    result = provider({"user_message": "quantum tunneling"})
    assert result is not None
    assert "quantum tunneling diodes" in result.text
    assert "garden composting" not in result.text


# ── P2-b(ii): empty message + active goals → synthetic query retrieves ──────


def test_empty_message_synthetic_query_from_goal_names(tmp_path):
    # A dock_goal page whose title/body matches the active goal NAME.
    _write_page(tmp_path, "dock_goal/ha.md", source_type="dock_goal",
                title="HumanityAI Grant Submission",
                body="HumanityAI grant submission readiness",
                dock_goal_refs=["humanity-ai-funding"])
    _write_page(tmp_path, "researcher_brief/noise.md", body="unrelated composting")
    provider = create_cellar_provider(
        wiki_root=tmp_path,
        dock_goals_loader=lambda: _goals(
            ("humanity-ai-funding", "HumanityAI Grant Submission")),
    )
    result = provider({"user_message": ""})        # empty → synthetic fallback
    assert result is not None
    assert "HumanityAI Grant Submission" in result.text
    assert "composting" not in result.text


def test_empty_message_synthetic_query_includes_keywords(tmp_path):
    # GUARD P2-a: an injected loader exposing keywords feeds them into the
    # synthetic query, so a page matching a keyword (not the name) retrieves.
    _write_page(tmp_path, "researcher_brief/kw.md", body="photonics research note")
    provider = create_cellar_provider(
        wiki_root=tmp_path,
        dock_goals_loader=lambda: _goals(
            ("g1", "Unrelated Goal Name", ["photonics"])),
    )
    result = provider({"user_message": ""})
    assert result is not None
    assert "photonics research note" in result.text


# ── P2-b(iii): empty message + no goals → no-op, NO query call ──────────────


def test_empty_message_no_goals_is_noop(tmp_path):
    fake = _FakeIndex(results=[_result()])
    provider = create_cellar_provider(
        index_factory=lambda: fake, dock_goals_loader=lambda: [],
    )
    result = provider({"user_message": ""})
    assert result is None
    assert fake.calls == []          # query NEVER called (no synthetic text)


# ── P2-a: dock loader injection (not from context) ──────────────────────────


def test_dock_goals_from_loader_not_context(tmp_path):
    fake = _FakeIndex(results=[_result()])
    provider = create_cellar_provider(
        index_factory=lambda: fake,
        dock_goals_loader=lambda: _goals(("loader-slug", "Loader Goal")),
    )
    # A dock goal in the CONTEXT must be ignored; the loader is authoritative.
    provider({"user_message": "hi", "dock_goals": [{"slug": "context-slug"}]})
    assert fake.calls[0]["dock_goal"] == "loader-slug"


# ── P2-c: TTL-gated ensure_fresh (monotonic, monkeypatched) ─────────────────


def test_ttl_gates_ensure_fresh(tmp_path):
    fake = _FakeIndex(results=[_result()])
    clock = {"t": 1000.0}
    provider = create_cellar_provider(
        index_factory=lambda: fake,
        dock_goals_loader=lambda: [],
        ttl=60.0,
        time_fn=lambda: clock["t"],
    )
    ctx = {"user_message": "quantum"}

    provider(ctx)                                  # first call → fresh
    assert fake.calls[-1]["ensure_fresh"] is True

    clock["t"] = 1030.0                            # +30s, within TTL
    provider(ctx)
    assert fake.calls[-1]["ensure_fresh"] is False

    clock["t"] = 1100.0                            # +70s from last refresh → expired
    provider(ctx)
    assert fake.calls[-1]["ensure_fresh"] is True


# ── cold start, budget, boost, header ───────────────────────────────────────


def test_cold_start_empty_cellar_returns_none(tmp_path):
    provider = create_cellar_provider(wiki_root=tmp_path, dock_goals_loader=lambda: [])
    assert provider({"user_message": "anything"}) is None


def test_token_budget_caps_results(tmp_path):
    for i in range(5):
        _write_page(tmp_path, f"researcher_brief/p{i}.md",
                    title=f"Page {i}", body=f"budgetterm body number {i}")
    # Tiny budget admits some but not all five blocks.
    provider = create_cellar_provider(
        wiki_root=tmp_path, dock_goals_loader=lambda: [], token_budget=20,
    )
    result = provider({"user_message": "budgetterm"})
    assert result is not None
    n = result.text.count("### ")
    assert 0 < n < 5                               # cap engaged

    # Default budget admits all five.
    provider_full = create_cellar_provider(wiki_root=tmp_path, dock_goals_loader=lambda: [])
    assert provider_full({"user_message": "budgetterm"}).text.count("### ") == 5


def test_dock_goal_boost_ranks_goal_linked_higher(tmp_path):
    _write_page(tmp_path, "researcher_brief/plain.md",
                title="Plain", body="alpha topic note", dock_goal_refs=[])
    _write_page(tmp_path, "dock_goal/linked.md", source_type="dock_goal",
                title="Linked", body="alpha topic note",
                dock_goal_refs=["grow-fleet"])
    provider = create_cellar_provider(
        wiki_root=tmp_path,
        dock_goals_loader=lambda: _goals(("grow-fleet", "Grow the Fleet")),
    )
    result = provider({"user_message": "alpha topic"})
    # The dock-goal-linked page (boosted) appears before the plain one.
    assert result.text.index("Linked") < result.text.index("Plain")


def test_section_header_and_format(tmp_path):
    _write_page(tmp_path, "researcher_brief/alpha.md", title="Alpha Brief",
                body="quantum tunneling")
    provider = create_cellar_provider(wiki_root=tmp_path, dock_goals_loader=lambda: [])
    text = provider({"user_message": "quantum"}).text
    assert text.startswith("## Cellar Knowledge")
    assert "### Alpha Brief (researcher_brief)" in text


def test_budget_constant_default():
    assert _CELLAR_TOKEN_BUDGET == 1500


# ── registration in the default composer (order=11, tier=context) ───────────


def test_registered_in_default_composer_order_11_context():
    from grove.prompt.composer import build_default_composer

    composer = build_default_composer()
    reg = composer._sections["cellar_knowledge"]
    assert reg.order == 11
    assert reg.tier == "context"
    # Strict inequality vs the JSONL provider (no stable-sort dependency).
    assert reg.order < composer._sections["accumulated_domain_memory"].order
    assert reg.order > composer._sections["system_message"].order


# ════════════════════════════════════════════════════════════════════════════
# P4 — composer-level integration: cellar_knowledge + accumulated_domain_memory
# coexisting in ONE compose() call.
# ════════════════════════════════════════════════════════════════════════════

_TS = "2026-06-01T12:00:00+00:00"


def _created(record_id, content, *, confidence=0.9):
    from grove.memory.events import MemoryCreated
    return MemoryCreated(
        event_id="evt_" + record_id[-8:], timestamp=_TS, record_id=record_id,
        entity_type="DomainFact", content=content, confidence=confidence,
        dock_goal_ref=None, sources=[], supersedes=None,
    )


def _two_provider_composer(wiki, store):
    from grove.memory.provider import create_memory_provider
    from grove.prompt.composer import PromptComposer

    composer = PromptComposer()
    composer.register_section(
        "cellar_knowledge",
        create_cellar_provider(wiki_root=wiki, dock_goals_loader=lambda: []),
        order=11, tier="context",
    )
    composer.register_section(
        "accumulated_domain_memory",
        create_memory_provider(store=store, dock_goals_loader=lambda: []),
        order=15, tier="context",
    )
    return composer


def test_composer_coexistence_cellar_and_jsonl(tmp_path, monkeypatch):
    """GUARD P4-a, assertions 1-6: both providers in one compose() — cellar
    before JSONL; graduated served by cellar only; ungraduated by JSONL only."""
    from grove.memory.digest import MemoryProposalHandler
    from grove.memory.store import MemoryStore
    from grove.wiki.pipeline import project_dock

    wiki = tmp_path / "wiki"
    monkeypatch.setattr("grove.wiki.pipeline.get_wiki_path", lambda: wiki)

    # (1) Seed the store + cellar with REAL pipeline output.
    store = MemoryStore(base_dir=tmp_path / "store")
    store.append_event(_created("mem_ungrad1", "Ungraduated jsonl fact about turbines."))
    store.append_event(_created("mem_grad1", "Graduated cellar fact about photons.",
                                confidence=0.95))
    store.rebuild_index()
    # Graduate mem_grad1 via the real flow → flips status + writes the cellar page.
    MemoryProposalHandler(store).apply({
        "action": "graduate", "target_id": "mem_grad1", "content": "x",
        "entity_type": "DomainFact", "confidence": 0.95, "access_count": 3,
    })
    # A real dock_goal page (second source type) for cellar realism.
    dock_yaml = tmp_path / "dock.yaml"
    dock_yaml.write_text(yaml.safe_dump({"version": 1, "goals": [{
        "id": "neutral-goal", "name": "Neutral Goal", "vector": "strategic",
        "status": "accelerating", "definition_of_done": "done",
        "context_sources": [], "keywords": ["unrelatedterm"], "unlocked_skills": [],
    }]}), encoding="utf-8")
    project_dock(wiki_root=wiki, dock_path=dock_yaml)

    # (2) Register BOTH providers in a real composer.
    composer = _two_provider_composer(wiki, store)

    # (3) Compose with terms from both surfaces.
    result = composer.compose(user_message="photons turbines", session_id="s1")
    sections = result.sections
    text = result.text

    cellar = sections["cellar_knowledge"]
    jsonl = sections.get("accumulated_domain_memory", "")

    # (3) cellar section carries the graduated page content.
    assert "photons" in cellar
    # (4) ordering: cellar (11) strictly before JSONL (15) in the output.
    assert text.index("## Cellar Knowledge") < text.index("## Accumulated Domain Memory")
    # (5) graduated record: cellar YES, JSONL NO (status=graduated suppresses query()).
    assert "photons" in cellar
    assert "photons" not in jsonl
    # (6) ungraduated record: JSONL YES, cellar NO (no cellar page exists for it).
    assert "turbines" in jsonl
    assert "turbines" not in cellar


def test_composer_cold_start_empty_cellar(tmp_path, monkeypatch):
    """GUARD P4-a, assertion 7: empty cellar → cellar provider serves nothing;
    accumulated_domain_memory still serves ungraduated records normally."""
    from grove.memory.store import MemoryStore

    wiki = tmp_path / "wiki"          # never populated
    monkeypatch.setattr("grove.wiki.pipeline.get_wiki_path", lambda: wiki)

    store = MemoryStore(base_dir=tmp_path / "store")
    store.append_event(_created("mem_ungrad2", "Ungraduated jsonl fact about turbines."))
    store.rebuild_index()

    composer = _two_provider_composer(wiki, store)
    result = composer.compose(user_message="turbines", session_id="s1")

    # Empty cellar → provider returns None → composer omits the section.
    assert "cellar_knowledge" not in result.sections
    # JSONL provider unaffected.
    assert "turbines" in result.sections["accumulated_domain_memory"]

"""memory-cellar-graduation-v1 — graduation event, field, and fold.

P1 covers the event-sourced primitives: a ``MemoryGraduated`` event, the
additive ``graduated_at`` field on ``MemoryRecord``, and the store fold.

K3 served graduated records via BOTH the JSONL path and the cellar page (the
dual-serve interim). unified-retrieval-provider-v1 (K4) CLOSES that window: the
fold now also flips ``status`` to ``"graduated"``, suppressing the record from
the JSONL/query path so the cellar page becomes its sole serving surface.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from grove.kaizen.renderable import MemoryProposalRenderable
from grove.memory.digest import MemoryProposalHandler
from grove.memory.events import (
    MemoryAccessed,
    MemoryCreated,
    MemoryDeprecated,
    MemoryGraduated,
)
from grove.memory.graduation import (
    _MAX_PROPOSALS,
    _MIN_ACCESS_COUNT,
    GraduationDetector,
)
from grove.memory.record import MemoryRecord
from grove.memory.store import _DEPRECATION_FLOOR, MemoryStore
from grove.wiki.index import WikiIndex
from grove.wiki.pipeline import _HASH_LEN, CanonicalPage, project_memory


def _ts(offset_days: float = 0.0) -> str:
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(days=offset_days)).isoformat()


@pytest.fixture()
def store(tmp_path):
    return MemoryStore(base_dir=tmp_path)


def _create(record_id: str, *, content: str = "Fact", confidence: float = 0.9):
    return MemoryCreated(
        event_id="evt_" + record_id[-8:],
        timestamp=_ts(),
        record_id=record_id,
        entity_type="DomainFact",
        content=content,
        confidence=confidence,
        dock_goal_ref=None,
        sources=[{"session_id": "s1", "turn_id": "t1"}],
        supersedes=None,
    )


# 1. MemoryGraduated round-trips through append/read.

def test_memory_graduated_event_round_trip(store):
    event = MemoryGraduated(
        event_id="evt_grad0001",
        timestamp=_ts(2),
        record_id="mem_11111111",
    )
    store.append_event(event)

    read_back = list(store.read_events())
    assert read_back == [event]


# 2. Fold sets graduated_at AND flips status to "graduated" (K4 closes dual-serve).

def test_graduation_fold_sets_graduated_at_and_flips_status(store):
    store.append_event(_create("mem_aaaaaaaa"))
    grad_ts = _ts(3)
    store.append_event(MemoryGraduated(
        event_id="evt_grad0002",
        timestamp=grad_ts,
        record_id="mem_aaaaaaaa",
    ))

    store.rebuild_index()
    rec = store.projected_records()["mem_aaaaaaaa"]
    assert rec.graduated_at == grad_ts
    # K4 dual-serve closure: graduation flips status to "graduated".
    assert rec.status == "graduated"


# 3. A record with no graduation event folds to graduated_at=None.

def test_ungraduated_record_folds_to_none(store):
    store.append_event(_create("mem_bbbbbbbb"))

    store.rebuild_index()
    rec = store.projected_records()["mem_bbbbbbbb"]
    assert rec.graduated_at is None


# 4. A graduated record is SUPPRESSED from query() (K4 dual-serve closure).

def test_graduated_record_suppressed_from_query(store):
    store.append_event(_create("mem_cccccccc", content="Notion tracks projects."))
    store.append_event(MemoryGraduated(
        event_id="evt_grad0003",
        timestamp=_ts(4),
        record_id="mem_cccccccc",
    ))
    store.rebuild_index()

    results = store.query(keywords=["notion"])
    ids = [r.id for r in results]
    assert "mem_cccccccc" not in ids       # served by the cellar, not the JSONL path


# 5. Graduation fold survives a later access event (independent fields).

def test_graduation_and_access_coexist(store):
    store.append_event(_create("mem_dddddddd"))
    store.append_event(MemoryGraduated(
        event_id="evt_grad0004",
        timestamp=_ts(2),
        record_id="mem_dddddddd",
    ))
    store.append_event(MemoryAccessed(
        event_id="evt_acc00001",
        timestamp=_ts(5),
        record_id="mem_dddddddd",
        session_id="s9",
        context="probe",
    ))
    store.rebuild_index()

    rec = store.projected_records()["mem_dddddddd"]
    assert rec.graduated_at == _ts(2)
    assert rec.access_count == 1
    assert rec.status == "graduated"      # K4 dual-serve closure


# ════════════════════════════════════════════════════════════════════════════
# P2 — project_memory(): deterministic MemoryRecord → CanonicalPage projection.
# ════════════════════════════════════════════════════════════════════════════


def _record(**over) -> MemoryRecord:
    fields = dict(
        id="mem_proj0001",
        entity_type="DomainFact",
        content="Take Flight Advisors uses Notion for project tracking.",
        confidence=0.9,
        dock_goal_ref=None,
        sources=[{"session_id": "s1", "turn_id": "t1"}],
        status="active",
        created_at=_ts(),
        last_accessed=_ts(2),
        access_count=4,
    )
    fields.update(over)
    return MemoryRecord(**fields)


def _frontmatter(markdown: str) -> dict:
    """Parse the YAML frontmatter block out of a rendered page."""
    lines = markdown.splitlines()
    assert lines[0] == "---"
    end = next(i for i in range(1, len(lines)) if lines[i] == "---")
    return yaml.safe_load("\n".join(lines[1:end]))


def _hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:_HASH_LEN]


def test_project_memory_never_calls_t1(monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise AssertionError("project_memory() must never call the LLM")

    monkeypatch.setattr("grove.wiki.pipeline.call_t1", _boom)
    page = project_memory(_record(), wiki_root=tmp_path / "wiki")
    assert isinstance(page, CanonicalPage)


def test_project_memory_mapping_correctness(tmp_path):
    rec = _record(dock_goal_ref="grow-fleet", confidence=0.84)
    page = project_memory(rec, wiki_root=tmp_path / "wiki")

    assert page.title == rec.content        # <= 80 chars → full
    assert page.source == "memory#mem_proj0001"
    assert page.source_type == "memory_graduated"
    assert page.dock_goal_refs == ["grow-fleet"]
    assert page.topics == ["DomainFact"]
    assert page.key_entities == []
    assert page.confidence == 0.84
    assert page.editor_ran is False

    fm = _frontmatter(page.markdown)
    assert fm["title"] == rec.content
    assert fm["source"] == "memory#mem_proj0001"
    assert fm["source_type"] == "memory_graduated"
    assert fm["status"] == "active"
    assert fm["confidence"] == 0.84
    assert fm["dock_goal_refs"] == ["grow-fleet"]
    assert fm["topics"] == ["DomainFact"]
    assert fm["key_entities"] == []


def test_project_memory_content_verbatim_in_body(tmp_path):
    rec = _record(content="The VM prod HEAD is 33a54dc1d as of K2 deploy.")
    page = project_memory(rec, wiki_root=tmp_path / "wiki")
    assert rec.content in page.body
    # entity_type, timestamps, and access telemetry all carried in the body.
    assert "DomainFact" in page.body
    assert rec.created_at in page.body
    assert rec.last_accessed in page.body
    assert "4" in page.body                  # access_count


def test_project_memory_title_truncates_over_80(tmp_path):
    long = "A" * 120
    rec = _record(content=long)
    page = project_memory(rec, wiki_root=tmp_path / "wiki")
    assert page.title == long[:80]
    assert len(page.title) == 80


def test_project_memory_title_full_when_under_80(tmp_path):
    short = "Short fact."
    rec = _record(content=short)
    page = project_memory(rec, wiki_root=tmp_path / "wiki")
    assert page.title == short


def test_project_memory_hash_stable_for_fixed_id(tmp_path):
    wiki = tmp_path / "wiki"
    rec = _record(id="mem_stable01")
    p1 = project_memory(rec, wiki_root=wiki).path
    # Re-project the same id (drifted content → new slug, same source hash).
    rec2 = _record(id="mem_stable01", content="Drifted content statement.")
    p2 = project_memory(rec2, wiki_root=wiki).path

    expected = _hash("memory#mem_stable01")
    assert p1.name.endswith(f"-{expected}.md")
    assert p2.name.endswith(f"-{expected}.md")
    files = list((wiki / "pages" / "memory_graduated").glob("*.md"))
    assert len(files) == 1                   # idempotent: one page per source


def test_project_memory_hash_distinct_across_ids(tmp_path):
    wiki = tmp_path / "wiki"
    pa = project_memory(_record(id="mem_alpha001"), wiki_root=wiki)
    pb = project_memory(_record(id="mem_beta0001"), wiki_root=wiki)
    assert pa.path.name.endswith(f"-{_hash('memory#mem_alpha001')}.md")
    assert pb.path.name.endswith(f"-{_hash('memory#mem_beta0001')}.md")
    assert pa.path != pb.path
    assert len(list((wiki / "pages" / "memory_graduated").glob("*.md"))) == 2


def test_project_memory_dock_goal_ref_absent_is_empty_list(tmp_path):
    page = project_memory(_record(dock_goal_ref=None), wiki_root=tmp_path / "wiki")
    assert page.dock_goal_refs == []
    assert _frontmatter(page.markdown)["dock_goal_refs"] == []


def test_project_memory_timestamps_passthrough_created_at(tmp_path):
    rec = _record(created_at=_ts(7))
    page = project_memory(rec, wiki_root=tmp_path / "wiki")
    assert page.created_at == _ts(7)
    assert page.updated_at == _ts(7)         # ruling: no wall-clock; pass through
    fm = _frontmatter(page.markdown)
    assert fm["created_at"] == _ts(7)
    assert fm["updated_at"] == _ts(7)


def test_project_memory_lands_in_memory_graduated_dir(tmp_path):
    wiki = tmp_path / "wiki"
    page = project_memory(_record(), wiki_root=wiki)
    assert page.path.parent == wiki / "pages" / "memory_graduated"


def test_project_memory_page_is_index_parseable_and_retrievable(tmp_path):
    wiki = tmp_path / "wiki"
    rec = _record(content="Quantum tunneling initiative status is staging.")
    project_memory(rec, wiki_root=wiki)
    idx = WikiIndex(wiki_root=wiki)
    idx.build_index()
    results = idx.query("quantum tunneling", k=5)
    assert len(results) == 1
    assert results[0].source_type == "memory_graduated"
    assert results[0].confidence == 0.9


# ════════════════════════════════════════════════════════════════════════════
# P3 — GraduationDetector: floor predicate, ranking, cap, staging.
# ════════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def detector(tmp_path):
    return GraduationDetector(base_dir=tmp_path)


def _seed(
    store,
    rid,
    *,
    entity_type="DomainFact",
    confidence=0.9,
    days_old=5,
    access_count=3,
    graduated=False,
    status="active",
    dock_goal_ref=None,
    content=None,
):
    """Append events so the folded record has the wanted shape, then the caller
    rebuilds the index. DomainFact (decay_rate 1.0) keeps confidence stable so
    a test controls it directly; access_count is built from N access events."""
    created = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    store.append_event(MemoryCreated(
        event_id="evt_c" + rid[-7:], timestamp=created, record_id=rid,
        entity_type=entity_type, content=content or f"Fact for {rid}",
        confidence=confidence, dock_goal_ref=dock_goal_ref,
        sources=[{"session_id": "s1", "turn_id": "t1"}], supersedes=None,
    ))
    for i in range(access_count):
        store.append_event(MemoryAccessed(
            event_id=f"evt_a{rid[-4:]}{i}", timestamp=created, record_id=rid,
            session_id="s", context="probe",
        ))
    if graduated:
        store.append_event(MemoryGraduated(
            event_id="evt_g" + rid[-7:], timestamp=created, record_id=rid))
    if status == "deprecated":
        store.append_event(MemoryDeprecated(
            event_id="evt_d" + rid[-7:], timestamp=created, record_id=rid,
            reason="retired"))


def test_eligible_record_is_proposed(store, detector):
    _seed(store, "mem_eligible1", confidence=0.9, access_count=4, days_old=5)
    store.rebuild_index()
    proposals = detector.detect(store)
    assert [p["target_id"] for p in proposals] == ["mem_eligible1"]


def test_low_confidence_filtered(store, detector):
    _seed(store, "mem_lowconf", confidence=0.5, access_count=4, days_old=5)
    store.rebuild_index()
    assert detector.detect(store) == []


def test_low_access_count_filtered(store, detector):
    _seed(store, "mem_lowacc", confidence=0.9,
          access_count=_MIN_ACCESS_COUNT - 1, days_old=5)
    store.rebuild_index()
    assert detector.detect(store) == []


def test_too_young_filtered(store, detector):
    _seed(store, "mem_young", confidence=0.9, access_count=4, days_old=1)
    store.rebuild_index()
    assert detector.detect(store) == []


def test_already_graduated_filtered(store, detector):
    _seed(store, "mem_grad", confidence=0.9, access_count=4, days_old=5,
          graduated=True)
    store.rebuild_index()
    assert detector.detect(store) == []


def test_non_active_status_filtered(store, detector):
    _seed(store, "mem_dep", confidence=0.9, access_count=4, days_old=5,
          status="deprecated")
    store.rebuild_index()
    # The deprecated record still lives in projected_records (all statuses),
    # so this proves the floor's status predicate, not index suppression.
    assert store.projected_records()["mem_dep"].status == "deprecated"
    assert detector.detect(store) == []


def test_floor_reads_post_decay_confidence(store, detector):
    """GUARD P3-b: a ProjectState that decays below _MIN_CONFIDENCE (but stays
    above the deprecation floor) is excluded — proving the floor reads the
    POST-decay confidence the freshness sweep wrote, not the pre-decay 0.9."""
    _seed(store, "mem_decayer", entity_type="ProjectState", confidence=0.9,
          access_count=5, days_old=10)
    store.rebuild_index()
    store.apply_decay()  # 0.9 * 0.95**10 ≈ 0.539 — below 0.80, above 0.2
    decayed = store.projected_records()["mem_decayer"].confidence
    assert _DEPRECATION_FLOOR < decayed < 0.80
    assert detector.detect(store) == []


def test_top_n_cap_enforced(store, detector):
    for i in range(_MAX_PROPOSALS + 2):
        _seed(store, f"mem_cap{i:04d}", confidence=0.9, access_count=4, days_old=5)
    store.rebuild_index()
    proposals = detector.detect(store)
    assert len(proposals) == _MAX_PROPOSALS


def test_ranking_confidence_then_access_count(store, detector):
    _seed(store, "mem_b_hi_acc", confidence=0.90, access_count=9, days_old=5)
    _seed(store, "mem_a_top", confidence=0.95, access_count=3, days_old=5)
    _seed(store, "mem_c_lo_acc", confidence=0.90, access_count=4, days_old=5)
    store.rebuild_index()
    proposals = detector.detect(store)
    # confidence DESC first (a_top 0.95), then access_count DESC tiebreak
    # among the 0.90 pair (b_hi_acc 9 before c_lo_acc 4).
    assert [p["target_id"] for p in proposals] == [
        "mem_a_top", "mem_b_hi_acc", "mem_c_lo_acc",
    ]


def test_empty_store_no_proposals(store, detector):
    assert detector.detect(store) == []


def test_runs_without_dock(store, detector):
    _seed(store, "mem_nodock", confidence=0.9, access_count=3, days_old=5)
    store.rebuild_index()
    proposals = detector.detect(store, dock=None)
    assert [p["target_id"] for p in proposals] == ["mem_nodock"]


def test_proposal_shape_is_single_locked_definition(store, detector):
    """GUARD P3-a: the staged proposal carries EXACTLY the locked key set."""
    _seed(store, "mem_shape001", entity_type="DomainFact", confidence=0.91,
          access_count=4, days_old=5, content="Shape fact.")
    store.rebuild_index()
    (prop,) = detector.detect(store)
    assert set(prop.keys()) == {
        "action", "target_id", "content", "entity_type",
        "confidence", "access_count",
    }
    assert prop["action"] == "graduate"
    assert prop["target_id"] == "mem_shape001"
    assert prop["content"] == "Shape fact."
    assert prop["entity_type"] == "DomainFact"
    assert prop["confidence"] == 0.91
    assert prop["access_count"] == 4


def test_stage_proposals_writes_pending_record(store, detector):
    _seed(store, "mem_w001", confidence=0.9, access_count=3, days_old=5)
    store.rebuild_index()
    staged = detector.stage_proposals(store)
    assert staged == 1

    lines = detector.proposals_path.read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["status"] == "pending"
    assert rec["proposal"]["action"] == "graduate"
    assert rec["proposal"]["target_id"] == "mem_w001"
    assert "session_id" in rec and "timestamp" in rec


def test_pending_graduate_target_not_restaged(store, detector):
    _seed(store, "mem_dedup001", confidence=0.9, access_count=4, days_old=5)
    store.rebuild_index()
    assert detector.stage_proposals(store) == 1
    # Second sweep: the target already has a pending graduate proposal.
    assert detector.stage_proposals(store) == 0


# ════════════════════════════════════════════════════════════════════════════
# P4 — graduate branch in digest.apply + renderer branches + dispatcher wiring.
# ════════════════════════════════════════════════════════════════════════════


_GRADUATE_PROPOSAL = {
    "action": "graduate",
    "target_id": "mem_render01",
    "content": "Grove memory is event-sourced.",
    "entity_type": "DomainFact",
    "confidence": 0.88,
    "access_count": 5,
}


def test_summary_renderer_graduate_no_keyerror_and_voice():
    # Ruling (c): the operator one-liner must NOT KeyError on a graduate
    # proposal (it has no proposed_record) and must speak graduation voice.
    summary = MemoryProposalHandler.summary_renderer(_GRADUATE_PROPOSAL)
    assert isinstance(summary, str) and summary
    assert "Grove memory is event-sourced." in summary
    low = summary.lower()
    assert "graduate" in low or "permanent" in low or "cellar" in low


def test_push_body_graduate_voice_not_create_voice():
    renderable = MemoryProposalRenderable(
        {"status": "pending", "proposal": _GRADUATE_PROPOSAL}
    )
    body = renderable.push_body("the core clause").lower()
    assert "cellar" in body or "permanent" in body
    assert "crystallized a domain insight" not in body   # not the create voice
    assert "retire" not in body                           # not the deprecate voice


def test_sort_key_reads_carried_confidence_for_graduate():
    renderable = MemoryProposalRenderable(
        {"status": "pending", "proposal": _GRADUATE_PROPOSAL}
    )
    assert renderable.sort_key == -0.88   # from top-level confidence, not 0.0


def test_apply_graduate_missing_target_raises(store):
    handler = MemoryProposalHandler(store)
    with pytest.raises(ValueError, match="missing target_id"):
        handler.apply({"action": "graduate"})


def test_apply_graduate_unknown_target_raises(store):
    handler = MemoryProposalHandler(store)
    with pytest.raises(ValueError, match="not in the memory index"):
        handler.apply({"action": "graduate", "target_id": "mem_ghost001"})


def test_dispatcher_sweep_wires_graduation_detector(monkeypatch, tmp_path):
    """GUARD P4-b: the dormant-session sweep instantiates GraduationDetector
    and calls stage_proposals(store, dock=dock_goals), after the freshness
    block. Other sweep stages are neutralized to keep the test hermetic."""
    from types import SimpleNamespace

    from grove.dispatcher import Dispatcher

    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setattr(
        "grove.memory.lifecycle.run_memory_extraction", lambda **k: None
    )
    monkeypatch.setattr(
        "grove.memory.lifecycle.load_active_dock_goal_dicts",
        lambda: [{"slug": "grow-fleet"}],
    )
    monkeypatch.setattr(
        "grove.memory.freshness.FreshnessDetector.detect", lambda self, s, g: []
    )
    monkeypatch.setattr(
        "grove.eval.consolidation_ratchet.ConsolidationRatchet.detect",
        lambda self, *a: [],
    )
    monkeypatch.setattr(
        "grove.dock.detector.DockMutationDetector.detect", lambda self, *a: []
    )

    seen = {}

    def _spy(self, store, dock=None, **k):
        seen["called"] = True
        seen["dock"] = dock
        return 0

    monkeypatch.setattr(
        "grove.memory.graduation.GraduationDetector.stage_proposals", _spy
    )

    fake = SimpleNamespace(
        session=SimpleNamespace(get_messages_as_conversation=lambda sid: [])
    )
    Dispatcher._extract_memory_from_dormant_sessions(fake, [])

    assert seen.get("called") is True
    assert seen["dock"] == [{"slug": "grow-fleet"}]


def test_graduation_end_to_end_closed_loop(store, detector, tmp_path, monkeypatch):
    """K4 closed-loop proof — graduation writes the cellar page AND suppresses
    the record from the JSONL/query path, plus idempotent re-graduation."""
    wiki = tmp_path / "wiki"
    monkeypatch.setattr("grove.wiki.pipeline.get_wiki_path", lambda: wiki)

    # Create an eligible record via MemoryCreated (+ access) events.
    _seed(store, "mem_e2e0001", entity_type="DomainFact", confidence=0.92,
          access_count=4, days_old=5,
          content="Grove uses an event-sourced memory substrate.")
    store.rebuild_index()

    # Detector stages a graduate proposal; pull it back out.
    assert detector.stage_proposals(store) == 1
    staged = [
        json.loads(line)
        for line in detector.proposals_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    proposal = next(
        r["proposal"] for r in staged
        if r["proposal"].get("target_id") == "mem_e2e0001"
    )

    # Approve → MemoryProposalHandler.apply executes.
    handler = MemoryProposalHandler(store)
    assert handler.apply(proposal) is True

    rec = store.projected_records()["mem_e2e0001"]

    # (1) MemoryGraduated event in the log.
    assert any(
        type(ev).__name__ == "MemoryGraduated" and ev.record_id == "mem_e2e0001"
        for ev in store.read_events()
    )
    # (2) graduated_at set.
    assert rec.graduated_at is not None
    # (3) status flipped to "graduated" — K4 dual-serve closure.
    assert rec.status == "graduated"
    # (4) cellar page with correct frontmatter + verbatim content.
    h = _hash("memory#mem_e2e0001")
    pages = list((wiki / "pages" / "memory_graduated").glob(f"*-{h}.md"))
    assert len(pages) == 1
    text = pages[0].read_text(encoding="utf-8")
    fm = _frontmatter(text)
    assert fm["source"] == "memory#mem_e2e0001"
    assert fm["source_type"] == "memory_graduated"
    assert fm["status"] == "graduated"
    assert "Grove uses an event-sourced memory substrate." in text
    # (5) record SUPPRESSED from query() — served by the cellar, not the JSONL path.
    assert "mem_e2e0001" not in [r.id for r in store.query(keywords=["event-sourced"])]

    # Idempotent: a re-graduation attempt is rejected at the write boundary.
    with pytest.raises(ValueError, match="already graduated"):
        handler.apply(proposal)


# ════════════════════════════════════════════════════════════════════════════
# P3 — dual-serve closure (status flip) + supersession reap.
# ════════════════════════════════════════════════════════════════════════════


def test_status_flip_suppresses_all_consumers(store, tmp_path):
    """GUARD P3-a: a graduated record is suppressed by ALL four consumers —
    query(), the saved index cache, FreshnessDetector, GraduationDetector."""
    from grove.memory.freshness import FreshnessDetector

    _seed(store, "mem_flip0001", entity_type="ProjectState", confidence=0.9,
          access_count=4, days_old=5, content="Flip suppression fact.")
    store.append_event(MemoryGraduated(
        event_id="evt_flip01", timestamp=_ts(6), record_id="mem_flip0001"))
    store.rebuild_index()

    assert store.projected_records()["mem_flip0001"].status == "graduated"
    # 1. query() suppresses (store.py:270).
    assert "mem_flip0001" not in [r.id for r in store.query(keywords=["flip"])]
    # 2. _save_index drops it from the on-disk cache (store.py:403-408).
    saved = json.loads(store.index_path.read_text(encoding="utf-8"))
    assert "mem_flip0001" not in saved
    # 3. FreshnessDetector does not propose it (skips non-active).
    fresh = FreshnessDetector(base_dir=tmp_path)
    assert "mem_flip0001" not in [
        p["target_id"] for p in fresh.detect(store, [])
    ]
    # 4. GraduationDetector does not re-propose it.
    grad = GraduationDetector(base_dir=tmp_path)
    assert "mem_flip0001" not in [p["target_id"] for p in grad.detect(store)]


def _graduate(store, rid):
    """Stage+approve a graduation for an already-eligible seeded record."""
    handler = MemoryProposalHandler(store)
    handler.apply({
        "action": "graduate", "target_id": rid, "content": "x",
        "entity_type": "DomainFact", "confidence": 0.9, "access_count": 4,
    })


def test_supersede_graduated_record_reaps_cellar_page(store, tmp_path, monkeypatch):
    """GUARD P3-b: superseding a graduated record deletes its cellar page; the
    graduated_at is captured pre-rebuild so the reap fires even though the
    record's post-rebuild status is 'superseded'."""
    wiki = tmp_path / "wiki"
    monkeypatch.setattr("grove.wiki.pipeline.get_wiki_path", lambda: wiki)

    _seed(store, "mem_reap0001", confidence=0.9, access_count=4, days_old=5,
          content="Reap me when superseded.")
    store.rebuild_index()
    _graduate(store, "mem_reap0001")

    h = _hash("memory#mem_reap0001")
    pages_dir = wiki / "pages" / "memory_graduated"
    assert list(pages_dir.glob(f"*-{h}.md"))           # page exists post-graduation

    # Supersede the graduated record.
    handler = MemoryProposalHandler(store)
    handler.apply({
        "action": "supersede", "target_id": "mem_reap0001",
        "proposed_record": {
            "entity_type": "DomainFact", "content": "Newer fact.",
            "confidence": 0.95,
        },
    })

    # Cellar page reaped; the old record is now superseded (proving the
    # graduated_at capture happened pre-rebuild, not via a status==active gate).
    assert list(pages_dir.glob(f"*-{h}.md")) == []
    assert store.projected_records()["mem_reap0001"].status == "superseded"


def test_supersede_non_graduated_record_no_page_action(store, tmp_path, monkeypatch):
    """A supersede of a never-graduated record performs no page reap and does
    not error (no memory_graduated dir need exist)."""
    wiki = tmp_path / "wiki"
    monkeypatch.setattr("grove.wiki.pipeline.get_wiki_path", lambda: wiki)

    _seed(store, "mem_plain001", confidence=0.9, access_count=4, days_old=5,
          content="Never graduated.")
    store.rebuild_index()

    handler = MemoryProposalHandler(store)
    assert handler.apply({
        "action": "supersede", "target_id": "mem_plain001",
        "proposed_record": {
            "entity_type": "DomainFact", "content": "Replacement.",
            "confidence": 0.9,
        },
    }) is True
    assert store.projected_records()["mem_plain001"].status == "superseded"
    # No cellar page was ever written, and none is created by the reap path.
    assert not (wiki / "pages" / "memory_graduated").exists() or \
        list((wiki / "pages" / "memory_graduated").glob("*.md")) == []

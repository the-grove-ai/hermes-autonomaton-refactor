"""Tests for grove.wiki.session_compactor — Sprint K5 (session-compaction-v1).

The fifth cellar producer compacts a dormant session's filtered transcript
into one canonical wiki page via the existing Writer→Evaluator→Editor pipeline.
No real T1 calls here: ``compact()`` is mocked at its source module so the
pipeline never runs, and the unit functions (serialize, ends-middle cap,
dominant-goal) are pure. The integration test drives the Dispatcher's dormancy
sweep with every heavy collaborator stubbed and asserts the sweep reaches
``compact_session``.
"""

from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace

from grove.dispatcher import Dispatcher
from grove.wiki.pipeline import CanonicalPage
from grove.wiki.session_compactor import (
    compact_session,
    dominant_dock_goal,
    ends_middle_cap,
    serialize_transcript,
)


# ── fakes ───────────────────────────────────────────────────────────────


class _FakeIntentStore:
    """Returns a fixed record list from ``filter(session_id=...)``."""

    def __init__(self, records):
        self._records = records

    def filter(self, *, session_id=None, **_kw):
        return self._records


def _good_transcript():
    """A filtered transcript that clears the ≥3-operator-message gate (D8)."""
    return [
        {"role": "user", "content": "first question about the deploy"},
        {"role": "assistant", "content": "here is the answer"},
        {"role": "user", "content": "a follow-up"},
        {"role": "user", "content": "a third operator message"},
    ]


def _install_fake_compact(monkeypatch):
    """Replace pipeline.compact with a capturing fake; no T1 calls. Returns a
    dict the test reads to inspect the NormalizedDoc the compactor built."""
    captured: dict = {}

    def fake_compact(doc, *, wiki_root=None):
        captured["doc"] = doc
        captured["wiki_root"] = wiki_root
        return CanonicalPage(
            source=doc.source_path,
            source_type=doc.source_type,
            title="Compacted Session",
            topics=[],
            key_entities=[],
            dock_goal_refs=list(doc.dock_goal_refs),
            confidence=0.9,
            created_at="2026-06-26T00:00:00+00:00",
            updated_at="2026-06-26T00:00:00+00:00",
            body="body",
            path=tmp_path_marker(wiki_root),
            markdown="md",
            editor_ran=False,
            evaluator_verdict={},
        )

    monkeypatch.setattr("grove.wiki.pipeline.compact", fake_compact)
    return captured


def tmp_path_marker(wiki_root):
    from pathlib import Path

    return Path(wiki_root or "/dev/null") / "pages" / "session_compacted" / "x.md"


# ── serialize_transcript (D3) ───────────────────────────────────────────


def test_basic_serialization():
    transcript = [
        {"role": "user", "content": "What's the deploy script location?"},
        {"role": "assistant", "content": "The script is at /scripts/deploy.sh."},
        {"role": "user", "content": "Thanks."},
    ]
    assert serialize_transcript(transcript) == (
        "[operator] What's the deploy script location?\n"
        "[assistant] The script is at /scripts/deploy.sh.\n"
        "[operator] Thanks."
    )


def test_tool_calls_rendered():
    transcript = [
        {
            "role": "assistant",
            "content": "Running it now.",
            "tool_calls": [
                {"function": {"name": "terminal", "arguments": "{}"}},
                {"function": {"name": "read_file", "arguments": "{}"}},
            ],
        },
    ]
    assert serialize_transcript(transcript) == (
        "[assistant] Running it now.\n[tool: terminal]\n[tool: read_file]"
    )


def test_system_messages_skipped():
    transcript = [
        {"role": "system", "content": "You are a governed Autonomaton."},
        {"role": "user", "content": "hello"},
    ]
    out = serialize_transcript(transcript)
    assert "governed Autonomaton" not in out
    assert out == "[operator] hello"


def test_empty_content_skipped():
    transcript = [
        {"role": "user", "content": "   "},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "real content"},
    ]
    assert serialize_transcript(transcript) == "[operator] real content"


def test_multimodal_content():
    transcript = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look at this"},
                {"type": "image", "source": {"data": "BASE64..."}},
                {"type": "text", "text": "and this"},
            ],
        },
    ]
    assert serialize_transcript(transcript) == "[operator] look at this\nand this"


# ── ends_middle_cap (D4) ────────────────────────────────────────────────


def test_under_budget_unchanged():
    text = "[operator] short question\n[assistant] short reply"
    assert ends_middle_cap(text, budget=10_000) == text


def test_over_budget_ends_middle():
    turns = [f"[operator] turn{i}_ " + "x" * 50 for i in range(20)]
    text = "\n".join(turns)
    out = ends_middle_cap(text, budget=400, head_turns=3)
    # first three turns (head) survive
    assert "turn0_" in out and "turn1_" in out and "turn2_" in out
    # the final turn (tail) survives
    assert "turn19_" in out
    # a middle turn is elided
    assert "turn10_" not in out
    # the elision marker is present
    assert "turns elided" in out


def test_elision_marker_format():
    turns = [f"[operator] turn{i}_ " + "x" * 50 for i in range(20)]
    out = ends_middle_cap("\n".join(turns), budget=400, head_turns=3)
    match = re.search(r"\n\n\[\.\.\. (\d+) turns elided \.\.\.\]\n\n", out)
    assert match is not None
    elided = int(match.group(1))
    kept = sum(1 for i in range(20) if f"turn{i}_" in out)
    assert elided == 20 - kept


def test_degenerate_head_only():
    # Three head turns whose joined length already exceeds the budget.
    turns = [f"[operator] turn{i}_ " + "x" * 100 for i in range(5)]
    text = "\n".join(turns)
    out = ends_middle_cap(text, budget=50, head_turns=3)
    assert len(out) == 50
    assert out == text[:50]


# ── dominant_dock_goal (D6 + A2) ────────────────────────────────────────


def test_single_goal():
    records = [SimpleNamespace(goal_alignment="grow-fleet") for _ in range(3)]
    assert dominant_dock_goal(_FakeIntentStore(records), "s") == ["grow-fleet"]


def test_multiple_goals_picks_dominant():
    records = [
        SimpleNamespace(goal_alignment="alpha"),
        SimpleNamespace(goal_alignment="alpha"),
        SimpleNamespace(goal_alignment="beta"),
    ]
    assert dominant_dock_goal(_FakeIntentStore(records), "s") == ["alpha"]


def test_no_goals():
    records = [
        SimpleNamespace(goal_alignment=None),
        SimpleNamespace(goal_alignment="   "),
    ]
    assert dominant_dock_goal(_FakeIntentStore(records), "s") == []


def test_store_error_returns_empty():
    class _Boom:
        def filter(self, **_kw):
            raise RuntimeError("intent store down")

    assert dominant_dock_goal(_Boom(), "s") == []


# ── compact_session (gate / idempotency / build) ────────────────────────


def test_below_complexity_gate_returns_none(monkeypatch, tmp_path):
    captured = _install_fake_compact(monkeypatch)
    trivial = [{"role": "user", "content": "hi"}]
    out = compact_session(
        "s1", trivial, _FakeIntentStore([]), 1.0, wiki_root=tmp_path / "wiki"
    )
    assert out is None
    assert "doc" not in captured  # compact (and its T1 calls) never reached


def test_idempotency_skip(monkeypatch, tmp_path):
    captured = _install_fake_compact(monkeypatch)
    sid = "sess-xyz"
    short_hash = hashlib.sha256(f"session#{sid}".encode()).hexdigest()[:8]
    out_dir = tmp_path / "wiki" / "pages" / "session_compacted"
    out_dir.mkdir(parents=True)
    (out_dir / f"prior-title-{short_hash}.md").write_text("existing", encoding="utf-8")

    out = compact_session(
        sid, _good_transcript(), _FakeIntentStore([]), 1.0,
        wiki_root=tmp_path / "wiki",
    )
    assert out is None
    assert "doc" not in captured  # existing page short-circuits before compact


def test_successful_compaction(monkeypatch, tmp_path):
    captured = _install_fake_compact(monkeypatch)
    page = compact_session(
        "sess-1", _good_transcript(), _FakeIntentStore([]), 123.0,
        wiki_root=tmp_path / "wiki",
    )
    assert isinstance(page, CanonicalPage)
    assert page.source_type == "session_compacted"
    assert captured["doc"].source_mtime == 123.0


def test_source_path_format(monkeypatch, tmp_path):
    captured = _install_fake_compact(monkeypatch)
    compact_session(
        "abc-123", _good_transcript(), _FakeIntentStore([]), 1.0,
        wiki_root=tmp_path / "wiki",
    )
    assert captured["doc"].source_path == "session#abc-123"
    assert captured["doc"].source_type == "session_compacted"


# ── Dispatcher integration ──────────────────────────────────────────────


class _NoArg:
    """Construct-and-no-op stub for the sweep's heavy collaborators."""

    def __init__(self, *_a, **_k):
        pass

    def detect(self, *_a, **_k):
        return []

    def stage_proposals(self, *_a, **_k):
        return None


class _FakeSession:
    def __init__(self, transcript):
        self._transcript = transcript

    def get_messages_as_conversation(self, _sid):
        return self._transcript


def _stub_sweep_collaborators(monkeypatch, tmp_path):
    """No-op every subsystem the dormancy sweep runs BEFORE session compaction,
    so the integration test exercises only the new block (and makes no T1
    calls). Each is patched at the source module the method imports from."""
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr("grove.memory.store.MemoryStore", _NoArg)
    monkeypatch.setattr("grove.memory.detector.ContextPersistenceDetector", _NoArg)
    monkeypatch.setattr(
        "grove.memory.lifecycle.load_active_dock_goal_dicts", lambda: []
    )
    monkeypatch.setattr(
        "grove.memory.lifecycle.run_memory_extraction", lambda **_k: None
    )
    monkeypatch.setattr("grove.memory.freshness.FreshnessDetector", _NoArg)
    monkeypatch.setattr("grove.memory.graduation.GraduationDetector", _NoArg)
    monkeypatch.setattr("grove.eval.consolidation_ratchet.ConsolidationRatchet", _NoArg)
    monkeypatch.setattr("grove.dock.detector.DockMutationDetector", _NoArg)


def test_dispatcher_fires_session_compaction(monkeypatch, tmp_path):
    _stub_sweep_collaborators(monkeypatch, tmp_path)

    calls: list = []

    def _recorder(sid, filtered, store, source_mtime, *, wiki_root=None):
        calls.append((sid, filtered, source_mtime))
        return None  # mock the pipeline — no real compaction / T1 calls

    monkeypatch.setattr(
        "grove.wiki.session_compactor.compact_session", _recorder
    )

    disp = Dispatcher.__new__(Dispatcher)
    disp.session = _FakeSession(_good_transcript())
    disp._intent_store = _FakeIntentStore(
        [SimpleNamespace(
            timestamp="2026-06-26T10:00:00+00:00", goal_alignment="grow-fleet"
        )]
    )

    disp._extract_memory_from_dormant_sessions(["sess-1"])

    assert len(calls) == 1
    sid, _filtered, source_mtime = calls[0]
    assert sid == "sess-1"
    assert isinstance(source_mtime, float) and source_mtime > 0

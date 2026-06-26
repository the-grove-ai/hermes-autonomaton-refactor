"""Tests for grove.wiki.pipeline — the three-call compaction pipeline.

Sprint K1 (living-cellar-v1) Phase 4. compact() runs Writer (plain text),
Evaluator (forced tool_use), and a CONDITIONAL single Editor pass — never more
than three T1 calls, never a re-evaluation loop. Deterministic fields are
pipeline-injected (the LLM cannot author source/source_type/timestamps/
confidence); confidence is the Evaluator's quality_score. Unparseable Writer
output fails loud. The pipeline never writes a source file (A6).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml

from grove.wiki.adapters import NormalizedDoc
from grove.wiki.index import WikiIndex
from grove.wiki.pipeline import CanonicalPage, MalformedWriterOutput, compact


# ── fakes ───────────────────────────────────────────────────────────────


class _FakeT1:
    """Routes by call shape: tool given → Evaluator verdict; else the Nth
    plain-text call is Writer (1) then Editor (2)."""

    def __init__(self, writer, verdict, editor=None):
        self.writer = writer
        self.verdict = verdict
        self.editor = editor
        self.calls: list = []
        self._text_n = 0

    def __call__(self, prompt, *, system=None, tool=None, max_tokens=4096):
        if tool is not None:
            self.calls.append(("tool", tool["name"]))
            return self.verdict
        self._text_n += 1
        self.calls.append(("text", None))
        return self.writer if self._text_n == 1 else self.editor

    @property
    def tool_calls(self):
        return [c for c in self.calls if c[0] == "tool"]

    @property
    def text_calls(self):
        return [c for c in self.calls if c[0] == "text"]


def _writer_md(title="The Moat Moved", topics=None, key_entities=None,
               body="Canonical body text.", extra_fm=None):
    fm = {
        "title": title,
        "topics": topics if topics is not None else ["moats", "orchestration"],
        "key_entities": key_entities if key_entities is not None else ["OpenRouter"],
    }
    if extra_fm:
        fm.update(extra_fm)
    return "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body + "\n"


def _verdict(complete=True, accurate=True, quality_score=0.9, issues=None):
    return {
        "complete": complete,
        "accurate": accurate,
        "quality_score": quality_score,
        "issues": issues or [],
    }


def _doc(tmp_path, source_type="researcher_brief", raw="raw source content"):
    src = tmp_path / "sink" / "brief-2026-06-25-x.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(raw, encoding="utf-8")
    return NormalizedDoc(
        source_type=source_type,
        source_path=str(src),
        source_mtime=src.stat().st_mtime,
        dock_goal_refs=["grow-fleet"],
        raw_content=raw,
    )


def _install(monkeypatch, fake):
    monkeypatch.setattr("grove.wiki.pipeline.call_t1", fake)
    return fake


# ── Writer happy path / Evaluator pass ──────────────────────────────────


def test_writer_pass_no_editor(monkeypatch, tmp_path):
    fake = _install(monkeypatch, _FakeT1(_writer_md(), _verdict(quality_score=0.9)))
    page = compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")
    assert isinstance(page, CanonicalPage)
    assert page.title == "The Moat Moved"
    assert page.editor_ran is False
    # exactly Writer + Evaluator, no Editor
    assert len(fake.text_calls) == 1
    assert len(fake.tool_calls) == 1


def test_writer_owns_semantic_fields(monkeypatch, tmp_path):
    fake = _install(
        monkeypatch,
        _FakeT1(
            _writer_md(topics=["a", "b"], key_entities=["X", "Y"]),
            _verdict(),
        ),
    )
    page = compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")
    assert page.topics == ["a", "b"]
    assert page.key_entities == ["X", "Y"]
    assert page.body.strip() == "Canonical body text."


# ── Evaluator fail → one Editor pass, no re-eval ────────────────────────


def test_evaluator_fail_runs_editor_once(monkeypatch, tmp_path):
    fake = _install(
        monkeypatch,
        _FakeT1(
            _writer_md(body="weak draft"),
            _verdict(complete=False, quality_score=0.2, issues=["thin"]),
            editor=_writer_md(body="revised stronger draft"),
        ),
    )
    page = compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")
    assert page.editor_ran is True
    assert "revised stronger draft" in page.body
    # bounded: Writer + Editor = 2 text calls; exactly ONE evaluation (no re-eval)
    assert len(fake.text_calls) == 2
    assert len(fake.tool_calls) == 1
    assert len(fake.calls) == 3  # never more than three calls


def test_threshold_is_named_constant(monkeypatch, tmp_path):
    import grove.wiki.pipeline as pipe

    assert isinstance(pipe.QUALITY_THRESHOLD, float)
    # a score just under threshold fails (Editor runs); at/above passes
    below = pipe.QUALITY_THRESHOLD - 0.01
    fake = _install(
        monkeypatch,
        _FakeT1(_writer_md(), _verdict(quality_score=below), editor=_writer_md()),
    )
    page = compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")
    assert page.editor_ran is True


# ── deterministic fields (LLM cannot override) ──────────────────────────


def test_deterministic_fields_injected_not_from_writer(monkeypatch, tmp_path):
    # Writer tries to smuggle source/confidence/timestamps — all ignored.
    fake = _install(
        monkeypatch,
        _FakeT1(
            _writer_md(extra_fm={
                "source": "EVIL",
                "source_type": "EVIL",
                "confidence": 0.01,
                "created_at": "1999-01-01",
                "updated_at": "1999-01-01",
            }),
            _verdict(quality_score=0.77),
        ),
    )
    doc = _doc(tmp_path)
    page = compact(doc, wiki_root=tmp_path / "wiki")
    assert page.source == doc.source_path
    assert page.source_type == "researcher_brief"
    assert page.confidence == 0.77            # from evaluator, not writer
    assert page.source != "EVIL"
    expected_created = datetime.fromtimestamp(
        doc.source_mtime, tz=timezone.utc
    ).isoformat()
    assert page.created_at == expected_created
    assert page.dock_goal_refs == ["grow-fleet"]


def test_confidence_equals_quality_score(monkeypatch, tmp_path):
    _install(monkeypatch, _FakeT1(_writer_md(), _verdict(quality_score=0.82)))
    page = compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")
    assert page.confidence == 0.82


# ── fail loud on bad Writer output ──────────────────────────────────────


def test_fail_loud_on_unparseable_writer_output(monkeypatch, tmp_path):
    _install(monkeypatch, _FakeT1("no frontmatter at all", _verdict()))
    with pytest.raises(MalformedWriterOutput):
        compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")


def test_fail_loud_on_writer_missing_title(monkeypatch, tmp_path):
    bad = "---\ntopics: [a]\nkey_entities: [b]\n---\n\nbody\n"
    _install(monkeypatch, _FakeT1(bad, _verdict()))
    with pytest.raises(MalformedWriterOutput):
        compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")


def test_fail_loud_on_editor_unparseable_output(monkeypatch, tmp_path):
    # Editor output is the last word, but it must still be a valid page.
    _install(
        monkeypatch,
        _FakeT1(_writer_md(), _verdict(quality_score=0.1), editor="garbage"),
    )
    with pytest.raises(MalformedWriterOutput):
        compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")


# ── write location / idempotency / A6 ───────────────────────────────────


def test_page_written_under_source_type_dir(monkeypatch, tmp_path):
    _install(monkeypatch, _FakeT1(_writer_md(), _verdict()))
    wiki = tmp_path / "wiki"
    page = compact(_doc(tmp_path), wiki_root=wiki)
    assert page.path.parent == wiki / "pages" / "researcher_brief"
    assert page.path.suffix == ".md"
    assert page.path.exists()


def test_idempotent_filename_per_source(monkeypatch, tmp_path):
    wiki = tmp_path / "wiki"
    doc = _doc(tmp_path)
    _install(monkeypatch, _FakeT1(_writer_md(), _verdict()))
    p1 = compact(doc, wiki_root=wiki).path
    _install(monkeypatch, _FakeT1(_writer_md(), _verdict()))
    p2 = compact(doc, wiki_root=wiki).path
    assert p1 == p2
    files = list((wiki / "pages" / "researcher_brief").glob("*.md"))
    assert len(files) == 1


def test_reingest_title_drift_leaves_single_page(monkeypatch, tmp_path):
    wiki = tmp_path / "wiki"
    doc = _doc(tmp_path)
    _install(monkeypatch, _FakeT1(_writer_md(title="First Title"), _verdict()))
    compact(doc, wiki_root=wiki)
    _install(monkeypatch, _FakeT1(_writer_md(title="Totally Different Title"), _verdict()))
    compact(doc, wiki_root=wiki)
    files = list((wiki / "pages" / "researcher_brief").glob("*.md"))
    assert len(files) == 1  # source-stable hash → no orphan on title drift


def test_never_writes_source_file(monkeypatch, tmp_path):
    _install(monkeypatch, _FakeT1(_writer_md(), _verdict()))
    doc = _doc(tmp_path)
    before = (doc.source_path, open(doc.source_path).read(),
              __import__("os").stat(doc.source_path).st_mtime)
    compact(doc, wiki_root=tmp_path / "wiki")
    after_content = open(doc.source_path).read()
    after_mtime = __import__("os").stat(doc.source_path).st_mtime
    assert after_content == before[1]
    assert after_mtime == before[2]


def test_written_page_is_index_parseable(monkeypatch, tmp_path):
    """Phase 4 output must satisfy the Phase 2 index contract end-to-end."""
    _install(monkeypatch, _FakeT1(
        _writer_md(body="quantum tunneling diodes"), _verdict(quality_score=0.9)))
    wiki = tmp_path / "wiki"
    compact(_doc(tmp_path), wiki_root=wiki)
    idx = WikiIndex(wiki_root=wiki)
    idx.build_index()
    results = idx.query("quantum tunneling", k=5)
    assert len(results) == 1
    assert results[0].source_type == "researcher_brief"
    assert results[0].confidence == 0.9

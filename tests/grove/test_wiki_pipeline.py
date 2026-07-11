"""Tests for grove.wiki.pipeline — the three-call compaction pipeline.

Sprint K1 (living-cellar-v1) Phase 4, migrated by wiki-writer-structured-output-v1
P2. compact() runs Writer (forced wiki_page tool), Evaluator (forced
wiki_evaluation tool), and a CONDITIONAL single Editor pass (forced wiki_page)
— never more than three T1 calls, never a re-evaluation loop. SemanticPage is
built directly from validated tool args; NO parse of prose remains.
_parse_semantic_page is the retained Andon: empty/missing/mistyped fields raise
MalformedWriterOutput exactly as the prose parser did. Deterministic fields are
pipeline-injected (the LLM cannot author source/source_type/timestamps/
confidence); confidence is the Evaluator's quality_score. Cap-hit truncation
(T1TruncationError) gets ONE raised-cap retry, then MalformedWriterOutput
(P0 findings 1+2). The pipeline never writes a source file (A6).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from grove.t1_call import T1TruncationError
from grove.wiki.adapters import NormalizedDoc
from grove.wiki.index import WikiIndex
from grove.wiki.pipeline import CanonicalPage, MalformedWriterOutput, compact


# ── fakes ───────────────────────────────────────────────────────────────


class _FakeT1:
    """Routes by forced-tool NAME (P2: all three calls are tool calls):
    wiki_evaluation → the verdict; wiki_page → the Nth page emission
    (Writer first, then Editor). A scripted entry may be an exception
    instance (raised) — the truncation-ladder pins use T1TruncationError."""

    def __init__(self, writer, verdict, editor=None, extra_pages=None):
        # Page-call script: writer, editor, then any extras (retry pins).
        self.pages = [writer] + ([editor] if editor is not None else [])
        if extra_pages:
            self.pages.extend(extra_pages)
        self.verdict = verdict
        self.calls: list = []
        self._page_n = 0

    def __call__(self, prompt, *, system=None, tool=None, max_tokens=4096):
        assert tool is not None, "P2: every pipeline call is a forced tool call"
        self.calls.append((tool["name"], max_tokens))
        if tool["name"] == "wiki_evaluation":
            return self.verdict
        assert tool["name"] == "wiki_page"
        if self._page_n >= len(self.pages):
            raise AssertionError("unexpected extra wiki_page call")
        result = self.pages[self._page_n]
        self._page_n += 1
        if isinstance(result, BaseException):
            raise result
        return result

    @property
    def page_calls(self):
        return [c for c in self.calls if c[0] == "wiki_page"]

    @property
    def eval_calls(self):
        return [c for c in self.calls if c[0] == "wiki_evaluation"]


def _page_args(title="The Moat Moved", topics=None, key_entities=None,
               body="Canonical body text.\n", extra=None):
    args = {
        "title": title,
        "topics": topics if topics is not None else ["moats", "orchestration"],
        "key_entities": key_entities if key_entities is not None else ["OpenRouter"],
        "body": body,
    }
    if extra:
        args.update(extra)
    return args


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
    fake = _install(monkeypatch, _FakeT1(_page_args(), _verdict(quality_score=0.9)))
    page = compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")
    assert isinstance(page, CanonicalPage)
    assert page.title == "The Moat Moved"
    assert page.editor_ran is False
    # exactly Writer + Evaluator, no Editor
    assert len(fake.page_calls) == 1
    assert len(fake.eval_calls) == 1


def test_writer_owns_semantic_fields(monkeypatch, tmp_path):
    fake = _install(
        monkeypatch,
        _FakeT1(
            _page_args(topics=["a", "b"], key_entities=["X", "Y"]),
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
            _page_args(body="weak draft"),
            _verdict(complete=False, quality_score=0.2, issues=["thin"]),
            editor=_page_args(body="revised stronger draft"),
        ),
    )
    page = compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")
    assert page.editor_ran is True
    assert "revised stronger draft" in page.body
    # bounded: Writer + Editor = 2 page calls; exactly ONE evaluation (no re-eval)
    assert len(fake.page_calls) == 2
    assert len(fake.eval_calls) == 1
    assert len(fake.calls) == 3  # never more than three calls


def test_threshold_is_named_constant(monkeypatch, tmp_path):
    import grove.wiki.pipeline as pipe

    assert isinstance(pipe.QUALITY_THRESHOLD, float)
    # a score just under threshold fails (Editor runs); at/above passes
    below = pipe.QUALITY_THRESHOLD - 0.01
    fake = _install(
        monkeypatch,
        _FakeT1(_page_args(), _verdict(quality_score=below), editor=_page_args()),
    )
    page = compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")
    assert page.editor_ran is True


# ── deterministic fields (LLM cannot override) ──────────────────────────


def test_deterministic_fields_injected_not_from_writer(monkeypatch, tmp_path):
    # Writer tries to smuggle source/confidence/timestamps as EXTRA args —
    # _parse_semantic_page reads only the four semantic fields; all ignored.
    fake = _install(
        monkeypatch,
        _FakeT1(
            _page_args(extra={
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
    _install(monkeypatch, _FakeT1(_page_args(), _verdict(quality_score=0.82)))
    page = compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")
    assert page.confidence == 0.82


# ── arg-validation Andon (MalformedWriterOutput, P2 CHANGE 3) ────────────


def test_fail_loud_on_non_object_writer_args(monkeypatch, tmp_path):
    # call_t1 contract violation (args not a dict) still Andons here.
    _install(monkeypatch, _FakeT1("not an object", _verdict()))
    with pytest.raises(MalformedWriterOutput):
        compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")


def test_fail_loud_on_writer_missing_title(monkeypatch, tmp_path):
    bad = _page_args()
    del bad["title"]
    _install(monkeypatch, _FakeT1(bad, _verdict()))
    with pytest.raises(MalformedWriterOutput):
        compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")


def test_fail_loud_on_writer_empty_body(monkeypatch, tmp_path):
    _install(monkeypatch, _FakeT1(_page_args(body="   \n"), _verdict()))
    with pytest.raises(MalformedWriterOutput):
        compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")


def test_fail_loud_on_mistyped_topics(monkeypatch, tmp_path):
    _install(monkeypatch, _FakeT1(_page_args(topics="not-a-list"), _verdict()))
    with pytest.raises(MalformedWriterOutput):
        compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")


def test_fail_loud_on_editor_invalid_args(monkeypatch, tmp_path):
    # Editor output is the last word, but it must still be a valid page.
    _install(
        monkeypatch,
        _FakeT1(_page_args(), _verdict(quality_score=0.1),
                editor={"title": "x", "body": ""}),
    )
    with pytest.raises(MalformedWriterOutput):
        compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")


# ── truncation ladder (P2 CHANGE 4; P0 findings 1+2) ────────────────────


def test_writer_truncation_raised_cap_retry_succeeds(monkeypatch, tmp_path):
    import grove.wiki.pipeline as pipe

    fake = _install(
        monkeypatch,
        _FakeT1(
            T1TruncationError("cap-cut"),
            _verdict(),
            editor=None,
            extra_pages=[_page_args()],
        ),
    )
    page = compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")
    assert page.title == "The Moat Moved"
    # ONE raised-cap retry: second wiki_page call at exactly double the cap.
    assert [mt for name, mt in fake.page_calls] == [
        pipe._WRITER_MAX_TOKENS, 2 * pipe._WRITER_MAX_TOKENS
    ]


def test_writer_double_truncation_is_malformed_andon(monkeypatch, tmp_path):
    fake = _install(
        monkeypatch,
        _FakeT1(
            T1TruncationError("cap-cut"),
            _verdict(),
            editor=None,
            extra_pages=[T1TruncationError("cap-cut again")],
        ),
    )
    with pytest.raises(MalformedWriterOutput, match="raised-cap retry"):
        compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")
    assert len(fake.page_calls) == 2  # bounded: exactly one raise, no loop


def test_editor_truncation_ladder(monkeypatch, tmp_path):
    import grove.wiki.pipeline as pipe

    fake = _install(
        monkeypatch,
        _FakeT1(
            _page_args(body="weak"),
            _verdict(complete=False, quality_score=0.2, issues=["thin"]),
            editor=T1TruncationError("cap-cut"),
            extra_pages=[_page_args(body="revised after edit")],
        ),
    )
    page = compact(_doc(tmp_path), wiki_root=tmp_path / "wiki")
    assert page.editor_ran is True
    assert "revised after edit" in page.body
    # Writer (normal) + Editor (truncated → raised-cap retry) = 3 page calls.
    assert [mt for name, mt in fake.page_calls] == [
        pipe._WRITER_MAX_TOKENS,
        pipe._EDITOR_MAX_TOKENS,
        2 * pipe._EDITOR_MAX_TOKENS,
    ]


# ── prompt contract (P2: no frontmatter-format instructions remain) ─────


def test_prompt_constants_carry_no_frontmatter_instructions():
    import grove.wiki.pipeline as pipe

    for const in (pipe._WRITER_SYSTEM, pipe._EDITOR_SYSTEM):
        low = const.lower()
        for banned in ("frontmatter", "---", "yaml", "code fence", "preamble"):
            assert banned not in low, (banned, const)
    # and both instruct the tool call
    assert "wiki_page" in pipe._WRITER_SYSTEM
    assert "wiki_page" in pipe._EDITOR_SYSTEM


def test_wiki_page_tool_schema_shape():
    import grove.wiki.pipeline as pipe

    schema = pipe._WIKI_PAGE_TOOL["input_schema"]
    assert set(schema["required"]) == {"title", "topics", "key_entities", "body"}
    assert set(schema["properties"]) == {"title", "topics", "key_entities", "body"}


# ── golden structure (canonical page file format is an INVARIANT) ───────


def test_golden_page_structure_matches_pre_p2_pipeline(monkeypatch, tmp_path):
    """The written page is byte-equivalent to the pre-P2 (prose-parse)
    pipeline's output for the same semantic fields — captured golden, with
    the two volatile lines (updated_at wall clock, source tmp path) masked.
    The transport changed; the canonical page file format did not."""
    src = tmp_path / "sink" / "brief-2026-06-25-x.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("raw source content", encoding="utf-8")
    os.utime(src, (1750000000, 1750000000))  # pin mtime → created_at deterministic
    doc = NormalizedDoc(
        source_type="researcher_brief",
        source_path=str(src),
        source_mtime=src.stat().st_mtime,
        dock_goal_refs=["grow-fleet"],
        raw_content="raw source content",
    )
    _install(monkeypatch, _FakeT1(_page_args(), _verdict(quality_score=0.9)))
    page = compact(doc, wiki_root=tmp_path / "wiki")

    masked = []
    for line in page.markdown.splitlines():
        if line.startswith("updated_at:"):
            line = "updated_at: <MASKED>"
        if line.startswith("source:"):
            line = "source: <MASKED-PATH>"
        masked.append(line)
    golden = [
        "---",
        "title: The Moat Moved",
        "source_type: researcher_brief",
        "source: <MASKED-PATH>",
        "created_at: '2025-06-15T15:06:40+00:00'",
        "updated_at: <MASKED>",
        "confidence: 0.9",
        "dock_goal_refs:",
        "- grow-fleet",
        "topics:",
        "- moats",
        "- orchestration",
        "key_entities:",
        "- OpenRouter",
        "---",
        "",
        "Canonical body text.",
    ]
    assert masked == golden
    # filename: slug + 8-hex source-PATH-stable hash (varies with tmp_path —
    # pin the shape, not the digest)
    import re as _re

    assert _re.fullmatch(r"the-moat-moved-[0-9a-f]{8}\.md", page.path.name)


# ── write location / idempotency / A6 ───────────────────────────────────


def test_page_written_under_source_type_dir(monkeypatch, tmp_path):
    _install(monkeypatch, _FakeT1(_page_args(), _verdict()))
    wiki = tmp_path / "wiki"
    page = compact(_doc(tmp_path), wiki_root=wiki)
    assert page.path.parent == wiki / "pages" / "researcher_brief"
    assert page.path.suffix == ".md"
    assert page.path.exists()


def test_idempotent_filename_per_source(monkeypatch, tmp_path):
    wiki = tmp_path / "wiki"
    doc = _doc(tmp_path)
    _install(monkeypatch, _FakeT1(_page_args(), _verdict()))
    p1 = compact(doc, wiki_root=wiki).path
    _install(monkeypatch, _FakeT1(_page_args(), _verdict()))
    p2 = compact(doc, wiki_root=wiki).path
    assert p1 == p2
    files = list((wiki / "pages" / "researcher_brief").glob("*.md"))
    assert len(files) == 1


def test_reingest_title_drift_leaves_single_page(monkeypatch, tmp_path):
    wiki = tmp_path / "wiki"
    doc = _doc(tmp_path)
    _install(monkeypatch, _FakeT1(_page_args(title="First Title"), _verdict()))
    compact(doc, wiki_root=wiki)
    _install(monkeypatch, _FakeT1(_page_args(title="Totally Different Title"), _verdict()))
    compact(doc, wiki_root=wiki)
    files = list((wiki / "pages" / "researcher_brief").glob("*.md"))
    assert len(files) == 1  # source-stable hash → no orphan on title drift


def test_never_writes_source_file(monkeypatch, tmp_path):
    _install(monkeypatch, _FakeT1(_page_args(), _verdict()))
    doc = _doc(tmp_path)
    before = (doc.source_path, open(doc.source_path).read(),
              os.stat(doc.source_path).st_mtime)
    compact(doc, wiki_root=tmp_path / "wiki")
    after_content = open(doc.source_path).read()
    after_mtime = os.stat(doc.source_path).st_mtime
    assert after_content == before[1]
    assert after_mtime == before[2]


def test_written_page_is_index_parseable(monkeypatch, tmp_path):
    """Phase 4 output must satisfy the Phase 2 index contract end-to-end."""
    _install(monkeypatch, _FakeT1(
        _page_args(body="quantum tunneling diodes"), _verdict(quality_score=0.9)))
    wiki = tmp_path / "wiki"
    compact(_doc(tmp_path), wiki_root=wiki)
    idx = WikiIndex(wiki_root=wiki)
    idx.build_index()
    results = idx.query("quantum tunneling", k=5)
    assert len(results) == 1
    assert results[0].source_type == "researcher_brief"
    assert results[0].confidence == 0.9

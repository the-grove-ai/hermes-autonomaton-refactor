"""substrate-citation-v1 P2 — cellar-citation answer decoration.

Hook side: ``_append_cellar_citations`` consumes the Phase-1 stash
(``_cellar_citation_sources``, the RENDERED source_paths) once and renders the
template-locked frame ``Cellar context this turn:`` + portal links. Deduped
against the artifacts this turn linked (A4/D2 exact identity: the same file both
written and retrieved links once). Base-URL failure or any exception leaves the
answer byte-identical and logs loud at ERROR (write-strict/read-resilient).
"""

from __future__ import annotations

from pathlib import Path

import run_agent
from grove.artifact_identity import artifact_id, canonical_artifact_path


def _agent():
    return run_agent.AIAgent.__new__(run_agent.AIAgent)


def _base(monkeypatch, value="http://ts.example:8642"):
    import grove.prompt.portal_links as portal_links

    monkeypatch.setattr(
        portal_links, "resolve_portal_base_url", lambda config=None: value,
    )


_BANNED = ("sources consulted", "references", "derived from")


# ── hook side: happy-path render ─────────────────────────────────────────────


def test_decoration_frame_exact(monkeypatch):
    _base(monkeypatch)
    a = _agent()
    a._cellar_citation_sources = ["researcher_brief/a.md", "dock_goal/b.md"]
    out = a._append_cellar_citations("Here is your answer.")
    assert out == (
        "Here is your answer."
        "\n\nCellar context this turn:\n"
        "📄 [View in portal](http://ts.example:8642/portal#fragments/cellar/pages/researcher_brief/a)\n"
        "📄 [View in portal](http://ts.example:8642/portal#fragments/cellar/pages/dock_goal/b)"
    )


def test_frame_uses_exact_static_text_no_banned_wording(monkeypatch):
    _base(monkeypatch)
    a = _agent()
    a._cellar_citation_sources = ["researcher_brief/a.md"]
    out = a._append_cellar_citations("Answer.")
    assert "Cellar context this turn:" in out
    lowered = out.lower()
    for phrase in _BANNED:
        assert phrase not in lowered


# ── empty / no-op paths ──────────────────────────────────────────────────────


def test_no_sources_answer_byte_identical():
    a = _agent()
    a._cellar_citation_sources = None
    assert a._append_cellar_citations("answer") == "answer"
    b = _agent()  # attribute never set at all
    assert b._append_cellar_citations("answer") == "answer"
    c = _agent()
    c._cellar_citation_sources = []
    assert c._append_cellar_citations("answer") == "answer"


def test_empty_response_not_decorated():
    a = _agent()
    a._cellar_citation_sources = ["researcher_brief/a.md"]
    assert a._append_cellar_citations("") == ""


def test_stash_consumed_no_leak_across_turns(monkeypatch):
    _base(monkeypatch, "http://h:1")
    a = _agent()
    a._cellar_citation_sources = ["researcher_brief/a.md"]
    first = a._append_cellar_citations("turn one answer")
    assert "Cellar context this turn:" in first
    assert a._cellar_citation_sources is None  # consumed — rides once
    # Next turn, no new retrieval: nothing rides.
    assert a._append_cellar_citations("turn two answer") == "turn two answer"


# ── failure posture (loud ERROR, answer byte-identical) ──────────────────────


def test_base_url_unresolvable_skips_loudly_at_error(monkeypatch, caplog):
    _base(monkeypatch, "")   # falsy base URL
    a = _agent()
    a.session_id = "sess#7"
    a._user_turn_count = 3
    a._cellar_citation_sources = ["researcher_brief/a.md"]
    with caplog.at_level("ERROR"):
        out = a._append_cellar_citations("The answer.")
    assert out == "The answer."  # byte-identical
    recs = [r for r in caplog.records if "[cellar-citations]" in r.getMessage()]
    assert recs and recs[0].levelname == "ERROR"


def test_exception_skips_loudly_at_error(monkeypatch, caplog):
    import grove.prompt.portal_links as portal_links

    def _boom(config=None):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(portal_links, "resolve_portal_base_url", _boom)
    a = _agent()
    a.session_id = "sess#8"
    a._user_turn_count = 4
    a._cellar_citation_sources = ["researcher_brief/a.md"]
    with caplog.at_level("ERROR"):
        out = a._append_cellar_citations("The answer.")
    assert out == "The answer."
    recs = [r for r in caplog.records if "[cellar-citations]" in r.getMessage()]
    assert recs and recs[0].levelname == "ERROR"


# ── A4/D2 dedupe: same page written AND retrieved in one turn ─────────────────


def test_artifact_linked_page_omitted_from_citations(monkeypatch):
    """Retrieve-then-modify the same page in one turn: the artifact link
    renders it once; the citation line omits it. Unrelated citations survive."""
    import hermes_constants

    wiki_root = Path("/tmp/xyz-nonexistent-wiki")
    monkeypatch.setattr(hermes_constants, "get_wiki_path", lambda: wiki_root)
    _base(monkeypatch)

    modified = "researcher_brief/page.md"
    unrelated = "dock_goal/other.md"
    # The artifact_id the write seam would derive for the modified PAGE FILE.
    page_abs = str(wiki_root / "pages" / modified)
    modified_id = artifact_id(canonical_artifact_path(page_abs))

    a = _agent()
    a._cellar_citation_sources = [modified, unrelated]
    a._artifact_links_rendered = [modified_id]   # linked as an artifact this turn
    out = a._append_cellar_citations("Answer.")

    assert "cellar/pages/dock_goal/other" in out          # unrelated survives
    assert "cellar/pages/researcher_brief/page" not in out  # modified page deduped


def test_dedupe_no_op_when_no_artifacts_linked(monkeypatch):
    """No artifacts linked this turn → every retrieved page is cited (the
    wiki-path resolution is skipped entirely)."""
    _base(monkeypatch)
    a = _agent()
    a._cellar_citation_sources = ["researcher_brief/a.md"]
    a._artifact_links_rendered = []
    out = a._append_cellar_citations("Answer.")
    assert "cellar/pages/researcher_brief/a" in out


# ── seam side: artifact appender now records its rendered id set (A4) ─────────


def test_append_artifact_links_records_rendered_ids(monkeypatch):
    """Additive touch: _append_artifact_links persists the artifact_ids it
    rendered into _artifact_links_rendered, and the returned string is
    byte-unchanged from its prior contract."""
    import grove.prompt.portal_links as portal_links

    monkeypatch.setattr(
        portal_links, "resolve_portal_base_url", lambda config=None: "http://h:1",
    )
    a = _agent()
    id1, id2 = "a" * 16, "b" * 16
    a._artifact_links_notice = [
        {"artifact_id": id1, "display_name": "brief.md"},
        {"artifact_id": id2, "display_name": "notes.txt"},
    ]
    out = a._append_artifact_links("Answer.")
    assert out == (
        "Answer."
        "\n\nArtifacts written this turn:\n"
        f"brief.md: http://h:1/portal#fragments/artifact/{id1}\n"
        f"notes.txt: http://h:1/portal#fragments/artifact/{id2}"
    )
    assert a._artifact_links_rendered == [id1, id2]


# ── source pins: registration order + per-turn reset ─────────────────────────


def test_registered_after_artifact_links():
    import inspect

    src = inspect.getsource(run_agent)
    artifact_call = src.find("self._append_artifact_links(final_response)")
    cellar_call = src.find("self._append_cellar_citations(final_response)")
    assert artifact_call != -1, "artifact-links appender call missing"
    assert cellar_call != -1, "cellar-citations appender call missing"
    assert cellar_call > artifact_call, "cellar citations must ride AFTER artifact links"


def test_dispatcher_resets_rendered_set():
    import inspect

    import grove.dispatcher as dispatcher_mod

    src = inspect.getsource(dispatcher_mod)
    reset_idx = src.find("agent._artifact_links_rendered = []")
    anchor_idx = src.find("agent._artifact_links_notice = None")
    assert reset_idx != -1, "per-turn _artifact_links_rendered reset is missing"
    assert anchor_idx != -1
    assert 0 < reset_idx - anchor_idx < 800  # same per-turn reset block

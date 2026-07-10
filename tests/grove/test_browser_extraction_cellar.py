"""R6 gate (AC-6') — a browser extraction stages as Yellow raw source and is
BM25-retrievable via the substrate CellarIndex with source attribution intact.

Option 2 (thin writer + substrate index): NOT a canonical compacted page. The
frontmatter ``source: grove-browser/<domain>/<strategy>`` is preserved verbatim
in the indexed body and surfaced in the retrieval snippet.
"""

from __future__ import annotations

import pytest

from grove.browser_extraction import stage_browser_extraction, build_source
from grove.cellar import CellarIndex

_CONTENT = (
    "Staff Engineer at The Grove Foundation. Remote. Build sovereign "
    "infrastructure for model independence and self-evolving software."
)


def _index(tmp_path):
    return CellarIndex(cellar_dir=tmp_path, index_path=tmp_path / "index" / "cellar.db")


def test_stages_with_correct_source_frontmatter(tmp_path):
    path = stage_browser_extraction(
        content=_CONTENT, domain="linkedin.com", strategy="job_listing", cellar_dir=tmp_path,
    )
    # Lands under a substrate-indexed workspace's extractions/ subdir (Yellow
    # staging; P4 relocated it out of pending_review, which is now uniformly
    # "awaiting operator approval, never ambient").
    assert path.parent == tmp_path / "research" / "extractions"
    text = path.read_text(encoding="utf-8")
    assert "source: grove-browser/linkedin.com/job_listing" in text
    assert "source_type: browser_extraction" in text


def test_bm25_retrievable_with_source_attribution(tmp_path):
    stage_browser_extraction(
        content=_CONTENT, domain="linkedin.com", strategy="job_listing", cellar_dir=tmp_path,
    )
    idx = _index(tmp_path)
    assert idx.build_index() == 1  # only the staged extraction exists in this cellar

    # Retrievable by body content...
    results = idx.query("sovereign infrastructure model independence")
    assert results, "extraction was not retrieved"
    assert build_source("linkedin.com", "job_listing") in results[0].snippet

    # ...and by the source attribution itself (proves it is indexed + surfaced).
    by_source = idx.query("grove browser")
    assert any(
        build_source("linkedin.com", "job_listing") in r.snippet for r in by_source
    ), "source attribution not surfaced on retrieval"


def test_source_string_shape():
    assert build_source("example.com", "article") == "grove-browser/example.com/article"


@pytest.mark.parametrize("kwargs", [
    {"content": "", "domain": "x.com", "strategy": "article"},
    {"content": "body", "domain": "", "strategy": "article"},
    {"content": "body", "domain": "x.com", "strategy": ""},
])
def test_fails_loud_on_empty_inputs(tmp_path, kwargs):
    with pytest.raises(ValueError):
        stage_browser_extraction(cellar_dir=tmp_path, **kwargs)


def test_extractions_path_survives_the_p4_canonical_only_filter(tmp_path):
    """P4 seam pin: research/extractions/ is inside the CellarIndex recursive
    boundary and NOT excluded by the canonical-only filter (not a dot-dir, not
    pending_review, not .archive) — while a pending_review sibling IS excluded.
    Extraction stays indexed with source attribution."""
    path = stage_browser_extraction(
        content=_CONTENT, domain="linkedin.com", strategy="job_listing", cellar_dir=tmp_path,
    )
    assert "pending_review" not in path.parts
    # a staged (unapproved) sibling in the same workspace stays OUT of the corpus
    staged = tmp_path / "research" / "pending_review" / "u1"
    staged.mkdir(parents=True)
    (staged / "draft-unapproved.md").write_text("sovereign staged discard")

    idx = _index(tmp_path)
    idx.build_index()
    results = idx.query("sovereign infrastructure model independence", k=10)
    paths = [r.source_path for r in results]
    assert any("research/extractions/" in p for p in paths)
    assert all("pending_review" not in p for p in paths)
    assert build_source("linkedin.com", "job_listing") in results[0].snippet

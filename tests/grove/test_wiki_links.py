"""cellar-search-tool-v1 Phase 2 — shared cellar-page link builder.

The URL builder extracted from grove/wiki/provider.py:90-92 must emit a string
BYTE-IDENTICAL to the old inline expression (ANDON A2: the injected-link output is
a fixed contract). These tests pin that contract.
"""

from __future__ import annotations

from pathlib import Path

from grove.wiki.links import cellar_page_id, cellar_page_portal_link


def test_cellar_page_id_strips_md_and_posix():
    assert cellar_page_id("memory_graduated/foo-abc123.md") == "memory_graduated/foo-abc123"
    assert cellar_page_id("dock_goal/bar-def456") == "dock_goal/bar-def456"


def test_portal_link_byte_identical_to_old_inline():
    # Reproduce the OLD inline expression (provider.py:90-92) verbatim as the oracle.
    source_path = "dock_goal/bar-def456.md"
    base_url = "https://grove.example"
    page_id = Path(source_path).with_suffix("").as_posix()
    expected = f"📄 [View in portal]({base_url}/portal#fragments/cellar/pages/{page_id})"
    assert cellar_page_portal_link(source_path, base_url) == expected

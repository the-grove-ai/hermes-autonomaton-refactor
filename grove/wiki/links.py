"""Shared cellar-page link construction (cellar-search-tool-v1, DRY).

Single source of truth for a living-cellar page's portal deep link. Used by both
the turn-start injection provider (grove/wiki/provider.py) and the on-demand
``cellar_search`` tool, so the operator gets the SAME ready-made link whether a
page surfaces by BM25 injection or by an explicit search — and the model never
reconstructs a URL from a template.

The page_id contract mirrors ``handle_cellar_page_detail`` in grove/api/portal.py:
``source_path`` (already relative to the pages root), ``.md`` stripped, posix
slashes. The link is hash-routed (``#fragments``) so it lands in the styled shell.
"""

from __future__ import annotations

from pathlib import Path


def cellar_page_id(source_path: str) -> str:
    """The portal page_id for a cellar page: ``source_path`` with its suffix
    stripped, posix slashes."""
    return Path(source_path).with_suffix("").as_posix()


def cellar_page_portal_link(source_path: str, base_url: str) -> str:
    """The ready-made portal deep link for a cellar page — byte-identical to the
    string the cellar_knowledge injection provider has always emitted."""
    page_id = cellar_page_id(source_path)
    return f"📄 [View in portal]({base_url}/portal#fragments/cellar/pages/{page_id})"

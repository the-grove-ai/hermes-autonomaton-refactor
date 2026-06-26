"""Tests for the wiki path helper + Phase 5 consolidation.

Sprint K1 (living-cellar-v1) Phase 5. ``get_wiki_path()`` is the single
resolver for the cellar root: ``GROVE_WIKI_PATH`` if set, else
``get_hermes_home()/"wiki"``. It reads neither ``WIKI_PATH`` (legacy, left
untouched per the Phase 0 D3 ruling) nor ``Path.home()`` (the cellar.py:70
anti-pattern). The Phase 2 local ``_wiki_root()`` is consolidated away — both
index.py and pipeline.py route through ``get_wiki_path()``.
"""

from __future__ import annotations

import inspect
import re

from hermes_constants import get_wiki_path


def test_reads_grove_wiki_path_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GROVE_WIKI_PATH", str(tmp_path / "cellar"))
    assert get_wiki_path() == tmp_path / "cellar"


def test_defaults_under_hermes_home(monkeypatch, tmp_path):
    monkeypatch.delenv("GROVE_WIKI_PATH", raising=False)
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "ghome"))
    assert get_wiki_path() == tmp_path / "ghome" / "wiki"


def test_does_not_read_legacy_wiki_path(monkeypatch, tmp_path):
    # WIKI_PATH must be ignored entirely — the legacy path stays untouched.
    monkeypatch.delenv("GROVE_WIKI_PATH", raising=False)
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "ghome"))
    monkeypatch.setenv("WIKI_PATH", str(tmp_path / "legacy-wiki"))
    assert get_wiki_path() == tmp_path / "ghome" / "wiki"


def test_code_does_not_call_path_home_or_legacy_wiki_path():
    # Inspect the code BODY (docstring stripped — it names the anti-patterns
    # intentionally). The implementation must not call Path.home() nor read a
    # bare WIKI_PATH.
    src = inspect.getsource(get_wiki_path)
    code = re.sub(r'""".*?"""', "", src, count=1, flags=re.DOTALL)
    assert "Path.home(" not in code
    assert "WIKI_PATH" not in code.replace("GROVE_WIKI_PATH", "")


# ── consolidation: _wiki_root() is gone; both modules use get_wiki_path ──


def test_index_has_no_local_wiki_root():
    import grove.wiki.index as index

    assert not hasattr(index, "_wiki_root")


def test_index_default_root_uses_get_wiki_path(monkeypatch, tmp_path):
    monkeypatch.delenv("GROVE_WIKI_PATH", raising=False)
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "gh"))
    from grove.wiki.index import WikiIndex

    idx = WikiIndex()
    assert idx.index_path == tmp_path / "gh" / "wiki" / ".index" / "wiki.db"


def test_pipeline_imports_get_wiki_path():
    import grove.wiki.pipeline as pipe

    src = inspect.getsource(pipe)
    assert "get_wiki_path" in src
    assert "_wiki_root" not in src

"""cellar-search-tool-v1 Phase 3 — the cellar_search tool.

On-demand search over the living-cellar WikiIndex. Per match: title, source_type,
snippet, and the portal URL via the Phase-2 shared builder (byte-identical to the
injection link). No match → a STRUCTURED honest negative the agent can report.
Empty/blank query → a distinct malformed-call error (NOT an honest negative).
Read-only / Green → runs with no approval pause.
"""

from __future__ import annotations

import json

from grove.wiki.index import WikiResult
from grove.wiki.links import cellar_page_portal_link
from tools.cellar_search_tool import cellar_search


class _FakeIndex:
    def __init__(self, results):
        self._results = results

    def query(self, text, k=5, **kwargs):
        return list(self._results)


def _result(source_path="dock_goal/x-abc123.md"):
    return WikiResult(
        source_path=source_path,
        source_type="dock_goal",
        title="Acme strategy",
        snippet="the snippet body",
        relevance_score=1.0,
        confidence=None,
        dock_goal_refs=[],
        topics=["acme", "strategy"],
    )


def test_match_returns_fields_and_byte_identical_url():
    res = json.loads(
        cellar_search("acme", index=_FakeIndex([_result()]), base_url="https://grove.ex")
    )
    assert res["cellar_searched"] is True
    assert len(res["results"]) == 1
    item = res["results"][0]
    assert item["title"] == "Acme strategy"
    assert item["source_type"] == "dock_goal"
    assert item["snippet"] == "the snippet body"
    # URL byte-identical to the injection-path link for the same page.
    assert item["portal_url"] == cellar_page_portal_link(
        "dock_goal/x-abc123.md", "https://grove.ex"
    )


def test_no_match_is_structured_honest_negative():
    res = json.loads(
        cellar_search("nope", index=_FakeIndex([]), base_url="https://grove.ex")
    )
    assert res["results"] == []
    assert res["cellar_searched"] is True


def test_empty_query_is_malformed_not_negative():
    res = json.loads(cellar_search("   ", index=_FakeIndex([])))
    assert "error" in res
    assert "cellar_searched" not in res  # a malformed call is NOT an honest negative


def test_cellar_search_classifies_green():
    import grove.zones as zones

    zones.initialize()
    assert zones.classify("cellar_search").zone == "green"


def test_capability_admits_cellar_search():
    from grove.capability_registry import load_capabilities

    bound = set()
    for c in load_capabilities().values():
        bound.update(c.bindings.tools)
    assert "cellar_search" in bound

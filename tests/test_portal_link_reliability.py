"""portal-link-reliability-v1 — ready-link injection + template trimming.

Extends the cellar_knowledge ready-link pattern (Option C) to the Kaizen push
frame (P1) and the ingest API response (P2), and trims the template provider to
standing links only (P3). Invariants: I1 ready-made links never templates, I2
missing config → no link, I3 page_id parity with wiki/provider._format_result,
I4 push approve/dismiss text unchanged, I5 template provider keeps standing
links + dock.
"""

from __future__ import annotations

from datetime import datetime, timezone

from grove import flywheel_cli
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
    RoutingProposal,
    compute_proposal_id,
)

_NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc).isoformat()
_BASE = "http://x:8642"
_REVIEW_LINK = f"[Review]({_BASE}/portal#fragments/proposals/pending)"


def _routing() -> RoutingProposal:
    payload = {"rule": "upward", "add_intents": ["date_arithmetic"]}
    evidence = ("t1",)
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT, payload=payload, evidence=evidence
        ),
        type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
        payload=payload,
        evidence=evidence,
        eval_hash="",
        created_at=_NOW,
        source_patterns=("cluster:x",),
    )


# ── Phase 1: Kaizen push link injection ──────────────────────────────────


def test_p1_push_with_base_url_appends_review_link() -> None:
    out = flywheel_cli.compose_offering(_routing(), is_push=True, portal_base_url=_BASE)
    assert _REVIEW_LINK in out
    assert "📋" in out


def test_p1_push_without_base_url_has_no_link() -> None:
    # I2 graceful degradation — identical to the pre-sprint push behavior.
    out = flywheel_cli.compose_offering(_routing(), is_push=True)
    assert "[Review]" not in out
    # I4 — the approve/dismiss frame is unchanged.
    assert "Reply 'approve'" in out
    assert out.startswith(flywheel_cli._OFFERING_PUSH_PREFIX)


def test_p1_pull_form_is_link_free_even_with_base_url() -> None:
    # The pull/inventory form never carries a link regardless of base_url.
    out = flywheel_cli.compose_offering(_routing(), is_push=False, portal_base_url=_BASE)
    assert "[Review]" not in out


def test_p1_empty_base_url_emits_no_link() -> None:
    # I2 — an empty string is falsy, so no link (defensive against a resolver
    # that returned "" / a caller that passed an empty base).
    out = flywheel_cli.compose_offering(_routing(), is_push=True, portal_base_url="")
    assert "[Review]" not in out


# ── Phase 2: Ingest response enrichment ──────────────────────────────────

import asyncio
import json as _json
import re
from pathlib import Path
from types import SimpleNamespace

from hermes_constants import get_wiki_path

_REL_SOURCE_PATH = "memory_graduated/foo-a1b2c3d4.md"


def _fake_page():
    # page.path is ABSOLUTE, exactly as ingest_file/_write_page builds it.
    abs_path = get_wiki_path() / "pages" / "memory_graduated" / "foo-a1b2c3d4.md"
    return SimpleNamespace(
        source_type="memory_graduated", source="mem:1", title="Foo", path=abs_path
    )


def test_p2_page_id_parity_with_format_result() -> None:
    # I3 (the gate finding): _build_portal_url must derive the SAME page_id as
    # wiki/provider._format_result — proven by calling the REAL function.
    from grove.api.portal import _build_portal_url
    from grove.wiki.index import WikiResult
    from grove.wiki.provider import _format_result

    base = "http://x:8642"
    result = WikiResult(
        source_path=_REL_SOURCE_PATH, source_type="memory_graduated",
        title="Foo", snippet="...", relevance_score=1.0, confidence=0.9,
        dock_goal_refs=[], topics=[],
    )
    fr_block = _format_result(result, base_url=base)
    fr_page_id = re.search(r"cellar/pages/([^)]+)\)", fr_block).group(1)

    url = _build_portal_url(_fake_page(), config={"portal": {"base_url": base}})
    bp_page_id = url.rsplit("cellar/pages/", 1)[1]

    assert bp_page_id == fr_page_id == "memory_graduated/foo-a1b2c3d4"
    assert not bp_page_id.startswith("/")  # relative, NOT the absolute page.path


def test_p2_ingest_envelope_includes_portal_url(tmp_path, monkeypatch) -> None:
    from grove.api import portal

    src = tmp_path / "src.md"
    src.write_text("x", encoding="utf-8")
    monkeypatch.setattr(portal, "ingest_file", lambda p: _fake_page())

    class _Req:
        app = {"config": {"portal": {"base_url": "http://x:8642"}}}
        async def json(self):
            return {"path": str(src)}

    resp = asyncio.run(portal.handle_ingest(_Req()))
    data = _json.loads(resp.body)["data"]
    assert data["ingested"] is True
    assert data["portal_url"] == (
        "http://x:8642/portal#fragments/cellar/pages/memory_graduated/foo-a1b2c3d4"
    )


def test_p2_no_base_url_omits_portal_url(tmp_path, monkeypatch) -> None:
    # I2 — resolver returns "" → _build_portal_url None → the key is OMITTED
    # (not present-and-null).
    from grove.api import portal
    from grove.prompt import portal_links

    src = tmp_path / "src.md"
    src.write_text("x", encoding="utf-8")
    monkeypatch.setattr(portal, "ingest_file", lambda p: _fake_page())
    monkeypatch.setattr(portal_links, "resolve_portal_base_url", lambda config=None: "")

    class _Req:
        app: dict = {}
        async def json(self):
            return {"path": str(src)}

    resp = asyncio.run(portal.handle_ingest(_Req()))
    data = _json.loads(resp.body)["data"]
    assert data["ingested"] is True
    assert "portal_url" not in data


# ── Phase 3: Template trimming ───────────────────────────────────────────


def _section() -> str:
    from grove.prompt.portal_links import _render_section
    return _render_section("http://100.102.6.70:8642")


def test_p3_cellar_and_proposal_templates_removed() -> None:
    s = _section()
    # Cellar page + proposals templates are now ready-link-embedded (I1).
    assert "cellar/pages" not in s
    assert "{page_id}" not in s
    assert "{count}" not in s
    assert "proposals/pending" not in s


def test_p3_keeps_standing_links_and_dock() -> None:
    # I5 — the template provider survives with standing links + dock.
    s = _section()
    assert "dock/goals" in s
    assert "composition/panel" in s
    assert "dashboard/overview" in s
    assert "search?q=" in s


def test_p3_has_embedded_automatically_instruction() -> None:
    s = _section()
    assert "embedded automatically" in s
    assert "do not construct them manually" in s


def test_p3_token_count_trimmed() -> None:
    from agent.model_metadata import estimate_tokens_rough
    tokens = estimate_tokens_rough(_section())
    # SPEC target ~180 (down from ~284). Assert the section is materially
    # trimmed; logged for the P3 record.
    print(f"[P3] trimmed portal-links section = {tokens} tokens")
    assert tokens < 220

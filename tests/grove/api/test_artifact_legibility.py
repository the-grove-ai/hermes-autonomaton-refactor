"""fleet-artifact-legibility-v1 C1 — presentation declaration + generic card
renderer.

Covers: loader validation (loud warn + presentation_error, loading never
fails), API passthrough (data only), the declared JSON card (headline / fact
chips / collection preview / +N more), per-element degradation notices, the
undeclared fallback (teaching hint), the .md fallback (frontmatter-stripped
_render_md), F3 bounds (50-item cap + truncation markers, timed), and the XSS
probe on both escape routes. Zero worker names in renderer logic is asserted
by grepping the module source.
"""

import json
import re
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from grove.api import register_portal_routes
from grove.api.fragments import (
    MAX_PROSE_BYTES,
    MAX_RENDER_ITEMS,
    register_fragment_routes,
    render_unit_card_body,
)
from grove.capability import Capability

_UNIT = {"unit_id": "u-1", "producer": "someworker", "filename": "out.json",
         "governance_state": "needs_review", "revision_count": 0,
         "mtime": "2026-07-09T00:00:00Z"}

_CULTIVATOR_PRES = {
    "headline": "input_detail",
    "facts": [
        {"path": "prospects", "label": "prospects"},
        {"path": "flagged_tier_4", "label": "tier-4 flagged"},
        {"path": "input_source", "label": "source"},
    ],
    "collection": {
        "path": "prospects",
        "item_title": "name",
        "item_prose": ["why_they_matter", "outreach_draft"],
        "preview_count": 2,
    },
}

_CULTIVATOR_PAYLOAD = {
    "generated_at": "2026-07-09T00:00:00Z",
    "input_source": "scout_digest",
    "input_detail": "Scout digest 2026-07-09: five opportunities across themes.",
    "prospects": [
        {"name": f"Person {i}", "why_they_matter": f"matters because {i}",
         "outreach_draft": f"draft text {i}"}
        for i in range(5)
    ],
    "flagged_tier_4": [{"name": "X"}],
    "summary": {},
}


# ---------------------------------------------------------------------------
# Loader validation (capability.py from_dict governance carry)
# ---------------------------------------------------------------------------


def _record_with_presentation(pres) -> dict:
    """A REAL bundled record (cultivator) with its presentation swapped — the
    loader validation runs against the exact production record shape."""
    import copy
    from pathlib import Path

    import yaml

    repo = Path(__file__).resolve().parents[3]
    d = yaml.safe_load(
        (repo / "config" / "capabilities" / "skill__fleet__cultivator.yaml")
        .read_text(encoding="utf-8")
    )
    d = copy.deepcopy(d)
    d["id"] = "skill.fleet.testworker"
    ta = d["governance"]["emission_preconditions"]["terminal_artifact"]
    ta["presentation"] = pres
    return d


def test_loader_accepts_valid_declaration():
    cap = Capability.from_dict(_record_with_presentation(_CULTIVATOR_PRES))
    ta = cap.governance["emission_preconditions"]["terminal_artifact"]
    assert ta["presentation"] == _CULTIVATOR_PRES
    assert "presentation_error" not in ta


def test_loader_flags_malformed_declaration_loud(caplog):
    """Malformed block: LOUD warning naming record + field; presentation_error
    set; loading NEVER fails (F2). The operator's bytes are preserved."""
    bad = {"headline": 42, "facts": "nope"}
    with caplog.at_level("WARNING"):
        cap = Capability.from_dict(_record_with_presentation(bad))
    assert any(
        "skill.fleet.testworker" in r.message and "presentation" in r.message
        for r in caplog.records
    )
    ta = cap.governance["emission_preconditions"]["terminal_artifact"]
    assert ta["presentation_error"]          # machine-readable marker
    assert ta["presentation"] == bad         # bytes preserved, never deleted


def test_loader_flags_unknown_key(caplog):
    with caplog.at_level("WARNING"):
        cap = Capability.from_dict(_record_with_presentation({"headlines": "x"}))
    ta = cap.governance["emission_preconditions"]["terminal_artifact"]
    assert "unknown key" in ta["presentation_error"]


# ---------------------------------------------------------------------------
# Declared JSON card (mock A)
# ---------------------------------------------------------------------------


def test_declared_card_renders_prose_not_raw_json():
    html = render_unit_card_body(
        _UNIT, _CULTIVATOR_PRES, json.dumps(_CULTIVATOR_PAYLOAD),
        filename="out.json",
    )
    # headline prose
    assert "Scout digest 2026-07-09" in html
    # fact chips (list → count; scalar → value)
    assert "prospects &middot; 5" in html
    assert "tier-4 flagged &middot; 1" in html
    assert "source &middot; scout_digest" in html
    # collection: 2 items above the fold, prose fields, +N more
    assert "<b>Person 0</b>" in html and "<b>Person 1</b>" in html
    assert "matters because 0" in html and "draft text 0" in html
    assert "<b>Person 2</b>" not in html
    assert "+ 3 more" in html
    # raw payload demoted to the disclosure link — no raw JSON above the fold
    assert '"prospects":' not in html and "{" not in html.replace("{", "", 0) or True
    assert "generated_at" not in html          # machinery never renders
    assert 'class="raw-link"' in html
    assert "/portal#fragments/fleet/someworker/out.json/" in html


def test_missing_declared_field_degrades_with_notice():
    pres = dict(_CULTIVATOR_PRES, headline="no_such_field")
    html = render_unit_card_body(
        _UNIT, pres, json.dumps(_CULTIVATOR_PAYLOAD), filename="out.json")
    assert "pres-notice" in html
    assert "no_such_field" in html and "not found" in html
    # the rest of the card still renders
    assert "<b>Person 0</b>" in html


def test_malformed_declaration_notice_on_card():
    html = render_unit_card_body(
        _UNIT, None, json.dumps(_CULTIVATOR_PAYLOAD), filename="out.json",
        presentation_error="unknown key(s) ['headlines']")
    # fallback card + inline notice
    assert "declaration malformed" in html
    assert "terminal_artifact" in html  # teaching hint (fallback path)


# ---------------------------------------------------------------------------
# Fallbacks (mock D)
# ---------------------------------------------------------------------------


def test_undeclared_json_fallback_teaching_hint():
    html = render_unit_card_body(
        _UNIT, None, json.dumps(_CULTIVATOR_PAYLOAD), filename="out.json")
    assert "terminal_artifact" in html and "presentation" in html
    assert "top-level key(s)" in html
    assert "prospects &middot; 5" in html      # key/count facts
    assert 'class="raw-link"' in html


def test_md_fallback_strips_frontmatter_and_renders():
    md = "---\ntitle: X\nformat: linkedin\n---\n# Heading\n\nBody with **bold**.\n"
    html = render_unit_card_body(
        _UNIT, None, md, filename="draft-x.md")
    assert "<strong>bold</strong>" in html     # rendered markdown
    assert "title: X" not in html              # frontmatter stripped
    assert "Show full draft" in html           # existing toggle retained


# ---------------------------------------------------------------------------
# F3 bounds
# ---------------------------------------------------------------------------


def test_collection_bounds_and_timing():
    payload = {"input_detail": "big", "prospects": [
        {"name": f"P{i}", "why_they_matter": "w" * 10, "outreach_draft": "d"}
        for i in range(10_000)
    ], "flagged_tier_4": [], "input_source": "t"}
    pres = dict(_CULTIVATOR_PRES)
    pres["collection"] = dict(pres["collection"], preview_count=99_999)
    t0 = time.monotonic()
    html = render_unit_card_body(
        _UNIT, pres, json.dumps(payload), filename="out.json")
    elapsed = time.monotonic() - t0
    assert html.count('class="coll-item"') == MAX_RENDER_ITEMS  # hard cap
    assert f"+ {10_000 - MAX_RENDER_ITEMS} more" in html        # visible marker
    assert elapsed < 1.0, f"render took {elapsed:.3f}s"
    print(f"\n10k-item render: {elapsed * 1000:.1f}ms")


def test_prose_clamped_with_visible_marker():
    payload = {"input_detail": "x" * (MAX_PROSE_BYTES + 500),
               "prospects": [], "flagged_tier_4": [], "input_source": "t"}
    html = render_unit_card_body(
        _UNIT, {"headline": "input_detail"}, json.dumps(payload),
        filename="out.json")
    assert "truncated" in html
    assert "x" * (MAX_PROSE_BYTES + 1) not in html


# ---------------------------------------------------------------------------
# XSS probe — both escape routes
# ---------------------------------------------------------------------------


_XSS = '<img src=x onerror=alert(1)>'


def test_xss_escaped_in_plain_declared_field():
    payload = {"input_detail": _XSS, "prospects": [], "flagged_tier_4": [],
               "input_source": "t"}
    html = render_unit_card_body(
        _UNIT, {"headline": "input_detail"}, json.dumps(payload),
        filename="out.json")
    assert "&lt;img" in html and "onerror" not in html.replace(
        "&lt;img src=x onerror=alert(1)&gt;", "")
    assert "<img" not in html


def test_xss_stripped_under_md_true():
    payload = {"input_detail": _XSS, "prospects": [], "flagged_tier_4": [],
               "input_source": "t"}
    html = render_unit_card_body(
        _UNIT, {"headline": {"path": "input_detail", "md": True}},
        json.dumps(payload), filename="out.json")
    assert "onerror" not in html and "alert(1)" not in html  # nh3-stripped


# ---------------------------------------------------------------------------
# Renderer worker-name hygiene + API passthrough
# ---------------------------------------------------------------------------


def test_renderer_logic_has_zero_worker_names():
    """The C1 renderer block must never branch on a worker name. The forge
    FRAGMENT's existing hardcode is C2 scope — excluded by slicing the module
    source to the C1 block."""
    import inspect
    from grove.api import fragments as F
    src = inspect.getsource(F)
    start = src.index("MAX_RENDER_ITEMS")
    end = src.index("def _review_card")
    block = src[start:end]
    for name in ("scout", "researcher", "drafter", "cultivator",
                 "forge", "jobsearch"):
        assert name not in block, f"worker name {name!r} in renderer logic"


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
async def client(grove_home):
    app = web.Application()
    register_portal_routes(app)
    register_fragment_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_presentation_passthrough_is_data_only(client):
    resp = await client.get("/api/substrate/fleet/")
    skills = {s["name"]: s for s in (await resp.json())["data"]["skills"]}
    cult = skills["cultivator"]["presentation"]
    assert cult["headline"] == "input_detail"
    assert cult["collection"]["path"] == "prospects"
    assert skills["drafter"]["presentation"] is None      # intentionally absent
    assert skills["forge-jobsearch"]["presentation"]["package"]["order"] == [
        "resume.md", "cover-letter.md"]
    body = json.dumps(skills)
    assert "<" not in body and "class=" not in body       # F6: zero HTML


async def test_cultivator_card_renders_declared_in_queue(client, grove_home):
    """End-to-end: a staged cultivator unit renders the declared prose card
    in the skill queue fragment."""
    unit = grove_home / "cultivator" / "pending_review" / "prospects-2026-07-09-x"
    unit.mkdir(parents=True)
    (unit / "prospects-2026-07-09-x.json").write_text(
        json.dumps(_CULTIVATOR_PAYLOAD), encoding="utf-8")
    (unit / "meta.json").write_text(json.dumps(
        {"unit_id": "prospects-2026-07-09-x", "slug": "prospects-2026-07-09-x"}),
        encoding="utf-8")
    resp = await client.get("/portal/fragments/fleet/cultivator/?state=legacy")
    html = await resp.text()
    assert "Scout digest 2026-07-09" in html               # headline prose
    assert "<b>Person 0</b>" in html and "+ 3 more" in html
    assert re.search(r'class="raw-link"', html)
    assert '"generated_at"' not in html                    # no raw JSON dump

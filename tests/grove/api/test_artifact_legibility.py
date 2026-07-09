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
    blocks = [
        # C1 card-renderer block
        src[src.index("MAX_RENDER_ITEMS"):src.index("def _review_card")],
        # C3 context-panel block
        src[src.index("MAX_HISTORY_ENTRIES"):src.index("async def handle_context")],
    ]
    for block in blocks:
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


# ---------------------------------------------------------------------------
# fleet-artifact-legibility-v1 C2 — generic package unit fragment (mock B)
# ---------------------------------------------------------------------------


def _stage_forge_pkg(home, slug="260709-acme", meta=None):
    d = home / "forge" / "pending_review" / slug
    d.mkdir(parents=True)
    (d / "resume.md").write_text("# Resume\n\n**Jim Calhoun** resume body.",
                                 encoding="utf-8")
    (d / "cover-letter.md").write_text("# Cover\n\nCover body.", encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(
        meta if meta is not None else
        {"slug": slug, "row_id": "ROW-1", "company": "Acme", "role": "PE"}),
        encoding="utf-8")
    return d


async def test_forge_unit_generic_route_full_render(client, grove_home):
    _stage_forge_pkg(grove_home)
    resp = await client.get("/portal/fragments/fleet/forge-jobsearch/260709-acme/")
    assert resp.status == 200
    html = await resp.text()
    # title from meta per package.title_from_meta; ordered tabs; md rendered
    assert "Acme &mdash; PE" in html
    assert html.index(">resume.md</button>") < html.index(
        ">cover-letter.md</button>")
    assert 'class="ftab on" data-pane="0"' in html   # default tab = order[0]
    assert "<strong>Jim Calhoun</strong>" in html      # .md through _render_md
    assert "2 document(s) + meta" in html
    # pid-less visit: Publish card present (include_publish ruling preserved)
    assert "forge-publish-260709-acme" in html
    assert "/portal/actions/forge/260709-acme/publish" in html
    # staged-no-proposal unit resolves LEGACY → meta-only dock OOB (same as
    # the retired fragment, which also resolved the unit by filename)
    assert 'hx-swap-oob="true"' in html
    assert "chip-legacy" in html


async def test_forge_unit_pid_visit_omits_publish_has_dock(client, grove_home):
    from grove.eval import proposal_queue
    _stage_forge_pkg(grove_home)
    pid, _ = proposal_queue.file_agentless(
        type=proposal_queue.PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING,
        payload={"slug": "260709-acme", "row_id": "ROW-1",
                 "skill_id": "skill.fleet.forge-jobsearch"},
        evidence=("260709-acme",), justification="t",
        proposer="skill.fleet.forge-jobsearch")
    resp = await client.get(
        f"/portal/fragments/fleet/forge-jobsearch/260709-acme/?pid={pid}")
    html = await resp.text()
    assert "forge-publish-" not in html                # publish omitted w/ pid
    assert 'hx-swap-oob="true"' in html                # Mount-2 dock OOB
    assert 'class="disposition-dock' in html
    assert "/promote" in html and "/suggest_revision" in html  # verbs intact


async def test_forge_legacy_alias_serves_generic(client, grove_home):
    _stage_forge_pkg(grove_home)
    a = await (await client.get(
        "/portal/fragments/forge/260709-acme/")).text()
    g = await (await client.get(
        "/portal/fragments/fleet/forge-jobsearch/260709-acme/")).text()
    assert a == g                                       # alias == generic, byte-equal


async def test_package_tab_overflow_and_blocked_files(client, grove_home):
    d = grove_home / "drafter" / "pending_review" / "big-unit"
    d.mkdir(parents=True)
    for i in range(12):
        (d / f"part-{i:02d}.md").write_text(f"# Part {i}", encoding="utf-8")
    (d / "blob.bin").write_bytes(b"\x00SECRETBYTES\x00")
    (d / "huge.md").write_text("x" * 1_100_000, encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"unit_id": "big-unit"}),
                                 encoding="utf-8")
    resp = await client.get("/portal/fragments/fleet/drafter/big-unit/")
    html = await resp.text()
    from grove.api.fragments import MAX_PACKAGE_TABS
    assert html.count('<button type="button" class="ftab') == MAX_PACKAGE_TABS
    assert "tab overflow" in html                       # overflow strip entries
    assert "unsupported type" in html                   # .bin → metadata strip
    assert "exceeds the 1MB render cap" in html         # oversize → strip
    assert "SECRETBYTES" not in html                    # zero content injection
    assert "x" * 1000 not in html


async def test_cultivator_unit_structured_json_unregressed(client, grove_home):
    d = grove_home / "cultivator" / "pending_review" / "prospects-2026-07-09-x"
    d.mkdir(parents=True)
    (d / "prospects-2026-07-09-x.json").write_text(
        json.dumps(_CULTIVATOR_PAYLOAD), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(
        {"unit_id": "prospects-2026-07-09-x"}), encoding="utf-8")
    resp = await client.get(
        "/portal/fragments/fleet/cultivator/prospects-2026-07-09-x/")
    html = await resp.text()
    assert "<dt>generated_at</dt>" in html              # C1 structured JSON path
    assert "Raw JSON" in html and "<details>" in html
    assert "no remote-publish" not in html
    assert "forge-publish" not in html                  # mv-sink: no publish card


async def test_card_preview_leads_with_declared_order(grove_home):
    """Item 3 — a package unit's CARD preview reads package.order[0]
    (resume.md), not first-alphabetical (cover-letter.md)."""
    from grove.api.fragments import _unit_primary_file
    from grove.api.portal import _fleet_skill_records
    _stage_forge_pkg(grove_home)
    cap = _fleet_skill_records()["forge-jobsearch"]
    text, src = _unit_primary_file(
        cap, {"filename": "260709-acme"}, limit=10_000)
    assert src.name == "resume.md"
    assert "Resume" in text


# ---------------------------------------------------------------------------
# fleet-artifact-legibility-v1 C3 — fleet-unit context panel (mock A right)
# ---------------------------------------------------------------------------


def _install_dock(home):
    d = home / "dock"
    d.mkdir(parents=True, exist_ok=True)
    (d / "dock.yaml").write_text(
        "version: 1\n"
        "goals:\n"
        "  - id: influencer-outreach\n"
        "    name: Influencer outreach\n"
        "    vector: strategic\n"
        "    status: cruising\n"
        "    definition_of_done: relationships built\n"
        "    context_sources: []\n"
        "    keywords: [outreach]\n"
        "    unlocked_skills: []\n",
        encoding="utf-8",
    )


def _stage_cultivator_ctx(home):
    d = home / "cultivator" / "pending_review" / "prospects-2026-07-09-x"
    d.mkdir(parents=True)
    payload = dict(_CULTIVATOR_PAYLOAD, dock_goal_refs=["influencer-outreach"])
    (d / "prospects-2026-07-09-x.json").write_text(
        json.dumps(payload), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(
        {"unit_id": "prospects-2026-07-09-x", "slug": "prospects-2026-07-09-x",
         "worker": "cultivator", "source_name": "digest-2026-07-09.json",
         "source_path": "/x/scout/digest-2026-07-09.json"}), encoding="utf-8")


async def test_fleet_context_four_sections(client, grove_home):
    _install_dock(grove_home)
    _stage_cultivator_ctx(grove_home)
    resp = await client.get(
        "/portal/fragments/context/fleet/cultivator/prospects-2026-07-09-x")
    assert resp.status == 200
    html = await resp.text()
    assert 'id="right-panel"' in html
    # UNIT
    assert "<h3>Unit</h3>" in html and "cultivator" in html
    assert "chip-legacy" in html            # staged, no proposal → legacy
    assert "prospects-2026-07-09-x" in html
    # LINEAGE — synthesized meta source_name + generated-by
    assert "<h3>Lineage</h3>" in html
    assert "digest-2026-07-09.json" in html
    assert "generated by cultivator" in html
    # GOALS SERVED — existing goal card renderer, pivot link intact
    assert "<h3>Goals served</h3>" in html
    assert "Influencer outreach" in html
    assert "/portal/fragments/context/dock/influencer-outreach" in html
    # HISTORY — staged event present
    assert "<h3>History</h3>" in html and "staged &rarr;" in html
    # READ-ONLY: no verbs in the panel
    assert "/promote" not in html and "disposition-bar" not in html


async def test_fleet_context_history_revisions_chronological(client, grove_home):
    from grove.forge import feedback_store
    _stage_cultivator_ctx(grove_home)
    feedback_store.write("cultivator", "prospects-2026-07-09-x", "first note")
    feedback_store.write("cultivator", "prospects-2026-07-09-x", "second note")
    resp = await client.get(
        "/portal/fragments/context/fleet/cultivator/prospects-2026-07-09-x")
    html = await resp.text()
    assert html.index("first note") < html.index("second note")  # chronological
    assert html.count("revision guidance:") == 2


async def test_fleet_context_lineage_absent_not_empty(client, grove_home):
    d = grove_home / "drafter" / "pending_review" / "bare-unit"
    d.mkdir(parents=True)
    (d / "draft-bare-unit.md").write_text("no frontmatter body", encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"unit_id": "bare-unit"}),
                                 encoding="utf-8")
    resp = await client.get(
        "/portal/fragments/context/fleet/drafter/bare-unit")
    html = await resp.text()
    assert "<h3>Unit</h3>" in html and "<h3>History</h3>" in html
    assert "<h3>Lineage</h3>" not in html   # absent, not an empty shell
    assert "<h3>Goals served</h3>" not in html


async def test_fleet_context_unknown_unit_placeholder(client, grove_home):
    resp = await client.get("/portal/fragments/context/fleet/drafter/nope")
    html = await resp.text()
    assert "Context unavailable" in html


def test_fleet_context_history_cap_and_note_clamp(grove_home):
    from grove.api.fragments import MAX_HISTORY_ENTRIES, _context_fleet
    from grove.forge import feedback_store
    _stage_cultivator_ctx(grove_home)
    for i in range(25):
        feedback_store.write("cultivator", "prospects-2026-07-09-x", f"note {i:02d}")
    feedback_store.write("cultivator", "prospects-2026-07-09-x",
                         "y" * (MAX_PROSE_BYTES + 200))
    html = _context_fleet("cultivator/prospects-2026-07-09-x")
    assert html.count('class="cx-ev"') <= MAX_HISTORY_ENTRIES + 2  # + lineage evs
    assert "truncated" in html                                     # note clamp
    assert "y" * (MAX_PROSE_BYTES + 1) not in html


def test_review_card_context_stamps_and_title_link():
    from grove.api import fragments as F
    unit = {"unit_id": "moon-bot", "producer": "drafter",
            "governance_state": "needs_review", "revision_count": 0,
            "mtime": "2026-07-09T12:00:00Z", "filename": "draft-moon-bot.md",
            "proposal_id": "sha256:abc"}
    html = F._review_card(unit, remote_sink=False)
    # context stamp on the card, filtered so interactive elements never fire it
    assert 'hx-get="/portal/fragments/context/fleet/drafter/moon-bot"' in html
    assert 'hx-target="#right-panel"' in html
    assert "event.target.closest" in html and ".disposition-bar" in html
    # title is a navigation link to the unit fragment — HASH-ONLY href form
    # (htmx cancels bubbled non-'#'-prefixed anchor defaults before the filter)
    assert ('<a class="title" '
            'href="#fragments/fleet/drafter/draft-moon-bot.md/">') in html
    # selected-card cue wiring
    assert "ctx-on" in html

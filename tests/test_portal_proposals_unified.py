"""Sprint P3.1 (portal-reader-contract-fix) — unified proposals review surface.

The portal's proposals panel must read BOTH backing files:

* ``proposals.jsonl``        — routing proposals (RoutingProposal records)
* ``memory_proposals.jsonl`` — memory_context crystallizations staged by the
  detector as ``{session_id, status, timestamp, proposal}`` records.

Before this sprint the portal read only the routing file, so 59 pending
memory crystallizations rendered as "No pending proposals". These tests pin
the dual-read for both the JSON endpoint and the HTMX fragment, and the
graceful (logged, non-crashing) handling of an empty or missing memory file.

Substrate is isolated to a temp GROVE_HOME per test, mirroring the P2 portal
fixtures: a couple of synthetic proposals.jsonl / memory_proposals.jsonl files
written directly so each test controls the exact record set.
"""

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from grove.api import (
    init_substrate_singletons,
    portal_auth_middleware,
    register_portal_routes,
)
from grove.api.fragments import _PORTAL_ASSETS, register_fragment_routes
from grove.eval import proposal_queue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_routing_proposal(home):
    """One routing proposal in ~/.grove/proposals.jsonl via the real writer."""
    proposal_queue.append(proposal_queue.RoutingProposal(
        proposal_id="routing_update:abc123",
        type="routing_update",
        payload={"rule": "downward", "add_intents": ["greet"]},
        evidence=("turn_1",),
        eval_hash="hash1",
        created_at="2026-06-26T00:00:00Z",
    ))


def _memory_record(content, *, status="pending", confidence=0.8, session_id="s1"):
    """A detector-shaped memory_proposals.jsonl record (create action)."""
    return {
        "session_id": session_id,
        "status": status,
        "timestamp": "2026-06-26T01:00:00Z",
        "proposal": {
            "action": "create",
            "proposed_record": {
                "entity_type": "DomainFact",
                "content": content,
                "confidence": confidence,
                "justification": "observed repeatedly",
            },
        },
    }


def _write_memory_proposals(home, records):
    """Write detector-shaped records to ~/.grove/memory_proposals.jsonl."""
    path = home / "memory_proposals.jsonl"
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records),
        encoding="utf-8",
    )


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(tmp_path / "wiki"))
    (tmp_path / "wiki" / "pages").mkdir(parents=True)
    return tmp_path


@pytest.fixture
async def client(grove_home):
    app = web.Application(middlewares=[portal_auth_middleware])
    init_substrate_singletons(app)
    register_portal_routes(app)
    app.router.add_static("/portal/static", str(_PORTAL_ASSETS))
    register_fragment_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


# ---------------------------------------------------------------------------
# JSON endpoint: /api/substrate/proposals/pending
# ---------------------------------------------------------------------------


async def test_json_endpoint_unions_routing_and_memory(client, grove_home):
    _write_routing_proposal(grove_home)
    _write_memory_proposals(grove_home, [_memory_record("Grove is sovereign.")])

    r = await client.get("/api/substrate/proposals/pending")
    assert r.status == 200
    body = await r.json()
    assert body["meta"]["count"] == 2
    types = {item["type"] for item in body["data"]}
    assert types == {"routing_update", "memory_context"}


async def test_json_endpoint_empty_memory_file_returns_only_routing(client, grove_home):
    _write_routing_proposal(grove_home)
    _write_memory_proposals(grove_home, [])  # exists but empty

    r = await client.get("/api/substrate/proposals/pending")
    assert r.status == 200
    body = await r.json()
    assert body["meta"]["count"] == 1
    assert body["data"][0]["type"] == "routing_update"


async def test_json_endpoint_missing_memory_file_returns_only_routing(client, grove_home):
    _write_routing_proposal(grove_home)
    # No memory_proposals.jsonl written at all.
    assert not (grove_home / "memory_proposals.jsonl").exists()

    r = await client.get("/api/substrate/proposals/pending")
    assert r.status == 200
    body = await r.json()
    assert body["meta"]["count"] == 1
    assert body["data"][0]["type"] == "routing_update"


async def test_json_endpoint_filters_memory_to_pending(client, grove_home):
    _write_memory_proposals(grove_home, [
        _memory_record("Pending fact.", status="pending"),
        _memory_record("Approved fact.", status="approved"),
        _memory_record("Rejected fact.", status="rejected"),
    ])

    r = await client.get("/api/substrate/proposals/pending")
    assert r.status == 200
    body = await r.json()
    memory_items = [i for i in body["data"] if i["type"] == "memory_context"]
    assert len(memory_items) == 1
    blob = json.dumps(body["data"])
    assert "Pending fact." in blob
    assert "Approved fact." not in blob
    assert "Rejected fact." not in blob


# ---------------------------------------------------------------------------
# HTMX fragment: /portal/fragments/proposals/pending
# ---------------------------------------------------------------------------


async def test_fragment_renders_both_routing_and_memory_cards(client, grove_home):
    _write_routing_proposal(grove_home)
    _write_memory_proposals(grove_home, [_memory_record("Grove runs on sovereignty.")])

    r = await client.get("/portal/fragments/proposals/pending")
    assert r.status == 200
    body = await r.text()
    assert 'id="proposals-listing"' in body
    # Routing card still rendered.
    assert "routing_update" in body
    # Memory card: type badge + the summary_renderer content.
    assert "memory_context" in body
    assert "Grove runs on sovereignty." in body
    # The empty-state placeholder must NOT appear when proposals exist.
    assert "No pending proposals" not in body


# ---------------------------------------------------------------------------
# fleet-ui-reconciliation-v1 C3 — one review surface: artifact partition
# ---------------------------------------------------------------------------


def _file_artifact_proposal(ptype, slug):
    """One live artifact-pending proposal via the real agentless writer."""
    proposal_queue.file_agentless(
        type=ptype,
        payload={"slug": slug, "unit_id": slug,
                 "skill_id": "skill.fleet.drafter", "canonical_sink": "drafter"},
        evidence=(slug,), justification="t", proposer="skill.fleet.drafter",
    )


async def test_artifact_proposals_partition_into_xlink_card(client, grove_home):
    """Mixed queue: artifact-pending types render as ONE cross-link card (N=2),
    zero artifact cards below; routing + memory cards unchanged."""
    _write_routing_proposal(grove_home)
    _write_memory_proposals(grove_home, [_memory_record("Pending fact.")])
    _file_artifact_proposal(
        proposal_queue.PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING, "moon-bot")
    _file_artifact_proposal(
        proposal_queue.PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING, "260709-acme")

    r = await client.get("/portal/fragments/proposals/pending")
    assert r.status == 200
    body = await r.text()
    # ONE cross-link card with the artifact count, linking into Fleet.
    assert body.count('class="card xlink"') == 1
    assert "2 fleet artifact(s) awaiting review" in body
    assert 'href="/portal#fragments/fleet/"' in body
    # ZERO artifact cards: neither type badge nor Promote affordance renders.
    assert "fleet_artifact_pending" not in body
    assert "forge_artifact_pending" not in body
    assert "/promote" not in body
    # Non-artifact cards unchanged.
    assert "routing_update" in body and "Pending fact." in body


async def test_no_artifact_proposals_no_xlink_card(client, grove_home):
    _write_routing_proposal(grove_home)
    r = await client.get("/portal/fragments/proposals/pending")
    body = await r.text()
    assert 'class="card xlink"' not in body


async def test_grouped_view_also_partitions_artifacts(client, grove_home):
    _write_routing_proposal(grove_home)
    _file_artifact_proposal(
        proposal_queue.PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING, "moon-bot")
    r = await client.get("/portal/fragments/proposals/pending?view=grouped")
    body = await r.text()
    assert body.count('class="card xlink"') == 1
    assert "fleet_artifact_pending" not in body
    # the artifact proposer never gets a section of its own
    assert 'data-proposer="skill.fleet.drafter"' not in body


async def test_proposals_nav_badge_matches_page_card_count(client, grove_home):
    """F3 — badge N == rendered card N, by the same partition: 1 routing + 1
    memory = 2; the 2 artifact proposals never count here (they count in the
    Fleet badge, C2)."""
    _write_routing_proposal(grove_home)
    _write_memory_proposals(grove_home, [_memory_record("Pending fact.")])
    _file_artifact_proposal(
        proposal_queue.PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING, "moon-bot")
    _file_artifact_proposal(
        proposal_queue.PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING, "260709-acme")

    r = await client.get("/portal/fragments/nav/proposals")
    assert r.status == 200
    body = await r.text()
    assert 'href="/portal#fragments/proposals/pending"' in body
    assert '<span class="nav-badge hot">2</span>' in body

    # and the page renders exactly 2 disposition-bearing cards
    page = await (await client.get("/portal/fragments/proposals/pending")).text()
    assert page.count('class="proposal-actions"') == 2


# ---------------------------------------------------------------------------
# proposal-card-legibility-v1 Phase 3 — registry-composed cards
# ---------------------------------------------------------------------------


def _write_typed_proposal(ptype, payload, *, pid, sj="", detail=None,
                          evidence=("t_1",)):
    """One proposal of an arbitrary type via the real queue writer."""
    proposal_queue.append(proposal_queue.RoutingProposal(
        proposal_id=pid,
        type=ptype,
        payload=payload,
        evidence=tuple(evidence),
        eval_hash="",
        created_at="2026-07-11T00:00:00+00:00",
        semantic_justification=sj,
        proposer="test",
        detail=detail,
    ))


async def test_dock_mutation_card_shows_summary_and_diff(client, grove_home):
    """The GATE-A defect card: an approvable dock_mutation must show the
    registry summary line AND the dock.autonomaton.yaml +add diff — the
    operator sees the goal they are consenting to. Evidence subordinates
    under <details>; the approve affordance is present."""
    goal = {
        "id": "auto-ecosystem", "name": "Ecosystem & Platform Strategy",
        "keywords": ["platform"], "vector": "personal", "status": "staging",
        "source_record_ids": [f"mem_{i}" for i in range(10)],
    }
    _write_typed_proposal(
        "dock_mutation", {"action": "create_goal", "goal": goal},
        pid="sha256:" + "d" * 64, evidence=tuple(goal["source_record_ids"]),
    )
    body = await (await client.get("/portal/fragments/proposals/pending")).text()
    # Summary line (registry renderer — same line the CLI prints).
    assert "10 memory records accumulating around" in body
    assert "Ecosystem &amp; Platform Strategy" in body
    # Material diff in <pre>: target file + +add block + goal fields.
    assert "dock.autonomaton.yaml" in body
    assert "+add" in body
    assert "auto-ecosystem" in body
    # Evidence subordinated, not a bare count line.
    assert "<details" in body and "evidence (10)" in body
    assert "evidence: 10 item(s)" not in body
    # Approvable: the approve affordance is offered.
    assert "/approve" in body


async def test_zone_promotion_card_shows_summary_and_diff(client, grove_home):
    _write_typed_proposal(
        "zone_promotion",
        {"tool": "mcp_grove_browser_read", "pattern": r".*\.md$",
         "zone": "green", "reason": "docs are safe"},
        pid="sha256:" + "e" * 64,
    )
    body = await (await client.get("/portal/fragments/proposals/pending")).text()
    assert "greenlight mcp_grove_browser_read" in body
    assert "tool_zones" in body
    assert "docs are safe" in body


async def test_fault_triage_card_judgment_led_with_samples(client, grove_home):
    """Rider 4 — judgment + counts lead; detail.samples render as compact
    date · subject · outcome lines inside <details>; the raw-JSON sj tail
    never reaches the lead."""
    sj = (
        "terminal is hitting the same RED shell.effect.red repeatedly — "
        "one defect, recurring, worsening.\n"
        "Seen 5 times across 3 session(s) in the last 14d "
        "(first 2026-07-05T22:34:55.020576+00:00, last 2026-07-08T17:31:09.753657+00:00).\n"
        'Samples: {"event_type": "red_resolution", "session_id": "20260705_183408_b871b096"}'
    )
    detail = {"samples": [
        {"ts": "2026-07-05", "subject": "terminal", "outcome": "cancel"},
        {"ts": "2026-07-08", "subject": "terminal", "outcome": "store_pending_approval"},
    ]}
    _write_typed_proposal(
        "fault_triage",
        {"source": "red_resolution", "tool": "terminal",
         "matched_rule": "shell.effect.red", "error_signature": ""},
        pid="sha256:" + "f" * 64, sj=sj, detail=detail,
        evidence=("fault_triage:synthetic",),
    )
    body = await (await client.get("/portal/fragments/proposals/pending")).text()
    # Judgment line leads as the card body.
    assert "one defect, recurring, worsening." in body
    # Counts line as meta.
    assert "Seen 5 times across 3 session(s)" in body
    # Compact sample lines inside <details>.
    assert "samples (2)" in body
    assert "2026-07-05 · terminal · cancel" in body
    assert "2026-07-08 · terminal · store_pending_approval" in body
    # The raw JSON sample dump stays OUT of the rendered card.
    assert "20260705_183408_b871b096" not in body
    assert "&quot;event_type&quot;" not in body
    # Verb set is acknowledge/dismiss — never approve.
    assert "/acknowledge" in body
    assert "/approve" not in body


async def test_fault_triage_legacy_row_falls_back_to_sj(client, grove_home):
    """A pre-Phase-2 queue row (no detail envelope) still renders: judgment
    lead + the verbatim sj collapsed under <details>. Never blank."""
    sj = (
        "cultivator is hitting the same resolver_failed fault repeatedly — "
        "one defect, active.\n"
        "Seen 6 times across 2 session(s) in the last 14d (first x, last y).\n"
        'Samples: {"raw": "json"}'
    )
    _write_typed_proposal(
        "fault_triage",
        {"source": "fleet_worker", "worker": "cultivator",
         "check": "resolver_failed", "error_signature": ""},
        pid="sha256:" + "a" * 64, sj=sj,
        evidence=("fault_triage:legacy",),
    )
    body = await (await client.get("/portal/fragments/proposals/pending")).text()
    assert "one defect, active." in body
    assert "Seen 6 times across 2 session(s)" in body
    # Verbatim sj collapsed — present, but inside a <details> block.
    assert "<details" in body and "<summary>details</summary>" in body
    assert "&quot;raw&quot;: &quot;json&quot;" in body


async def test_approvable_render_failure_is_defect_card(client, grove_home):
    """Rider 3 — an approvable proposal whose diff renderer raises (here: a
    routing_adjustment with an unknown sink rule) renders the DEFECT card:
    badge + id + inspect directive, and NO disposition buttons."""
    _write_typed_proposal(
        "routing_adjustment",
        {"rule": "not_a_real_sink", "add_intents": ["greet"]},
        pid="sha256:" + "b" * 64,
    )
    body = await (await client.get("/portal/fragments/proposals/pending")).text()
    assert "DEFECT — mutation cannot be rendered." in body
    assert "hermes flywheel show" in body
    # No disposition affordances on the defect card (it is the only card).
    assert "/approve" not in body
    assert "/reject" not in body
    assert "/dismiss" not in body


async def test_render_only_failure_falls_back_to_sj(client, grove_home, monkeypatch):
    """Rider 3 (render-only arm) — a render-only type whose renderer raises
    falls back to the verbatim sj with a warning log; the card still renders
    with its dismiss affordance (never a defect card, never blank)."""
    from grove.kaizen import rendering as kaizen_rendering

    def _boom(_):
        raise RuntimeError("synthetic renderer failure")

    monkeypatch.setitem(
        kaizen_rendering.RENDER_REGISTRY, "portal_action_failure", _boom)
    _write_typed_proposal(
        "portal_action_failure",
        {"failure_class": "http_500", "action": "dock_status"},
        pid="sha256:" + "c" * 64,
        sj="portal action 'dock_status' keeps failing (http_500) [detail]",
    )
    body = await (await client.get("/portal/fragments/proposals/pending")).text()
    assert "keeps failing (http_500)" in body
    assert "DEFECT" not in body
    assert "/dismiss" in body


# ---------------------------------------------------------------------------
# proposal-feed-navigation-v1 — per-type subnav + composable filters
# ---------------------------------------------------------------------------

_FT_SJ = (
    "cultivator is hitting the same resolver_failed fault repeatedly — "
    "one defect, active.\n"
    "Seen 6 times across 2 session(s) in the last 14d (first x, last y).\n"
    'Samples: {"raw": "json"}'
)


def _write_fault_triage(pid_char):
    _write_typed_proposal(
        "fault_triage",
        {"source": "fleet_worker", "worker": "cultivator",
         "check": "resolver_failed", "error_signature": pid_char},
        pid="sha256:" + pid_char * 64, sj=_FT_SJ,
        evidence=(f"fault_triage:{pid_char}",),
    )


async def test_type_tabs_counts_match_rendered_cards(client, grove_home):
    """(a) Tab badges come from the unfiltered post-partition feed; each type
    tab's count equals exactly the card count its filter renders. Order: all,
    types by count desc, memory."""
    _write_routing_proposal(grove_home)
    _write_fault_triage("1")
    _write_fault_triage("2")
    _write_memory_proposals(grove_home, [
        _memory_record("Fact A."), _memory_record("Fact B."),
    ])

    body = await (await client.get("/portal/fragments/proposals/pending")).text()
    assert 'class="queue-tabs"' in body
    assert "all &middot; 5" in body
    assert "fault_triage &middot; 2" in body
    assert "routing_update &middot; 1" in body
    assert "memory &middot; 2" in body
    # Tab order: all, then types by count desc, then memory.
    assert (body.index("all &middot;") < body.index("fault_triage &middot;")
            < body.index("routing_update &middot;") < body.index("memory &middot;"))
    # Tab count == rendered card count under that tab's own filter.
    for t, n in (("fault_triage", 2), ("routing_update", 1), ("memory", 2)):
        page = await (await client.get(
            f"/portal/fragments/proposals/pending?type={t}")).text()
        assert page.count('<div class="card"') == n, t


async def test_type_filter_selects_one_type_and_memory_pseudo_type(client, grove_home):
    """(b) ?type= narrows to one proposal type (memory excluded); ?type=memory
    shows memory cards only; unrecognized value → no filter."""
    _write_routing_proposal(grove_home)
    _write_memory_proposals(grove_home, [_memory_record("Only memory fact.")])

    page = await (await client.get(
        "/portal/fragments/proposals/pending?type=routing_update")).text()
    assert '<span class="badge">routing_update</span>' in page
    assert "Only memory fact." not in page

    page = await (await client.get(
        "/portal/fragments/proposals/pending?type=memory")).text()
    assert "Only memory fact." in page
    assert '<span class="badge">routing_update</span>' not in page

    page = await (await client.get(
        "/portal/fragments/proposals/pending?type=not_a_type")).text()
    assert '<span class="badge">routing_update</span>' in page
    assert "Only memory fact." in page


async def test_class_filter_actionable_renderonly_split(client, grove_home):
    """(c) ?class= splits on the adjudicated predicate: _type_offers_approve
    OR PROPOSAL_VERBS. routing_update (handler) and fault_triage (verb set)
    are actionable; portal_action_failure is render-only; memory cards are
    actionable."""
    _write_routing_proposal(grove_home)
    _write_fault_triage("3")
    _write_typed_proposal(
        "portal_action_failure",
        {"failure_class": "http_500", "action": "dock_status"},
        pid="sha256:" + "9" * 64,
        sj="portal action 'dock_status' keeps failing (http_500)",
    )
    _write_memory_proposals(grove_home, [_memory_record("Mem fact.")])

    page = await (await client.get(
        "/portal/fragments/proposals/pending?class=actionable")).text()
    assert '<span class="badge">routing_update</span>' in page
    assert '<span class="badge">fault_triage</span>' in page
    assert '<span class="badge">portal_action_failure</span>' not in page
    assert "Mem fact." in page

    page = await (await client.get(
        "/portal/fragments/proposals/pending?class=renderonly")).text()
    assert '<span class="badge">portal_action_failure</span>' in page
    assert '<span class="badge">routing_update</span>' not in page
    assert '<span class="badge">fault_triage</span>' not in page
    assert "Mem fact." not in page


def test_get_record_time_prefers_created_at_falls_back_timestamp():
    """(d, unit) _get_record_time: created_at first, timestamp fallback,
    empty-string floor."""
    from grove.api.fragments import _get_record_time

    assert _get_record_time({"created_at": "A"}) == "A"
    assert _get_record_time({"timestamp": "B"}) == "B"
    assert _get_record_time({"created_at": "", "timestamp": "B"}) == "B"
    assert _get_record_time({}) == ""


async def test_age_filter_cutoff_on_both_stores(client, grove_home):
    """(d) ?age= keeps records newer than the ISO cutoff on both stores:
    routing proposals key created_at, memory records key timestamp (projected
    to created_at by the portal reader)."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    old, fresh = ((now - timedelta(days=30)).isoformat(),
                  (now - timedelta(hours=1)).isoformat())
    for pid, ts in (("routing_update:old00001", old),
                    ("routing_update:fresh001", fresh)):
        proposal_queue.append(proposal_queue.RoutingProposal(
            proposal_id=pid, type="routing_update",
            payload={"rule": "downward", "add_intents": ["greet"]},
            evidence=("t",), eval_hash="h", created_at=ts,
        ))
    old_mem = _memory_record("Old memory fact.")
    old_mem["timestamp"] = old
    fresh_mem = _memory_record("Fresh memory fact.")
    fresh_mem["timestamp"] = fresh
    _write_memory_proposals(grove_home, [old_mem, fresh_mem])

    page = await (await client.get(
        "/portal/fragments/proposals/pending?age=7d")).text()
    assert page.count('<span class="badge">routing_update</span>') == 1
    assert "Fresh memory fact." in page
    assert "Old memory fact." not in page

    page = await (await client.get(
        "/portal/fragments/proposals/pending?age=24h")).text()
    assert page.count('<span class="badge">routing_update</span>') == 1
    assert "Fresh memory fact." in page
    assert "Old memory fact." not in page

    # No age param → everything renders.
    page = await (await client.get(
        "/portal/fragments/proposals/pending")).text()
    assert page.count('<span class="badge">routing_update</span>') == 2
    assert "Old memory fact." in page


async def test_nav_badge_is_unfiltered_while_filter_active(client, grove_home):
    """(e) The nav badge (handle_proposals_nav) counts the unfiltered total —
    a page filter narrows cards but never the badge."""
    _write_routing_proposal(grove_home)
    _write_memory_proposals(grove_home, [_memory_record("Pending fact.")])

    nav = await (await client.get("/portal/fragments/nav/proposals")).text()
    assert '<span class="nav-badge hot">2</span>' in nav
    page = await (await client.get(
        "/portal/fragments/proposals/pending?type=memory")).text()
    assert page.count('<div class="card"') == 1
    nav = await (await client.get("/portal/fragments/nav/proposals")).text()
    assert '<span class="nav-badge hot">2</span>' in nav


async def test_filters_compose_with_grouped_view(client, grove_home):
    """(f) ?type= composes with ?view=grouped: grouped runs on the filtered
    set, and the view toggle round-trips all active filter params."""
    _write_routing_proposal(grove_home)          # proposer → unattributed
    _write_fault_triage("4")                     # proposer → test
    _write_memory_proposals(grove_home, [_memory_record("Mem fact.")])

    body = await (await client.get(
        "/portal/fragments/proposals/pending?type=fault_triage&view=grouped"
    )).text()
    assert 'data-proposer="test"' in body
    assert 'data-proposer="unattributed"' not in body
    assert 'data-proposer="memory"' not in body      # memory excluded by type
    # Round-trip: both toggle hrefs carry the active filter.
    assert ('hx-get="/portal/fragments/proposals/pending'
            '?view=grouped&type=fault_triage"') in body
    assert ('hx-get="/portal/fragments/proposals/pending'
            '?type=fault_triage"') in body


async def test_hostile_param_values_sanitized(client, grove_home):
    """(g) Hostile param values are stripped to alphanumerics + underscore;
    the leftovers are unrecognized → no filter, no markup injection."""
    _write_routing_proposal(grove_home)
    r = await client.get(
        "/portal/fragments/proposals/pending",
        params={"type": "../<script>alert(1)</script>",
                "class": "action<img>able!", "age": "24h; DROP TABLE"},
    )
    assert r.status == 200
    body = await r.text()
    assert "<script>" not in body and "<img>" not in body
    # Sanitized values are unrecognized → no filter: the card still renders.
    assert '<span class="badge">routing_update</span>' in body

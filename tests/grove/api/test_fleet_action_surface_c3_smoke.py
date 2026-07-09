"""Render + wiring smoke for fleet-review-unification-v1 C3 — the Action Surface
disposition component (two mounts) over the C2 four-state API.

VERDICT A — write-path untouched: C3 is style.css + fragments.py only (structural;
asserted by the sprint diff). The render helpers below perform NO writes.
VERDICT B — render matrix: each C2 state → correct rail / chip / affordances;
revision-bearing needs_review shows the version + directive echo; terminals dim;
legacy is list-only; empty producer is clean.
VERDICT C — both mounts (card + dock) render for forge AND a file producer; the
feedback block toggles + hx-includes the textarea; the revision counter reflects count.
VERDICT D — the wired verbs drive the EXISTING routes; a suggest_revision from the
surface stores feedback and increments the N-breaker (both proposal types).
"""

from __future__ import annotations

import json
import re

import pytest

from grove.api import fragments as F


def _unit(state, **kw):
    u = {"unit_id": "moon-bot", "producer": "drafter", "governance_state": state,
         "revision_count": 0, "mtime": "2026-07-09T12:00:00Z", "filename": "draft-moon-bot.md"}
    u.update(kw)
    return u


# ---------------------------------------------------------------------------
# VERDICT B — render matrix
# ---------------------------------------------------------------------------


def test_needs_review_card_rail_chip_and_bar():
    html = F._review_card(_unit("needs_review", proposal_id="sha256:abc123"), remote_sink=False)
    assert "review-card rail-needs_review" in html
    assert 'class="state-chip chip-needs_review"' in html and "needs review" in html
    # full disposition bar wired to the three verbs
    assert "/proposals/sha256:abc123/promote" in html
    assert "/proposals/sha256:abc123/reject" in html
    assert "/proposals/sha256:abc123/suggest_revision" in html
    assert "card-resolved" not in html  # not dimmed


def test_needs_review_revision_bearing_shows_version_and_echo():
    html = F._review_card(
        _unit("needs_review", proposal_id="sha256:x", revision_count=2,
              directive_echo="Rewrite #1 to Number one"), remote_sink=False)
    assert "draft v3" in html  # revision_count + 1
    assert 'class="revised-disclosure"' in html
    assert "Rewrite #1 to Number one" in html


def test_revision_requested_inflight_note_no_bar():
    html = F._review_card(
        _unit("revision_requested", revision_count=1, directive_echo="tighten"), remote_sink=False)
    assert "rail-revision_requested" in html
    assert 'class="inflight-note"' in html and "Guidance banked" in html
    assert "tighten" in html
    assert 'class="disposition-bar"' not in html  # no actions in-flight


@pytest.mark.parametrize("state", ["promoted", "rejected"])
def test_terminal_states_dim_no_bar(state):
    html = F._review_card(_unit(state), remote_sink=False)
    assert f"rail-{state}" in html and "card-resolved" in html
    assert 'class="disposition-bar"' not in html


def test_legacy_is_list_only():
    html = F._review_card(_unit("legacy"), remote_sink=False)
    assert "rail-legacy" in html
    assert 'class="disposition-bar"' not in html and "card-resolved" not in html


# ---------------------------------------------------------------------------
# VERDICT C — both mounts, feedback toggle, hx-include, counter, sink copy
# ---------------------------------------------------------------------------


def test_both_mounts_render_bar_for_forge_and_file_producer():
    for remote in (True, False):
        u = _unit("needs_review", proposal_id="sha256:m")
        card = F._review_card(u, remote_sink=remote)
        dock = F._disposition_dock(u, remote_sink=remote, producer="p")
        for html in (card, dock):
            assert "disposition-bar" in html and "/promote" in html
        assert "disposition-dock" in dock and "dock-meta" in dock


def test_promote_consequence_copy_by_sink():
    forge = F._disposition_bar("sha256:a", remote_sink=True)
    mv = F._disposition_bar("sha256:a", remote_sink=False)
    assert "publish to Drive" in forge and "publish to Drive" not in mv
    assert "ingest to wiki" in mv


def test_feedback_block_toggles_and_hx_includes_textarea():
    html = F._disposition_bar("sha256:deadbeef", remote_sink=False)
    short = F._short_id("sha256:deadbeef")
    rev_id = "rev-" + short
    assert 'class="feedback-block"' in html
    assert f'id="{rev_id}"' in html and 'name="revision_text"' in html
    assert f'hx-include="#{rev_id}"' in html          # colon-free id (C1a Andon)
    assert "classList.toggle('open')" in html          # the disclosure toggle
    assert "Send guidance &amp; redraft" in html
    assert "Cancel" in html


def test_revision_counter_reflects_count():
    assert "Revision 1 of 3" in F._disposition_bar("sha256:a", False, revision_count=0)
    assert "Revision 3 of 3" in F._disposition_bar("sha256:a", False, revision_count=2)


def test_relative_age_buckets():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    def iso(**kw):
        return (now - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert F._relative_age(iso(seconds=10)) == "just now"
    assert F._relative_age(iso(minutes=38)) == "38 min ago"
    assert F._relative_age(iso(hours=3)) == "3 h ago"
    assert F._relative_age(iso(hours=30)) == "yesterday"
    assert F._relative_age(iso(days=2)) == "2 days ago"
    assert F._relative_age("not-a-date") == "not-a-date"  # never raises


def test_state_chip_all_states_tinted():
    for state in ("needs_review", "revision_requested", "promoted", "rejected", "legacy"):
        chip = F._state_chip(state)
        assert f"chip-{state}" in chip and '<span class="dot">' in chip


# ---------------------------------------------------------------------------
# VERDICT C (mount 1 inbox) + D (verbs end-to-end from the surface)
# ---------------------------------------------------------------------------


@pytest.fixture()
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


def _stage(home, sink, unit, content_name, meta):
    d = home / sink / "pending_review" / unit
    d.mkdir(parents=True, exist_ok=True)
    (d / content_name).write_text("---\ntitle: X\n---\nbody text here", encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


async def test_inbox_page_renders_pending_pill_and_cards(grove_home):
    from types import SimpleNamespace
    _stage(grove_home, "drafter", "moon-bot", "draft-moon-bot.md",
           {"unit_id": "moon-bot", "slug": "moon-bot"})
    from grove.eval.proposal_queue import PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING as T
    from grove.eval import proposal_queue
    proposal_queue.file_agentless(
        type=T, payload={"slug": "moon-bot", "unit_id": "moon-bot",
                         "skill_id": "skill.fleet.drafter", "canonical_sink": "drafter"},
        evidence=("moon-bot",), justification="t", proposer="skill.fleet.drafter")
    req = SimpleNamespace(match_info={"skill_name": "drafter"}, query={})
    resp = await F.handle_fleet_skill_fragment(req)
    body = resp.text
    assert 'class="pending-pill"' in body and "1 needs review" in body
    assert "review-card rail-needs_review" in body
    assert "ingest to wiki" in body  # mv-sink consequence copy
    assert "body text here" in body  # 3-line preview read from the staged content


@pytest.mark.parametrize("producer,worker,payload", [
    ("skill.fleet.drafter", "drafter",
     {"slug": "moon-bot", "unit_id": "moon-bot", "skill_id": "skill.fleet.drafter",
      "canonical_sink": "drafter"}),
    ("skill.fleet.forge-jobsearch", "forge",
     {"slug": "260707-x", "row_id": "ROW-X", "skill_id": "skill.fleet.forge-jobsearch"}),
])
async def test_verb_from_surface_stores_feedback(grove_home, monkeypatch, producer, worker, payload):
    """VERDICT D — the suggest_revision URL the surface wires drives the REAL route:
    feedback is stored under (worker, unit_id) and the N-breaker count increments,
    for BOTH proposal types."""
    from types import SimpleNamespace
    from grove.api import actions
    from grove.eval import proposal_queue
    from grove.forge import feedback_store

    unit_id = payload.get("unit_id") or payload["row_id"]
    sink = "drafter" if worker == "drafter" else "forge"
    content = "draft-moon-bot.md" if worker == "drafter" else "resume.md"
    _stage(grove_home, sink, payload["slug"], content, payload)
    ptype = (proposal_queue.PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING if worker == "drafter"
             else proposal_queue.PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING)
    pid, _ = proposal_queue.file_agentless(
        type=ptype, payload=payload, evidence=(unit_id,), justification="t", proposer=producer)

    # the exact URL the C3 bar wires the "Send guidance & redraft" button to:
    bar = F._disposition_bar(pid, remote_sink=(worker == "forge"))
    m = re.search(r'hx-post="(/portal/actions/proposals/[^"]+/suggest_revision)"', bar)
    assert m, "surface must wire the suggest_revision route"

    async def _text(_r):
        return "Rewrite #1 to Number one"
    monkeypatch.setattr(actions, "_suggest_revision_text", _text)
    monkeypatch.setattr(actions, "broadcast_to_operator", lambda msg: _noop())

    req = SimpleNamespace(match_info={"proposal_id": pid})
    resp = await actions._suggest_revision_disposition(req, producer=worker)
    assert resp.status == 200
    entry = feedback_store.read(worker, unit_id)  # keyed (worker, unit_id)
    assert entry is not None and entry["count"] == 1  # N-breaker seeded from the surface


async def _noop():
    return None

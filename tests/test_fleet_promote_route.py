"""fleet-pipeline-v1 P3 — tap route + reject archive + contents-aware guard.

Safety-critical. GROVE_HOME is per-test isolated (autouse conftest), so the
default proposal queue + ledger + forge dirs land in a tempdir.
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

import pytest

from grove.api import actions
from grove.eval import proposal_queue as pq
from grove.eval.proposal_queue import PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING as FT
from grove.forge import PublishError
from grove.forge.publish import folder_name, publish_application_package
from hermes_constants import get_hermes_home


class _Req:
    def __init__(self, **mi):
        self.match_info = mi
        self.query = {}
        self.content_type = ""


def _emit(slug="260704-acme-pm", row_id="pg1"):
    pid, _ = pq.file_agentless(
        type=FT, payload={"slug": slug, "row_id": row_id, "skill_id": "skill.fleet.forge-jobsearch",
                          "fit_score": 91}, evidence=(row_id,))
    return pid


def _ledger_events():
    out = []
    for f in glob.glob(str(Path(get_hermes_home()) / ".kaizen_ledger" / "*.jsonl")):
        out += [json.loads(l) for l in open(f)]
    return out


# ── promote route: the two failure dispositions (Gemini 1a') ─────────────────


async def test_promote_happy_path_finalizes(monkeypatch):
    pid = _emit()
    monkeypatch.setattr(actions, "_forge_publish_core",
                        lambda slug, loop: _async({"ok": True, "folder_link": "drive://x", "row_id": "pg1"}))
    resp = await actions.handle_forge_promote(_Req(proposal_id=pid))
    assert resp.status == 200
    assert pq.read(pid) is None  # finalized -> removed from queue
    ev = [e for e in _ledger_events() if e["event_type"] == "kaizen_disposition"]
    assert ev and ev[-1]["disposition"] == "applied"
    assert ev[-1]["applied_result"] == {"folder_link": "drive://x"}


async def test_double_tap_returns_409_and_holds_first_lease(monkeypatch):
    pid = _emit()
    assert pq.set_lease(pid, holder="tap1") == pq.LEASE_ACQUIRED  # tap1 in flight
    resp = await actions.handle_forge_promote(_Req(proposal_id=pid))
    assert resp.status == 409
    assert pq.read(pid).lease["held_by"] == "tap1"  # first lease intact
    assert pq.read(pid) is not None  # NOT removed


async def test_timeout_keeps_lease_held(monkeypatch):
    pid = _emit()
    monkeypatch.setattr(actions, "_FORGE_PUBLISH_TIMEOUT", 0.05)

    async def _slow(slug, loop):
        import asyncio
        await asyncio.sleep(5)  # exceeds the tiny timeout
        return {"ok": True, "folder_link": "x"}

    monkeypatch.setattr(actions, "_forge_publish_core", _slow)
    resp = await actions.handle_forge_promote(_Req(proposal_id=pid))
    assert resp.status == 504
    # THE load-bearing assertion: the lease is STILL HELD after the timeout path
    held = pq.read(pid)
    assert held is not None and held.lease is not None  # not cleared, not finalized


async def test_completed_failure_clears_lease(monkeypatch):
    pid = _emit()
    monkeypatch.setattr(actions, "_forge_publish_core",
                        lambda slug, loop: _async(
                            {"ok": False, "kind": "forge_notion_cold", "status": 400,
                             "message": "Notion cold", "folder_link": "x"}))
    resp = await actions.handle_forge_promote(_Req(proposal_id=pid))
    assert resp.status == 400
    rec = pq.read(pid)
    assert rec is not None and rec.lease is None  # cleared -> re-tappable, record stays


async def test_promote_missing_proposal_404(monkeypatch):
    resp = await actions.handle_forge_promote(_Req(proposal_id="sha256:gone"))
    assert resp.status == 404


def _async(value):
    async def _c(*a, **k):
        return value
    return _c()


# ── reject: archive-then-clear ───────────────────────────────────────────────


async def test_reject_archives_then_clears(monkeypatch):
    slug = "260704-acme-pm"
    pid = _emit(slug=slug)
    home = Path(get_hermes_home())
    sink = home / "forge" / "pending_review" / slug
    sink.mkdir(parents=True)
    (sink / "resume.md").write_text("R")
    (sink / "meta.json").write_text(json.dumps({"row_id": "pg1"}))

    resp = await actions.handle_proposal_reject(_Req(proposal_id=pid))
    assert resp.status == 200
    # slug dir MOVED out of pending_review -> skip-marker cleared
    assert not sink.exists()
    archived = list((home / "forge" / ".archive").glob(f"{slug}-*"))
    assert len(archived) == 1 and (archived[0] / "resume.md").read_text() == "R"
    # proposal finalized rejected with the archive path
    assert pq.read(pid) is None
    ev = [e for e in _ledger_events() if e["event_type"] == "kaizen_disposition"]
    assert ev[-1]["disposition"] == "rejected"
    assert "archive_path" in ev[-1]["applied_result"]


def test_archive_before_finalize_ordering():
    # structural: the reject branch archives BEFORE finalize, so a crash between
    # leaves the proposal live (never cleared-with-unarchived-dir).
    import inspect
    src = inspect.getsource(actions._apply_routing)
    a = src.index("_archive_forge_slug")
    f = src.index("finalize_proposal_state")
    assert a < f, "archive must precede finalize in the reject branch"


# ── contents-aware Drive guard (Gemini 1b') ──────────────────────────────────


def _mock_gapi(folder_present, docs_present):
    """docs_present: set of doc titles already in the folder."""
    calls = []

    def gapi(service, action, positional, flags):
        calls.append((action, flags.get("--name")))
        if action == "search":
            q = positional[0]
            if "application/vnd.google-apps.folder" in q:
                return ([{"name": folder_name("Acme", "PM", "r1"), "id": "fid",
                          "webViewLink": "flink"}] if folder_present else [])
            for title in docs_present:  # doc-by-title search
                if f"name = '{title}'" in q:
                    return [{"name": title, "id": f"doc-{title}", "webViewLink": "dl"}]
            return []
        if action == "create-folder":
            return {"id": "fid-new", "webViewLink": "flink-new"}
        if action == "upload":
            return {"id": "up", "mimeType": "application/vnd.google-apps.document",
                    "webViewLink": "up-link"}
        return {}

    return gapi, calls


@pytest.fixture
def assets(tmp_path):
    r = tmp_path / "resume.md"; r.write_text("R")
    c = tmp_path / "cover-letter.md"; c.write_text("C")
    return str(r), str(c)


def test_guard_exists_when_folder_and_all_docs_present(assets, tmp_path):
    resume, cover = assets
    titles = {"Acme — PM — Resume", "Acme — PM — Cover Letter"}
    gapi, calls = _mock_gapi(folder_present=True, docs_present=titles)
    res = publish_application_package("r1", "Acme", "PM", resume, cover,
                                      gapi=gapi, audit_path=tmp_path / "a.jsonl")
    assert res["status"] == "exists"  # complete -> no create, no upload
    assert not [c for c in calls if c[0] == "upload"]


def test_guard_upserts_missing_doc_never_exists_on_partial(assets, tmp_path):
    resume, cover = assets
    # folder present but ONLY the resume doc exists — cover is missing (the crash gap)
    gapi, calls = _mock_gapi(folder_present=True, docs_present={"Acme — PM — Resume"})
    res = publish_application_package("r1", "Acme", "PM", resume, cover,
                                      gapi=gapi, audit_path=tmp_path / "a.jsonl")
    assert res["status"] == "published"  # NOT "exists" on a partial folder
    uploads = [c for c in calls if c[0] == "upload"]
    assert [u[1] for u in uploads] == ["Acme — PM — Cover Letter"]  # only the missing one


def test_guard_fails_loud_on_search_error(assets, tmp_path):
    resume, cover = assets

    def gapi(service, action, positional, flags):
        if action == "search" and "application/vnd.google-apps.folder" not in positional[0]:
            return {"error": "drive search 500"}  # doc verify search fails
        if action == "search":
            return [{"name": folder_name("Acme", "PM", "r1"), "id": "fid", "webViewLink": "f"}]
        return {}

    with pytest.raises(PublishError, match="doc verify search failed"):
        publish_application_package("r1", "Acme", "PM", resume, cover,
                                   gapi=gapi, audit_path=tmp_path / "a.jsonl")


# ── live-race: finalize-wins / sweep-wins under one _lock ────────────────────


def test_race_finalize_then_sweep(tmp_path):
    q = tmp_path / "q.jsonl"
    pid, _ = pq.file_agentless(type=FT, payload={"slug": "s"}, evidence=("s",), path=q)
    pq.set_lease(pid, holder="t", path=q)
    # finalize wins: removes the proposal (lease + all)
    assert pq.finalize_proposal_state(pid, "applied", {"folder_link": "x"},
                                      path=q, ledger_dir=tmp_path / "l") is True
    # sweep then finds nothing to revert — no resurrection, no double-effect
    assert pq.sweep_stuck_leases(path=q) == []
    assert pq.read(pid, path=q) is None


def test_race_sweep_then_finalize(tmp_path):
    q = tmp_path / "q.jsonl"
    pid, _ = pq.file_agentless(type=FT, payload={"slug": "s"}, evidence=("s",), path=q)
    pq.set_lease(pid, holder="t", path=q)
    # sweep wins: reverts the lease (proposal stays, actionable)
    reverted = pq.sweep_stuck_leases(path=q)
    assert [r.proposal_id for r in reverted] == [pid]
    assert pq.read(pid, path=q).lease is None
    # a later finalize STILL disposes cleanly (idempotent, no corruption)
    assert pq.finalize_proposal_state(pid, "applied", path=q, ledger_dir=tmp_path / "l") is True
    assert pq.read(pid, path=q) is None

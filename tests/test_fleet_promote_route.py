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
        self.app = {}  # P3 — the emit helper probes request.app for memory_store


def _emit(slug="260704-acme-pm", row_id="pg1"):
    pid, _ = pq.file_agentless(
        type=FT, payload={"slug": slug, "row_id": row_id, "skill_id": "skill.fleet.forge-jobsearch",
                          "fit_score": 91}, evidence=(row_id,))
    return pid


def _stage(slug="260704-acme-pm", row_id="pg1"):
    """Stage a forge package (P1 fixture — the canonicalize step precedes the
    mocked publish, so the promote sequence needs real staged content)."""
    slug_dir = Path(get_hermes_home()) / "forge" / "pending_review" / slug
    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / "resume.md").write_text("R")
    (slug_dir / "cover-letter.md").write_text("C")
    (slug_dir / "meta.json").write_text(json.dumps(
        {"row_id": row_id, "company": "Acme", "role": "PM", "slug": slug}))
    return slug_dir


def _canonical_dir(slug="260704-acme-pm"):
    return Path(get_hermes_home()) / "forge" / slug


def _ledger_events():
    out = []
    for f in glob.glob(str(Path(get_hermes_home()) / ".kaizen_ledger" / "*.jsonl")):
        out += [json.loads(l) for l in open(f)]
    return out


# ── promote route: the two failure dispositions (Gemini 1a') ─────────────────


async def test_promote_happy_path_finalizes(monkeypatch):
    pid = _emit()
    slug_dir = _stage()
    monkeypatch.setattr(actions, "_forge_publish_core",
                        lambda slug, loop: _async({"ok": True, "folder_link": "drive://x", "row_id": "pg1"}))
    resp = await actions.handle_forge_promote(_Req(proposal_id=pid))
    assert resp.status == 200
    assert pq.read(pid) is None  # finalized -> removed from queue
    ev = [e for e in _ledger_events() if e["event_type"] == "kaizen_disposition"]
    assert ev and ev[-1]["disposition"] == "applied"
    # fleet-review-unification-v1 C2 — the disposition OUTCOME is unchanged
    # (folder_link preserved); the applied_result gains ADDITIVE unit identity so the
    # read-side viewer's ledger join keys forge 'promoted' reliably.
    ar = ev[-1]["applied_result"]
    assert ar["folder_link"] == "drive://x"
    assert ar["unit_id"] == "pg1" and ar["slug"] == "260704-acme-pm"
    # P1 (promoted-artifact-persistence-v1) — Verdict B: local canonical copies
    # exist in the per-unit subdir; the staged dir is archived META-ONLY.
    canon = _canonical_dir()
    assert (canon / "resume.md").read_text() == "R"
    assert (canon / "cover-letter.md").read_text() == "C"
    assert ar["canonical_files"] == sorted(
        str(canon / n) for n in ("cover-letter.md", "resume.md"))
    assert not slug_dir.exists()  # archived away
    archived = list((Path(get_hermes_home()) / "forge" / ".archive").glob("260704-acme-pm-*"))
    assert len(archived) == 1
    assert sorted(p.name for p in archived[0].iterdir()) == ["meta.json"]


async def test_double_tap_returns_409_and_holds_first_lease(monkeypatch):
    pid = _emit()
    assert pq.set_lease(pid, holder="tap1") == pq.LEASE_ACQUIRED  # tap1 in flight
    resp = await actions.handle_forge_promote(_Req(proposal_id=pid))
    assert resp.status == 409
    assert pq.read(pid).lease["held_by"] == "tap1"  # first lease intact
    assert pq.read(pid) is not None  # NOT removed


async def test_timeout_keeps_lease_held(monkeypatch):
    pid = _emit()
    slug_dir = _stage()
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
    # P1 ruling 3 — canonical write preceded the (timed-out) publish and STAYS
    # intact; the staged dir is NOT archived (meta.json is the retry substrate).
    assert (_canonical_dir() / "resume.md").read_text() == "R"
    assert (slug_dir / "meta.json").is_file()


async def test_completed_failure_clears_lease(monkeypatch):
    pid = _emit()
    slug_dir = _stage()
    monkeypatch.setattr(actions, "_forge_publish_core",
                        lambda slug, loop: _async(
                            {"ok": False, "kind": "forge_notion_cold", "status": 400,
                             "message": "Notion cold", "folder_link": "x"}))
    resp = await actions.handle_forge_promote(_Req(proposal_id=pid))
    assert resp.status == 400
    rec = pq.read(pid)
    assert rec is not None and rec.lease is None  # cleared -> re-tappable, record stays
    # P1 ruling 3 — publish failure AFTER canonical write: proposal held OPEN
    # (asserted above), canonical intact, staged dir NOT archived, NO finalize,
    # and the Andon is raised (the loud path files a portal_action_failure).
    assert (_canonical_dir() / "resume.md").read_text() == "R"
    assert (_canonical_dir() / "cover-letter.md").read_text() == "C"
    assert (slug_dir / "meta.json").is_file()
    applied = [e for e in _ledger_events()
               if e["event_type"] == "kaizen_disposition"
               and e.get("disposition") == "applied"]
    assert not applied  # never finalize an undelivered package
    andons = [p for p in pq.read_all()
              if p.type == "portal_action_failure"]
    assert andons  # the Andon surface fired


async def test_promote_missing_proposal_404(monkeypatch):
    resp = await actions.handle_forge_promote(_Req(proposal_id="sha256:gone"))
    assert resp.status == 404


def _async(value):
    async def _c(*a, **k):
        return value
    return _c()


# ── P1 (promoted-artifact-persistence-v1): canonicalize → publish → archive ──


async def test_publish_failure_then_retap_succeeds(monkeypatch):
    """Verdict C — the full ruling-3 + ruling-4 arc: publish fails AFTER the
    canonical write (hold open, canonical intact), then a re-tap is SATISFIED
    on the canonical write (staging content already moved) and proceeds to a
    successful publish, archive, finalize."""
    pid = _emit()
    slug_dir = _stage()
    monkeypatch.setattr(actions, "_forge_publish_core",
                        lambda slug, loop: _async(
                            {"ok": False, "kind": "forge_drive_publish_error",
                             "status": 422, "message": "Drive down"}))
    resp = await actions.handle_forge_promote(_Req(proposal_id=pid))
    assert resp.status == 422
    assert pq.read(pid) is not None  # held open
    canon_before = {p.name: p.read_text() for p in _canonical_dir().iterdir()}
    assert canon_before == {"resume.md": "R", "cover-letter.md": "C"}

    # re-tap — canonical write satisfied (idempotent), publish now succeeds
    monkeypatch.setattr(actions, "_forge_publish_core",
                        lambda slug, loop: _async(
                            {"ok": True, "folder_link": "drive://x", "row_id": "pg1"}))
    resp2 = await actions.handle_forge_promote(_Req(proposal_id=pid))
    assert resp2.status == 200
    assert pq.read(pid) is None  # finalized
    canon_after = {p.name: p.read_text() for p in _canonical_dir().iterdir()}
    assert canon_after == canon_before  # canonical untouched by the re-tap
    assert not slug_dir.exists()  # archived meta-only on the successful pass
    archived = list((Path(get_hermes_home()) / "forge" / ".archive").glob("260704-acme-pm-*"))
    assert len(archived) == 1
    assert sorted(p.name for p in archived[0].iterdir()) == ["meta.json"]
    ev = [e for e in _ledger_events() if e["event_type"] == "kaizen_disposition"]
    assert ev[-1]["disposition"] == "applied"


async def test_canonicalize_missing_aborts_before_publish(monkeypatch):
    """Ruling 3 — a canonical write that cannot be satisfied (no staged content,
    no canonical package) ABORTS the promote before any delivery attempt."""
    pid = _emit()  # no _stage(): nothing staged, nothing canonical

    async def _never(slug, loop):
        raise AssertionError("publish must not run when canonicalize fails")

    monkeypatch.setattr(actions, "_forge_publish_core", _never)
    resp = await actions.handle_forge_promote(_Req(proposal_id=pid))
    assert resp.status == 404
    rec = pq.read(pid)
    assert rec is not None and rec.lease is None  # held open, re-tappable


def test_canonicalize_core_is_producer_blind():
    """Verdict E — GATE-B ruling 2 pin: the ONE canonicalization core contains
    zero producer names, and both promote entry points delegate to it. P3
    extends the pin to the acceptance-event emission path."""
    import inspect

    from grove.utils import fs_utils

    core_src = (inspect.getsource(fs_utils.canonicalize_files)
                + inspect.getsource(actions._emit_promote_accepted))
    for name in ("forge", "scout", "drafter", "cultivator", "researcher"):
        assert name not in core_src, f"producer name {name!r} leaked into the core"
    # single-implementation pin: both entry points route through the core
    assert "canonicalize_files" in inspect.getsource(actions._fleet_promote_core)
    assert "canonicalize_files" in inspect.getsource(fs_utils.promote_artifact)
    assert "canonicalize_files" in inspect.getsource(actions._canonicalize_staged_package)


# ── P3 (promoted-artifact-persistence-v1): acceptance memory event ───────────


def _accept_events():
    """Every FleetPromoteAccepted line in the memory event log."""
    p = Path(get_hermes_home()) / "memory_records.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        if d.get("__type__") == "FleetPromoteAccepted":
            out.append(d)
    return out


async def test_forge_promote_emits_acceptance_event_fresh(monkeypatch):
    """Verdicts A+B (forge, fresh): exactly ONE FleetPromoteAccepted append per
    promote; revision_count 0, empty directive history, full identity."""
    pid = _emit()
    _stage()
    monkeypatch.setattr(actions, "_forge_publish_core",
                        lambda slug, loop: _async({"ok": True, "folder_link": "drive://x", "row_id": "pg1"}))
    resp = await actions.handle_forge_promote(_Req(proposal_id=pid))
    assert resp.status == 200

    evs = _accept_events()
    assert len(evs) == 1  # exactly one append per promote
    ev = evs[0]
    assert ev["unit_id"] == "pg1"
    assert ev["slug"] == "260704-acme-pm"
    assert ev["producer"] == "skill.fleet.forge-jobsearch"
    assert ev["sink"] == "forge"  # capability-declared canonical_dir value
    assert ev["revision_count"] == 0
    assert ev["directive_history"] == []
    assert ev["proposal_id"] == pid
    assert sorted(Path(f).name for f in ev["canonical_files"]) == [
        "cover-letter.md", "resume.md"]
    assert ev["event_id"].startswith("evt_") and ev["timestamp"]


async def test_forge_promote_event_snapshots_revision_history(monkeypatch):
    """Verdict B (revised): the feedback store's guidance is snapshotted INTO
    the event at promote time — the record that survives feedback TTL-GC."""
    from grove.forge import feedback_store
    pid = _emit()
    _stage()
    feedback_store.write("forge", "pg1", "tighten the intro")
    feedback_store.write("forge", "pg1", "lead with the metric")
    monkeypatch.setattr(actions, "_forge_publish_core",
                        lambda slug, loop: _async({"ok": True, "folder_link": "drive://x", "row_id": "pg1"}))
    resp = await actions.handle_forge_promote(_Req(proposal_id=pid))
    assert resp.status == 200

    (ev,) = _accept_events()
    assert ev["revision_count"] == 2
    notes = [h["revision_note"] for h in ev["directive_history"]]
    assert notes == ["tighten the intro", "lead with the metric"]


async def test_file_producer_promote_emits_acceptance_event(monkeypatch):
    """Verdict B (file-producer variant): the mv-sink branch emits the same
    event shape — sink from the capability declaration, files from the move."""
    from grove.eval.proposal_queue import PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING
    slug = "draft-2026-07-04-moon"
    pid, _ = pq.file_agentless(
        type=PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING,
        payload={"slug": slug, "unit_id": "moon", "skill_id": "skill.fleet.drafter",
                 "canonical_sink": "drafter"},
        evidence=("moon",))
    d = Path(get_hermes_home()) / "drafter" / "pending_review" / slug
    d.mkdir(parents=True)
    (d / "draft-2026-07-04-moon.md").write_text("D")
    (d / "meta.json").write_text(json.dumps({"unit_id": "moon"}))

    resp = await actions.handle_forge_promote(_Req(proposal_id=pid))
    assert resp.status == 200

    (ev,) = _accept_events()
    assert ev["unit_id"] == "moon"
    assert ev["producer"] == "skill.fleet.drafter"
    assert ev["sink"] == "drafter"
    assert ev["revision_count"] == 0
    assert [Path(f).name for f in ev["canonical_files"]] == [
        "draft-2026-07-04-moon.md"]


async def test_acceptance_event_failure_never_unwinds(monkeypatch, caplog):
    """Verdict C: emission failure → finalize completes, loud log, no unwind,
    no event."""
    import logging

    from grove.memory.store import MemoryStore
    pid = _emit()
    _stage()
    monkeypatch.setattr(actions, "_forge_publish_core",
                        lambda slug, loop: _async({"ok": True, "folder_link": "drive://x", "row_id": "pg1"}))

    def _boom(self, event):
        raise RuntimeError("event store down")

    monkeypatch.setattr(MemoryStore, "append_event", _boom)
    with caplog.at_level(logging.WARNING):
        resp = await actions.handle_forge_promote(_Req(proposal_id=pid))
    assert resp.status == 200  # the promote is NOT unwound
    assert pq.read(pid) is None  # finalized
    assert _accept_events() == []  # no event landed
    assert any("promote-accepted memory event FAILED" in r.message
               for r in caplog.records)  # announced loud


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

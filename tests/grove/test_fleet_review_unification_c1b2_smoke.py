"""Seam smoke for fleet-review-unification-v1 C1b-2 (file producers become real
workers via the file_source resolver + declarative emission).

The spine crosses five modules:

    file_source resolver → stable unit_id from the source filename   (resolvers)
      → declarative emit: skill authors CONTENT, runtime synthesizes   (worker_entry)
        meta.json + stages under unit_id
      → generic fleet_artifact_pending proposal by canonical_sink      (manager)
      → promote = mv pending_review → flat canonical (poller ingests)   (api.actions)
        / suggest_revision → (worker=drafter, unit_id=slug) feedback

VERDICT A — forge byte-identical (the self-authored notion path is untouched).
VERDICT B — drafter end-to-end seams with a fixture brief.
VERDICT C — grandfather (pre-staged → no re-dispatch), cultivator empty no-op,
            forced_exit grep-absent from source.

Runs entirely local: GROVE_HOME → tmp_path; no network, no model.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture()
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


_DRAFTER_INPUT = {
    "type": "file_source",
    "source_dir": "researcher",
    "pattern": "brief-*.json",
    "slug_regex": r"^brief-\d{4}-\d{2}-\d{2}-(.+)\.json$",
    "select_one": True,
    "skip_already_staged": True,
}
_CULTIVATOR_INPUT = {
    "type": "file_source",
    "source_dir": "scout",
    "pattern": "digest-*.json",
    "slug_regex": r"^digest-(\d{4}-\d{2}-\d{2})\.json$",
    "select_one": True,
    "skip_already_staged": True,
}


def _write_brief(grove_home, name: str) -> Path:
    d = grove_home / "researcher"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps({"topic": "moon bot"}), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# VERDICT A — forge (notion, self-authored) byte-identical
# ---------------------------------------------------------------------------


def test_forge_prompt_byte_identical_without_content_files():
    """content_files=None → the self-authored forge prompt, unchanged (still tells the
    worker to emit meta.json; never the declarative 'do NOT author a meta.json')."""
    from grove.fleet import worker_entry

    payload = {"rows": [{"id": "r1"}], "unit_id": "r1"}
    prompt = worker_entry._build_worker_prompt("fleet/forge-jobsearch", payload, "abc12345")
    assert 'One file MUST be meta.json' in prompt
    assert "do NOT author a meta.json" not in prompt.lower()


def test_forge_event_has_no_unit_id_key():
    """A notion producer's event omits unit_id entirely (byte-identical event JSON)."""
    from grove.fleet import worker_entry

    ev = worker_entry._event("forge", "run1", "skill.fleet.forge-jobsearch", "success",
                             slug="260709-x", row_id="ROW", fit_score=70)
    assert "unit_id" not in ev
    ev2 = worker_entry._event("drafter", "run2", "skill.fleet.drafter", "success",
                              slug="moon-bot", unit_id="moon-bot")
    assert ev2["unit_id"] == "moon-bot"  # declarative producer DOES carry it


def test_forge_emission_payload_byte_identical(monkeypatch):
    """The forge reap emission is unchanged: type forge_artifact_pending, payload
    EXACTLY {slug,row_id,skill_id,fit_score}, evidence (row_id,). Adding a unit_id key
    would fork the content-addressed proposal_id — this guards against it."""
    from grove.fleet.manager import FleetManager
    from grove.eval import proposal_queue

    captured = {}

    def _cap(**kw):
        captured.update(kw)
        return ("sha256:x", True)

    monkeypatch.setattr(proposal_queue, "file_agentless", _cap)
    event = {"skill": "skill.fleet.forge-jobsearch", "status": "success",
             "slug": "260709-legends", "row_id": "ROW123", "fit_score": 70}
    FleetManager()._maybe_emit_artifact_proposal("forge", "run1", event)
    assert captured["type"] == proposal_queue.PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING
    assert captured["payload"] == {
        "slug": "260709-legends", "row_id": "ROW123",
        "skill_id": "skill.fleet.forge-jobsearch", "fit_score": 70,
    }
    assert captured["evidence"] == ("ROW123",)


# ---------------------------------------------------------------------------
# VERDICT B — drafter file_source + declarative emission end-to-end seams
# ---------------------------------------------------------------------------


def test_file_source_selects_unit_with_stable_slug(grove_home):
    from grove.fleet import resolvers

    _write_brief(grove_home, "brief-2026-07-09-moon-bot.json")
    payload = resolvers.resolve_file_source(_DRAFTER_INPUT, "drafter")
    assert payload is not None
    assert payload["unit_id"] == "moon-bot"  # date stripped
    assert payload["source_name"] == "brief-2026-07-09-moon-bot.json"


def test_file_source_unit_id_stable_across_redate(grove_home):
    """A refreshed/re-dated brief for the same topic maps to the SAME unit_id — the
    disposition/feedback history persists across the upstream refresh."""
    from grove.fleet import resolvers

    _write_brief(grove_home, "brief-2026-07-09-moon-bot.json")
    p1 = resolvers.resolve_file_source(_DRAFTER_INPUT, "drafter")
    # upstream refresh: same topic, newer date
    (grove_home / "researcher" / "brief-2026-07-09-moon-bot.json").unlink()
    _write_brief(grove_home, "brief-2026-07-15-moon-bot.json")
    p2 = resolvers.resolve_file_source(_DRAFTER_INPUT, "drafter")
    assert p1["unit_id"] == p2["unit_id"] == "moon-bot"


def test_file_source_absent_or_empty_source_is_noop(grove_home):
    from grove.fleet import resolvers

    assert resolvers.resolve_file_source(_DRAFTER_INPUT, "drafter") is None  # absent dir
    (grove_home / "researcher").mkdir()
    assert resolvers.resolve_file_source(_DRAFTER_INPUT, "drafter") is None  # empty dir


def test_file_source_bad_name_fails_loud(grove_home):
    from grove.fleet import resolvers
    from grove.fleet.errors import FleetWorkerAndon

    d = grove_home / "researcher"
    d.mkdir()
    (d / "brief-nodate.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FleetWorkerAndon):
        resolvers.resolve_file_source(_DRAFTER_INPUT, "drafter")


def test_declarative_content_filename_from_terminal_artifact():
    """The content filename derives from the record's terminal_artifact.path_pattern
    with '*' filled by unit_id — matching the flat canonical adapter glob."""
    from grove.fleet import worker_entry

    cap = SimpleNamespace(governance={
        "emission_preconditions": {"terminal_artifact": {"path_pattern": "draft-*.md"}}
    })
    files = worker_entry._declarative_content_files(cap, {"unit_id": "moon-bot"}, "drafter")
    assert files == ["draft-moon-bot.md"]


def test_synthesize_meta_is_runtime_authored():
    from grove.fleet import worker_entry

    meta = json.loads(worker_entry._synthesize_meta(
        {"source_path": "/x/brief-2026-07-09-moon-bot.json",
         "source_name": "brief-2026-07-09-moon-bot.json"}, "drafter", "moon-bot"))
    assert meta["unit_id"] == "moon-bot" and meta["slug"] == "moon-bot"
    assert meta["worker"] == "drafter"
    assert meta["source_name"] == "brief-2026-07-09-moon-bot.json"


def test_extract_declarative_content_takes_content_only(tmp_path):
    """The declarative extractor requires the content file, ignores/discards any stray
    skill-emitted meta.json (identity is the runtime's)."""
    from grove.fleet import worker_entry

    tag = "abc12345"
    body = (
        f"@@@FILE_START: draft-moon-bot.md [{tag}]@@@\n"
        "---\ntitle: X\n---\nbody text\n"
        f"@@@FILE_END: draft-moon-bot.md [{tag}]@@@\n"
        f"@@@FILE_START: meta.json [{tag}]@@@\n"
        '{"slug": "skill-authored-BAD"}\n'
        f"@@@FILE_END: meta.json [{tag}]@@@\n"
    )
    messages = [{"role": "assistant", "content": body}]
    got, reason = worker_entry._extract_declarative_content(
        messages, tag, tmp_path, ["draft-moon-bot.md"])
    assert reason is None
    assert set(got["files"]) == {"draft-moon-bot.md"}  # stray meta.json DISCARDED
    assert "skill-authored-BAD" not in json.dumps(got["files"])


def test_stage_declarative_package_and_staged_unit_ids(grove_home):
    """Runtime stages content + synthesized meta under unit_id; _staged_unit_ids reads
    the unit_id back off the synthesized meta (drives skip-already-staged)."""
    from grove.fleet import resolvers
    from grove.fleet.staging import stage_package
    from grove.fleet import worker_entry

    sink = grove_home / "drafter" / "pending_review"
    sink.mkdir(parents=True)
    files = {
        "draft-moon-bot.md": "---\ntitle: X\n---\nbody",
        "meta.json": worker_entry._synthesize_meta(
            {"source_path": "s", "source_name": "brief-2026-07-09-moon-bot.json"},
            "drafter", "moon-bot"),
    }
    stage_package(sink, "moon-bot", files)
    assert resolvers._staged_unit_ids("drafter") == {"moon-bot"}


def test_manager_emits_fleet_artifact_pending_for_drafter(monkeypatch):
    """A drafter SUCCESS event (canonical_sink 'drafter' != 'forge') emits the GENERIC
    fleet_artifact_pending, keyed on unit_id (no row_id)."""
    from grove.fleet.manager import FleetManager
    from grove.eval import proposal_queue

    captured = {}
    monkeypatch.setattr(proposal_queue, "file_agentless",
                        lambda **kw: (captured.update(kw), ("sha256:y", True))[1])
    event = {"skill": "skill.fleet.drafter", "status": "success",
             "slug": "moon-bot", "unit_id": "moon-bot"}
    FleetManager()._maybe_emit_artifact_proposal("drafter", "run1", event)
    assert captured["type"] == proposal_queue.PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING
    assert captured["payload"] == {
        "slug": "moon-bot", "unit_id": "moon-bot",
        "skill_id": "skill.fleet.drafter", "canonical_sink": "drafter",
        # drafter-quality-checks-v1 P4 — the quality rider (always-present,
        # null when the event carries no gate fields).
        "quality_score": None, "rubric_version": None,
        "redraft_count": None, "evaluator_model": None,
    }
    assert captured["evidence"] == ("moon-bot",)


def test_fleet_promote_core_moves_content_to_flat_canonical(grove_home):
    """Promote = mv the content file to the FLAT canonical sink (poller glob draft-*.md);
    meta.json is NOT promoted; the emptied staged dir is archived (skip marker cleared)."""
    from grove.api.actions import _fleet_promote_core

    staged = grove_home / "drafter" / "pending_review" / "moon-bot"
    staged.mkdir(parents=True)
    (staged / "draft-moon-bot.md").write_text("---\ntitle: X\n---\nbody", encoding="utf-8")
    (staged / "meta.json").write_text('{"unit_id": "moon-bot"}', encoding="utf-8")

    proposal = SimpleNamespace(payload={"slug": "moon-bot", "canonical_sink": "drafter"})
    res = _fleet_promote_core(proposal)
    assert res["ok"] is True
    canonical = grove_home / "drafter" / "draft-moon-bot.md"
    assert canonical.is_file()                                   # flat → poller ingests
    assert not (grove_home / "drafter" / "meta.json").exists()   # meta NOT promoted
    assert not staged.exists()                                   # staged dir archived
    assert (grove_home / "drafter" / ".archive").is_dir()


def test_suggest_revision_seam_generic_unit_and_reselect(grove_home):
    """suggest_revision's spine for a file producer: feedback keyed on (worker=drafter,
    unit_id=slug); the resolver floats the revision-pending unit AFTER its stale draft
    is archived; the N-breaker terminally excludes it."""
    from grove.forge import feedback_store
    from grove.fleet import resolvers
    from grove.api.actions import _REVISION_MAX

    _write_brief(grove_home, "brief-2026-07-09-moon-bot.json")

    # feedback store keyed on (drafter, unit_id) — the generic path the portal writes.
    feedback_store.write("drafter", "moon-bot", "tighten the open")
    assert feedback_store._entry_path("drafter", "moon-bot") == \
        grove_home / "drafter" / ".feedback" / "moon-bot.json"
    # the manager fold reads it by (worker, unit_id):
    assert resolvers._revision_directive("moon-bot", "drafter") is not None
    assert resolvers._has_revision_priority("moon-bot", "drafter") is True

    # re-selection: the (unstaged) revision-pending unit is selected again.
    payload = resolvers.resolve_file_source(_DRAFTER_INPUT, "drafter")
    assert payload is not None and payload["unit_id"] == "moon-bot"

    # N-breaker: at count >= _REVISION_MAX the portal marks terminal → resolver excludes.
    for _ in range(_REVISION_MAX):
        feedback_store.write("drafter", "moon-bot", "again")
    feedback_store.set_terminal_skip("drafter", "moon-bot")
    assert resolvers.resolve_file_source(_DRAFTER_INPUT, "drafter") is None


# ---------------------------------------------------------------------------
# VERDICT C — negatives
# ---------------------------------------------------------------------------


def test_grandfather_prestaged_unit_gets_no_redispatch(grove_home):
    """A pre-existing staged unit is skipped by skip_already_staged → no new dispatch,
    hence no new proposal (proposals emit only on run-reap SUCCESS, never a scan)."""
    from grove.fleet import resolvers
    from grove.fleet.staging import stage_package
    from grove.fleet import worker_entry

    _write_brief(grove_home, "brief-2026-07-09-moon-bot.json")
    sink = grove_home / "drafter" / "pending_review"
    sink.mkdir(parents=True)
    stage_package(sink, "moon-bot", {
        "draft-moon-bot.md": "---\ntitle: X\n---\nb",
        "meta.json": worker_entry._synthesize_meta(
            {"source_path": "s", "source_name": "brief-2026-07-09-moon-bot.json"},
            "drafter", "moon-bot"),
    })
    assert resolvers.resolve_file_source(_DRAFTER_INPUT, "drafter") is None  # already staged


def test_cultivator_empty_source_dir_noops_clean(grove_home):
    from grove.fleet import resolvers

    assert resolvers.resolve_file_source(_CULTIVATOR_INPUT, "cultivator") is None  # absent
    (grove_home / "scout").mkdir()
    assert resolvers.resolve_file_source(_CULTIVATOR_INPUT, "cultivator") is None  # empty


@pytest.mark.parametrize("count,producer,payload", [
    (1, "forge", {"slug": "260707-sirion", "row_id": "ROW-1", "skill_id": "skill.fleet.forge-jobsearch", "fit_score": 70}),
    (3, "forge", {"slug": "260707-sirion", "row_id": "ROW-1", "skill_id": "skill.fleet.forge-jobsearch", "fit_score": 70}),
    (1, "drafter", {"slug": "moon-bot", "unit_id": "moon-bot", "skill_id": "skill.fleet.drafter", "canonical_sink": "drafter"}),
    (3, "drafter", {"slug": "moon-bot", "unit_id": "moon-bot", "skill_id": "skill.fleet.drafter", "canonical_sink": "drafter"}),
])
def test_suggest_revision_handler_finalizes_without_nameerror(monkeypatch, count, producer, payload):
    """REGRESSION (C1b-2 hotfix) — the disposition handler must reach finalize for BOTH
    the normal (count<N) and won't-converge (count>=N) paths, for forge AND a file
    producer, WITHOUT a NameError. The won't-converge + finalize branches reference
    ``row_id``; a missing local there is exactly the crash two forge revisions hit on
    prod. Drives the async handler with the collaborators stubbed."""
    import asyncio
    from grove.api import actions
    from grove.eval import proposal_queue

    proposal = SimpleNamespace(type="forge_artifact_pending" if producer == "forge"
                               else "fleet_artifact_pending", payload=payload,
                               proposal_id="sha256:deadbeef")

    async def _text(_req):
        return "tighten the open"

    async def _noop_broadcast(_msg):
        return None

    monkeypatch.setattr(actions, "_suggest_revision_text", _text)
    monkeypatch.setattr(actions, "broadcast_to_operator", _noop_broadcast)
    monkeypatch.setattr(actions, "_worker_id_for_skill", lambda s: producer)
    monkeypatch.setattr(actions, "_write_archive_pending_marker", lambda slug: None)
    monkeypatch.setattr(actions, "_archive_forge_slug", lambda p: "/archive/x")
    monkeypatch.setattr(proposal_queue, "read", lambda pid: proposal)
    monkeypatch.setattr(proposal_queue, "set_lease", lambda pid, holder: "nonce")
    monkeypatch.setattr(proposal_queue, "clear_lease", lambda pid: None)
    monkeypatch.setattr(proposal_queue, "finalize_proposal_state", lambda *a, **k: True)
    monkeypatch.setattr(proposal_queue, "file_agentless_proposal", lambda **k: ("sha256:z", True))
    from grove.forge import feedback_store
    monkeypatch.setattr(feedback_store, "write", lambda w, u, note: {"count": count})
    monkeypatch.setattr(feedback_store, "set_terminal_skip", lambda w, u: None)

    # fleet-artifact-legibility-v1 C4 — the handler reads the presentation
    # mount selector from request.query; the stub carries an empty mapping.
    request = SimpleNamespace(match_info={"proposal_id": "sha256:deadbeef"},
                              query={})
    resp = asyncio.run(actions._suggest_revision_disposition(request, producer=producer))
    assert resp.status == 200  # resolved card (revision requested / won't-converge) — no NameError


def test_forced_exit_absent_from_source():
    """The forced_exit mode is fully removed — no enum, routing, record, or dead branch."""
    repo = Path(__file__).resolve().parents[2]
    out = subprocess.run(
        ["grep", "-rniE", "forced_exit|forced-exit",
         str(repo / "grove"), str(repo / "config")],
        capture_output=True, text=True,
    )
    assert out.stdout.strip() == "", f"forced_exit still present:\n{out.stdout}"

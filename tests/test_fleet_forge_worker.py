"""Phase-4 tests: forge as the first worker + Option-2 package staging.

Forge appears here for the first time (config + skill delta). Also covers the
generic Option-2 runtime primitives forge exercises — stage_package (two-level
path-jail) and _extract_fleet_package — and the gate-requested re-dispatch cycle
(a worker's not-running gate clears on exit and it re-dispatches next tick).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grove.capability import Capability
from grove.capability_registry import load_capabilities
from grove.fleet import manager as manager_mod, staging, worker_entry
from grove.fleet.config import default_fleet_workers_path, load_fleet_workers, WorkerConfig
from grove.fleet.errors import FleetWorkerAndon
from grove.fleet.read_surfaces import enforce_declared_surfaces


# ── forge capability record ──────────────────────────────────────────────────


def _forge():
    return load_capabilities()["skill.fleet.forge-jobsearch"]


def test_forge_declares_only_corpus_file():
    assert _forge().read_surfaces == ["corpus_file"]


def test_forge_enforce_passes_corpus_only():
    assert enforce_declared_surfaces(_forge(), "forge") == ["corpus_file"]


def test_forge_sink_is_pending_review():
    gov = _forge().governance
    assert gov["write_zone"]["staging_dir"] == "forge/pending_review"


def test_derive_skill_name_is_category_qualified():
    # Skills are category-nested; invoke_skill needs "<category>/<name>", not the
    # bare name (which resolves to a nonexistent flat dir). Pinned by the live run.
    assert worker_entry._derive_skill_name(_forge(), "forge") == "fleet/forge-jobsearch"


def test_guard_fires_if_forge_reaches_an_index():
    # A forge that declared cellar/wiki must Andon — it must NOT be able to reach one.
    d = _forge().to_dict()
    d["read_surfaces"] = ["corpus_file", "cellar"]
    with pytest.raises(FleetWorkerAndon) as ei:
        enforce_declared_surfaces(Capability.from_dict(d), "forge")
    assert ei.value.check == "index_surface_unwired"


def test_forge_payload_still_byte_matches_repo_skill():
    from pathlib import Path
    repo = Path("skills/fleet/forge-jobsearch/SKILL.md").read_text(encoding="utf-8")
    assert _forge().context.payload.strip() == repo.strip()


# ── forge fleet_workers.yaml entry ───────────────────────────────────────────


def test_forge_worker_entry_enabled_and_correct():
    w = load_fleet_workers(default_fleet_workers_path())["forge"]
    assert w.enabled is True  # live fleet producer (authorized post Phase-5 smoke)
    assert w.skill == "skill.fleet.forge-jobsearch"
    ist = w.input_state
    assert ist["type"] == "notion_query"
    assert ist["data_source"] == "5eb5630d-42ae-4a7f-8eee-8b04f0e96eaa"
    assert ist["filter"] == {"Status": "To Apply"}
    # fleet-pipeline-v1 P0 — declarative single-unit selection + ranking
    assert ist["select_one"] is True and ist["skip_already_staged"] is True
    assert ist["order_by"] == [
        {"field": "Fit Score", "direction": "desc"},
        {"field": "id", "direction": "asc"},
    ]
    assert w.limits["wall_clock_secs"] == 900


# ── Option-2 package staging (two-level path-jail) ───────────────────────────


def test_stage_package_writes_files_under_slug(tmp_path):
    sink = tmp_path / "forge" / "pending_review"
    files = {"resume.md": "R", "cover-letter.md": "C", "meta.json": '{"row_id":"x"}'}
    staged = staging.stage_package(sink, "260704-acme-pm", files)
    slug_dir = sink / "260704-acme-pm"
    assert {p.name for p in staged} == set(files)
    assert all(p.parent == slug_dir.resolve() for p in staged)
    assert (slug_dir / "resume.md").read_text() == "R"
    assert not list(sink.rglob("*.tmp"))  # atomic, no torn tmp


def test_stage_package_slug_traversal_neutralized(tmp_path):
    sink = tmp_path / "sink"
    # basename() collapses the traversal; the file lands INSIDE the sink, jailed.
    staged = staging.stage_package(sink, "../evil", {"a.md": "x"})
    assert staged[0].is_relative_to(sink.resolve())


def test_stage_package_rejects_unsafe_slug(tmp_path):
    with pytest.raises(FleetWorkerAndon) as ei:
        staging.stage_package(tmp_path, "..", {"a.md": "x"})
    assert ei.value.check == "path_escape"


def test_stage_package_rejects_uppercase_slug(tmp_path):
    with pytest.raises(FleetWorkerAndon):
        staging.stage_package(tmp_path, "NotASlug", {"a.md": "x"})


def test_stage_package_file_traversal_neutralized(tmp_path):
    # basename() collapses the filename traversal; the file lands jailed under the
    # slug dir (not at /etc/passwd), never rejected — parity with stage_draft.
    sink = tmp_path / "sink"
    staged = staging.stage_package(sink, "slug", {"../../etc/passwd": "x"})
    assert staged[0].name == "passwd"
    assert staged[0].is_relative_to(sink.resolve())
    assert staged[0].parent == (sink / "slug").resolve()


def test_stage_package_file_reducing_to_dotdot_rejected(tmp_path):
    with pytest.raises(FleetWorkerAndon) as ei:
        staging.stage_package(tmp_path / "sink", "slug", {"..": "x"})
    assert ei.value.check == "path_escape"


def test_stage_package_empty_files_andons(tmp_path):
    with pytest.raises(FleetWorkerAndon) as ei:
        staging.stage_package(tmp_path, "slug", {})
    assert ei.value.check == "empty_package"


# ── fleet_package extraction (delimited, forge-fleet-package-emission-v1) ─────
#
# MIGRATED from the retired JSON-transport contract. The worker no longer emits a
# single hand-authored JSON blob (byte-confirmed to fail no_package whenever the
# résumé/cover prose carried an unescaped ``"`` — "god-object" / "Lean AI"); it emits
# each file inside sentinel-framed delimited blocks parsed by the P1 state machine.
# These cases feed DELIMITED text and assert the (package{slug,files} | None, reason)
# contract. Pure-parser coverage lives comprehensively in test_fleet_delimited_parser.py;
# these preserve the forge-context regressions the old JSON cases guarded.
#
# Two old JSON cases were DELETED as now-meaningless (reported in HANDOFF): the
# ```json-fenced and whole-message-is-JSON variants — both were JSON *transport-shape*
# tests whose only delimited analog is "a clean parse," already covered by
# test_extract_clean_delimited_package below (keeping them would be three identical
# clean-parse tests). Fence *stripping* — the delimited feature that shares the word
# "fence" — is a DIFFERENT behavior and is migrated (test_extract_strips_markdown_fences).

_TAG = "abc12345"           # a per-run short-hex tag (run_id[:8])
_SINK = "/tmp/forge-sink"
_REQUIRED = {"resume.md", "cover-letter.md", "meta.json"}


def _msgs(text):
    return [{"role": "assistant", "content": text}]


def _blk(name, body, tag=_TAG):
    return f"@@@FILE_START: {name} [{tag}]@@@\n{body}\n@@@FILE_END: {name} [{tag}]@@@"


def _meta_body(slug="260705-acme-vp"):
    return json.dumps({"row_id": "row-123", "company": "Acme", "role": "VP", "slug": slug})


def _emit(resume="# Jane Doe\nBuilt platforms.", cover="Dear Acme,\n\nStrong fit.\n\nJane",
          meta=None, tag=_TAG):
    return "\n".join(
        [_blk("resume.md", resume, tag), _blk("cover-letter.md", cover, tag),
         _blk("meta.json", meta if meta is not None else _meta_body(), tag)]
    )


def _extract(text):
    return worker_entry._extract_fleet_package(_msgs(text), _TAG, _SINK, _REQUIRED)


def test_extract_clean_delimited_package():
    # (← bare_json) a clean multi-file emit → {slug (from meta.json), files}, reason None.
    pkg, reason = _extract(_emit())
    assert reason is None
    assert pkg["slug"] == "260705-acme-vp"          # recovered from meta.json's body
    assert set(pkg["files"]) == _REQUIRED


def test_extract_strips_markdown_fences():
    # (← fenced_json) a body wrapped in ```markdown fences is stripped to clean content.
    body = "```markdown\n# Jane Doe\nBuilt platforms.\n```"
    pkg, reason = _extract(_emit(resume=body))
    assert reason is None
    assert pkg["files"]["resume.md"] == "# Jane Doe\nBuilt platforms."


def test_extract_no_blocks_is_no_files():
    # (← missing_package) prose with no delimited blocks → fail-loud no-files.
    pkg, reason = _extract("no package here, just prose")
    assert pkg is None and reason == "no-files"


def test_extract_body_preserves_literal_quotes_and_newlines():
    # (← tolerates_raw_control_char) THE regression the sprint kills: a body with literal
    # double-quotes AND raw newlines is transported verbatim — no JSON escaping to break.
    quoted = 'Re-architected the harness, removing a "god-object" defect.\nGhost-authored "Lean AI".'
    pkg, reason = _extract(_emit(resume=quoted))
    assert reason is None
    assert pkg["files"]["resume.md"] == quoted       # bytes intact, quotes and all


def test_prose_preamble_before_blocks_is_ignored():
    # (← peel_prose_preamble) the founding failure shape: "I have everything needed.
    # Building the fleet package now.\n\nRow: …" preamble. Prose OUTSIDE any block is
    # structurally ignored; the delimited blocks parse.
    preamble = (
        "I have the corpus and voice guide loaded. Now generating the package for "
        "Acme VP Systems, row id row-123.\n\nPositioning thesis: leads architecture.\n\n"
    )
    pkg, reason = _extract(preamble + _emit())
    assert reason is None and pkg["slug"] == "260705-acme-vp"
    assert set(pkg["files"]) == _REQUIRED


def test_prose_between_blocks_is_ignored():
    # (← peel_synthetic_prose) narration BETWEEN blocks (WAITING_FOR_START) is ignored.
    text = "\n".join(
        [_blk("resume.md", "# R"), "Here is my reasoning about the role.",
         _blk("cover-letter.md", "C"), _blk("meta.json", _meta_body())]
    )
    pkg, reason = _extract(text)
    assert reason is None and pkg["slug"] == "260705-acme-vp"


def test_wrong_tag_decoy_marker_is_ignored_real_parses():
    # (← peel_decoy) a prose EXAMPLE that shows a marker with the WRONG tag is treated as
    # text (not a marker); the real, correctly-tagged blocks parse.
    decoy = "For example a block looks like @@@FILE_START: example.md [deadbeef]@@@"
    pkg, reason = _extract(decoy + "\n\n" + _emit())
    assert reason is None and pkg["slug"] == "260705-acme-vp"


def test_duplicate_file_fails_loud():
    # (← peel_two_full_valid ambiguity) the delimited analog of "ambiguous → never guess":
    # two blocks naming the same file → fail-loud duplicate-file.
    text = "\n".join([_blk("resume.md", "a"), _blk("resume.md", "b"), _blk("meta.json", _meta_body())])
    pkg, reason = _extract(text)
    assert pkg is None and reason.startswith("duplicate-file")


def test_empty_slug_in_meta_is_bad_meta():
    # (← peel_empty_slug) slug is recovered from meta.json; an empty/invalid slug there
    # fails loud bad-meta (was: empty top-level slug → None).
    pkg, reason = _extract(_emit(meta=_meta_body(slug="")))
    assert pkg is None and reason == "bad-meta"


def test_empty_body_fails_loud():
    # (← peel_empty_files) an empty file body → fail-loud empty-body (was: empty files dict).
    text = "\n".join([f"@@@FILE_START: resume.md [{_TAG}]@@@", "   ", f"@@@FILE_END: resume.md [{_TAG}]@@@"])
    pkg, reason = _extract(text)
    assert pkg is None and reason.startswith("empty-body")


def test_missing_required_file_fails_loud():
    # (← peel_missing_files_key) a required file absent → fail-loud missing-required-files
    # (was: missing "files" key → None). cover-letter.md omitted here.
    text = "\n".join([_blk("resume.md", "a"), _blk("meta.json", _meta_body())])
    pkg, reason = _extract(text)
    assert pkg is None and reason.startswith("missing-required-files")


def test_garbled_prose_only_is_no_files():
    # (← peel_garbled) prose, no blocks at all → no-files.
    pkg, reason = _extract("I thought about it but produced only prose.")
    assert pkg is None and reason == "no-files"


# ── re-dispatch cycle: not-running gate clears on exit (gate confirmation) ────


class _CyclingProc:
    """poll() returns None (running) until flipped, then the exit code."""

    def __init__(self):
        self.pid = 4242
        self._rc = None

    def poll(self):
        return self._rc

    def exit(self, code=0):
        self._rc = code


def test_worker_redispatches_after_exit(monkeypatch, tmp_path):
    # A stuck "running" flag would silently block ALL future dispatches — verify
    # the full cycle: dispatch -> running -> exit (reap clears) -> re-dispatch.
    dispatched = []
    proc = _CyclingProc()
    event_path = tmp_path / "e.json"

    class _Handle:
        worker_id = "forge"
        run_id = "r"
        wall_clock_secs = 900
        pgid = 4242

        def __init__(self):
            self.proc = proc
            self.event_path = event_path

    def _fake_dispatch(cfg, payload, run_id=None):
        dispatched.append(cfg.id)
        return _Handle()

    monkeypatch.setattr(manager_mod.runner, "dispatch", _fake_dispatch)
    monkeypatch.setattr(manager_mod, "resolve_input_state", lambda *_a, **_k: {"rows": [1]})
    monkeypatch.setattr(manager_mod, "surface_fleet_andon", lambda *a, **k: None)
    monkeypatch.setattr(manager_mod, "remove_pidfile", lambda *_a, **_k: None)
    monkeypatch.setattr(manager_mod, "enforce_wall_clock", lambda *_a, **_k: False)
    # cadence=None -> always due, so re-dispatch is gated only by not-running.
    wc = WorkerConfig(id="forge", skill="s", enabled=True, cadence=None,
                      input_state={"type": "notion_query"}, limits={"wall_clock_secs": 900})
    monkeypatch.setattr(manager_mod, "load_fleet_workers", lambda *_a, **_k: {"forge": wc})

    m = manager_mod.FleetManager()
    m.tick()                              # tick 1: dispatch
    assert dispatched == ["forge"] and "forge" in m._running

    m.tick()                              # tick 2: still running -> no new dispatch
    assert dispatched == ["forge"] and "forge" in m._running

    proc.exit(0)                          # worker exits successfully
    event_path.write_text(json.dumps({"status": "success"}))
    m.tick()                              # tick 3: reap clears running, then re-dispatch
    assert dispatched == ["forge", "forge"]  # re-dispatched — gate cleared
    assert "forge" in m._running


# ── fleet-failure-forensics-v1: raw output on the no_package failed terminal ──


_NO_PACKAGE_DETAIL_PREFIX = (
    "delimited emit did not parse to a valid fleet_package (reason: "
)


class _FakeSessionDB:
    def __init__(self, *a, **k):
        pass


def _drive_no_package(monkeypatch, tmp_path, messages, run_id="rid0"):
    """Drive the REAL run_worker to the no_package branch.

    ``run_conversation`` returns ``messages`` that carry no delimited blocks, so the
    REAL ``_extract_fleet_package`` returns ``(None, "no-files")`` and the no_package
    branch fires. Everything upstream is stubbed at clean seams; ``get_hermes_home``
    points the events/ sink (and the raw sidecar) at ``tmp_path``.
    """
    from grove.fleet import paths as _paths

    class _Cap:
        id = "skill.fleet.forge-jobsearch"

    class _Agent:
        def run_conversation(self, prompt, task_id=None):
            return {"messages": messages, "completed": True}

    class _Dispatcher:
        def __init__(self, *a, **k):
            self.agent = _Agent()

    monkeypatch.setattr(_paths, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(worker_entry, "_load_capability_for", lambda wid: _Cap())
    monkeypatch.setattr(
        worker_entry, "_resolve_declared_sink", lambda cap, wid: tmp_path / "sink"
    )
    monkeypatch.setattr(
        worker_entry, "_derive_skill_name", lambda cap, wid: "fleet/forge-jobsearch"
    )
    monkeypatch.setattr(
        worker_entry, "_resolve_worker_runtime",
        lambda cap, wid: ("m", 100, {"provider": "p"}),
    )
    # function-local imports resolve from their origin modules at call time
    monkeypatch.setattr("gateway.session_context.set_session_vars", lambda **k: object())
    monkeypatch.setattr("gateway.session_context.clear_session_vars", lambda *a, **k: None)
    monkeypatch.setattr("grove.grants.get_grant_store", lambda *a, **k: None)
    monkeypatch.setattr(
        "grove.fleet.read_surfaces.enforce_declared_surfaces", lambda *a, **k: []
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: {})
    monkeypatch.setattr("hermes_state.SessionDB", _FakeSessionDB)
    monkeypatch.setattr("grove.dispatcher.Dispatcher", _Dispatcher)
    monkeypatch.setattr("grove.dispatcher.RuntimeContext", lambda **k: object())
    return worker_entry.run_worker("forge", run_id, {"rows": [{"id": "r1"}]})


def test_no_package_event_carries_preview_and_raw_path(monkeypatch, tmp_path):
    # (a) the discarded model output is now BOTH previewed in detail AND persisted.
    text = "I reviewed the corpus but produced prose, not a fleet_package."
    ev = _drive_no_package(
        monkeypatch, tmp_path, [{"role": "assistant", "content": text}]
    )
    assert ev["status"] == "failed"
    assert ev["check"] == "no_package"
    assert text in ev["detail"]                       # preview embedded in detail
    assert ev["raw_text_path"] is not None
    raw = Path(ev["raw_text_path"])
    assert raw.name == "rid0.raw.txt"
    assert raw.parent.name == "events"                # sibling of the event JSON
    assert raw.read_text(encoding="utf-8") == text    # FULL raw text, not truncated


def test_no_package_regression_status_check_detail_prefix(monkeypatch, tmp_path):
    # (e) REGRESSION GUARD — reap-relevant fields (status/check) byte-identical to
    # pre-change; the detail now folds in the delimited-parse fail-loud reason (a
    # forensics upgrade over the old opaque JSON message). "prose" has no blocks →
    # the parser returns (None, "no-files").
    ev = _drive_no_package(
        monkeypatch, tmp_path, [{"role": "assistant", "content": "prose"}]
    )
    assert ev["status"] == "failed"
    assert ev["check"] == "no_package"
    assert ev["detail"].startswith(_NO_PACKAGE_DETAIL_PREFIX)
    assert "reason: no-files" in ev["detail"]          # the fail-loud reason is surfaced
    assert "raw_text_path" in ev                       # additive field present


def test_event_accepts_additive_raw_text_path():
    # (e) the additive field is keyword-only + back-compatible (None when omitted).
    ev = worker_entry._event(
        "forge", "r", "sk", "failed",
        detail="d", check="no_package", raw_text_path="/x/r.raw.txt",
    )
    assert ev["raw_text_path"] == "/x/r.raw.txt"
    assert worker_entry._event("forge", "r", "sk", "success")["raw_text_path"] is None


def test_persist_raw_output_writes_exact_text(monkeypatch, tmp_path):
    # (c) the sidecar holds the exact bytes, at events/<run_id>.raw.txt.
    from grove.fleet import paths as _paths

    monkeypatch.setattr(_paths, "get_hermes_home", lambda: str(tmp_path))
    p = worker_entry._persist_raw_output("forge", "runX", "line1\nline2\n")
    assert p is not None
    rp = Path(p)
    assert rp.read_text(encoding="utf-8") == "line1\nline2\n"
    assert rp.name == "runX.raw.txt"
    assert rp.parent.name == "events"


def test_persist_raw_output_unwritable_returns_none(monkeypatch, tmp_path):
    # (d) an un-writable sink is swallowed — best-effort NEVER masks the real
    # failure with a second one. A FILE where the home dir should be makes the
    # events/ mkdir raise; the helper must return None, not propagate.
    bogus = tmp_path / "home-is-a-file"
    bogus.write_text("x")
    from grove.fleet import paths as _paths

    monkeypatch.setattr(_paths, "get_hermes_home", lambda: str(bogus))
    assert worker_entry._persist_raw_output("forge", "runY", "data") is None

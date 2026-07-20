"""End-to-end local smoke for suggest-revision-verb-v1 (P5).

The informed-path loop-back tap has a spine that crosses four modules:

    operator taps "Suggest revision"           (fragments._disposition_bar render)
      -> feedback_store.write accumulates        (grove.forge.feedback_store)
        -> resolver selects that unit FIRST,      (grove.fleet.resolvers._select_units)
           the manager folds the framed directive (grove.fleet.manager, C1b-1)
          -> worker prompt surfaces the directive  (grove.fleet.worker_entry._build_worker_prompt)

plus the P4 N-breaker (terminal_skip exclusion + won't-converge threshold), the
TTL-GC, the orphan-staged crash-residual sweep, and the fail-loud corrupt-store
Andon. fleet-review-unification-v1 C1b-1 generalized the store to (worker, unit_id)
keying; for notion_query unit_id == row_id and worker is the fleet worker id.

Runs entirely local: ``GROVE_HOME`` is redirected to a per-test ``tmp_path`` (the
store, the pending_review sink, the ``.feedback`` dir all land under it).
"""

from __future__ import annotations

import json

import pytest

from grove.fleet.errors import FleetWorkerAndon


@pytest.fixture()
def grove_home(tmp_path, monkeypatch):
    """Redirect ``get_hermes_home()`` (which reads ``GROVE_HOME`` live per call) to a
    temp dir, so every sprint module's store/sink lands under tmp_path."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# feedback_store — accumulate-with-history, fail-loud, N-breaker state, GC
# (C1b-1: keyed on (worker, unit_id); forge uses worker="forge", unit_id=row_id)
# ---------------------------------------------------------------------------


def test_store_accumulates_history_and_count(grove_home):
    from grove.forge import feedback_store

    unit = "row-abc"
    e1 = feedback_store.write("forge", unit, "tighten the summary")
    e2 = feedback_store.write("forge", unit, "add a metrics bullet")
    assert e1["count"] == 1 and e2["count"] == 2
    assert [h["revision_note"] for h in e2["history"]] == [
        "tighten the summary",
        "add a metrics bullet",
    ]
    # read returns the persisted entry verbatim (raw notes, never escaped by the store)
    persisted = feedback_store.read("forge", unit)
    assert persisted == e2
    assert persisted["terminal_skip"] is False


def test_store_write_empty_unit_id_fails_loud(grove_home):
    from grove.forge import feedback_store

    with pytest.raises(ValueError):
        feedback_store.write("forge", "", "guidance")


def test_store_read_corrupt_entry_raises_not_swallow(grove_home):
    """A present-but-unreadable entry must RAISE (never a feedback-blind None)."""
    from grove.forge import feedback_store

    feedback_store.write("forge", "row-x", "seed")
    path = feedback_store._entry_path("forge", "row-x")
    path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        feedback_store.read("forge", "row-x")


def test_store_absent_entry_is_none(grove_home):
    from grove.forge import feedback_store

    assert feedback_store.read("forge", "never-written") is None


def test_store_forge_path_byte_identical(grove_home):
    """C1b-1 byte-identity: worker="forge", unit_id=row_id reproduces the pre-C1b
    path ~/.grove/forge/.feedback/<row_id>.json exactly."""
    from grove.forge import feedback_store

    p = feedback_store._entry_path("forge", "ROW123")
    assert p == grove_home / "forge" / ".feedback" / "ROW123.json"


def test_set_terminal_skip_is_idempotent(grove_home):
    from grove.forge import feedback_store

    feedback_store.write("forge", "row-t", "g1")
    feedback_store.set_terminal_skip("forge", "row-t")
    feedback_store.set_terminal_skip("forge", "row-t")  # idempotent — no raise, no dup
    assert feedback_store.read("forge", "row-t")["terminal_skip"] is True
    # absent entry -> no-op (nothing to skip)
    feedback_store.set_terminal_skip("forge", "row-absent")
    assert feedback_store.read("forge", "row-absent") is None


def test_gc_reclaims_stale_exempts_terminal_and_leaves_unreadable(grove_home):
    from grove.forge import feedback_store

    # stale non-terminal -> reclaimed
    feedback_store.write("forge", "stale", "old")
    stale_path = feedback_store._entry_path("forge", "stale")
    stale = json.loads(stale_path.read_text())
    stale["written_at"] = "2000-01-01T00:00:00+00:00"
    stale_path.write_text(json.dumps(stale), encoding="utf-8")

    # stale terminal_skip -> EXEMPT (won't-converge must not resurrect after TTL)
    feedback_store.write("forge", "stale-terminal", "old")
    feedback_store.set_terminal_skip("forge", "stale-terminal")
    term_path = feedback_store._entry_path("forge", "stale-terminal")
    term = json.loads(term_path.read_text())
    term["written_at"] = "2000-01-01T00:00:00+00:00"
    term_path.write_text(json.dumps(term), encoding="utf-8")

    # fresh -> kept
    feedback_store.write("forge", "fresh", "new")

    # unreadable -> left in place (never delete blind)
    bad = feedback_store._store_dir("forge") / "corrupt.json"
    bad.write_text("{ not json", encoding="utf-8")

    reclaimed = feedback_store.gc("forge", ttl_seconds=60)
    assert reclaimed == ["stale"]
    assert not feedback_store._entry_path("forge", "stale").exists()
    assert feedback_store._entry_path("forge", "stale-terminal").exists()
    assert feedback_store._entry_path("forge", "fresh").exists()
    assert bad.exists()


# ---------------------------------------------------------------------------
# resolvers — framed directive, priority tier, terminal exclusion, fail-loud
# (C1b-1: the store the helpers read is keyed on (worker_id, unit_id))
# ---------------------------------------------------------------------------


def test_revision_directive_latest_authoritative_priors_context(grove_home):
    from grove.forge import feedback_store
    from grove.fleet import resolvers

    feedback_store.write("worker-1", "row-d", "make it shorter")
    feedback_store.write("worker-1", "row-d", "add the salary band")
    directive = resolvers._revision_directive("row-d", "worker-1")
    # latest is the authoritative directive; priors are framed as context-only
    assert "<<<add the salary band>>>" in directive
    assert "authoritative" in directive
    assert "make it shorter" in directive  # prior, present as context
    # index of latest note appears before the priors framing (latest leads)
    assert directive.index("add the salary band") < directive.index("context only")


def test_revision_directive_single_note_no_priors_clause(grove_home):
    from grove.forge import feedback_store
    from grove.fleet import resolvers

    feedback_store.write("w", "row-s", "only guidance")
    directive = resolvers._revision_directive("row-s", "w")
    assert "<<<only guidance>>>" in directive
    assert "context only" not in directive


def test_revision_directive_none_when_no_guidance_or_terminal(grove_home):
    from grove.forge import feedback_store
    from grove.fleet import resolvers

    assert resolvers._revision_directive("row-none", "w") is None
    feedback_store.write("w", "row-term", "g")
    feedback_store.set_terminal_skip("w", "row-term")
    assert resolvers._revision_directive("row-term", "w") is None


def test_has_revision_priority_and_terminal_skip_gates(grove_home):
    from grove.forge import feedback_store
    from grove.fleet import resolvers

    feedback_store.write("w", "row-p", "g")
    assert resolvers._has_revision_priority("row-p", "w") is True
    assert resolvers._is_terminal_skip("row-p", "w") is False
    feedback_store.set_terminal_skip("w", "row-p")
    # terminal rows lose priority AND are flagged terminal
    assert resolvers._has_revision_priority("row-p", "w") is False
    assert resolvers._is_terminal_skip("row-p", "w") is True


def test_select_units_prioritizes_pending_and_excludes_terminal(grove_home):
    """The spine's selection contract: a revision-pending unit jumps the fresh-fit
    queue; a won't-converge (terminal_skip) unit is removed entirely."""
    from grove.forge import feedback_store
    from grove.fleet import resolvers

    feedback_store.write("worker-1", "revised", "redo it")       # -> priority
    feedback_store.write("worker-1", "dead", "loop")
    feedback_store.set_terminal_skip("worker-1", "dead")          # -> excluded

    rows = [{"id": "fresh1"}, {"id": "dead"}, {"id": "revised"}, {"id": "fresh2"}]
    selected = resolvers._select_units(rows, {}, "worker-1")
    ids = [r["id"] for r in selected]
    assert "dead" not in ids                          # terminal EXCLUSION
    assert ids[0] == "revised"                         # revision-priority tier first
    assert ids[1:] == ["fresh1", "fresh2"]             # order_by-stable within rest


def test_select_units_no_guidance_is_byte_identical_order(grove_home):
    from grove.fleet import resolvers

    rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    selected = resolvers._select_units(list(rows), {}, "w")
    assert [r["id"] for r in selected] == ["a", "b", "c"]


def test_resolver_read_corrupt_store_raises_fleet_andon(grove_home):
    """B7 — a corrupt store entry surfaces as a LOUD FleetWorkerAndon, never a
    feedback-blind draft."""
    from grove.forge import feedback_store
    from grove.fleet import resolvers

    feedback_store.write("worker-9", "row-c", "seed")
    feedback_store._entry_path("worker-9", "row-c").write_text("{ broken", encoding="utf-8")
    with pytest.raises(FleetWorkerAndon) as ei:
        resolvers._has_revision_priority("row-c", "worker-9")
    assert ei.value.check == "revision_store_unreadable"


# ---------------------------------------------------------------------------
# worker_entry — B1 attention fix: directive is its OWN segment, out of the json
# ---------------------------------------------------------------------------


def test_worker_prompt_surfaces_directive_out_of_json():
    from grove.fleet import worker_entry

    payload = {"rows": [{"id": "r1"}], "revision_directive": "Produce a NEW draft."}
    # forge-fleet-package-emission-v1 P2 added the per-run `tag` param (run_id[:8]);
    # the directive-lift behavior this asserts is unchanged by that sprint.
    prompt = worker_entry._build_worker_prompt("job-application-forge", payload, "abc12345")
    # explicit directive segment, before RESOLVED INPUT
    assert "REVISION DIRECTIVE" in prompt
    assert "Produce a NEW draft." in prompt
    assert prompt.index("REVISION DIRECTIVE") < prompt.index("RESOLVED INPUT")
    # lifted OUT of the json blob (a passive key the corpus-only worker would ignore)
    json_start = prompt.index("RESOLVED INPUT")
    assert "revision_directive" not in prompt[json_start:]


def test_worker_prompt_byte_identical_without_directive():
    from grove.fleet import worker_entry

    payload = {"rows": [{"id": "r1"}]}
    # forge-fleet-package-emission-v1 P2 added the per-run `tag` param (run_id[:8]).
    prompt = worker_entry._build_worker_prompt("job-application-forge", payload, "abc12345")
    assert "REVISION DIRECTIVE" not in prompt
    assert "RESOLVED INPUT:" in prompt


# ---------------------------------------------------------------------------
# manager — C1b-1 the revision-directive fold, LIFTED to the worker runtime seam
# and AMENDMENT-gated on approval_handoff.mode == "action_surface_publish".
# Exercises the REAL capability mode read (forge=action_surface_publish,
# scout=ingest_post) through _maybe_dispatch_one, not a monkeypatched gate.
# (researcher-fleet-worker-v1 P1 flipped researcher to action_surface_publish;
# scout is the surviving real ingest_post exemplar.)
# ---------------------------------------------------------------------------


class _FakeHandle:
    def __init__(self):
        self.run_id = "run-0001"


def _dispatch_capturing(monkeypatch, skill_id, unit_id):
    """Drive FleetManager._maybe_dispatch_one with the resolver/mcp/runner seams
    stubbed, returning the payload runner.dispatch actually received. The mode gate
    (_review_mode_for_skill) runs FOR REAL against the capability registry."""
    from datetime import datetime, timezone

    from grove.fleet import manager as mgr
    from grove.fleet.config import WorkerConfig

    captured = {}

    def _fake_resolve(input_state, wid):
        # the resolver's job post-C1b-1: build the payload + stamp unit_id, NO fold
        return {"rows": [{"id": unit_id}], "unit_id": unit_id}

    def _fake_dispatch(cfg, payload):
        captured["payload"] = payload
        return _FakeHandle()

    monkeypatch.setattr(mgr, "resolve_input_state", _fake_resolve)
    monkeypatch.setattr(mgr.runner, "dispatch", _fake_dispatch)
    monkeypatch.setattr(mgr.FleetManager, "_ensure_mcp_warm_sync", lambda self, s, w: None)

    cfg = WorkerConfig(
        id="w", skill=skill_id, enabled=True, cadence="* * * * *",
        input_state={"type": "notion_query", "server": "notion"},
        limits={}, quiet_hours=None,
    )
    m = mgr.FleetManager()
    m._maybe_dispatch_one("w", cfg, datetime.now(timezone.utc))
    return captured.get("payload")


def test_manager_injects_directive_for_action_surface_publish(grove_home, monkeypatch):
    """VERDICT A — forge (mode=action_surface_publish, read FOR REAL from the cap
    record): a feedback file present for the unit -> the manager folds the framed
    directive into the dispatch payload at the runtime seam."""
    from grove.forge import feedback_store

    feedback_store.write("w", "row-a", "tighten the ask")
    payload = _dispatch_capturing(monkeypatch, "skill.fleet.forge-jobsearch", "row-a")
    assert payload is not None
    assert "revision_directive" in payload
    assert "<<<tighten the ask>>>" in payload["revision_directive"]


def test_manager_no_injection_for_ingest_post_mode(grove_home, monkeypatch):
    """VERDICT B — scout (mode=ingest_post, read FOR REAL): even WITH a feedback
    file present for the unit, the amendment gate suppresses injection entirely."""
    from grove.forge import feedback_store

    feedback_store.write("w", "row-b", "add a citation")
    payload = _dispatch_capturing(monkeypatch, "skill.fleet.scout", "row-b")
    assert payload is not None
    assert "revision_directive" not in payload  # amendment gate held


def test_redraft_cycle_n_breaker_increments_then_terminates(grove_home):
    """VERDICT A (cycle) — suggest_revision accumulates (count 1..N), the directive is
    available for re-draft each round, and the N-breaker (count >= _REVISION_MAX) sets
    terminal_skip, which then EXCLUDES the unit from re-selection (directive -> None)."""
    from grove.api.actions import _REVISION_MAX
    from grove.forge import feedback_store
    from grove.fleet import resolvers

    unit = "row-cycle"
    for i in range(1, _REVISION_MAX + 1):
        entry = feedback_store.write("w", unit, f"round {i}")
        assert entry["count"] == i
        # each round the framed directive is available to the next re-draft
        assert resolvers._revision_directive(unit, "w") is not None
        if int(entry["count"]) >= _REVISION_MAX:  # the portal's N-breaker predicate
            feedback_store.set_terminal_skip("w", unit)
    # won't-converge: excluded from re-selection, and no directive is served
    assert resolvers._is_terminal_skip(unit, "w") is True
    assert resolvers._revision_directive(unit, "w") is None
    rows = [{"id": unit}, {"id": "fresh"}]
    assert unit not in [r["id"] for r in resolvers._select_units(rows, {}, "w")]


# ---------------------------------------------------------------------------
# fragments — the render affordance (colon-free DOM id, hx-include wiring)
# ---------------------------------------------------------------------------


def test_forge_kaizen_div_renders_enabled_suggest_revision():
    from grove.api import fragments

    pid = "sha256:deadbeefcafe"
    # fleet-artifact-legibility-v1 C2 — the C1a compat shim retired; render the
    # bar directly (remote_sink=True = the forge case the shim defaulted to).
    html = fragments._disposition_bar(pid, remote_sink=True)
    short = fragments._short_id(pid)
    rev_id = f"rev-{short}"
    # enabled textarea (not the old disabled placeholder button)
    assert f'id="{rev_id}"' in html
    assert 'name="revision_text"' in html

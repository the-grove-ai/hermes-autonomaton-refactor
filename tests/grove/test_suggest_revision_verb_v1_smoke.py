"""End-to-end local smoke for suggest-revision-verb-v1 (P5).

The informed-path loop-back tap has a spine that crosses four modules:

    operator taps "Suggest revision"           (fragments._forge_kaizen_div render)
      -> feedback_store.write accumulates        (grove.forge.feedback_store)
        -> resolver selects that row FIRST,       (grove.fleet.resolvers._select_units)
           folds the framed directive             (grove.fleet.resolvers._revision_directive)
          -> worker prompt surfaces the directive  (grove.fleet.worker_entry._build_worker_prompt)

plus the P4 N-breaker (terminal_skip exclusion + won't-converge threshold), the
TTL-GC, the orphan-staged crash-residual sweep, and the fail-loud corrupt-store
Andon. This smoke exercises that spine against a REAL temp GROVE_HOME store (no
network, no model) — the deterministic seam is the prompt build; the model turn
downstream of it is out of scope for a deterministic test.

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
# ---------------------------------------------------------------------------


def test_store_accumulates_history_and_count(grove_home):
    from grove.forge import feedback_store

    row = "row-abc"
    e1 = feedback_store.write(row, "tighten the summary")
    e2 = feedback_store.write(row, "add a metrics bullet")
    assert e1["count"] == 1 and e2["count"] == 2
    assert [h["revision_note"] for h in e2["history"]] == [
        "tighten the summary",
        "add a metrics bullet",
    ]
    # read returns the persisted entry verbatim (raw notes, never escaped by the store)
    persisted = feedback_store.read(row)
    assert persisted == e2
    assert persisted["terminal_skip"] is False


def test_store_write_empty_row_id_fails_loud(grove_home):
    from grove.forge import feedback_store

    with pytest.raises(ValueError):
        feedback_store.write("", "guidance")


def test_store_read_corrupt_entry_raises_not_swallow(grove_home):
    """A present-but-unreadable entry must RAISE (never a feedback-blind None)."""
    from grove.forge import feedback_store

    feedback_store.write("row-x", "seed")
    path = feedback_store._entry_path("row-x")
    path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        feedback_store.read("row-x")


def test_store_absent_entry_is_none(grove_home):
    from grove.forge import feedback_store

    assert feedback_store.read("never-written") is None


def test_set_terminal_skip_is_idempotent(grove_home):
    from grove.forge import feedback_store

    feedback_store.write("row-t", "g1")
    feedback_store.set_terminal_skip("row-t")
    feedback_store.set_terminal_skip("row-t")  # idempotent — no raise, no dup
    assert feedback_store.read("row-t")["terminal_skip"] is True
    # absent entry -> no-op (nothing to skip)
    feedback_store.set_terminal_skip("row-absent")
    assert feedback_store.read("row-absent") is None


def test_gc_reclaims_stale_exempts_terminal_and_leaves_unreadable(grove_home):
    from grove.forge import feedback_store

    # stale non-terminal -> reclaimed
    feedback_store.write("stale", "old")
    stale_path = feedback_store._entry_path("stale")
    stale = json.loads(stale_path.read_text())
    stale["written_at"] = "2000-01-01T00:00:00+00:00"
    stale_path.write_text(json.dumps(stale), encoding="utf-8")

    # stale terminal_skip -> EXEMPT (won't-converge must not resurrect after TTL)
    feedback_store.write("stale-terminal", "old")
    feedback_store.set_terminal_skip("stale-terminal")
    term_path = feedback_store._entry_path("stale-terminal")
    term = json.loads(term_path.read_text())
    term["written_at"] = "2000-01-01T00:00:00+00:00"
    term_path.write_text(json.dumps(term), encoding="utf-8")

    # fresh -> kept
    feedback_store.write("fresh", "new")

    # unreadable -> left in place (never delete blind)
    bad = feedback_store._store_dir() / "corrupt.json"
    bad.write_text("{ not json", encoding="utf-8")

    reclaimed = feedback_store.gc(ttl_seconds=60)
    assert reclaimed == ["stale"]
    assert not feedback_store._entry_path("stale").exists()
    assert feedback_store._entry_path("stale-terminal").exists()
    assert feedback_store._entry_path("fresh").exists()
    assert bad.exists()


# ---------------------------------------------------------------------------
# resolvers — framed directive, priority tier, terminal exclusion, fail-loud
# ---------------------------------------------------------------------------


def test_revision_directive_latest_authoritative_priors_context(grove_home):
    from grove.forge import feedback_store
    from grove.fleet import resolvers

    feedback_store.write("row-d", "make it shorter")
    feedback_store.write("row-d", "add the salary band")
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

    feedback_store.write("row-s", "only guidance")
    directive = resolvers._revision_directive("row-s", "w")
    assert "<<<only guidance>>>" in directive
    assert "context only" not in directive


def test_revision_directive_none_when_no_guidance_or_terminal(grove_home):
    from grove.forge import feedback_store
    from grove.fleet import resolvers

    assert resolvers._revision_directive("row-none", "w") is None
    feedback_store.write("row-term", "g")
    feedback_store.set_terminal_skip("row-term")
    assert resolvers._revision_directive("row-term", "w") is None


def test_has_revision_priority_and_terminal_skip_gates(grove_home):
    from grove.forge import feedback_store
    from grove.fleet import resolvers

    feedback_store.write("row-p", "g")
    assert resolvers._has_revision_priority("row-p", "w") is True
    assert resolvers._is_terminal_skip("row-p", "w") is False
    feedback_store.set_terminal_skip("row-p")
    # terminal rows lose priority AND are flagged terminal
    assert resolvers._has_revision_priority("row-p", "w") is False
    assert resolvers._is_terminal_skip("row-p", "w") is True


def test_select_units_prioritizes_pending_and_excludes_terminal(grove_home):
    """The spine's selection contract: a revision-pending row jumps the fresh-fit
    queue; a won't-converge (terminal_skip) row is removed entirely."""
    from grove.forge import feedback_store
    from grove.fleet import resolvers

    feedback_store.write("revised", "redo it")       # -> priority
    feedback_store.write("dead", "loop")
    feedback_store.set_terminal_skip("dead")          # -> excluded

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

    feedback_store.write("row-c", "seed")
    feedback_store._entry_path("row-c").write_text("{ broken", encoding="utf-8")
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
# fragments — the render affordance (colon-free DOM id, hx-include wiring)
# ---------------------------------------------------------------------------


def test_forge_kaizen_div_renders_enabled_suggest_revision():
    from grove.api import fragments

    pid = "sha256:deadbeefcafe"
    html = fragments._forge_kaizen_div(pid)
    short = fragments._short_id(pid)
    rev_id = f"rev-{short}"
    # enabled textarea (not the old disabled placeholder button)
    assert f'id="{rev_id}"' in html
    assert 'name="revision_text"' in html
    assert "disabled" not in html
    # colon-free CSS selector (the P1 Andon fix): #id must not carry the raw pid colon
    assert f'hx-include="#{rev_id}"' in html
    assert ":" not in rev_id
    # submit routes to the suggest_revision endpoint with the raw pid in the path
    assert f"/portal/actions/proposals/{pid}/suggest_revision" in html


# ---------------------------------------------------------------------------
# actions — N-breaker constant, orphan-staged sweep (marker-gated), error card
# ---------------------------------------------------------------------------


def test_revision_max_constant_is_the_n_breaker_threshold():
    from grove.api import actions

    assert actions._REVISION_MAX == 3


def test_orphan_sweep_only_archives_marked_dirs(grove_home):
    """The finalize-before-archive crash residual self-heals: ONLY a dir carrying
    the ``.archive-pending`` marker is swept; a healthy/actively-staging dir is not
    (false-positive guard)."""
    from grove.api import actions

    pending = grove_home / "forge" / "pending_review"
    marked = pending / "marked-slug"
    healthy = pending / "healthy-slug"
    marked.mkdir(parents=True)
    healthy.mkdir(parents=True)
    (marked / ".archive-pending").write_text("2026-01-01T00:00:00+00:00", encoding="utf-8")
    (marked / "meta.json").write_text("{}", encoding="utf-8")
    (healthy / "meta.json").write_text("{}", encoding="utf-8")

    swept = actions._sweep_orphan_staged()
    assert swept == ["marked-slug"]
    assert not marked.exists()          # archived (moved out of pending_review)
    assert healthy.exists()             # untouched — no marker


def test_orphan_sweep_empty_when_no_pending_dir(grove_home):
    from grove.api import actions

    assert actions._sweep_orphan_staged() == []


def test_forge_suggest_error_card_escapes_and_targets_short_id():
    from grove.api import actions

    card = actions._forge_suggest_error_card("abc123", "Revision guidance is <empty>")
    assert 'id="proposal-abc123"' in card
    assert "&lt;empty&gt;" in card      # HTML-escaped at render

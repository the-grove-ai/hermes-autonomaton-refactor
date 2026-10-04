"""dock-as-mutation-target-v1 — memory proposes Dock goals, two-file merge.

Covers the detector (T1-gated proposal synthesis), the machine-file writer
(atomic, dedup, backup), the load_dock two-file merge (operator wins, cap 3),
the dock_mutation proposal-type registration, and the R-2 raise-on-T1-failure
contract (detector-sweep-resilience-v1; containment is the sweep guard's job).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from grove.dock import (
    MACHINE_DOCK_FILENAME,
    active_goals,
    append_machine_goal,
    load_dock,
)
from grove.dock.detector import DockMutationDetector


# ── fakes ────────────────────────────────────────────────────────────────────


def _rec(rid, content="some content", entity_type="ProjectState",
         dock_goal_ref=None, status="active"):
    return SimpleNamespace(
        id=rid, content=content, entity_type=entity_type,
        dock_goal_ref=dock_goal_ref, status=status,
    )


class _FakeStore:
    def __init__(self, records):
        self._records = {r.id: r for r in records}

    def projected_records(self):
        return self._records


_THEME = {"name": "Memory Substrate Architecture",
          "keywords": ["memory", "substrate", "event-sourcing"]}


def _patch_t1(monkeypatch, theme):
    monkeypatch.setattr(
        DockMutationDetector, "_synthesize_goal",
        lambda self, contents: theme,
    )


def _op_goal(gid="op-goal", name="Op Goal", status="accelerating",
             vector="strategic"):
    return {
        "id": gid, "name": name, "vector": vector, "status": status,
        "definition_of_done": "done", "context_sources": [],
        "keywords": ["op"], "unlocked_skills": [],
    }


def _write_operator_dock(dock_dir, goals):
    p = dock_dir / "dock.yaml"
    p.write_text(yaml.safe_dump({"version": 1, "goals": goals}), encoding="utf-8")
    return p


def _write_machine(dock_dir, goals):
    (dock_dir / MACHINE_DOCK_FILENAME).write_text(
        yaml.safe_dump({"goals": goals}), encoding="utf-8"
    )


def _mgoal(gid, name="M", created_at="2026-06-22T12:00:00Z", **extra):
    g = {"id": gid, "name": name, "keywords": ["m"], "vector": "personal",
         "status": "staging", "definition_of_done": "", "created_at": created_at}
    g.update(extra)
    return g


# ── detector (SPEC tests 1-5) ─────────────────────────────────────────────────


class TestDetector:
    def test_1_theme_yields_proposal(self, monkeypatch):
        _patch_t1(monkeypatch, _THEME)
        store = _FakeStore([_rec(f"mem_{i}") for i in range(5)])
        proposals = DockMutationDetector().detect(store, set())
        assert len(proposals) == 1
        goal = proposals[0]["goal"]
        assert proposals[0]["action"] == "create_goal"
        assert goal["id"] == "auto-memory-substrate-architecture"
        assert goal["status"] == "staging"
        assert goal["vector"] == "personal"
        assert len(goal["source_record_ids"]) == 5

    def test_2_below_threshold_no_proposal(self, monkeypatch):
        _patch_t1(monkeypatch, _THEME)
        store = _FakeStore([_rec(f"mem_{i}") for i in range(3)])
        assert DockMutationDetector().detect(store, set()) == []

    def test_3_t1_null_no_proposal(self, monkeypatch):
        _patch_t1(monkeypatch, None)
        store = _FakeStore([_rec(f"mem_{i}") for i in range(6)])
        assert DockMutationDetector().detect(store, set()) == []

    def test_4_all_attached_no_proposal(self, monkeypatch):
        _patch_t1(monkeypatch, _THEME)
        store = _FakeStore(
            [_rec(f"mem_{i}", dock_goal_ref="some-goal") for i in range(8)]
        )
        assert DockMutationDetector().detect(store, set()) == []

    def test_5_max_one_per_session(self, monkeypatch):
        _patch_t1(monkeypatch, _THEME)
        store = _FakeStore([_rec(f"mem_{i}") for i in range(50)])
        assert len(DockMutationDetector().detect(store, set())) == 1

    def test_non_goal_worthy_types_ignored(self, monkeypatch):
        _patch_t1(monkeypatch, _THEME)
        store = _FakeStore(
            [_rec(f"p_{i}", entity_type="OperatorPreference") for i in range(8)]
        )
        assert DockMutationDetector().detect(store, set()) == []

    def test_existing_slug_not_reproposed(self, monkeypatch):
        _patch_t1(monkeypatch, _THEME)
        store = _FakeStore([_rec(f"mem_{i}") for i in range(6)])
        slugs = {"auto-memory-substrate-architecture"}
        assert DockMutationDetector().detect(store, slugs) == []

    def test_r2_t1_failure_raises(self, monkeypatch):
        # detector-sweep-resilience-v1 R-2 (moved pin, was
        # test_a6_t1_failure_skips): a T1 synthesis failure now RAISES from
        # detect() instead of swallowing to [] — containment + the
        # producer_failure filing happen one layer up, at the Dispatcher's
        # per-producer sweep guard (pinned in
        # tests/grove/test_detector_sweep_resilience.py).
        def _boom(self, contents):
            raise TimeoutError("simulated slow T1")

        monkeypatch.setattr(DockMutationDetector, "_synthesize_goal", _boom)
        store = _FakeStore([_rec(f"mem_{i}") for i in range(6)])
        with pytest.raises(TimeoutError):
            DockMutationDetector().detect(store, set())


# ── writer (SPEC tests 6-8) ────────────────────────────────────────────────────


def _dedup_cfg(**kw):
    from grove.dock.detector import DockDetectorConfig

    return DockDetectorConfig(
        dedup_claimed_records=True, dedup_theme_overlap=True, **kw
    )


class TestDetectorDedup:
    """dock-detector-dedup-v1 — the OPT-IN interim guards: claimed records +
    reworded themes never re-propose (the live defect: 19 near-duplicate goals
    from the same 10 records). Default OFF — pinned first."""

    def test_guards_are_off_by_default(self, monkeypatch):
        # Same inputs that the guards would suppress still propose by default.
        _patch_t1(monkeypatch, _THEME)
        store = _FakeStore([_rec(f"mem_{i}") for i in range(6)])
        existing = [{"name": _THEME["name"], "keywords": _THEME["keywords"],
                     "source_record_ids": [f"mem_{i}" for i in range(6)]}]
        assert DockMutationDetector().detect(store, set(), existing_themes=existing)

    def test_detect_reads_nothing_itself(self, monkeypatch):
        # PURE: with no themes/config passed, no Dock/queue/config read happens.
        import grove.dock.detector as det

        def _boom(*a, **kw):
            raise AssertionError("detect() must not read — the caller gathers")

        monkeypatch.setattr(det, "existing_goal_themes", _boom)
        monkeypatch.setattr(det, "load_dock_detector_config", _boom)
        _patch_t1(monkeypatch, _THEME)
        store = _FakeStore([_rec(f"mem_{i}") for i in range(6)])
        assert DockMutationDetector().detect(store, set())

    def test_records_claimed_by_existing_goal_are_not_unattached(self, monkeypatch):
        _patch_t1(monkeypatch, _THEME)
        store = _FakeStore([_rec(f"mem_{i}") for i in range(6)])
        existing = [{"name": "Unrelated Carpentry", "keywords": ["wood"],
                     "source_record_ids": [f"mem_{i}" for i in range(6)]}]
        assert DockMutationDetector().detect(
            store, set(), existing_themes=existing, config=_dedup_cfg()) == []

    def test_claimed_suppression_is_logged(self, monkeypatch, caplog):
        import logging

        _patch_t1(monkeypatch, _THEME)
        store = _FakeStore([_rec(f"mem_{i}") for i in range(6)])
        existing = [{"name": "Unrelated Carpentry", "keywords": ["wood"],
                     "source_record_ids": [f"mem_{i}" for i in range(6)]}]
        with caplog.at_level(logging.INFO, logger="grove.dock.detector"):
            DockMutationDetector().detect(
                store, set(), existing_themes=existing, config=_dedup_cfg())
        assert "dedup_claimed_records" in caplog.text

    def test_only_unclaimed_records_are_sampled(self, monkeypatch):
        _patch_t1(monkeypatch, _THEME)
        store = _FakeStore([_rec(f"mem_{i}") for i in range(12)])
        existing = [{"name": "Unrelated Carpentry", "keywords": ["wood"],
                     "source_record_ids": [f"mem_{i}" for i in range(6)]}]
        out = DockMutationDetector().detect(
            store, set(), existing_themes=existing, config=_dedup_cfg())
        assert out[0]["goal"]["source_record_ids"] == [f"mem_{i}" for i in range(6, 12)]

    def test_reworded_theme_is_skipped_and_logged(self, monkeypatch, caplog):
        import logging

        # Fresh records, but T1 rewords a theme the Dock already tracks.
        _patch_t1(monkeypatch, {"name": "Evolving Core Architecture",
                                "keywords": ["model independence", "orchestration"]})
        store = _FakeStore([_rec(f"mem_{i}") for i in range(6)])
        existing = [{"name": "Evolve Autonomaton Architecture",
                     "keywords": ["architectural", "model-independent", "orchestrator"],
                     "source_record_ids": []}]
        with caplog.at_level(logging.INFO, logger="grove.dock.detector"):
            out = DockMutationDetector().detect(
                store, set(), existing_themes=existing, config=_dedup_cfg())
        assert out == []
        assert "SUPPRESSED" in caplog.text

    def test_distinct_theme_still_proposes(self, monkeypatch):
        _patch_t1(monkeypatch, _THEME)
        store = _FakeStore([_rec(f"mem_{i}") for i in range(6)])
        existing = [{"name": "Carriage House Renovation",
                     "keywords": ["contractor", "permits"], "source_record_ids": []}]
        out = DockMutationDetector().detect(
            store, set(), existing_themes=existing, config=_dedup_cfg())
        assert out and out[0]["goal"]["name"] == _THEME["name"]

    def test_existing_themes_reads_all_machine_goals_and_pending(self, tmp_path, monkeypatch):
        # More machine goals than load_dock's merge cap — every one still claims.
        import grove.dock as dock_mod
        import grove.dock.detector as det
        import grove.eval.proposal_queue as pq

        dock_dir = tmp_path / "dock"
        dock_dir.mkdir()
        _write_operator_dock(dock_dir, [_op_goal()])
        _write_machine(dock_dir, [
            _mgoal(f"auto-g{i}", name=f"Theme {i}", source_record_ids=[f"mem_{i}"])
            for i in range(5)
        ])
        monkeypatch.setattr(dock_mod, "_machine_dock_dir", lambda: dock_dir)
        monkeypatch.setattr(
            dock_mod, "load_dock", lambda path=None: load_dock(dock_dir / "dock.yaml")
        )
        pending = SimpleNamespace(
            type=pq.PROPOSAL_TYPE_DOCK_MUTATION,
            payload={"goal": {"name": "Pending Theme", "keywords": ["p"],
                              "source_record_ids": ["mem_99"]}},
        )
        monkeypatch.setattr(pq, "read_all", lambda **kw: [pending])

        themes = det.existing_goal_themes()
        claimed = {i for t in themes for i in t["source_record_ids"]}
        assert {f"mem_{i}" for i in range(5)} <= claimed and "mem_99" in claimed
        assert "Op Goal" in {t["name"] for t in themes}


class TestCardFields:
    """The card's "why": rationale, definition of done, and vector come from
    T1 — no more empty justification and hardcoded bottom-rank vector."""

    def test_rationale_becomes_the_card_justification(self, monkeypatch):
        import grove.eval.proposal_queue as pq

        _patch_t1(monkeypatch, {**_THEME, "rationale": "All six concern the memory substrate."})
        store = _FakeStore([_rec(f"mem_{i}") for i in range(6)])
        det = DockMutationDetector()
        out = det.detect(store, set())
        captured = []
        monkeypatch.setattr(pq, "append", lambda rec, **kw: captured.append(rec) or True)
        assert det.stage_proposals(out, session_id="s") == 1
        assert captured[0].semantic_justification == "All six concern the memory substrate."
        assert "rationale" not in captured[0].payload   # payload stays {action, goal}

    def test_vector_and_dod_from_theme(self, monkeypatch):
        _patch_t1(monkeypatch, {**_THEME, "vector": "strategic",
                                "definition_of_done": "Substrate spec published."})
        store = _FakeStore([_rec(f"mem_{i}") for i in range(6)])
        goal = DockMutationDetector().detect(store, set())[0]["goal"]
        assert goal["vector"] == "strategic"
        assert goal["definition_of_done"] == "Substrate spec published."

    def test_vector_and_dod_defaults_when_theme_omits_them(self, monkeypatch):
        from grove.dock.detector import DockDetectorConfig

        _patch_t1(monkeypatch, _THEME)
        store = _FakeStore([_rec(f"mem_{i}") for i in range(6)])
        goal = DockMutationDetector().detect(store, set())[0]["goal"]
        assert goal["vector"] == "personal" and goal["definition_of_done"] == ""
        goal = DockMutationDetector().detect(
            store, set(), config=DockDetectorConfig(default_vector="operational")
        )[0]["goal"]
        assert goal["vector"] == "operational"

    def test_parse_theme_optional_fields(self):
        parse = DockMutationDetector._parse_theme
        assert parse('{"name": "X", "keywords": ["a"]}') == {"name": "X", "keywords": ["a"]}
        full = parse('{"name": "X", "keywords": ["a"], "rationale": " why ", '
                     '"definition_of_done": " done ", "vector": "strategic"}')
        assert full["rationale"] == "why" and full["definition_of_done"] == "done"
        assert full["vector"] == "strategic"
        # An invalid vector is dropped (the configured default then applies).
        assert "vector" not in parse('{"name": "X", "keywords": [], "vector": "cosmic"}')


class TestCardRendering:
    """The approval card shows the rationale and the definition of done —
    rendered from the stored proposal, and unchanged for older proposals."""

    def _proposal(self, sj="", dod=""):
        return SimpleNamespace(
            semantic_justification=sj,
            payload={"action": "create_goal", "goal": {
                "id": "auto-x", "name": "X Theme", "keywords": ["a"],
                "vector": "strategic", "status": "staging",
                "definition_of_done": dod,
                "source_record_ids": ["m1", "m2"],
            }},
        )

    def test_summary_carries_the_rationale(self):
        from grove.kaizen.rendering import _summary_dock_mutation

        out = _summary_dock_mutation(self._proposal(sj="Both concern X."))
        assert out.startswith("2 memory records accumulating around 'X Theme'.")
        assert out.endswith("Why: Both concern X.")

    def test_summary_unchanged_without_rationale(self):
        from grove.kaizen.rendering import _summary_dock_mutation

        assert _summary_dock_mutation(self._proposal()) == (
            "2 memory records accumulating around 'X Theme'. No Dock goal "
            "tracks this. Add a staging goal?"
        )

    def test_diff_shows_definition_of_done_only_when_present(self):
        from grove.kaizen.rendering import _dock_mutation_to_diff

        add = _dock_mutation_to_diff(self._proposal(dod="X shipped."))[
            "dock.autonomaton.yaml"]["goals"]["+add"]
        assert add["definition_of_done"] == "X shipped."
        add = _dock_mutation_to_diff(self._proposal())[
            "dock.autonomaton.yaml"]["goals"]["+add"]
        assert "definition_of_done" not in add


class TestDetectorConfig:
    def test_absent_uses_defaults_and_bad_values_are_loud(self, tmp_path):
        from grove.dock.detector import DockDetectorConfig, load_dock_detector_config

        assert load_dock_detector_config(tmp_path / "missing.yaml") == DockDetectorConfig()
        assert DockDetectorConfig().dedup_claimed_records is False
        assert DockDetectorConfig().dedup_theme_overlap is False
        cfg = tmp_path / "flywheel.config.yaml"
        head = "dock_mutation_detector:\n  "
        cfg.write_text(head + "theme_overlap_threshold: 0.8\n  dedup_theme_overlap: true\n")
        loaded = load_dock_detector_config(cfg)
        assert loaded.theme_overlap_threshold == 0.8 and loaded.dedup_theme_overlap is True
        for bad, key in (
            ("theme_overlap_threshold: 2", "theme_overlap_threshold"),
            ("max_records_to_t1: 0", "max_records_to_t1"),
            ("dedup_claimed_records: yes please", "dedup_claimed_records"),
            ("default_vector: cosmic", "default_vector"),
        ):
            cfg.write_text(head + bad + "\n")
            with pytest.raises(ValueError, match=key):
                load_dock_detector_config(cfg)

    def test_repo_template_block_loads(self):
        from pathlib import Path

        from grove.dock.detector import DockDetectorConfig, load_dock_detector_config

        template = Path(__file__).resolve().parents[2] / "config" / "flywheel.config.yaml"
        assert load_dock_detector_config(template) == DockDetectorConfig()


class TestWriter:
    def test_6_goal_written(self, tmp_path):
        path = append_machine_goal(_THEME_goal(), dock_dir=tmp_path)
        assert path.name == MACHINE_DOCK_FILENAME
        data = yaml.safe_load(path.read_text())
        ids = [g["id"] for g in data["goals"]]
        assert "auto-x" in ids

    def test_7_dedup_no_double_write(self, tmp_path):
        append_machine_goal(_THEME_goal(), dock_dir=tmp_path)
        append_machine_goal(_THEME_goal(), dock_dir=tmp_path)
        data = yaml.safe_load((tmp_path / MACHINE_DOCK_FILENAME).read_text())
        assert [g["id"] for g in data["goals"]].count("auto-x") == 1

    def test_8_atomic_no_temp_left(self, tmp_path):
        append_machine_goal(_THEME_goal(), dock_dir=tmp_path)
        append_machine_goal(_THEME_goal(gid="auto-y"), dock_dir=tmp_path)
        # No .tmp residue; backup created on the second (file-exists) write.
        assert not (tmp_path / (MACHINE_DOCK_FILENAME + ".tmp")).exists()
        assert (tmp_path / (MACHINE_DOCK_FILENAME + ".bak")).exists()
        data = yaml.safe_load((tmp_path / MACHINE_DOCK_FILENAME).read_text())
        assert {"auto-x", "auto-y"} <= {g["id"] for g in data["goals"]}

    def test_created_at_stamped(self, tmp_path):
        append_machine_goal({"id": "auto-z", "name": "Z"}, dock_dir=tmp_path)
        data = yaml.safe_load((tmp_path / MACHINE_DOCK_FILENAME).read_text())
        assert data["goals"][0]["created_at"]


def _THEME_goal(gid="auto-x"):
    return {"id": gid, "name": "X", "keywords": ["x"], "vector": "personal",
            "status": "staging", "definition_of_done": "",
            "source_record_ids": ["mem_a", "mem_b"]}


# ── merge (SPEC tests 9-13) ────────────────────────────────────────────────────


class TestMerge:
    def test_9_operator_goals_present(self, tmp_path):
        _write_operator_dock(tmp_path, [_op_goal()])
        _write_machine(tmp_path, [_mgoal("auto-a")])
        dock = load_dock(tmp_path / "dock.yaml")
        assert "op-goal" in {g.id for g in dock.goals}

    def test_10_system_goals_merged(self, tmp_path):
        _write_operator_dock(tmp_path, [_op_goal()])
        _write_machine(tmp_path, [_mgoal("auto-a")])
        dock = load_dock(tmp_path / "dock.yaml")
        assert "auto-a" in {g.id for g in dock.goals}

    def test_11_operator_wins_collision(self, tmp_path):
        _write_operator_dock(tmp_path, [_op_goal(gid="shared", status="accelerating")])
        _write_machine(tmp_path, [_mgoal("shared", name="SYSTEM VERSION")])
        dock = load_dock(tmp_path / "dock.yaml")
        shared = [g for g in dock.goals if g.id == "shared"]
        assert len(shared) == 1
        assert shared[0].name == "Op Goal"  # operator's, not system's

    def test_12_cap_three_recency(self, tmp_path):
        _write_operator_dock(tmp_path, [_op_goal()])
        _write_machine(tmp_path, [
            _mgoal("auto-1", created_at="2026-01-01T00:00:00Z"),
            _mgoal("auto-2", created_at="2026-02-01T00:00:00Z"),
            _mgoal("auto-3", created_at="2026-03-01T00:00:00Z"),
            _mgoal("auto-4", created_at="2026-04-01T00:00:00Z"),
        ])
        dock = load_dock(tmp_path / "dock.yaml")
        sys_ids = {g.id for g in dock.goals if g.id.startswith("auto-")}
        assert sys_ids == {"auto-2", "auto-3", "auto-4"}  # oldest (auto-1) dropped

    def test_13_absent_machine_file_unchanged(self, tmp_path):
        _write_operator_dock(tmp_path, [_op_goal()])
        dock = load_dock(tmp_path / "dock.yaml")
        assert {g.id for g in dock.goals} == {"op-goal"}

    def test_malformed_machine_file_does_not_break_dock(self, tmp_path):
        _write_operator_dock(tmp_path, [_op_goal()])
        (tmp_path / MACHINE_DOCK_FILENAME).write_text(": not valid yaml :\n[", encoding="utf-8")
        dock = load_dock(tmp_path / "dock.yaml")  # must NOT raise
        assert "op-goal" in {g.id for g in dock.goals}

    def test_a4_staging_system_goal_is_inert(self, tmp_path):
        # Staging system goal merges into dock.goals but is NOT active — so the
        # six active-gated load_dock callers never surface it.
        _write_operator_dock(tmp_path, [_op_goal()])
        _write_machine(tmp_path, [_mgoal("auto-a", status="staging")])
        dock = load_dock(tmp_path / "dock.yaml")
        assert "auto-a" in {g.id for g in dock.goals}
        assert "auto-a" not in {g.id for g in active_goals(dock)}


# ── rendering (SPEC tests 14-15) ───────────────────────────────────────────────


class TestRendering:
    def _proposal(self):
        from grove.eval.proposal_queue import (
            PROPOSAL_TYPE_DOCK_MUTATION, RoutingProposal, _now_iso,
        )
        goal = {"id": "auto-atlas-ux", "name": "Atlas UX",
                "keywords": ["atlas", "ux"], "vector": "personal",
                "status": "staging", "definition_of_done": "",
                "source_record_ids": ["m1", "m2", "m3", "m4", "m5", "m6"]}
        return RoutingProposal(
            proposal_id="sha256:abc", type=PROPOSAL_TYPE_DOCK_MUTATION,
            payload={"action": "create_goal", "goal": goal}, evidence=(),
            eval_hash="", created_at=_now_iso(),
        )

    def test_14_summary_has_count_and_theme(self):
        from grove.flywheel_cli import _summary_dock_mutation
        s = _summary_dock_mutation(self._proposal())
        assert "6 memory records" in s
        assert "Atlas UX" in s

    def test_15_push_frame(self):
        body = self._proposal().push_body("a new goal")
        assert body.startswith("I've observed a pattern worth tracking")

    def test_diff_renders_machine_file_add(self):
        from grove.flywheel_cli import _dock_mutation_to_diff
        diff = _dock_mutation_to_diff(self._proposal())
        assert MACHINE_DOCK_FILENAME in diff


# ── type registration end-to-end (SPEC test 16) ───────────────────────────────


class TestTypeRegistration:
    def test_16_end_to_end_queue_to_apply(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GROVE_HOME", str(tmp_path))
        from grove.eval import proposal_queue as pq
        from grove.flywheel_cli import _handler_for

        proposal = {"action": "create_goal", "goal": _THEME_goal("auto-e2e")}
        staged = DockMutationDetector().stage_proposals([proposal], "sess-1")
        assert staged == 1

        queued = pq.read_all()
        dm = [p for p in queued if p.type == pq.PROPOSAL_TYPE_DOCK_MUTATION]
        assert len(dm) == 1

        handler = _handler_for(dm[0].type)  # registry dispatch resolves
        target, applied = handler.apply_callback(dm[0], machine_path=None)
        assert applied["goal_id"] == "auto-e2e"
        data = yaml.safe_load((tmp_path / "dock" / MACHINE_DOCK_FILENAME).read_text())
        assert "auto-e2e" in {g["id"] for g in data["goals"]}

    def test_stage_dedup_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GROVE_HOME", str(tmp_path))
        proposal = {"action": "create_goal", "goal": _THEME_goal("auto-dup")}
        assert DockMutationDetector().stage_proposals([proposal], "s") == 1
        assert DockMutationDetector().stage_proposals([proposal], "s") == 0

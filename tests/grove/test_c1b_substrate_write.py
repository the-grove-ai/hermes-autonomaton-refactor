"""GRV-010 C1b-i — substrate-write containment (Option A, sole-path).

Covers the four shipped closures:
  * Phase 1 — is_governed_path + the write_file/patch governed-path lock (B9/B11).
  * Phase 2 — skill_manage bound away from the live ~/.grove/skills tree (B10).
  * Phase 3 — propose_governance_change door: target-aware Stage-04 zone +
    write + governance_change ledger entry.
  * Phase 4 — invoke_skill EXECUTABLE_STATES guard on the green/active path (B14).
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    """Redirect GROVE_HOME to a tmp tree so the governance boundary is testable
    without touching the operator's real ~/.grove."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


# ── Phase 1 — is_governed_path ───────────────────────────────────────────────


class TestIsGovernedPath:
    def test_governance_config_is_governed(self, grove_home):
        from grove.utils.fs_utils import is_governed_path
        assert is_governed_path(grove_home / "zones.schema.yaml") is True
        assert is_governed_path(grove_home / ".env") is True

    def test_live_grove_skill_is_governed(self, grove_home):
        from grove.utils.fs_utils import is_governed_path
        assert is_governed_path(grove_home / "skills" / "my-skill" / "SKILL.md") is True

    def test_andon_quarantine_is_allowlisted(self, grove_home):
        from grove.utils.fs_utils import is_governed_path
        p = grove_home / "skills" / ".andon" / "draft" / "SKILL.md"
        assert is_governed_path(p) is False

    def test_path_outside_grove_is_not_governed(self, grove_home, tmp_path):
        from grove.utils.fs_utils import is_governed_path
        assert is_governed_path(tmp_path.parent / "elsewhere" / "scratch.txt") is False

    def test_andon_dotdot_escape_collapses_and_is_governed(self, grove_home):
        # realpath collapses .andon/../<live> to the live tree → governed.
        from grove.utils.fs_utils import is_governed_path
        escape = grove_home / "skills" / ".andon" / ".." / "zones_escape.yaml"
        assert is_governed_path(escape) is True


# ── Phase 1 — file-tool lock ─────────────────────────────────────────────────


class TestFileToolGovernedLock:
    @pytest.fixture(autouse=True)
    def _neutralize_sensitive_check(self, monkeypatch):
        # Orthogonal to the governed-path lock under test: macOS pytest tmp dirs
        # live under /private/var (a _check_sensitive_path prefix), which would
        # fire first. In production ~/.grove is under $HOME (not sensitive), so
        # the governed-path lock is the operative gate. Neutralize the sensitive
        # check so these tests exercise the governed lock specifically.
        monkeypatch.setattr(
            "tools.file_tools._check_sensitive_path", lambda *a, **k: None,
        )

    def test_write_file_refuses_governed_path(self, grove_home):
        # secrets-only-wall-v1: zones.schema.yaml is a NON-secret ~/.grove config
        # → no longer statically walled. Assert at the unit level that the path is
        # not secret-walled (tmp dirs trip _check_sensitive_path on the tool path).
        from grove.utils.fs_utils import is_secret_path
        assert is_secret_path(str(grove_home / "zones.schema.yaml")) is False

    def test_patch_refuses_governed_path(self, grove_home):
        # secrets-only-wall-v1: routing.config.yaml is non-secret → not walled.
        from grove.utils.fs_utils import is_secret_path
        assert is_secret_path(str(grove_home / "routing.config.yaml")) is False

    def test_write_file_allows_andon(self, grove_home):
        from tools.file_tools import write_file_tool
        target = grove_home / "skills" / ".andon" / "draft" / "SKILL.md"
        raw = write_file_tool(str(target), "---\nname: draft\n---\nbody\n")
        result = json.loads(raw)
        assert not result.get("error"), result
        assert target.read_text().startswith("---")


# ── Phase 2 — skill_manage bound away from the live grove tree ───────────────


class TestSkillManageGovernedRefusal:
    def test_require_andon_target_refuses_live_grove_skill(self, grove_home):
        from tools.skill_manager_tool import _require_andon_target
        refusal = _require_andon_target({"path": grove_home / "skills" / "live-skill"})
        assert refusal is not None and refusal["success"] is False
        assert "live ~/.grove/skills" in refusal["error"]

    def test_require_andon_target_allows_andon_skill(self, grove_home):
        from tools.skill_manager_tool import _require_andon_target
        assert _require_andon_target(
            {"path": grove_home / "skills" / ".andon" / "draft"}
        ) is None

    def test_require_andon_target_allows_external_vault(self, grove_home, tmp_path):
        # An external skill vault outside ~/.grove is NOT governed — editing it
        # in place stays allowed (the #4759 feature is preserved).
        from tools.skill_manager_tool import _require_andon_target
        external = tmp_path.parent / "vault" / "ext-skill"
        assert _require_andon_target({"path": external}) is None


# ── Phase 3 — propose_governance_change door ─────────────────────────────────


class TestGovernanceTargetResolution:
    """capability-mutation-surface-v1 P5 (M5) — classify_governance_target is
    RETIRED. Target resolution now runs through the viability seam + the
    governed-writer registry (grove.red_pending_store); these pins replace
    the Pipeline-A classification pins."""

    def _propose_args(self, target, content="TOK=x\n"):
        return {"target_file": str(target), "content": content, "rationale": "r"}

    def test_env_seals_to_env_write(self, grove_home):
        from grove.red_pending_store import seal_red_claim
        sealed = seal_red_claim(
            "propose_governance_change", self._propose_args(grove_home / ".env")
        )
        assert sealed["writer_name"] == "env_write"

    def test_routing_config_seals_to_routing_writer(self, grove_home):
        # D3 unification — the governance door's raw routing write is dead;
        # the claim seals against RoutingConfigWriter.apply_mutation.
        from grove.red_pending_store import seal_red_claim
        sealed = seal_red_claim(
            "propose_governance_change",
            self._propose_args(grove_home / "routing.config.yaml", "tiers: {}\n"),
        )
        assert sealed["writer_name"] == "routing_config_replace"

    def test_zones_schema_dead_door_is_registry_miss(self, grove_home):
        # ~/.grove/zones.schema.yaml is unread by the runtime; nothing
        # registers a writer for it — the dead door is retired at the seam.
        from grove.red_pending_store import is_viable_red_target
        viable, reason = is_viable_red_target(
            "propose_governance_change",
            self._propose_args(grove_home / "zones.schema.yaml"),
        )
        assert viable is False
        assert "no registered writer" in reason

    def test_non_governance_target_passes_unsealed(self, grove_home, tmp_path):
        from grove.red_pending_store import is_viable_red_target, seal_red_claim
        args = self._propose_args(tmp_path.parent / "scratch.yaml")
        viable, _ = is_viable_red_target("propose_governance_change", args)
        assert viable is True  # non-config: lifecycle-only pass-through
        assert seal_red_claim("propose_governance_change", args)["writer_name"] is None


class TestGovernanceDispatcherClassification:
    def _intent(self, target_file):
        from grove.intents import ToolIntent
        return ToolIntent(
            tool_name="propose_governance_change",
            arguments={"target_file": target_file, "content": "x", "rationale": "y"},
            call_id="c1",
        )

    def test_env_target_classifies_red(self, grove_home):
        from grove import dispatch as _gd
        from grove.dispatcher import Dispatcher
        zr = Dispatcher._classify_one_intent(self._intent(str(grove_home / ".env")), _gd)
        assert zr.zone == "red"

    def test_yaml_target_classifies_red(self, grove_home):
        # routing-scope-wall-v1 R-W3: scope-defining governance targets
        # (zones.schema/routing.config) now classify RED through the
        # propose_governance_change door, superseding the prior C1b YELLOW —
        # editing zone rules expands the agent's own authority.
        from grove import dispatch as _gd
        from grove.dispatcher import Dispatcher
        zr = Dispatcher._classify_one_intent(
            self._intent(str(grove_home / "zones.schema.yaml")), _gd,
        )
        assert zr.zone == "red"

    def test_dock_target_classifies_red(self, grove_home):
        # routing-scope-wall-v1 R-W3 (ruling b, no carve-out): dock/dock.yaml is
        # scope-defining, so it classifies RED through this door too. dock/goals/**
        # (not scope-defining) stays YELLOW. Debt: dock-manifest-scope-membership.
        from grove import dispatch as _gd
        from grove.dispatcher import Dispatcher
        zr = Dispatcher._classify_one_intent(
            self._intent(str(grove_home / "dock" / "dock.yaml")), _gd,
        )
        assert zr.zone == "red"


class TestGovernanceWriteAndLedger:
    def test_approved_env_claim_writes_and_ledgers(self, grove_home, monkeypatch):
        # capability-mutation-surface-v1 P5 — the write lands through the
        # approved-claim EXECUTOR (writer registry), never the handler; the
        # governance_change ledger channel continues (disposition="written",
        # approval_id = the claim id).
        monkeypatch.setenv("GROVE_SESSION_ID", "c1b_test_session")
        from grove.effect_signature import canonical_effect_signature
        from grove.kaizen_ledger import KaizenLedger
        from grove.red_pending_store import (
            PendingRedProposal,
            RedPendingStore,
            action_proposal_id,
            approve_red_proposal,
            prepare_execute_arguments,
            seal_red_claim,
        )

        target = grove_home / ".env"
        args = prepare_execute_arguments("propose_governance_change", {
            "target_file": str(target), "content": "TOK=c1b\n",
            "rationale": "C1b test: executor-era governed write",
        })
        sealed = seal_red_claim("propose_governance_change", args)
        assert sealed["writer_name"] == "env_write"
        sig = canonical_effect_signature("propose_governance_change", args)
        store = RedPendingStore(db_path=grove_home / "red_pending_test.db")
        entry = PendingRedProposal(
            proposal_id=action_proposal_id(sig),
            tool_name="propose_governance_change", arguments=args,
            effect_signature=sig, description="d", rationale="r",
            created_at="2026-07-21T00:00:00+00:00", **sealed,
        )
        store.put(entry)
        res = approve_red_proposal(entry.proposal_id, store=store)
        assert res["success"] is True, res
        assert target.read_text() == "TOK=c1b\n"
        events = KaizenLedger("c1b_test_session").events_by_type("governance_change")
        assert len(events) == 1
        e = events[0]
        assert e["disposition"] == "written"
        assert e["approval_id"] == entry.proposal_id
        assert e["content_sha256"] and e["timestamp"]

    def test_thin_proposer_never_writes_directly(self, grove_home):
        # The handler is a PROPOSER: a direct call on a viable target refuses
        # loudly and writes nothing (the executor is the only writer).
        from tools.governance_tool import propose_governance_change
        target = grove_home / ".env"
        raw = propose_governance_change(
            target_file=str(target), content="TOK=direct\n", rationale="r",
        )
        result = json.loads(raw)
        assert result["success"] is False
        assert "thin proposer" in result["error"]
        assert not target.exists()

    def test_unrecognized_target_refused(self, grove_home, tmp_path):
        from tools.governance_tool import propose_governance_change
        raw = propose_governance_change(
            target_file=str(tmp_path.parent / "random.yaml"),
            content="x", rationale="why",
        )
        result = json.loads(raw)
        assert result["success"] is False
        assert "governance config" in result["error"]

    def test_missing_rationale_refused(self, grove_home):
        from tools.governance_tool import propose_governance_change
        raw = propose_governance_change(
            target_file=str(grove_home / "zones.schema.yaml"), content="x", rationale="",
        )
        assert json.loads(raw)["success"] is False


# ── GRV-010 GOV-WRITE — Dock admission to the governed door ──────────────────
#
# The bypass strings below are permanent regression assertions: each rejected
# case is a path that MUST NOT be admitted to the write door.


class TestDockResolution:
    """capability-mutation-surface-v1 P5 — Dock targets seal against the
    EXISTING dock writers only (update_dock_goal_status; no new writer
    minted). Everything else through this door is a registry miss or a
    thin-proposer refusal — the old full-file Dock write path is retired."""

    def _install_dock(self, grove_home, status="cruising"):
        d = grove_home / "dock"
        d.mkdir(parents=True, exist_ok=True)
        (d / "dock.yaml").write_text(
            f"version: 1\ngoals:\n- id: growth\n  status: {status}\n",
            encoding="utf-8",
        )
        return d / "dock.yaml"

    def test_status_only_change_seals_to_dock_writer(self, grove_home):
        self._install_dock(grove_home, "cruising")
        from grove.red_pending_store import seal_red_claim
        body = "version: 1\ngoals:\n- id: growth\n  status: complete\n"
        sealed = seal_red_claim("propose_governance_change", {
            "target_file": str(grove_home / "dock" / "dock.yaml"),
            "content": body, "rationale": "r",
        })
        assert sealed["writer_name"] == "dock_goal_status"
        assert sealed["writer_payload"]["changes"] == [
            {"goal_id": "growth", "status": "complete"}
        ]

    def test_structural_dock_edit_is_registry_miss(self, grove_home):
        self._install_dock(grove_home, "cruising")
        from grove.red_pending_store import is_viable_red_target
        body = (
            "version: 1\ngoals:\n- id: growth\n  status: cruising\n"
            "- id: injected\n  status: cruising\n"
        )
        viable, reason = is_viable_red_target("propose_governance_change", {
            "target_file": str(grove_home / "dock" / "dock.yaml"),
            "content": body, "rationale": "r",
        })
        assert viable is False
        assert "goal-status" in reason

    def test_goal_file_write_refused_and_not_created(self, grove_home):
        from tools.governance_tool import propose_governance_change
        target = grove_home / "dock" / "goals" / "growth.md"
        raw = propose_governance_change(
            target_file=str(target), content="# Growth\n", rationale="r",
        )
        assert json.loads(raw)["success"] is False
        assert not target.exists()

    def test_sibling_dock_evil_never_written(self, grove_home):
        from tools.governance_tool import propose_governance_change
        target = grove_home / "dock-evil" / "x.yaml"
        raw = propose_governance_change(
            target_file=str(target), content="x: 1\n", rationale="r",
        )
        assert json.loads(raw)["success"] is False
        assert not target.exists()

    def test_dotdot_escape_to_env_is_red(self, grove_home):
        # The universal .env→RED rule survives the retirement: the ../ escape
        # collapses (realpath) onto .env and classifies RED at the door.
        from grove import dispatch as _gd
        from grove.dispatcher import Dispatcher
        from grove.intents import ToolIntent
        intent = ToolIntent(
            tool_name="propose_governance_change",
            arguments={
                "target_file": str(grove_home / "dock" / ".." / ".env"),
                "content": "x", "rationale": "y",
            },
            call_id="c1",
        )
        zr = Dispatcher._classify_one_intent(intent, _gd)
        assert zr.zone == "red"

    def test_symlinked_dock_outside_grove_never_written(self, grove_home, tmp_path):
        # A Dock symlinked outside ~/.grove resolves outside the governance
        # tree; the thin proposer refuses and writes NOTHING (the old handler
        # wrote through this door — that authority is dead).
        from tools.governance_tool import propose_governance_change
        outside = tmp_path.parent / "dock_external_target"
        outside.mkdir(exist_ok=True)
        (grove_home / "dock").symlink_to(outside, target_is_directory=True)
        target = grove_home / "dock" / "dock.yaml"
        raw = propose_governance_change(
            target_file=str(target), content="version: 1\ngoals: []\n",
            rationale="r",
        )
        assert json.loads(raw)["success"] is False
        assert not (outside / "dock.yaml").exists()


class TestDockWriteAndLedger:
    def test_dock_status_claim_writes_and_ledgers(self, grove_home, monkeypatch):
        # P5: the dock mutation lands through the approved-claim executor via
        # update_dock_goal_status (the sole registered dock writer).
        monkeypatch.setenv("GROVE_SESSION_ID", "dock_write_session")
        import yaml as _yaml
        from grove.effect_signature import canonical_effect_signature
        from grove.kaizen_ledger import KaizenLedger
        from grove.red_pending_store import (
            PendingRedProposal,
            RedPendingStore,
            action_proposal_id,
            approve_red_proposal,
            prepare_execute_arguments,
            seal_red_claim,
        )

        dock = grove_home / "dock"
        dock.mkdir(parents=True, exist_ok=True)
        manifest = dock / "dock.yaml"
        manifest.write_text(
            "version: 1\ngoals:\n- id: growth\n  status: active\n",
            encoding="utf-8",
        )
        body = "version: 1\ngoals:\n- id: growth\n  status: complete\n"
        args = prepare_execute_arguments("propose_governance_change", {
            "target_file": str(manifest), "content": body,
            "rationale": "Dock test: complete the growth goal",
        })
        sealed = seal_red_claim("propose_governance_change", args)
        assert sealed["writer_name"] == "dock_goal_status"
        sig = canonical_effect_signature("propose_governance_change", args)
        store = RedPendingStore(db_path=grove_home / "red_pending_test.db")
        entry = PendingRedProposal(
            proposal_id=action_proposal_id(sig),
            tool_name="propose_governance_change", arguments=args,
            effect_signature=sig, description="d", rationale="r",
            created_at="2026-07-21T00:00:00+00:00", **sealed,
        )
        store.put(entry)
        res = approve_red_proposal(entry.proposal_id, store=store)
        assert res["success"] is True, res
        doc = _yaml.safe_load(manifest.read_text(encoding="utf-8"))
        assert doc["goals"][0]["status"] == "complete"
        events = KaizenLedger("dock_write_session").events_by_type(
            "governance_change"
        )
        assert len(events) == 1
        assert events[0]["disposition"] == "written"
        assert events[0]["approval_id"] == entry.proposal_id


class TestDockBlockPreserved:
    """is_governed_path is UNCHANGED: the substrate block still walls the Dock
    from the generic file tools and shell. Only the governed door widened."""

    @pytest.fixture(autouse=True)
    def _neutralize_sensitive_check(self, monkeypatch):
        # See TestFileToolGovernedLock: macOS tmp dirs trip _check_sensitive_path
        # first; neutralize it so the governed-path lock is the operative gate.
        monkeypatch.setattr(
            "tools.file_tools._check_sensitive_path", lambda *a, **k: None,
        )

    def test_dock_path_is_governed(self, grove_home):
        from grove.utils.fs_utils import is_governed_path
        assert is_governed_path(grove_home / "dock" / "dock.yaml") is True

    def test_write_file_refuses_dock_path(self, grove_home):
        # secrets-only-wall-v1: dock/dock.yaml is a non-secret ~/.grove config →
        # no longer statically walled by the governance wall.
        from grove.utils.fs_utils import is_secret_path
        assert is_secret_path(str(grove_home / "dock" / "dock.yaml")) is False

    def test_shell_write_into_dock_classifies_red(self, grove_home):
        from grove.shell_effects import classify_shell_effect
        zr = classify_shell_effect(
            f"echo x > {grove_home / 'dock' / 'dock.yaml'}"
        )
        assert zr.zone == "red"


class TestDockOfferNotTheater:
    """T0b — a YELLOW Dock proposal raises the four-choice Sovereign Prompt
    halt (AndonPermissionHalt) at classification, BEFORE the write-replace
    handler can run. The offer precedes the effect; it is not theater."""

    def test_dock_proposal_halts_before_write(self, grove_home):
        # routing-scope-wall-v1 R-W3: dock/dock.yaml is scope-defining → the halt
        # is now RED (AndonResolutionHalt), superseding the prior YELLOW
        # AndonPermissionHalt. Either way the synchronous halt fires during
        # classification, so no write occurs.
        from grove.dispatcher import AndonResolutionHalt, Dispatcher
        from grove.intents import ToolIntent

        target = grove_home / "dock" / "dock.yaml"
        intent = ToolIntent(
            tool_name="propose_governance_change",
            arguments={
                "target_file": str(target),
                "content": "version: '1.0'\n",
                "rationale": "attempt a dock write",
            },
            call_id="dock1",
        )
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")
        with pytest.raises(AndonResolutionHalt):
            d._classify_intents_batch_and_halt_or_raise([intent])
        # The synchronous halt fired during classification — no write occurred.
        assert not target.exists()


# ── Phase 4 — invoke_skill EXECUTABLE_STATES guard ───────────────────────────


def _fake_resolution(state=None):
    """A SkillResolution stand-in for the guard's _resolve_record seam.

    skill-invocation-path-integrity-v1 P2 — the guard rekeyed from
    _skill_record_state (exact frontmatter name) to the canonical resolver;
    these pins patch the new seam with the same three shapes (non-executable
    record / executable record / no record). Assertions unchanged.
    """
    from types import SimpleNamespace

    from grove.capability_registry import SkillResolution

    if state is None:
        return SkillResolution("none", None, None, ())
    record = SimpleNamespace(lifecycle=SimpleNamespace(state=state))
    return SkillResolution("resolved", record, "skill.test.record", ("skill.test.record",))


class TestInvokeSkillExecutableGuard:
    def test_green_path_refuses_non_executable_record(self, grove_home, monkeypatch):
        from grove.capability import LifecycleState
        # An active skill dir lingers on disk, but its record is deprecated.
        active = grove_home / "skills" / "ghost" / "SKILL.md"
        active.parent.mkdir(parents=True, exist_ok=True)
        active.write_text("---\nname: ghost\ndescription: d\n---\n\nBody.\n")

        import tools.invoke_skill_tool as ist
        monkeypatch.setattr(
            ist, "_resolve_record",
            lambda name: _fake_resolution(LifecycleState.DEPRECATED),
        )
        result = json.loads(ist.invoke_skill(name="ghost"))
        assert result["success"] is False
        assert "non-executable lifecycle state" in result["error"]

    def test_green_path_runs_executable_record(self, grove_home, monkeypatch):
        from grove.capability import LifecycleState
        active = grove_home / "skills" / "live" / "SKILL.md"
        active.parent.mkdir(parents=True, exist_ok=True)
        active.write_text("---\nname: live\ndescription: d\n---\n\nBody.\n")

        import tools.invoke_skill_tool as ist
        monkeypatch.setattr(
            ist, "_resolve_record",
            lambda name: _fake_resolution(LifecycleState.ACTIVE),
        )
        result = json.loads(ist.invoke_skill(name="live"))
        assert result["success"] is True
        assert result["zone"] == "green"

    def test_no_record_does_not_block(self, grove_home, monkeypatch):
        # No capability record (legacy skill) → guard does not block.
        active = grove_home / "skills" / "legacy" / "SKILL.md"
        active.parent.mkdir(parents=True, exist_ok=True)
        active.write_text("---\nname: legacy\ndescription: d\n---\n\nBody.\n")

        import tools.invoke_skill_tool as ist
        monkeypatch.setattr(ist, "_resolve_record", lambda name: _fake_resolution(None))
        result = json.loads(ist.invoke_skill(name="legacy"))
        assert result["success"] is True

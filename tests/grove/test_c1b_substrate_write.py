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


class TestGovernanceTargetClassification:
    def test_env_is_red(self, grove_home):
        from tools.governance_tool import classify_governance_target
        assert classify_governance_target(str(grove_home / ".env")) == "red"

    def test_yaml_config_is_yellow(self, grove_home):
        from tools.governance_tool import classify_governance_target
        assert classify_governance_target(str(grove_home / "zones.schema.yaml")) == "yellow"
        assert classify_governance_target(str(grove_home / "routing.config.yaml")) == "yellow"

    def test_non_governance_target_is_none(self, grove_home, tmp_path):
        from tools.governance_tool import classify_governance_target
        assert classify_governance_target(str(tmp_path.parent / "scratch.yaml")) is None


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

    def test_yaml_target_classifies_yellow(self, grove_home):
        from grove import dispatch as _gd
        from grove.dispatcher import Dispatcher
        zr = Dispatcher._classify_one_intent(
            self._intent(str(grove_home / "zones.schema.yaml")), _gd,
        )
        assert zr.zone == "yellow"

    def test_dock_target_classifies_yellow(self, grove_home):
        from grove import dispatch as _gd
        from grove.dispatcher import Dispatcher
        zr = Dispatcher._classify_one_intent(
            self._intent(str(grove_home / "dock" / "dock.yaml")), _gd,
        )
        assert zr.zone == "yellow"


class TestGovernanceWriteAndLedger:
    def test_approved_write_persists_change_and_ledger(self, grove_home, monkeypatch):
        monkeypatch.setenv("GROVE_SESSION_ID", "c1b_test_session")
        from tools.governance_tool import propose_governance_change
        from grove.kaizen_ledger import KaizenLedger

        target = grove_home / "zones.schema.yaml"
        raw = propose_governance_change(
            target_file=str(target),
            content="schema_version: 1\n",
            rationale="C1b test: tighten the terminal default zone",
        )
        result = json.loads(raw)
        assert result["success"] is True
        assert result["zone"] == "yellow"
        # The change was written through the door (not a generic file tool).
        assert target.read_text() == "schema_version: 1\n"
        # And a provenance ledger entry was recorded.
        ledger = KaizenLedger("c1b_test_session")
        events = ledger.events_by_type("governance_change")
        assert len(events) == 1
        entry = events[0]
        assert entry["rationale"].startswith("C1b test")
        assert entry["disposition"] == "approved"
        assert entry["zone"] == "yellow"
        assert entry["content_sha256"]
        assert entry["timestamp"]

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


class TestDockAdmission:
    def test_dock_yaml_is_yellow(self, grove_home):
        from tools.governance_tool import classify_governance_target
        assert classify_governance_target(
            str(grove_home / "dock" / "dock.yaml")
        ) == "yellow"

    def test_nested_dock_goal_md_is_yellow(self, grove_home):
        from tools.governance_tool import classify_governance_target
        assert classify_governance_target(
            str(grove_home / "dock" / "goals" / "q3" / "interview.md")
        ) == "yellow"

    def test_nested_dock_goal_yaml_is_yellow(self, grove_home):
        # .yaml is a loadable Dock suffix (manifest + goal context sources).
        from tools.governance_tool import classify_governance_target
        assert classify_governance_target(
            str(grove_home / "dock" / "goals" / "q3" / "context.yaml")
        ) == "yellow"

    # ── OVER-ADMISSION REJECTED ──────────────────────────────────────────────

    def test_sibling_dock_evil_is_rejected(self, grove_home):
        # is_relative_to anchors to the dock tree; a sibling whose name merely
        # starts with "dock" is NOT contained. (str.startswith would mis-admit.)
        from tools.governance_tool import classify_governance_target
        assert classify_governance_target(
            str(grove_home / "dock-evil" / "x.yaml")
        ) is None

    def test_non_loadable_suffix_under_dock_is_rejected(self, grove_home):
        from tools.governance_tool import classify_governance_target
        assert classify_governance_target(
            str(grove_home / "dock" / "notes.txt")
        ) is None

    # ── TRAVERSAL / SYMLINK REJECTED ─────────────────────────────────────────

    def test_dotdot_escape_to_governance_yaml_stays_yellow_via_name(self, grove_home):
        # ~/.grove/dock/../zones.schema.yaml collapses (realpath) to
        # ~/.grove/zones.schema.yaml → YELLOW via the NAME check, NOT the Dock
        # rule (the collapse removed dock containment).
        from tools.governance_tool import classify_governance_target
        assert classify_governance_target(
            str(grove_home / "dock" / ".." / "zones.schema.yaml")
        ) == "yellow"

    def test_dotdot_escape_to_non_governance_yaml_is_rejected(self, grove_home):
        # Proves the prior YELLOW came from the name rule, not dock containment:
        # the same ../ escape to a non-governance yaml is NOT admitted.
        from tools.governance_tool import classify_governance_target
        assert classify_governance_target(
            str(grove_home / "dock" / ".." / "random.yaml")
        ) is None

    def test_dotdot_escape_to_env_is_red(self, grove_home):
        from tools.governance_tool import classify_governance_target
        assert classify_governance_target(
            str(grove_home / "dock" / ".." / ".env")
        ) == "red"

    def test_symlinked_dock_outside_grove_not_admitted(self, grove_home, tmp_path):
        # A Dock whose root symlinks OUTSIDE ~/.grove resolves outside the
        # governance tree → rejected by the inside_grove gate before the Dock
        # rule is ever consulted.
        from tools.governance_tool import classify_governance_target
        outside = tmp_path.parent / "dock_external_target"
        outside.mkdir(exist_ok=True)
        (grove_home / "dock").symlink_to(outside, target_is_directory=True)
        assert classify_governance_target(
            str(grove_home / "dock" / "dock.yaml")
        ) is None

    # ── WATERFALL ORDERING ───────────────────────────────────────────────────

    def test_env_under_dock_classifies_red_not_yellow(self, grove_home):
        # The strict .env→RED check fires first; a .env that sits under dock/
        # keeps RED, never the Dock YELLOW.
        from tools.governance_tool import classify_governance_target
        assert classify_governance_target(
            str(grove_home / "dock" / ".env")
        ) == "red"


class TestDockWriteAndLedger:
    def test_dock_write_persists_change_and_ledger(self, grove_home, monkeypatch):
        monkeypatch.setenv("GROVE_SESSION_ID", "dock_write_session")
        from tools.governance_tool import propose_governance_change
        from grove.kaizen_ledger import KaizenLedger

        target = grove_home / "dock" / "goals" / "growth.md"
        raw = propose_governance_change(
            target_file=str(target),
            content="# Growth goal\n\nShip the thing.\n",
            rationale="Dock test: record the growth goal",
        )
        result = json.loads(raw)
        assert result["success"] is True
        assert result["zone"] == "yellow"
        # Write-replace landed through the door (parent dirs created).
        assert target.read_text() == "# Growth goal\n\nShip the thing.\n"
        # Provenance preserved: the governance_change ledger entry was appended.
        ledger = KaizenLedger("dock_write_session")
        events = ledger.events_by_type("governance_change")
        assert len(events) == 1
        assert events[0]["zone"] == "yellow"
        assert events[0]["rationale"].startswith("Dock test")
        assert events[0]["content_sha256"]


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
        from grove.dispatcher import AndonPermissionHalt, Dispatcher
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
        with pytest.raises(AndonPermissionHalt):
            d._classify_intents_batch_and_halt_or_raise([intent])
        # The synchronous halt fired during classification — no write occurred.
        assert not target.exists()


# ── Phase 4 — invoke_skill EXECUTABLE_STATES guard ───────────────────────────


class TestInvokeSkillExecutableGuard:
    def test_green_path_refuses_non_executable_record(self, grove_home, monkeypatch):
        from grove.capability import LifecycleState
        # An active skill dir lingers on disk, but its record is deprecated.
        active = grove_home / "skills" / "ghost" / "SKILL.md"
        active.parent.mkdir(parents=True, exist_ok=True)
        active.write_text("---\nname: ghost\ndescription: d\n---\n\nBody.\n")

        import tools.invoke_skill_tool as ist
        monkeypatch.setattr(
            ist, "_skill_record_state", lambda name: LifecycleState.DEPRECATED,
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
            ist, "_skill_record_state", lambda name: LifecycleState.ACTIVE,
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
        monkeypatch.setattr(ist, "_skill_record_state", lambda name: None)
        result = json.loads(ist.invoke_skill(name="legacy"))
        assert result["success"] is True

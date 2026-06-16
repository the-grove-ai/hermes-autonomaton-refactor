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
        from tools.file_tools import write_file_tool
        raw = write_file_tool(str(grove_home / "zones.schema.yaml"), "schema_version: 1\n")
        result = json.loads(raw)
        assert result.get("error")
        assert "Governed path" in result["error"]

    def test_patch_refuses_governed_path(self, grove_home):
        from tools.file_tools import patch_tool
        raw = patch_tool(
            mode="replace", path=str(grove_home / "routing.config.yaml"),
            old_string="a", new_string="b",
        )
        result = json.loads(raw)
        assert result.get("error")
        assert "Governed path" in result["error"]

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

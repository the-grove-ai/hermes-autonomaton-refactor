"""GRV-010 C3b — conformance-closure-substrate-altitude.

The C3 adversarial gate found the C1b substrate lock enforced at the wrong
altitude: ``is_governed_path`` was checked only at the file_tools entry-regex
(Update/Add/Delete) and at write_file/patch-replace — never at the agent
file-op layer. A V4A ``Move`` verb reached ``ShellFileOperations.move_file``
(guarded only by the legacy ~/.ssh/.env denylist) and could rewrite
``~/.grove/routing.config.yaml``. ``read_file`` was likewise unguarded for
``~/.grove/.env``.

C3b closes the altitude: every agent-initiated file op resolves through a
realpath-resolved governed-path wall BEFORE the raw primitive — at the
ShellFileOperations chokepoint (write/move/delete), the read_file tool, and
the Copilot ACP shim. The ``.andon`` authoring quarantine stays allowlisted;
internal loaders and the sanctioned governance/skill doors (raw Python) are
untouched.

These are the C3 re-trace targets — they must pass at HEAD+fix.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from tools.file_operations import ShellFileOperations, WriteResult


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def grove(tmp_path, monkeypatch):
    """Redirect GROVE_HOME to a tmp ``grove`` subtree so the governance
    boundary is testable without touching the operator's real ~/.grove.
    ``tmp_path`` itself is the non-governed workspace around it."""
    g = tmp_path / "grove"
    g.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(g))
    return g


@pytest.fixture
def mock_env():
    """A terminal env whose shell calls are no-ops (returncode 0). The
    chokepoint guard raises BEFORE any exec, so refusal tests never need a
    real shell."""
    env = MagicMock()
    env.cwd = "/tmp"
    env.execute.return_value = {"output": "", "returncode": 0}
    return env


@pytest.fixture
def mock_ops(mock_env):
    return ShellFileOperations(mock_env)


def _local_env():
    """A terminal env that actually execs locally — so read_file_raw reflects
    real disk state (needed for the two-phase V4A validate→apply path)."""
    env = MagicMock()
    env.cwd = "/"

    def execute(command, **kwargs):
        completed = subprocess.run(
            command, shell=True, text=True, capture_output=True
        )
        return {"output": completed.stdout, "returncode": completed.returncode}

    env.execute = execute
    return env


# ── Helper unit tests — agent FS policy lives in agent/file_safety.py ────────


class TestGovernedAgentGuards:
    def test_write_guard_raises_on_governed_config(self, grove):
        # secrets-only-wall-v1: routing.config.yaml is a non-secret ~/.grove
        # config → no longer statically walled (the write guard now keys on
        # is_secret_path only).
        from grove.utils.fs_utils import is_secret_path
        assert is_secret_path(str(grove / "routing.config.yaml")) is False

    def test_write_guard_raises_on_env_secret(self, grove):
        from agent.file_safety import reject_governed_agent_write
        with pytest.raises(PermissionError):
            reject_governed_agent_write(str(grove / ".env"))

    def test_write_guard_allows_andon(self, grove):
        from agent.file_safety import reject_governed_agent_write
        # No raise = allowed.
        reject_governed_agent_write(str(grove / "skills" / ".andon" / "draft" / "SKILL.md"))

    def test_write_guard_allows_outside(self, grove, tmp_path):
        from agent.file_safety import reject_governed_agent_write
        reject_governed_agent_write(str(tmp_path / "scratch.txt"))

    def test_write_guard_collapses_dotdot_traversal(self, grove):
        # secrets-only-wall-v1: the ../-collapsed target is routing.config.yaml,
        # a non-secret config → not secret-walled. (realpath still collapses the
        # traversal; the resolved path is simply no longer blocked.)
        from grove.utils.fs_utils import is_secret_path
        escape = grove / "sub" / ".." / "routing.config.yaml"
        assert is_secret_path(str(escape)) is False

    def test_read_guard_blocks_governed_then_allows_andon(self, grove, tmp_path):
        from agent.file_safety import reject_governed_agent_read
        # secrets-only-wall-v1: .env is a SECRET → still read-blocked; but
        # routing.config.yaml is non-secret → now readable (was blocked).
        assert reject_governed_agent_read(str(grove / ".env")) is not None
        assert reject_governed_agent_read(str(grove / "routing.config.yaml")) is None
        assert reject_governed_agent_read(
            str(grove / "skills" / ".andon" / "draft" / "SKILL.md")
        ) is None
        assert reject_governed_agent_read(str(tmp_path / "notes.md")) is None


# ── Chokepoint — ShellFileOperations.{write,move,delete}_file ────────────────


class TestChokepointWriteMoveDelete:
    def test_move_dst_governed_refused(self, mock_ops, grove, tmp_path):
        # secrets-only-wall-v1: routing.config.yaml is a non-secret ~/.grove
        # config → moving into it is no longer secret-walled.
        from grove.utils.fs_utils import is_secret_path
        assert is_secret_path(str(grove / "routing.config.yaml")) is False

    def test_move_src_governed_refused(self, mock_ops, grove, tmp_path):
        # secrets-only-wall-v1: routing.config.yaml is non-secret → moving it out
        # is no longer secret-walled.
        from grove.utils.fs_utils import is_secret_path
        assert is_secret_path(str(grove / "routing.config.yaml")) is False

    def test_move_into_symlinked_grove_refused(self, mock_ops, grove, tmp_path):
        # secrets-only-wall-v1: /tmp/safe -> ~/.grove ; the symlinked dst realpath-
        # resolves to a non-secret config → not secret-walled. (Symlink intent
        # preserved: is_secret_path is called on the symlinked path.)
        from grove.utils.fs_utils import is_secret_path
        link = tmp_path / "safe"
        link.symlink_to(grove)
        assert is_secret_path(str(link / "routing.config.yaml")) is False

    def test_write_governed_refused(self, mock_ops, grove):
        # secrets-only-wall-v1: zones.schema.yaml is non-secret → not secret-walled.
        from grove.utils.fs_utils import is_secret_path
        assert is_secret_path(str(grove / "zones.schema.yaml")) is False

    def test_write_governed_via_dotdot_refused(self, mock_ops, grove):
        with pytest.raises(PermissionError):
            mock_ops.write_file(str(grove / "x" / ".." / ".env"), "EVIL=1\n")

    def test_delete_governed_refused(self, mock_ops, grove):
        # secrets-only-wall-v1: routing.config.yaml is non-secret → not secret-walled.
        from grove.utils.fs_utils import is_secret_path
        assert is_secret_path(str(grove / "routing.config.yaml")) is False

    def test_write_outside_grove_allowed(self, mock_ops, tmp_path):
        # Non-governed path reaches the method body (no governed raise).
        res = mock_ops.write_file(str(tmp_path / "ok.txt"), "data")
        assert isinstance(res, WriteResult)

    def test_move_outside_grove_allowed(self, mock_ops, tmp_path):
        res = mock_ops.move_file(str(tmp_path / "a.txt"), str(tmp_path / "b.txt"))
        assert isinstance(res, WriteResult)

    def test_write_andon_allowed(self, mock_ops, grove):
        res = mock_ops.write_file(
            str(grove / "skills" / ".andon" / "draft" / "SKILL.md"), "---\nx\n"
        )
        assert isinstance(res, WriteResult)


# ── End-to-end V4A Move through patch_v4a → _apply_move → chokepoint ─────────


class TestV4AMoveExploitRefused:
    def test_move_patch_into_grove_refused_and_dst_not_created(self, grove, tmp_path):
        # secrets-only-wall-v1: routing.config.yaml is a non-secret ~/.grove
        # config → moving a payload into it is no longer secret-walled. (The
        # V4A move destination is what the governance wall keys on.)
        from grove.utils.fs_utils import is_secret_path
        dst = grove / "routing.config.yaml"
        assert is_secret_path(str(dst)) is False


# ── read_file tool — governed-tree read block ────────────────────────────────


class TestReadFileToolGovernedBlock:
    def test_read_env_refused(self, grove):
        # secrets-only-wall-v1: .env is a SECRET → still read-blocked.
        (grove / ".env").write_text("SECRET=1\n")
        from tools.file_tools import read_file_tool
        raw = read_file_tool(str(grove / ".env"))
        result = json.loads(raw)
        assert result.get("error")
        assert "is protected" in result["error"]

    def test_read_routing_config_refused(self, grove):
        # secrets-only-wall-v1: routing.config.yaml is non-secret → now readable;
        # the governance wall no longer blocks it.
        from grove.utils.fs_utils import is_secret_path
        assert is_secret_path(str(grove / "routing.config.yaml")) is False


# ── Copilot ACP shim — write/read surfaces ───────────────────────────────────


class TestACPGovernedSurface:
    def _client(self, cwd):
        from agent.copilot_acp_client import CopilotACPClient
        return CopilotACPClient(acp_cwd=str(cwd))

    def _fake_process(self):
        proc = MagicMock()
        proc.stdin = MagicMock()
        return proc

    def _written_response(self, proc):
        # The handler writes one JSON-RPC line to stdin.
        payload = proc.stdin.write.call_args[0][0]
        return json.loads(payload)

    def test_acp_write_into_grove_refused(self, grove, tmp_path):
        # secrets-only-wall-v1: routing.config.yaml is a non-secret ~/.grove
        # config → the ACP write surface no longer secret-walls it.
        from grove.utils.fs_utils import is_secret_path
        target = grove / "routing.config.yaml"
        assert is_secret_path(str(target)) is False

    def test_acp_write_outside_grove_succeeds(self, grove, tmp_path):
        client = self._client(tmp_path)
        proc = self._fake_process()
        target = tmp_path / "work.txt"
        msg = {
            "method": "fs/write_text_file", "id": 8,
            "params": {"path": str(target), "content": "hello\n"},
        }
        client._handle_server_message(
            msg, process=proc, cwd=str(tmp_path),
            text_parts=None, reasoning_parts=None,
        )
        resp = self._written_response(proc)
        assert "error" not in resp
        assert target.read_text() == "hello\n"

    def test_acp_read_env_refused(self, grove, tmp_path):
        (grove / ".env").write_text("SECRET=1\n")
        client = self._client(tmp_path)
        proc = self._fake_process()
        msg = {
            "method": "fs/read_text_file", "id": 9,
            "params": {"path": str(grove / ".env")},
        }
        client._handle_server_message(
            msg, process=proc, cwd=str(tmp_path),
            text_parts=None, reasoning_parts=None,
        )
        resp = self._written_response(proc)
        assert "error" in resp

    def test_acp_read_outside_grove_succeeds(self, grove, tmp_path):
        f = tmp_path / "doc.txt"
        f.write_text("body\n")
        client = self._client(tmp_path)
        proc = self._fake_process()
        msg = {
            "method": "fs/read_text_file", "id": 10,
            "params": {"path": str(f)},
        }
        client._handle_server_message(
            msg, process=proc, cwd=str(tmp_path),
            text_parts=None, reasoning_parts=None,
        )
        resp = self._written_response(proc)
        assert "error" not in resp
        assert resp["result"]["content"] == "body\n"


# ── Regression — sanctioned doors (raw Python) unaffected by the chokepoint ──


class TestSanctionedDoorsUnaffected:
    def test_governance_door_still_resolves(self, grove, monkeypatch):
        # capability-mutation-surface-v1 P5 — the door no longer writes
        # directly (thin proposer); "unaffected by the chokepoint" now means
        # the SEAL path still resolves the governed target to its registered
        # writer, and the direct handler call refuses without writing.
        monkeypatch.setenv("GROVE_SESSION_ID", "c3b_test_session")
        from grove.red_pending_store import seal_red_claim
        from tools.governance_tool import propose_governance_change

        target = grove / "routing.config.yaml"
        sealed = seal_red_claim("propose_governance_change", {
            "target_file": str(target),
            "content": "zones:\n  terminal: green\n",
            "rationale": "C3b regression: governance door bypasses the chokepoint",
        })
        assert sealed["writer_name"] == "routing_config_replace"
        raw = propose_governance_change(
            target_file=str(target),
            content="zones:\n  terminal: green\n",
            rationale="C3b regression: governance door bypasses the chokepoint",
        )
        result = json.loads(raw)
        assert result["success"] is False        # proposer, not writer
        assert not target.exists()               # nothing written directly

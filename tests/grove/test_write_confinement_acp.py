"""write-confinement-v1 Phase 4 — ACP surface parity.

The ACP write shim drops its bespoke ``_ensure_path_within_cwd`` confinement for
the single ``is_write_allowed(path, session_cwd=cwd)`` evaluator. ACP thereby
gains ~/.grove-non-secret + declared-workspace + tmp writes (it only had cwd
before); the live IDE session cwd remains honored as source (d). Reads are
unchanged.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def grove(tmp_path, monkeypatch):
    g = tmp_path / "grove"
    g.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(g))
    return g


def _client(cwd):
    from agent.copilot_acp_client import CopilotACPClient

    return CopilotACPClient(acp_cwd=str(cwd))


def _fake_process():
    proc = MagicMock()
    proc.stdin = MagicMock()
    return proc


def _written_response(proc):
    payload = proc.stdin.write.call_args[0][0]
    return json.loads(payload)


def _write(client, proc, cwd, target, content="hello\n"):
    msg = {
        "method": "fs/write_text_file",
        "id": 1,
        "params": {"path": str(target), "content": content},
    }
    client._handle_server_message(
        msg, process=proc, cwd=str(cwd), text_parts=None, reasoning_parts=None
    )
    return _written_response(proc)


def test_acp_write_grove_outside_cwd_now_succeeds(grove, tmp_path):
    """The Phase 4 benefit: a non-secret ~/.grove write OUTSIDE the session cwd
    was refused by the old cwd-confinement; is_write_allowed (source a) permits
    it."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    target = grove / "research" / "x.md"  # under grove, NOT under cwd
    resp = _write(_client(cwd), _fake_process(), cwd, target)
    assert "error" not in resp
    assert target.read_text() == "hello\n"


def test_acp_write_within_cwd_still_succeeds(grove, tmp_path):
    """Source (d) preserved: a write under the live session cwd is allowed."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    target = cwd / "sub" / "file.txt"
    resp = _write(_client(cwd), _fake_process(), cwd, target)
    assert "error" not in resp
    assert target.read_text() == "hello\n"


def test_acp_write_outside_union_refused(grove, tmp_path):
    """Outside cwd AND outside the allow-list → hard reject."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    resp = _write(
        _client(cwd), _fake_process(), cwd, "/home/nonexistent-acp-xyz/evil.txt"
    )
    assert "error" in resp


def test_acp_write_secret_in_cwd_refused(grove, tmp_path):
    """A secret is refused WHEREVER it lives, even inside the session cwd."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    resp = _write(_client(cwd), _fake_process(), cwd, cwd / ".env", content="K=v\n")
    assert "error" in resp
    assert not (cwd / ".env").exists()

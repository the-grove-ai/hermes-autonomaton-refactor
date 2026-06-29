"""write-confinement-v1 Phase 2 — pre-classification write-confinement gate.

The dispatcher consults ``is_write_allowed`` for every write-family intent
(``write_file`` / ``patch``) BEFORE classification. An out-of-workspace write is
hard-rejected with the remediation message and never classified, prompted, or
executed. Delete/Move are the ``patch`` V4A verbs, so a Move's BOTH endpoints
are extracted and checked.
"""

from __future__ import annotations

from typing import Dict, List

import pytest

from grove.dispatcher import Dispatcher
from grove.intents import ToolIntent
from tests._runtime_ctx import MOCK_RUNTIME_CTX
from tests.grove.test_dispatch_turn import (
    _patch_classifier_green,
    _phase2_executor_stub,
    _synthetic_generator,
)
from tools.file_tools import extract_write_targets


# ── extract_write_targets: single source of truth for write targets ──────────


def test_extract_write_file_target():
    assert extract_write_targets("write_file", {"path": "/a/b.txt", "content": "x"}) == [
        "/a/b.txt"
    ]


def test_extract_patch_replace_target():
    assert extract_write_targets("patch", {"mode": "replace", "path": "/a/b.txt"}) == [
        "/a/b.txt"
    ]


def test_extract_patch_v4a_update_add_delete():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: /a/u.txt\n@@\n-x\n+y\n"
        "*** Add File: /a/n.txt\n+hello\n"
        "*** Delete File: /a/d.txt\n"
        "*** End Patch\n"
    )
    assert extract_write_targets("patch", {"mode": "patch", "patch": patch}) == [
        "/a/u.txt",
        "/a/n.txt",
        "/a/d.txt",
    ]


def test_extract_patch_move_both_endpoints():
    patch = "*** Begin Patch\n*** Move File: /a/from.txt -> /a/to.txt\n*** End Patch\n"
    assert extract_write_targets("patch", {"mode": "patch", "patch": patch}) == [
        "/a/from.txt",
        "/a/to.txt",
    ]


def test_extract_nonwrite_tool_is_empty():
    assert extract_write_targets("read_file", {"path": "/a/b.txt"}) == []


# ── dispatcher integration: refuse BEFORE classification ─────────────────────


def _agent(msgs: List[Dict]):
    import run_agent

    agent = object.__new__(run_agent.AIAgent)
    agent._runtime_ctx = MOCK_RUNTIME_CTX
    agent._current_messages = msgs
    agent.session_id = "wc-dispatch-test"
    agent.model = "m"
    agent.provider = "p"
    _phase2_executor_stub(agent)
    return agent


def test_out_of_confinement_write_refused_before_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "grove"))
    (tmp_path / "grove").mkdir()
    prompted: List = []
    msgs: List[Dict] = []
    agent = _agent(msgs)
    intents = [
        ToolIntent(
            tool_name="write_file",
            arguments={"path": "/etc/evil.txt", "content": "x"},
            call_id="c1",
        )
    ]
    agent._run_turn_generator = lambda **kw: _synthetic_generator(
        intents, {"final_response": "done"}
    )
    d = Dispatcher(sovereign_prompt_handler=lambda halt: prompted.append(halt) or "deny")
    d.dispatch_turn(agent, user_message="hi")

    assert agent._exec_called is False  # the write never executed
    assert prompted == []  # no sovereignty prompt fired (refused pre-classification)
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert any(
        "outside your declared write workspaces" in (m.get("content") or "")
        and "write_workspaces.yaml" in (m.get("content") or "")
        for m in tool_msgs
    )


def test_in_confinement_write_proceeds(tmp_path, monkeypatch):
    """Guard: an ALLOWED write is NOT over-blocked — the gate returns None and the
    (green) batch executes normally."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "grove"))
    (tmp_path / "grove").mkdir()
    _patch_classifier_green(monkeypatch)
    msgs: List[Dict] = []
    agent = _agent(msgs)
    target = str(tmp_path / "ok.txt")  # under the system temp → allowed (source c)
    intents = [
        ToolIntent(
            tool_name="write_file",
            arguments={"path": target, "content": "x"},
            call_id="c1",
        )
    ]
    agent._run_turn_generator = lambda **kw: _synthetic_generator(
        intents, {"final_response": "ok"}
    )
    d = Dispatcher()
    d.dispatch_turn(agent, user_message="hi")
    assert agent._exec_called is True


def test_patch_out_of_confinement_refused_before_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "grove"))
    (tmp_path / "grove").mkdir()
    prompted: List = []
    msgs: List[Dict] = []
    agent = _agent(msgs)
    patch = (
        "*** Begin Patch\n*** Update File: /etc/evil.conf\n@@\n-a\n+b\n*** End Patch\n"
    )
    intents = [
        ToolIntent(
            tool_name="patch",
            arguments={"mode": "patch", "patch": patch},
            call_id="c1",
        )
    ]
    agent._run_turn_generator = lambda **kw: _synthetic_generator(
        intents, {"final_response": "d"}
    )
    d = Dispatcher(sovereign_prompt_handler=lambda halt: prompted.append(halt) or "deny")
    d.dispatch_turn(agent, user_message="hi")
    assert agent._exec_called is False
    assert prompted == []

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


def test_write_target_extraction_covers_every_file_write_tool():
    """routing-scope-wall-v1 R-W5 — whitelist-sync tripwire.

    The scope-wall (Seam β + the execution guard) keys on the targets
    extract_write_targets yields. So EVERY file_write-class tool (TOOL_CLASS_MAP)
    MUST be handled by extract_write_targets, or a scope-defining write through
    that tool is a silent blind spot. If this fails, a new write-class tool was
    registered without wiring it into extract_write_targets — the banked debt
    item write-target-extraction-universality. Wire the new tool into
    extract_write_targets (and the sensitive/governed walls) before shipping it.
    """
    from grove.tool_classes import TOOL_CLASS_MAP
    file_write_tools = {n for n, c in TOOL_CLASS_MAP.items() if c == "file_write"}
    assert file_write_tools, "no file_write tools in TOOL_CLASS_MAP — map changed?"
    probe = {"path": "/tmp/probe.txt", "mode": "replace", "content": "x"}
    unhandled = {t for t in file_write_tools if not extract_write_targets(t, probe)}
    assert not unhandled, (
        "file_write-class tools NOT handled by extract_write_targets — scope-wall "
        "blind spot (debt: write-target-extraction-universality): "
        f"{sorted(unhandled)}"
    )


# ── dispatcher integration: refuse BEFORE classification ─────────────────────


def _agent(msgs: List[Dict]):
    import run_agent

    agent = object.__new__(run_agent.AIAgent)
    agent._runtime_ctx = MOCK_RUNTIME_CTX
    agent._current_messages = msgs
    agent.session_id = "wc-dispatch-test"
    agent.model = "m"
    agent.provider = "p"
    # switch_model dereferences self.api_key/self.base_url on the governed
    # tier-bind (run_agent.py:3852-3853); object.__new__ skips __init__.
    agent.api_key = ""
    agent.base_url = ""
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
        and "add_write_workspace" in (m.get("content") or "")
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


# ── routing-scope-wall-v1 R-W3 — Seam β: target-keyed scope-defining RED ──────


def _classify(tool_name, args):
    import grove.dispatch as _gd
    return Dispatcher._classify_one_intent(
        ToolIntent(tool_name=tool_name, arguments=args, call_id="c1"), _gd
    )


def test_write_file_to_scope_defining_is_red(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "grove"))
    (tmp_path / "grove").mkdir()
    zr = _classify("write_file", {
        "path": str(tmp_path / "grove" / "routing.config.yaml"), "content": "x",
    })
    assert zr.zone == "red"
    assert "scope_defining" in (zr.matched_rule or "")


def test_patch_to_scope_defining_is_red(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "grove"))
    (tmp_path / "grove").mkdir()
    zr = _classify("patch", {
        "mode": "replace", "path": str(tmp_path / "grove" / "zones.schema.yaml"),
    })
    assert zr.zone == "red"


def test_write_file_autonomaton_overlay_is_red(tmp_path, monkeypatch):
    # the new R-W2 authority surfaces are RED via the generic write tools too.
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "grove"))
    (tmp_path / "grove").mkdir()
    zr = _classify("write_file", {
        "path": str(tmp_path / "grove" / "routing.autonomaton.yaml"), "content": "x",
    })
    assert zr.zone == "red"


def test_write_file_nonscope_grove_target_stays_yellow(tmp_path, monkeypatch, hermetic_grove_home):
    # a non-scope-defining write still classifies YELLOW (bare-tool default) —
    # the wall is target-keyed, not a blanket ~/.grove RED.
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "grove"))
    (tmp_path / "grove" / "memory").mkdir(parents=True)
    zr = _classify("write_file", {
        "path": str(tmp_path / "grove" / "memory" / "note.txt"), "content": "x",
    })
    assert zr.zone == "yellow"


def test_propose_governance_change_scope_defining_is_red(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "grove"))
    (tmp_path / "grove").mkdir()
    zr = _classify("propose_governance_change", {
        "target_file": str(tmp_path / "grove" / "routing.config.yaml"), "content": "x",
    })
    assert zr.zone == "red"


def test_propose_governance_change_nonscope_dock_stays_yellow(tmp_path, monkeypatch):
    # a Dock goal file is governance-writable YELLOW, not scope-defining.
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "grove"))
    (tmp_path / "grove" / "dock" / "goals").mkdir(parents=True)
    zr = _classify("propose_governance_change", {
        "target_file": str(tmp_path / "grove" / "dock" / "goals" / "g.yaml"), "content": "x",
    })
    assert zr.zone == "yellow"


def test_confinement_denies_whole_intent_if_any_target_out_of_union(tmp_path, monkeypatch):
    # 0b regression pin (G3 minimal-fix branch is DEAD): _enforce_write_confinement
    # extracts ALL targets from one intent; a single out-of-union endpoint denies
    # the whole intent/batch before classification. A Move with one allowed and one
    # out-of-union endpoint must NOT execute.
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "grove"))
    (tmp_path / "grove").mkdir()
    prompted: List = []
    msgs: List[Dict] = []
    agent = _agent(msgs)
    good = str(tmp_path / "ok.txt")  # /tmp → allowed
    patch = f"*** Begin Patch\n*** Move File: {good} -> /etc/evil.txt\n*** End Patch\n"
    intents = [ToolIntent(tool_name="patch", arguments={"mode": "patch", "patch": patch}, call_id="c1")]
    agent._run_turn_generator = lambda **kw: _synthetic_generator(intents, {"final_response": "d"})
    d = Dispatcher(sovereign_prompt_handler=lambda halt: prompted.append(halt) or "deny")
    d.dispatch_turn(agent, user_message="hi")
    assert agent._exec_called is False  # whole intent denied on the out-of-union endpoint
    assert prompted == []               # denied pre-classification, no prompt

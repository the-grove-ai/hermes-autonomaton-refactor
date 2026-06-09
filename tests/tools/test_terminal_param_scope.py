"""Sprint 75 Phase 2 — T1 param-scoping for terminal.

terminal exposes only command + workdir on T1 (the async/background params —
background/timeout/pty/notify_on_complete/watch_patterns — are T2/T3). It stays
EAGER and directly callable on T1; no pull, no round-trip. Single-source: the
T1 view DERIVES from the full schema + the tier-invariant core description, not
a parallel 'lite' copy.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("GROVE_HOME", os.path.expanduser("~/.grove"))

from agent.model_metadata import estimate_tokens_rough as _tok
from tools.terminal_tool import (
    TERMINAL_SCHEMA,
    TERMINAL_TOOL_DESCRIPTION,
    TERMINAL_T1_PARAMS,
    scope_terminal_def_for_t1,
)

_ADVANCED = ("background", "timeout", "pty", "notify_on_complete", "watch_patterns")


def _openai_terminal():
    return {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": TERMINAL_SCHEMA["description"],
            "parameters": TERMINAL_SCHEMA["parameters"],
        },
    }


def test_t1_params_are_command_and_workdir_only():
    assert set(TERMINAL_T1_PARAMS) == {"command", "workdir"}
    scoped = scope_terminal_def_for_t1(_openai_terminal())
    props = scoped["function"]["parameters"]["properties"]
    assert set(props) == {"command", "workdir"}
    assert scoped["function"]["parameters"]["required"] == ["command"]
    for advanced in _ADVANCED:
        assert advanced not in props        # async machinery is T2/T3 only


def test_t1_description_keeps_routing_drops_advanced():
    desc = scope_terminal_def_for_t1(_openai_terminal())["function"]["description"]
    # tier-invariant routing guidance is kept (it drives tool-selection on T1).
    assert "read_file" in desc and "search_files" in desc and "patch" in desc
    assert "workdir" in desc
    # the advanced-feature guidance is gone (its params don't exist on T1).
    assert "background=true" not in desc
    assert "pty=true" not in desc
    assert "notify_on_complete" not in desc


def test_t1_scope_is_substantially_smaller():
    full = _openai_terminal()
    scoped = scope_terminal_def_for_t1(full)
    f, s = _tok(json.dumps(full)), _tok(json.dumps(scoped))
    assert s < f * 0.5          # the param-scope is a real cut
    assert s < 400              # T1 terminal is small + eager


def test_non_terminal_def_passes_through_unchanged():
    other = {"type": "function", "function": {"name": "read_file", "parameters": {}}}
    assert scope_terminal_def_for_t1(other) is other


def test_full_schema_unchanged_single_source():
    # The full (T2/T3) description still carries BOTH the core routing guidance
    # and the advanced block — composed from the same fragments the T1 core uses.
    assert "read_file" in TERMINAL_TOOL_DESCRIPTION      # core routing
    assert "workdir" in TERMINAL_TOOL_DESCRIPTION
    assert "background=true" in TERMINAL_TOOL_DESCRIPTION  # advanced retained
    assert "pty=true" in TERMINAL_TOOL_DESCRIPTION
    # The full schema still exposes every param (T2/T3 lose nothing).
    assert set(TERMINAL_SCHEMA["parameters"]["properties"]) == {
        "command", "background", "timeout", "workdir", "pty",
        "notify_on_complete", "watch_patterns",
    }

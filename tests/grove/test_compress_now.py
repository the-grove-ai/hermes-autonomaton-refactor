"""Compression outside a turn — ``Dispatcher.compress_now``.

``AIAgent._compress_context`` is a generator the turn loop drives. Three
callers compress with no turn running (the gateway's session hygiene and
``/compress`` on the gateway and in the CLI). They called the generator as a
plain function and unpacked it, which failed every time with "not enough values
to unpack (expected 2, got 1)". They now go through ``compress_now``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from grove.dispatcher import Dispatcher
from grove.intents import MemoryLifecycleIntent, SessionRotateIntent

REPO = Path(__file__).resolve().parents[2]


class _Agent:
    """Stands in for the agent: a compression that yields what the real one
    yields, in the real order, and returns the real shape."""

    platform = "telegram"
    model = "some/model"

    def __init__(self, extra=None):
        self.seen = []
        self._extra = extra

    def _compress_context(self, messages, system_message, *, approx_tokens=None, focus_topic=None):
        self.args = (list(messages), system_message, approx_tokens, focus_topic)
        yield MemoryLifecycleIntent(event="on_pre_compress", messages=messages)
        rotated = yield SessionRotateIntent(reason="compression", new_system_prompt="NEW PROMPT")
        self.seen.append(("rotated", rotated.success, rotated.value))
        if self._extra is not None:
            yield self._extra
        yield MemoryLifecycleIntent(event="on_session_switch", parent_session_id="old")
        return [{"role": "user", "content": "summary"}], "NEW PROMPT"


def _dispatcher(agent):
    d = object.__new__(Dispatcher)
    d.agent = agent
    calls = []
    d.rotate_session = lambda **kw: calls.append(("rotate", kw)) or "new-session-id"
    d.execute_memory_lifecycle = lambda intent: calls.append(("lifecycle", intent.event))
    return d, calls


def test_compress_now_runs_the_compression_to_its_result():
    agent = _Agent()
    d, calls = _dispatcher(agent)
    compressed, prompt = d.compress_now(
        [{"role": "user", "content": "a"}], "", approx_tokens=1234, focus_topic="invoices",
    )
    assert compressed == [{"role": "user", "content": "summary"}]
    assert prompt == "NEW PROMPT"
    assert agent.args == ([{"role": "user", "content": "a"}], "", 1234, "invoices")


def test_each_yielded_step_is_executed_in_order_and_the_new_session_id_goes_back():
    agent = _Agent()
    d, calls = _dispatcher(agent)
    d.compress_now([], "")
    assert [c[0] if c[0] == "rotate" else c for c in calls] == [
        ("lifecycle", "on_pre_compress"), "rotate", ("lifecycle", "on_session_switch"),
    ]
    rotate = next(c[1] for c in calls if c[0] == "rotate")
    assert rotate["reason"] == "compression"
    assert rotate["new_system_prompt"] == "NEW PROMPT"
    assert rotate["source"] == "telegram" and rotate["model"] == "some/model"
    assert agent.seen == [("rotated", True, "new-session-id")]


def test_a_step_only_a_turn_can_execute_is_refused_not_skipped():
    d, _calls = _dispatcher(_Agent(extra=object()))
    with pytest.raises(RuntimeError, match="only a running turn"):
        d.compress_now([], "")


def test_no_caller_outside_the_turn_loop_unpacks_the_generator():
    """Inside run_agent.py the generator is consumed with ``yield from``.
    Anywhere else it must go through ``compress_now``."""
    offenders = []
    for rel in ("gateway/run.py", "cli.py"):
        for n, line in enumerate((REPO / rel).read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\._compress_context\(", line) and not line.lstrip().startswith("#"):
                offenders.append(f"{rel}:{n}")
    assert offenders == []

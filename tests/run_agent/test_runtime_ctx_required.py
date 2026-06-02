"""Sprint 34 contract tests — RuntimeContext mandatory on AIAgent.

These tests assert the fail-fast / fail-loud contract that Sprint 34
introduced: every Agent must be constructed with a RuntimeContext;
absence raises a named, loud error rather than silently falling back to
direct substrate reads.

The companion D1 changes are also verified here:

* The Dispatcher injects ``runtime_ctx=self._base_runtime_ctx`` into
  the Agent it constructs (``Dispatcher(agent_kwargs={...}).agent``).
* The inline lazy Dispatcher build inside ``AIAgent.run_conversation``
  propagates the Agent's own ``_runtime_ctx`` into the Dispatcher it
  creates, so tests that bypass the inversion still share the same
  substrate snapshot.
"""

from __future__ import annotations

import pytest

from grove.dispatcher import Dispatcher, RuntimeContext
from run_agent import AIAgent
from tests._runtime_ctx import MOCK_RUNTIME_CTX, MOCK_CAPABILITY_PROVIDER


# ── Construction contract ──────────────────────────────────────────────


def test_construction_without_runtime_ctx_raises_type_error():
    """No ``runtime_ctx=`` kwarg → TypeError from the kwonly signature."""
    with pytest.raises(TypeError, match="runtime_ctx"):
        AIAgent(model="test/model", api_key="k", base_url="http://x")


def test_construction_with_explicit_none_raises_value_error():
    """Explicit ``runtime_ctx=None`` → named ValueError pointing at Dispatcher."""
    with pytest.raises(ValueError, match="RuntimeContext"):
        AIAgent(
            model="test/model",
            api_key="k",
            base_url="http://x",
            runtime_ctx=None,
        )


def test_construction_with_runtime_ctx_succeeds(monkeypatch):
    """A real RuntimeContext satisfies the contract; __init__ completes."""
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda *args, **_: [])
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda *args, **kw: {})
    monkeypatch.setattr("run_agent.OpenAI", lambda **_: object())
    agent = AIAgent(
        model="test/model",
        api_key="k",
        base_url="http://x",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        runtime_ctx=MOCK_RUNTIME_CTX, get_available_tools=MOCK_CAPABILITY_PROVIDER
    )
    assert agent._runtime_ctx is MOCK_RUNTIME_CTX


# ── D1 verification: Dispatcher injects runtime_ctx ─────────────────────


def test_dispatcher_injects_runtime_ctx_into_constructed_agent(monkeypatch):
    """``Dispatcher(agent_kwargs=...)`` forwards ``_base_runtime_ctx``."""
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda *args, **_: [])
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda *args, **kw: {})
    monkeypatch.setattr("run_agent.OpenAI", lambda **_: object())
    injected = RuntimeContext(env={"INJECTED": "yes"}, config={"injected": True})
    d = Dispatcher(
        runtime_ctx=injected,
        agent_kwargs=dict(
            model="test/model",
            api_key="k",
            base_url="http://x",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        ),
    )
    assert d.agent is not None
    assert d.agent._runtime_ctx is injected


def test_caller_supplied_runtime_ctx_in_agent_kwargs_wins(monkeypatch):
    """If a caller pre-fills ``runtime_ctx`` in agent_kwargs, it survives.

    The Dispatcher uses ``setdefault`` so an explicit caller-provided ctx
    is not overwritten by ``_base_runtime_ctx``.
    """
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda *args, **_: [])
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda *args, **kw: {})
    monkeypatch.setattr("run_agent.OpenAI", lambda **_: object())
    base = RuntimeContext(env={"BASE": "1"})
    explicit = RuntimeContext(env={"EXPLICIT": "1"})
    d = Dispatcher(
        runtime_ctx=base,
        agent_kwargs=dict(
            model="test/model",
            api_key="k",
            base_url="http://x",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            runtime_ctx=explicit,
        ),
    )
    assert d.agent._runtime_ctx is explicit


# ── D1 verification: inline lazy Dispatcher build propagates ctx ───────


def test_inline_lazy_dispatcher_inherits_agent_runtime_ctx(monkeypatch):
    """When an Agent without a back-reference reaches run_conversation, the
    inline lazy Dispatcher build must construct itself with the Agent's
    own ``_runtime_ctx`` — no fresh substrate snapshot, no None default."""
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda *args, **_: [])
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda *args, **kw: {})
    monkeypatch.setattr("run_agent.OpenAI", lambda **_: object())
    agent = AIAgent(
        model="test/model",
        api_key="k",
        base_url="http://x",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        runtime_ctx=MOCK_RUNTIME_CTX, get_available_tools=MOCK_CAPABILITY_PROVIDER
    )
    # Clear any back-reference so the inline lazy build path fires.
    if hasattr(agent, "_dispatcher_singleton"):
        del agent._dispatcher_singleton

    captured: dict = {}

    class _FakeDispatcher:
        def __init__(self, *, runtime_ctx, sovereign_prompt_handler=None, intent_store=None):
            captured["runtime_ctx"] = runtime_ctx

        def dispatch_turn(self, *args, **kwargs):
            return {"final_response": "ok"}

    monkeypatch.setattr("grove.dispatcher.Dispatcher", _FakeDispatcher)
    agent.run_conversation("hello")
    assert captured["runtime_ctx"] is MOCK_RUNTIME_CTX

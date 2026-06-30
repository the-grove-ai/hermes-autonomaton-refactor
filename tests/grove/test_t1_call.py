"""Tests for grove.t1_call — the shared T1 (Cheap Cognition) call primitive.

Sprint K1 (living-cellar-v1) Phase 1. The primitive resolves the T1 tier BY
NAME via the router's PUBLIC tier API (no import of classify's private
``_telemetry_tier_runtime``), builds the client through the credential-aware
``build_anthropic_client``, and either forces a tool_use call (structured
dict result) or runs a plain-text completion (str result). It asserts the
resolved tier is anthropic_messages and fails loud on a malformed response.
"""

from __future__ import annotations

import pytest

import grove.t1_call as t1_call


# ── Fakes ──────────────────────────────────────────────────────────────


class _FakeUsage:
    def __init__(self, input_tokens=100, output_tokens=50):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeToolUseBlock:
    def __init__(self, name, inp):
        self.type = "tool_use"
        self.name = name
        self.input = inp


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, blocks, usage=None):
        self.content = blocks
        self.usage = usage or _FakeUsage()


class _FakeMessages:
    def __init__(self, response, captured):
        self._response = response
        self._captured = captured

    def create(self, **kwargs):
        self._captured["create_kwargs"] = kwargs
        return self._response


class _FakeClient:
    def __init__(self, response, captured):
        self.messages = _FakeMessages(response, captured)


class _FakeTierConfig:
    def __init__(self, tier="T1", cost_in=0.80, cost_out=4.0):
        self.tier = tier
        self.handler = None
        self.provider = "anthropic"
        self.model = "claude-haiku-4-5-20251001"
        self.max_tokens = 4096
        self.max_latency_ms = None
        self.description = "Cheap Cognition"
        self.cost_per_mtok_input = cost_in
        self.cost_per_mtok_output = cost_out


def _install(
    monkeypatch,
    *,
    blocks,
    api_mode="anthropic_messages",
    tier_config=None,
    usage=None,
):
    """Wire the three public seams t1_call resolves through and return a
    ``captured`` dict recording what each seam received."""
    captured: dict = {}
    tc = tier_config if tier_config is not None else _FakeTierConfig()

    def _fake_get_tier_config(tier):
        captured["tier_name"] = tier
        return tc

    runtime = {
        "model": tc.model,
        "provider": "anthropic",
        "api_key": "sk-ant-test",
        "base_url": None,
        "api_mode": api_mode,
        "credential_pool": None,
        "auth_type": "api_key",
    }

    def _fake_resolve(cfg):
        captured["resolved_from"] = cfg
        return runtime

    client = _FakeClient(_FakeResponse(blocks, usage=usage), captured)

    def _fake_build_client(**kwargs):
        captured["client_kwargs"] = kwargs
        return client

    monkeypatch.setattr("grove.router.get_tier_config", _fake_get_tier_config)
    monkeypatch.setattr("grove.providers.resolve_tier_to_runtime", _fake_resolve)
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client", _fake_build_client
    )
    return captured


_TOOL = {
    "name": "verdict",
    "description": "structured verdict",
    "input_schema": {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    },
}


# ── Plain-text path ────────────────────────────────────────────────────


def test_plaintext_returns_joined_text(monkeypatch):
    _install(monkeypatch, blocks=[_FakeTextBlock("hello "), _FakeTextBlock("world")])
    out = t1_call.call_t1("write a page")
    assert out == "hello world"


def test_plaintext_threads_prompt_system_and_model(monkeypatch):
    captured = _install(monkeypatch, blocks=[_FakeTextBlock("ok")])
    t1_call.call_t1("the prompt", system="be terse", max_tokens=321)
    kwargs = captured["create_kwargs"]
    assert kwargs["messages"] == [{"role": "user", "content": "the prompt"}]
    assert kwargs["system"] == "be terse"
    assert kwargs["model"] == "claude-haiku-4-5-20251001"
    assert kwargs["max_tokens"] == 321
    # plain-text path must NOT force a tool
    assert "tools" not in kwargs and "tool_choice" not in kwargs


def test_plaintext_no_system_omits_system_key(monkeypatch):
    captured = _install(monkeypatch, blocks=[_FakeTextBlock("ok")])
    t1_call.call_t1("p")
    assert "system" not in captured["create_kwargs"]


def test_plaintext_fails_loud_when_no_text(monkeypatch):
    _install(monkeypatch, blocks=[])
    with pytest.raises(ValueError, match="no text"):
        t1_call.call_t1("p")


# ── Tool (forced tool_use) path ────────────────────────────────────────


def test_tool_returns_input_dict(monkeypatch):
    _install(
        monkeypatch,
        blocks=[_FakeToolUseBlock("verdict", {"ok": True})],
    )
    out = t1_call.call_t1("evaluate", tool=_TOOL)
    assert out == {"ok": True}


def test_tool_forces_that_tool(monkeypatch):
    captured = _install(
        monkeypatch, blocks=[_FakeToolUseBlock("verdict", {"ok": False})]
    )
    t1_call.call_t1("evaluate", tool=_TOOL)
    kwargs = captured["create_kwargs"]
    assert kwargs["tools"] == [_TOOL]
    assert kwargs["tool_choice"] == {"type": "tool", "name": "verdict"}


def test_tool_fails_loud_when_no_tool_use_block(monkeypatch):
    _install(monkeypatch, blocks=[_FakeTextBlock("I refuse to use the tool")])
    with pytest.raises(ValueError, match="no .*tool_use"):
        t1_call.call_t1("evaluate", tool=_TOOL)


# ── Tier resolution (A1: public, by name, anthropic-native) ────────────


def test_resolves_t1_tier_by_name(monkeypatch):
    captured = _install(monkeypatch, blocks=[_FakeTextBlock("ok")])
    t1_call.call_t1("p")
    assert captured["tier_name"] == "T1"


def test_builds_client_with_runtime_credentials(monkeypatch):
    captured = _install(monkeypatch, blocks=[_FakeTextBlock("ok")])
    t1_call.call_t1("p")
    assert captured["client_kwargs"]["api_key"] == "sk-ant-test"
    assert captured["client_kwargs"]["base_url"] is None


def test_unsupported_api_mode_fails_loud(monkeypatch):
    # provider-agnostic-v1: chat_completions is now supported (see the
    # chat_completions section below). An api_mode that is NEITHER
    # anthropic_messages nor chat_completions must still fail loud — no silent
    # mis-shaped call.
    _install(
        monkeypatch,
        blocks=[_FakeTextBlock("ok")],
        api_mode="bedrock_converse",
    )
    with pytest.raises(RuntimeError, match="unsupported"):
        t1_call.call_t1("p")


def test_initializes_router_when_uninitialized(monkeypatch):
    """A fresh CLI process has no initialized router; the primitive must
    initialize it via the public initialize() and retry — not swallow."""
    tc = _FakeTierConfig()
    calls = {"get": 0, "init": 0}

    def _flaky_get_tier_config(tier):
        calls["get"] += 1
        if calls["get"] == 1:
            raise RuntimeError("grove.router is not initialized")
        return tc

    def _fake_initialize(*a, **k):
        calls["init"] += 1

    runtime = {
        "model": tc.model,
        "api_key": "k",
        "base_url": None,
        "api_mode": "anthropic_messages",
    }
    monkeypatch.setattr("grove.router.get_tier_config", _flaky_get_tier_config)
    monkeypatch.setattr("grove.router.initialize", _fake_initialize)
    monkeypatch.setattr(
        "grove.providers.resolve_tier_to_runtime", lambda cfg: runtime
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client",
        lambda **kw: _FakeClient(_FakeResponse([_FakeTextBlock("ok")]), {}),
    )
    out = t1_call.call_t1("p")
    assert out == "ok"
    assert calls["init"] == 1
    assert calls["get"] == 2


# ── Cost telemetry (replicated field read; Jidoka on missing cost) ──────


def test_missing_cost_does_not_crash(monkeypatch):
    _install(
        monkeypatch,
        blocks=[_FakeTextBlock("ok")],
        tier_config=_FakeTierConfig(cost_in=None, cost_out=None),
    )
    # Must complete normally — surface the gap (warn), never hard-fail the call.
    assert t1_call.call_t1("p") == "ok"


# ── chat_completions (OpenAI-compatible) path — provider-agnostic-v1 ──────
#
# call_t1 now speaks chat_completions as well as anthropic_messages, so the wiki
# pipeline runs on an OpenRouter-bound T1 (the same tier the telemetry classifier
# already uses). The Anthropic path above is unchanged (I1). These cases cover
# the new branch: plain-text + forced-tool, the GENERIC Anthropic→OpenAI tool
# reshape, fail-loud on a missing tool_call / empty content, and OpenAI-named
# usage fields for cost.


class _FakeOAIFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeOAIToolCall:
    def __init__(self, name, arguments):
        self.function = _FakeOAIFunction(name, arguments)


class _FakeOAIMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeOAIChoice:
    def __init__(self, message):
        self.message = message


class _FakeOAIUsage:
    def __init__(self, prompt_tokens=100, completion_tokens=50):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeOAIResponse:
    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage or _FakeOAIUsage()


def _install_openai(
    monkeypatch, *, response, tier_config=None,
    provider="openrouter", base_url="https://openrouter.ai/api/v1",
):
    """Wire t1_call's seams for the chat_completions branch: tier config,
    runtime (api_mode=chat_completions, OpenRouter creds), and a fake
    ``openai.OpenAI`` whose ``chat.completions.create`` returns ``response`` and
    records its kwargs in the returned ``captured`` dict."""
    captured: dict = {}
    tc = tier_config if tier_config is not None else _FakeTierConfig()

    def _fake_get_tier_config(tier):
        captured["tier_name"] = tier
        return tc

    runtime = {
        "model": tc.model,
        "provider": provider,
        "api_key": "or-test",
        "base_url": base_url,
        "api_mode": "chat_completions",
        "credential_pool": None,
        "auth_type": "api_key",
    }

    def _fake_resolve(cfg):
        captured["resolved_from"] = cfg
        return runtime

    class _FakeCompletions:
        def create(self, **kwargs):
            captured["oai_create_kwargs"] = kwargs
            return response

    class _FakeChat:
        def __init__(self):
            self.completions = _FakeCompletions()

    class _FakeOpenAIClient:
        def __init__(self):
            self.chat = _FakeChat()

    def _fake_ctor(**kwargs):
        captured["oai_client_kwargs"] = kwargs
        return _FakeOpenAIClient()

    monkeypatch.setattr("grove.router.get_tier_config", _fake_get_tier_config)
    monkeypatch.setattr("grove.providers.resolve_tier_to_runtime", _fake_resolve)
    monkeypatch.setattr("openai.OpenAI", _fake_ctor)
    return captured


def _oai_text(content):
    return _FakeOAIResponse([_FakeOAIChoice(_FakeOAIMessage(content=content))])


def _oai_tool(name, arguments):
    msg = _FakeOAIMessage(tool_calls=[_FakeOAIToolCall(name, arguments)])
    return _FakeOAIResponse([_FakeOAIChoice(msg)])


def test_chat_completions_plaintext_returns_content(monkeypatch):
    _install_openai(monkeypatch, response=_oai_text("hello from openrouter"))
    assert t1_call.call_t1("write a page") == "hello from openrouter"


def test_chat_completions_threads_model_system_messages(monkeypatch):
    captured = _install_openai(monkeypatch, response=_oai_text("ok"))
    t1_call.call_t1("the prompt", system="be terse", max_tokens=321)
    kw = captured["oai_create_kwargs"]
    assert kw["model"] == "claude-haiku-4-5-20251001"
    assert kw["max_tokens"] == 321
    assert kw["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "the prompt"},
    ]
    assert "tools" not in kw and "tool_choice" not in kw


def test_chat_completions_no_system_omits_system_message(monkeypatch):
    captured = _install_openai(monkeypatch, response=_oai_text("ok"))
    t1_call.call_t1("p")
    assert captured["oai_create_kwargs"]["messages"] == [
        {"role": "user", "content": "p"}
    ]


def test_chat_completions_builds_client_with_runtime_credentials(monkeypatch):
    captured = _install_openai(monkeypatch, response=_oai_text("ok"))
    t1_call.call_t1("p")
    assert captured["oai_client_kwargs"]["api_key"] == "or-test"
    assert (
        captured["oai_client_kwargs"]["base_url"]
        == "https://openrouter.ai/api/v1"
    )


def test_chat_completions_plaintext_fails_loud_on_empty_content(monkeypatch):
    _install_openai(monkeypatch, response=_oai_text(None))
    with pytest.raises((ValueError, RuntimeError), match="(?i)empty|content"):
        t1_call.call_t1("p")


def test_chat_completions_tool_returns_parsed_dict(monkeypatch):
    _install_openai(monkeypatch, response=_oai_tool("verdict", '{"ok": true}'))
    assert t1_call.call_t1("evaluate", tool=_TOOL) == {"ok": True}


def test_chat_completions_tool_reshapes_anthropic_tool_to_openai(monkeypatch):
    captured = _install_openai(
        monkeypatch, response=_oai_tool("verdict", '{"ok": false}')
    )
    t1_call.call_t1("evaluate", tool=_TOOL)
    kw = captured["oai_create_kwargs"]
    assert kw["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "verdict",
                "description": "structured verdict",
                "parameters": _TOOL["input_schema"],
            },
        }
    ]
    assert kw["tool_choice"] == {
        "type": "function",
        "function": {"name": "verdict"},
    }


def test_chat_completions_tool_fails_loud_when_no_tool_calls(monkeypatch):
    _install_openai(
        monkeypatch,
        response=_FakeOAIResponse(
            [_FakeOAIChoice(_FakeOAIMessage(content="I refuse"))]
        ),
    )
    with pytest.raises((ValueError, RuntimeError), match="(?i)tool"):
        t1_call.call_t1("evaluate", tool=_TOOL)


def test_chat_completions_cost_reads_openai_token_fields(monkeypatch):
    # OpenAI usage uses prompt_tokens/completion_tokens. The tracker must read
    # them — Anthropic field names would silently accumulate zero.
    _install_openai(
        monkeypatch,
        response=_FakeOAIResponse(
            [_FakeOAIChoice(_FakeOAIMessage(content="ok"))],
            usage=_FakeOAIUsage(prompt_tokens=1000, completion_tokens=500),
        ),
    )
    before = t1_call.cumulative_cost_usd()
    t1_call.call_t1("p")
    assert t1_call.cumulative_cost_usd() > before


def test_anthropic_path_preserved_after_provider_branch(monkeypatch):
    # I1 regression guard: the anthropic_messages path still returns text and
    # still forces tools exactly as before the provider branch was added.
    captured = _install(monkeypatch, blocks=[_FakeTextBlock("anthropic ok")])
    assert t1_call.call_t1("p", system="s") == "anthropic ok"
    kw = captured["create_kwargs"]
    assert kw["messages"] == [{"role": "user", "content": "p"}]
    assert kw["system"] == "s"


# ── openrouter-zero-retention-routing-v1 — provider pass-through ─────────


def test_chat_completions_attaches_openrouter_provider(monkeypatch):
    import grove.router as gr
    pref = {"order": ["DeepInfra"], "allow_fallbacks": True, "data_collection": "deny"}
    monkeypatch.setattr(gr, "get_provider_routing", lambda: {"openrouter": pref})
    captured = _install_openai(monkeypatch, response=_oai_text("ok"))
    t1_call.call_t1("p")
    assert captured["oai_create_kwargs"].get("extra_body") == {"provider": pref}


def test_chat_completions_no_provider_when_routing_unset(monkeypatch):
    import grove.router as gr
    monkeypatch.setattr(gr, "get_provider_routing", lambda: {})
    captured = _install_openai(monkeypatch, response=_oai_text("ok"))
    t1_call.call_t1("p")
    assert "extra_body" not in captured["oai_create_kwargs"]


def test_chat_completions_no_provider_when_not_openrouter(monkeypatch):
    import grove.router as gr
    monkeypatch.setattr(gr, "get_provider_routing",
                        lambda: {"openrouter": {"order": ["DeepInfra"]}})
    # A non-OpenRouter chat_completions provider (e.g. Ollama) must NOT receive
    # the OpenRouter-specific provider field.
    captured = _install_openai(
        monkeypatch, response=_oai_text("ok"),
        provider="ollama", base_url="http://localhost:11434/v1",
    )
    t1_call.call_t1("p")
    assert "extra_body" not in captured["oai_create_kwargs"]

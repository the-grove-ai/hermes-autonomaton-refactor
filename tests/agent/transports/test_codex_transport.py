"""Tests for the ResponsesApiTransport (Codex)."""

import json
import pytest
from types import SimpleNamespace

from agent.transports import get_transport
from agent.transports.types import NormalizedResponse, ToolCall
from grove.router import ModelFacts

# binding-opacity-v1 P4b 1c — whether an xAI model accepts the reasoning.effort
# dial is the declared fact model_facts.reasoning_support, threaded via the
# codex build_kwargs params carrier — not a grok-* name allowlist.
_EFFORT_CAPABLE = ModelFacts(reasoning_support=True)


@pytest.fixture
def transport():
    import agent.transports.codex  # noqa: F401
    return get_transport("codex_responses")


class TestCodexTransportBasic:

    def test_api_mode(self, transport):
        assert transport.api_mode == "codex_responses"

    def test_registered_on_import(self, transport):
        assert transport is not None

    def test_convert_tools(self, transport):
        tools = [{
            "type": "function",
            "function": {
                "name": "terminal",
                "description": "Run a command",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            }
        }]
        result = transport.convert_tools(tools)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["name"] == "terminal"


class TestCodexBuildKwargs:

    def test_basic_kwargs(self, transport):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        kw = transport.build_kwargs(
            model="gpt-5.4",
            messages=messages,
            tools=[],
        )
        assert kw["model"] == "gpt-5.4"
        assert kw["instructions"] == "You are helpful."
        assert "input" in kw
        assert kw["store"] is False

    def test_system_extracted_from_messages(self, transport):
        messages = [
            {"role": "system", "content": "Custom system prompt"},
            {"role": "user", "content": "Hi"},
        ]
        kw = transport.build_kwargs(model="gpt-5.4", messages=messages, tools=[])
        assert kw["instructions"] == "Custom system prompt"

    def test_no_system_uses_default(self, transport):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(model="gpt-5.4", messages=messages, tools=[])
        assert kw["instructions"]  # should be non-empty default

    def test_reasoning_config(self, transport):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=messages, tools=[],
            reasoning_config={"effort": "high"},
        )
        assert kw.get("reasoning", {}).get("effort") == "high"

    def test_reasoning_disabled(self, transport):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=messages, tools=[],
            reasoning_config={"enabled": False},
        )
        assert "reasoning" not in kw or kw.get("include") == []

    def test_session_id_sets_cache_key(self, transport):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=messages, tools=[],
            session_id="test-session-123",
        )
        assert kw.get("prompt_cache_key") == "test-session-123"

    def test_github_responses_no_cache_key(self, transport):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=messages, tools=[],
            session_id="test-session",
            is_github_responses=True,
        )
        assert "prompt_cache_key" not in kw

    def test_xai_responses_sends_cache_key_via_extra_body(self, transport):
        """xAI's Responses API documents ``prompt_cache_key`` as the
        body-level cache-routing key (the ``x-grok-conv-id`` header is
        Chat-Completions-only). Passing it via ``extra_body`` is robust
        against openai SDK builds whose ``Responses.stream()`` kwarg
        signature ever drops the field — the body field still serializes
        and reaches xAI either way. The ``x-grok-conv-id`` header is kept
        as a belt-and-braces fallback so cache routing survives even
        when the body field would be stripped by an intermediate proxy.
        Ref: https://docs.x.ai/developers/advanced-api-usage/prompt-caching/maximizing-cache-hits
        """
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="grok-4.3", messages=messages, tools=[],
            session_id="conv-xai-1",
            is_xai_responses=True,
        )
        assert "prompt_cache_key" not in kw
        assert kw.get("extra_body", {}).get("prompt_cache_key") == "conv-xai-1"
        assert kw.get("extra_headers", {}).get("x-grok-conv-id") == "conv-xai-1"

    def test_xai_responses_extra_body_preserves_caller_fields(self, transport):
        """When the caller already supplies ``extra_body`` (e.g. via
        request_overrides), the xAI cache-key injection must merge into
        the existing dict instead of overwriting it. Caller-supplied
        ``prompt_cache_key`` wins (setdefault semantics) so user overrides
        aren't silently clobbered by the transport."""
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="grok-4.3", messages=messages, tools=[],
            session_id="conv-xai-1",
            is_xai_responses=True,
            request_overrides={"extra_body": {"prompt_cache_key": "caller-override", "other_field": 42}},
        )
        eb = kw.get("extra_body", {})
        assert eb.get("prompt_cache_key") == "caller-override"
        assert eb.get("other_field") == 42

    def test_max_tokens(self, transport):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=messages, tools=[],
            max_tokens=4096,
        )
        assert kw.get("max_output_tokens") == 4096

    def test_codex_backend_no_max_output_tokens(self, transport):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=messages, tools=[],
            max_tokens=4096,
            is_codex_backend=True,
        )
        assert "max_output_tokens" not in kw

    def test_xai_headers(self, transport):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="grok-3", messages=messages, tools=[],
            session_id="conv-123",
            is_xai_responses=True,
        )
        assert kw.get("extra_headers", {}).get("x-grok-conv-id") == "conv-123"

    def test_xai_headers_preserve_request_override_headers(self, transport):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="grok-3", messages=messages, tools=[],
            session_id="conv-123",
            is_xai_responses=True,
            request_overrides={"extra_headers": {"X-Test": "1", "X-Trace": "abc"}},
        )
        assert kw.get("extra_headers") == {
            "X-Test": "1",
            "X-Trace": "abc",
            "x-grok-conv-id": "conv-123",
        }

    def test_minimal_effort_clamped(self, transport):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=messages, tools=[],
            reasoning_config={"effort": "minimal"},
        )
        # "minimal" should be clamped to "low"
        assert kw.get("reasoning", {}).get("effort") == "low"

    def test_xai_reasoning_effort_passed(self, transport):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="grok-4.3", messages=messages, tools=[],
            is_xai_responses=True,
            model_facts=_EFFORT_CAPABLE,
            reasoning_config={"effort": "high"},
        )
        # xAI Responses receives reasoning.effort when the model declares support.
        assert kw.get("reasoning") == {"effort": "high"}
        # As of May 2026 we deliberately do NOT request
        # reasoning.encrypted_content back from xAI — the OAuth/SuperGrok
        # surface rejects replayed encrypted reasoning items on turn 2+
        # (the multi-turn "Expected to have received response.created
        # before error" failure).  Grok still reasons natively each turn;
        # we just don't try to thread the prior turn's encrypted blob back
        # in.  See tests/run_agent/test_codex_xai_oauth_recovery.py.
        assert "reasoning.encrypted_content" not in kw.get("include", [])

    def test_xai_reasoning_disabled_no_reasoning_key(self, transport):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="grok-4.3", messages=messages, tools=[],
            is_xai_responses=True,
            reasoning_config={"enabled": False},
        )
        # When reasoning is disabled, do not send the reasoning key at all
        assert "reasoning" not in kw

    def test_xai_minimal_effort_clamped(self, transport):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="grok-4.3", messages=messages, tools=[],
            is_xai_responses=True,
            model_facts=_EFFORT_CAPABLE,
            reasoning_config={"effort": "minimal"},
        )
        # "minimal" should be clamped to "low" for xAI as well
        assert kw.get("reasoning", {}).get("effort") == "low"

    # --- Grok reasoning-effort capability: DECLARED, not name-allowlisted ---
    # binding-opacity-v1 P4b 1c — EXCISED the eight grok-NAME-roster tests
    # (test_xai_grok_4_omits / _grok_4_fast_omits / _grok_3_non_mini_omits /
    # _grok_3_mini_keeps / _grok_4_20_0309_variants_omit /
    # _grok_4_20_multi_agent_keeps / _grok_code_fast_omits /
    # _aggregator_prefix_stripped). Every one pinned _GROK_EFFORT_CAPABLE_PREFIXES
    # — which grok-* NAMES accept the reasoning.effort dial — the inference the
    # migration deleted. api.x.ai still 400s on the incapable models; the
    # operator now DECLARES reasoning_support per bound xAI model, and the two
    # tests below pin the surviving behavior: declared -> dial sent, undeclared
    # -> no reasoning key (safe/loud, the model reasons natively on its own).

    def test_xai_declared_reasoning_support_sends_effort(self, transport):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="any-xai-slug", messages=messages, tools=[],
            is_xai_responses=True,
            model_facts=_EFFORT_CAPABLE,
            reasoning_config={"effort": "high"},
        )
        assert kw.get("reasoning") == {"effort": "high"}
        # We never request encrypted_content back from xAI (turn-2+ rejection).
        assert "reasoning.encrypted_content" not in kw.get("include", [])

    def test_xai_undeclared_reasoning_support_omits_effort(self, transport):
        # The VM-on-deploy path + models that 400 on the dial: undeclared ->
        # no reasoning key. The model still reasons natively each turn.
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="any-xai-slug", messages=messages, tools=[],
            is_xai_responses=True,
            reasoning_config={"effort": "high"},
        )
        assert "reasoning" not in kw


class TestCodexValidateResponse:

    def test_none_response(self, transport):
        assert transport.validate_response(None) is False

    def test_empty_output(self, transport):
        r = SimpleNamespace(output=[], output_text=None)
        assert transport.validate_response(r) is False

    def test_valid_output(self, transport):
        r = SimpleNamespace(output=[{"type": "message", "content": []}])
        assert transport.validate_response(r) is True

    def test_output_text_fallback_not_valid(self, transport):
        """validate_response is strict — output_text doesn't make it valid.
        The caller handles output_text fallback with diagnostic logging."""
        r = SimpleNamespace(output=None, output_text="Some text")
        assert transport.validate_response(r) is False


class TestCodexMapFinishReason:

    def test_completed(self, transport):
        assert transport.map_finish_reason("completed") == "stop"

    def test_incomplete(self, transport):
        assert transport.map_finish_reason("incomplete") == "length"

    def test_failed(self, transport):
        assert transport.map_finish_reason("failed") == "stop"

    def test_unknown(self, transport):
        assert transport.map_finish_reason("unknown_status") == "stop"


class TestCodexNormalizeResponse:

    def test_text_response(self, transport):
        """Normalize a simple text Codex response."""
        r = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    role="assistant",
                    content=[SimpleNamespace(type="output_text", text="Hello world")],
                    status="completed",
                ),
            ],
            status="completed",
            incomplete_details=None,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                  input_tokens_details=None, output_tokens_details=None),
        )
        nr = transport.normalize_response(r)
        assert isinstance(nr, NormalizedResponse)
        assert nr.content == "Hello world"
        assert nr.finish_reason == "stop"

    def test_message_items_preserved_in_provider_data(self, transport):
        """Codex assistant message item ids/phases must survive transport normalization."""
        r = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    role="assistant",
                    id="msg_abc",
                    phase="final_answer",
                    content=[SimpleNamespace(type="output_text", text="Hello world")],
                    status="completed",
                ),
            ],
            status="completed",
            incomplete_details=None,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                  input_tokens_details=None, output_tokens_details=None),
        )
        nr = transport.normalize_response(r)
        assert nr.codex_message_items == [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Hello world"}],
                "id": "msg_abc",
                "phase": "final_answer",
            }
        ]

    def test_tool_call_response(self, transport):
        """Normalize a Codex response with tool calls."""
        r = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="function_call",
                    call_id="call_abc123",
                    name="terminal",
                    arguments=json.dumps({"command": "ls"}),
                    id="fc_abc123",
                    status="completed",
                ),
            ],
            status="completed",
            incomplete_details=None,
            usage=SimpleNamespace(input_tokens=10, output_tokens=20,
                                  input_tokens_details=None, output_tokens_details=None),
        )
        nr = transport.normalize_response(r)
        assert nr.finish_reason == "tool_calls"
        assert len(nr.tool_calls) == 1
        tc = nr.tool_calls[0]
        assert tc.name == "terminal"
        assert '"command"' in tc.arguments

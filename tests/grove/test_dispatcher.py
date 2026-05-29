"""Tests for grove.dispatcher — Sprint 26 Phase 1a.

Covers the RuntimeContext snapshot dataclass and the Dispatcher skeleton.
Phase 1a is structural only: the Dispatcher captures env + config; it
does not yet build heavy singletons or invert tool execution. These
tests verify the substrate snapshot semantics and the graceful
degradation when load_config raises.

Subsequent phases will extend this file as the Dispatcher grows
responsibilities (heavy singletons → Phase 1b, intent protocol →
Phase 2, generator-shaped loop → Phase 3, etc.).
"""

from __future__ import annotations

import logging
from typing import Dict

import pytest

from grove.dispatcher import CompressionProbe, Dispatcher, RuntimeContext


# ── RuntimeContext ────────────────────────────────────────────────────────


class TestRuntimeContextEnv:
    def test_env_get_returns_snapshotted_value(self):
        ctx = RuntimeContext(env={"FOO": "bar"}, config={})
        assert ctx.env_get("FOO") == "bar"

    def test_env_get_returns_default_when_missing(self):
        ctx = RuntimeContext(env={}, config={})
        assert ctx.env_get("MISSING", "fallback") == "fallback"

    def test_env_get_returns_empty_string_default_when_absent_and_no_default(self):
        ctx = RuntimeContext(env={}, config={})
        assert ctx.env_get("MISSING") == ""

    def test_env_get_int_parses_numeric_string(self):
        ctx = RuntimeContext(env={"TIMEOUT": "1800"}, config={})
        assert ctx.env_get_int("TIMEOUT", 60) == 1800

    def test_env_get_int_returns_default_when_missing(self):
        ctx = RuntimeContext(env={}, config={})
        assert ctx.env_get_int("TIMEOUT", 60) == 60

    def test_env_get_int_returns_default_when_empty_string(self):
        # Mirrors the legacy ``int(os.getenv("X", "1800"))`` pattern's
        # behavior on an empty value — fall back rather than ValueError.
        ctx = RuntimeContext(env={"TIMEOUT": ""}, config={})
        assert ctx.env_get_int("TIMEOUT", 60) == 60

    def test_env_get_int_returns_default_on_parse_failure(
        self, caplog: pytest.LogCaptureFixture
    ):
        ctx = RuntimeContext(env={"TIMEOUT": "not-a-number"}, config={})
        with caplog.at_level(logging.DEBUG, logger="grove.dispatcher"):
            value = ctx.env_get_int("TIMEOUT", 60)
        assert value == 60
        assert any("not-a-number" in r.getMessage() for r in caplog.records)

    def test_env_get_float_parses_decimal_string(self):
        ctx = RuntimeContext(env={"TIMEOUT": "1800.5"}, config={})
        assert ctx.env_get_float("TIMEOUT", 60.0) == 1800.5

    def test_env_get_float_returns_default_when_missing(self):
        ctx = RuntimeContext(env={}, config={})
        assert ctx.env_get_float("TIMEOUT", 60.0) == 60.0

    def test_env_get_float_returns_default_on_parse_failure(self):
        ctx = RuntimeContext(env={"TIMEOUT": "abc"}, config={})
        assert ctx.env_get_float("TIMEOUT", 60.0) == 60.0


class TestRuntimeContextConfig:
    def test_config_get_walks_nested_path(self):
        ctx = RuntimeContext(
            env={},
            config={"memory": {"enabled": True, "store": {"path": "/x"}}},
        )
        assert ctx.config_get("memory", "enabled") is True
        assert ctx.config_get("memory", "store", "path") == "/x"

    def test_config_get_returns_default_when_path_missing(self):
        ctx = RuntimeContext(env={}, config={"memory": {}})
        assert ctx.config_get("memory", "absent", default="fallback") == "fallback"

    def test_config_get_returns_default_when_intermediate_is_non_mapping(self):
        # Walking past a non-dict value returns the default rather than raising.
        ctx = RuntimeContext(env={}, config={"memory": "not-a-dict"})
        assert ctx.config_get("memory", "enabled", default=False) is False

    def test_config_get_returns_none_when_no_default(self):
        ctx = RuntimeContext(env={}, config={})
        assert ctx.config_get("nonexistent") is None


class TestRuntimeContextImmutability:
    def test_runtime_context_is_frozen(self):
        ctx = RuntimeContext(env={}, config={})
        # dataclass(frozen=True) raises FrozenInstanceError on attribute set
        with pytest.raises(Exception):  # FrozenInstanceError, but be permissive
            ctx.env = {"NEW": "value"}  # type: ignore[misc]


# ── Dispatcher ────────────────────────────────────────────────────────────


class TestDispatcherConstruction:
    def test_dispatcher_captures_env_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("GROVE_TEST_DISPATCHER_PROBE", "captured")
        d = Dispatcher()
        assert d.runtime_ctx.env_get("GROVE_TEST_DISPATCHER_PROBE") == "captured"

    def test_dispatcher_env_snapshot_is_frozen_at_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Env mutation after Dispatcher construction must NOT leak into
        # the snapshot — operators who edit env mid-session must restart.
        monkeypatch.setenv("GROVE_TEST_INITIAL", "first")
        d = Dispatcher()
        monkeypatch.setenv("GROVE_TEST_INITIAL", "second")
        assert d.runtime_ctx.env_get("GROVE_TEST_INITIAL") == "first"

    def test_dispatcher_captures_config_snapshot(self):
        d = Dispatcher()
        # The real config dict is captured; verify it's a dict (specific
        # contents depend on the operator's ~/.grove/config.yaml).
        assert isinstance(d.runtime_ctx.config, dict)

    def test_dispatcher_runtime_ctx_property_returns_same_instance(self):
        d = Dispatcher()
        assert d.runtime_ctx is d.runtime_ctx  # idempotent getter

    def test_build_runtime_context_returns_same_snapshot(self):
        d = Dispatcher()
        assert d.build_runtime_context() is d.runtime_ctx


class TestDispatcherConstructsAgent:
    """Sprint 33 — inversion of construction.

    ``Dispatcher(agent_kwargs={...}).agent`` is the post-Sprint-33
    sole sanctioned Agent construction path. Verifies the new
    parameters thread cleanly, the conditional construction guard
    only fires when ``agent_kwargs`` is provided, the runtime_ctx
    injection path works alongside the fallback, and the back-
    reference for ``run_conversation`` is wired.
    """

    def test_no_agent_constructed_when_agent_kwargs_omitted(self):
        d = Dispatcher()
        assert d.agent is None

    def test_no_agent_constructed_when_agent_kwargs_explicit_none(self):
        d = Dispatcher(agent_kwargs=None)
        assert d.agent is None

    def test_agent_constructed_when_agent_kwargs_provided(self):
        from unittest.mock import patch
        agent_kwargs = dict(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            d = Dispatcher(agent_kwargs=agent_kwargs)
        from run_agent import AIAgent
        assert isinstance(d.agent, AIAgent)

    def test_constructed_agent_back_references_dispatcher(self):
        from unittest.mock import patch
        agent_kwargs = dict(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            d = Dispatcher(agent_kwargs=agent_kwargs)
        # The back-reference is what lets the Agent's
        # ``run_conversation()`` reach this Dispatcher after the
        # Phase 2 lazy-singleton deletion.
        assert d.agent._dispatcher_singleton is d

    def test_runtime_ctx_injection_path_uses_provided_context(self):
        # When runtime_ctx is provided, the Dispatcher uses it directly
        # instead of reading os.environ + load_config_safely().
        from grove.dispatcher import RuntimeContext
        injected = RuntimeContext(
            env={"GROVE_TEST_INJECTED_PROBE": "captured"},
            config={"injected": True},
        )
        d = Dispatcher(runtime_ctx=injected)
        assert d.runtime_ctx is injected
        assert d.runtime_ctx.env_get("GROVE_TEST_INJECTED_PROBE") == "captured"
        assert d.runtime_ctx.config == {"injected": True}

    def test_runtime_ctx_none_fallback_still_reads_env_and_config(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        # Sprint 34 removes this fallback; Sprint 33 preserves it.
        # When runtime_ctx is omitted, the Dispatcher's pre-Sprint-33
        # behavior holds — env + config are read at construction.
        monkeypatch.setenv("GROVE_TEST_FALLBACK_PROBE", "captured")
        d = Dispatcher()  # no runtime_ctx, no agent_kwargs
        assert d.runtime_ctx.env_get("GROVE_TEST_FALLBACK_PROBE") == "captured"

    def test_agent_kwargs_and_runtime_ctx_compose(self):
        # Both new parameters work together: Dispatcher receives the
        # explicit RuntimeContext AND constructs the Agent from the
        # forwarded kwargs.
        from unittest.mock import patch
        from grove.dispatcher import RuntimeContext
        injected = RuntimeContext(env={}, config={})
        agent_kwargs = dict(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            d = Dispatcher(runtime_ctx=injected, agent_kwargs=agent_kwargs)
        assert d.runtime_ctx is injected
        assert d.agent is not None
        assert d.agent._dispatcher_singleton is d


class TestDispatcherGracefulDegradation:
    def test_dispatcher_handles_load_config_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        # If hermes_cli.config.load_config raises, the Dispatcher must
        # still construct with an empty config snapshot rather than
        # propagating the exception. The runtime context remains usable
        # for env reads even when config is unavailable.
        import hermes_cli.config as hcfg

        def _boom() -> Dict:
            raise RuntimeError("config broken")

        monkeypatch.setattr(hcfg, "load_config", _boom)
        with caplog.at_level(logging.WARNING, logger="grove.dispatcher"):
            d = Dispatcher()
        assert d.runtime_ctx.config == {}
        assert any("load_config" in r.getMessage() for r in caplog.records)

    def test_dispatcher_handles_non_dict_config(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # If load_config returns something other than a dict (corrupt
        # YAML deserializing to a list, etc.), the Dispatcher coerces
        # to an empty dict rather than letting an unexpected type
        # propagate into the Agent.
        import hermes_cli.config as hcfg
        monkeypatch.setattr(hcfg, "load_config", lambda: ["not", "a", "dict"])
        d = Dispatcher()
        assert d.runtime_ctx.config == {}


# ── AIAgent integration with runtime_ctx ──────────────────────────────────


class TestAIAgentRuntimeCtxInjection:
    """Smoke tests confirming AIAgent's _env_or / _config_load_or helpers
    route through runtime_ctx.

    Sprint 34 made RuntimeContext mandatory; the substrate-fallback arms
    that the helpers held during Sprint 26-33 are gone. The remaining
    tests assert the only contract: helpers read from ``self._runtime_ctx``.

    These tests do NOT instantiate a full AIAgent (too heavy). They
    construct a bare AIAgent via ``object.__new__`` and set only the
    state the helpers read.
    """

    def _bare_agent(self, ctx):
        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        agent._runtime_ctx = ctx
        return agent

    def test_env_or_reads_from_runtime_ctx_when_injected(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        ctx = RuntimeContext(env={"GROVE_PROBE": "from-ctx"}, config={})
        agent = self._bare_agent(ctx)
        # Set env to a different value to prove ctx wins
        monkeypatch.setenv("GROVE_PROBE", "from-env")
        assert agent._env_or("GROVE_PROBE") == "from-ctx"

    def test_env_or_int_routes_through_ctx(self):
        ctx = RuntimeContext(env={"TIMEOUT": "1800"}, config={})
        agent = self._bare_agent(ctx)
        assert agent._env_or_int("TIMEOUT", 60) == 1800

    def test_env_or_float_routes_through_ctx(self):
        ctx = RuntimeContext(env={"TIMEOUT": "1800.5"}, config={})
        agent = self._bare_agent(ctx)
        assert agent._env_or_float("TIMEOUT", 60.0) == 1800.5

    def test_config_load_or_routes_through_ctx_when_injected(self):
        ctx = RuntimeContext(env={}, config={"injected": True})
        agent = self._bare_agent(ctx)
        assert agent._config_load_or() == {"injected": True}


# ── Phase 1b heavy-resource injection ─────────────────────────────────────


class TestRuntimeContextHeavyResourceSlots:
    """RuntimeContext gains optional fields for pre-built heavy resources
    (Sprint 26 Phase 1b). Each defaults to None / empty so existing
    callers that construct RuntimeContext bare still work."""

    def test_defaults_are_none_or_empty(self):
        ctx = RuntimeContext()
        assert ctx.tools is None
        assert ctx.memory_store is None
        assert ctx.context_length_by_model == {}
        assert ctx.anthropic_client is None
        assert ctx.openai_client is None
        assert ctx.compression_probe is None

    def test_fields_round_trip(self):
        probe = CompressionProbe(
            aux_model="qwen2.5:32b",
            aux_context=32768,
            aux_base_url="http://localhost:11434",
            aux_api_key="",
            aux_cfg_provider="ollama",
        )
        ctx = RuntimeContext(
            env={},
            config={},
            tools=[{"function": {"name": "t1"}}],
            memory_store=object(),
            context_length_by_model={"claude-sonnet-4-6": 200000},
            anthropic_client=object(),
            compression_probe=probe,
        )
        assert ctx.tools == [{"function": {"name": "t1"}}]
        assert ctx.context_length_by_model["claude-sonnet-4-6"] == 200000
        assert ctx.compression_probe is probe


class TestCompressionProbe:
    def test_compression_probe_is_frozen(self):
        probe = CompressionProbe(
            aux_model="m", aux_context=None, aux_base_url="",
            aux_api_key="", aux_cfg_provider="",
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            probe.aux_model = "other"  # type: ignore[misc]

    def test_compression_probe_holds_none_context(self):
        # When the underlying get_model_context_length returns None, the
        # probe still constructs and the Agent's downstream code handles
        # the None gracefully (existing MINIMUM_CONTEXT_LENGTH check).
        probe = CompressionProbe(
            aux_model="m", aux_context=None, aux_base_url="",
            aux_api_key="", aux_cfg_provider="",
        )
        assert probe.aux_context is None


class TestDispatcherHeavyResourceBuilders:
    def test_runtime_context_for_returns_runtime_context(self):
        d = Dispatcher()
        ctx = d.runtime_context_for(skip_memory=True, skip_tools=True,
                                    skip_compression_probe=True)
        assert isinstance(ctx, RuntimeContext)

    def test_runtime_context_for_inherits_env_and_config(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The per-call context shares the base substrate snapshot.
        monkeypatch.setenv("GROVE_TEST_INHERIT", "yes")
        d = Dispatcher()
        ctx = d.runtime_context_for(skip_memory=True, skip_tools=True,
                                    skip_compression_probe=True)
        assert ctx.env_get("GROVE_TEST_INHERIT") == "yes"

    def test_tools_cache_returns_same_list_on_repeat_call(self):
        d = Dispatcher()
        ctx1 = d.runtime_context_for(skip_memory=True, skip_compression_probe=True,
                                     enabled_toolsets=["clarify"])
        ctx2 = d.runtime_context_for(skip_memory=True, skip_compression_probe=True,
                                     enabled_toolsets=["clarify"])
        # Same toolset shape → same cached tools list instance.
        assert ctx1.tools is ctx2.tools

    def test_compression_probe_cache_returns_same_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # When the probe builders are stubbed to return a known result,
        # both calls return the SAME cached probe instance.
        from grove import dispatcher as dmod
        from agent import auxiliary_client as ac
        from agent import model_metadata as mm

        fake_client = type("FakeClient", (), {
            "base_url": "http://localhost:11434",
            "api_key": "",
        })()
        monkeypatch.setattr(
            ac, "get_text_auxiliary_client",
            lambda task, main_runtime: (fake_client, "qwen2.5:32b"),
        )
        monkeypatch.setattr(
            ac, "_resolve_task_provider_model",
            lambda task: ("ollama", None, None, None, None),
        )
        monkeypatch.setattr(mm, "get_model_context_length", lambda *a, **kw: 32768)

        d = Dispatcher()
        ctx1 = d.runtime_context_for(model="claude-sonnet-4-6", provider="anthropic",
                                     skip_memory=True, skip_tools=True)
        ctx2 = d.runtime_context_for(model="claude-sonnet-4-6", provider="anthropic",
                                     skip_memory=True, skip_tools=True)
        assert ctx1.compression_probe is not None
        assert ctx1.compression_probe.aux_model == "qwen2.5:32b"
        assert ctx1.compression_probe.aux_context == 32768
        # Same probe instance across calls (Dispatcher's cache hit)
        assert ctx1.compression_probe is ctx2.compression_probe

    def test_compression_probe_returns_none_when_no_client(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # When get_text_auxiliary_client returns (None, ""), the probe
        # returns None and the Agent falls back to legacy path.
        from agent import auxiliary_client as ac
        monkeypatch.setattr(
            ac, "get_text_auxiliary_client",
            lambda task, main_runtime: (None, ""),
        )
        d = Dispatcher()
        ctx = d.runtime_context_for(model="claude-sonnet-4-6", provider="anthropic",
                                    skip_memory=True, skip_tools=True)
        assert ctx.compression_probe is None

    def test_compression_probe_returns_none_when_imports_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Module-import-failure path returns None gracefully.
        import sys
        monkeypatch.setitem(sys.modules, "agent.auxiliary_client", None)
        d = Dispatcher()
        ctx = d.runtime_context_for(model="claude-sonnet-4-6", provider="anthropic",
                                    skip_memory=True, skip_tools=True)
        assert ctx.compression_probe is None

    def test_skip_compression_probe_flag(self):
        # skip_compression_probe=True bypasses the probe even when model is set.
        d = Dispatcher()
        ctx = d.runtime_context_for(model="claude-sonnet-4-6", provider="anthropic",
                                    skip_memory=True, skip_tools=True,
                                    skip_compression_probe=True)
        assert ctx.compression_probe is None


# ── Sprint 27 GATE-B: _classify_one_intent hierarchical-rule bridge ───────


class TestClassifyOneIntentHierarchicalBridge:
    """End-to-end verification of the Sprint 22 hierarchical-rule bridge
    at ``Dispatcher._classify_one_intent`` (dispatcher.py:1191-1196).

    For ``terminal`` and ``execute_code`` tool intents carrying a
    ``command`` argument, the bridge routes through
    ``grove.dispatch.classify_command`` so the tool's hierarchical
    ``tool_zones`` entry (default_zone + rules) fires. For all other
    tool intents, the bridge is skipped and the bare-string
    ``classify(action == tool_name)`` path runs.

    These tests exercise the repo's canonical ``config/zones.schema.yaml``
    (not the operator copy at ``~/.grove/``) so they remain stable across
    machines and reproduce the schema-as-checked-in behavior.
    """

    @pytest.fixture(autouse=True)
    def _initialize_classifier_from_repo_schema(self):
        from pathlib import Path
        from grove.zones import initialize
        repo_schema = (
            Path(__file__).resolve().parents[2] / "config" / "zones.schema.yaml"
        )
        initialize(schema_path=repo_schema)

    def test_gapi_workspace_command_classifies_green_via_hierarchical_rule(self):
        # The Sprint 27 GATE-A-prime root-cause finding: the corrected
        # GAPI regex (commit 6ef2a24e0) targets the actual install path
        # ``~/.grove/skills/productivity/google-workspace/``. A terminal
        # intent invoking a script under that path must land green via
        # the terminal hierarchical rule, not default-yellow.
        from grove import dispatch as _grove_dispatch
        from grove.dispatcher import Dispatcher
        from grove.intents import ToolIntent

        intent = ToolIntent(
            tool_name="terminal",
            arguments={
                "command": (
                    "GAPI=/tmp/key.json python3 "
                    "/Users/jimcalhoun/.grove/skills/productivity/"
                    "google-workspace/scripts/calendar_read.py"
                )
            },
        )
        result = Dispatcher._classify_one_intent(intent, _grove_dispatch)
        assert result.zone == "green"
        assert result.source.startswith("tool_zones.terminal.rules")
        assert "google-workspace" in result.matched_rule

    def test_sudo_command_classifies_red_via_hierarchical_rule(self):
        # The terminal hierarchical entry encodes sudo/su/doas as red
        # explicitly per the schema's "Privilege escalation: explicit RED"
        # block. The bridge must surface that rule, not fall through to
        # the generic action-prefix classifier.
        from grove import dispatch as _grove_dispatch
        from grove.dispatcher import Dispatcher
        from grove.intents import ToolIntent

        intent = ToolIntent(
            tool_name="terminal",
            arguments={"command": "sudo apt install vim"},
        )
        result = Dispatcher._classify_one_intent(intent, _grove_dispatch)
        assert result.zone == "red"
        assert result.source.startswith("tool_zones.terminal.rules")

    def test_unmatched_terminal_command_falls_to_default_zone_yellow(self):
        # A plain command that matches no hierarchical rule must take
        # the terminal entry's ``default_zone`` (yellow), not the bare
        # action-string lookup.
        from grove import dispatch as _grove_dispatch
        from grove.dispatcher import Dispatcher
        from grove.intents import ToolIntent

        intent = ToolIntent(
            tool_name="terminal",
            arguments={"command": "echo hello"},
        )
        result = Dispatcher._classify_one_intent(intent, _grove_dispatch)
        assert result.zone == "yellow"

    def test_execute_code_tool_routes_through_bridge(self):
        # The bridge predicate accepts both ``terminal`` and
        # ``execute_code`` per dispatcher.py:1191. execute_code has no
        # dedicated hierarchical entry, so the call falls through inside
        # ``classify_command_string`` to the bare-tool lookup which
        # honors sovereign patterns on the derived action identifier.
        from grove import dispatch as _grove_dispatch
        from grove.dispatcher import Dispatcher
        from grove.intents import ToolIntent

        intent = ToolIntent(
            tool_name="execute_code",
            arguments={"command": "sudo rm -rf /"},
        )
        result = Dispatcher._classify_one_intent(intent, _grove_dispatch)
        assert result.zone == "red"

    def test_non_terminal_tool_bypasses_bridge_and_uses_bare_action_classify(self):
        # browser_back is a flat green entry in tool_zones. The bridge
        # only fires for terminal/execute_code, so this intent must take
        # the generic ``classify(tool_name)`` path and resolve to green
        # via the bare-string entry.
        from grove import dispatch as _grove_dispatch
        from grove.dispatcher import Dispatcher
        from grove.intents import ToolIntent

        intent = ToolIntent(
            tool_name="browser_back",
            arguments={},
        )
        result = Dispatcher._classify_one_intent(intent, _grove_dispatch)
        assert result.zone == "green"
        assert "rules" not in result.source

    def test_terminal_without_command_arg_bypasses_bridge(self):
        # The bridge predicate requires ``args["command"]`` to be a
        # non-empty string. A terminal intent with no command (degenerate
        # but possible during model-side malformation) must NOT route
        # through classify_command_string — it should fall through to the
        # generic ``classify("terminal")`` path which resolves via the
        # bare-string seed of the terminal hierarchical entry's
        # default_zone (yellow).
        from grove import dispatch as _grove_dispatch
        from grove.dispatcher import Dispatcher
        from grove.intents import ToolIntent

        intent = ToolIntent(tool_name="terminal", arguments={})
        result = Dispatcher._classify_one_intent(intent, _grove_dispatch)
        assert result.zone == "yellow"


# ── Sprint 33 Phase 2: Dispatcher handler injection ──────────────────────


class TestDispatcherHandlerInjection:
    """The Dispatcher's ``sovereign_prompt_handler`` kwarg stores the
    handler that fires when an AndonHalt is raised during dispatch_turn.

    Sprint 27 originally tested this contract via the Agent's lazy
    singleton helper. Sprint 33 Phase 2 deleted that helper; the
    handler injection now happens at Dispatcher construction
    directly — via either
    ``Dispatcher(sovereign_prompt_handler=h)`` or
    ``Dispatcher(agent_kwargs={..., 'sovereign_prompt_handler': h})``
    when the Agent is constructed alongside.
    """

    def test_injected_handler_stored_on_dispatcher(self):
        from grove.sovereign_prompt_handlers import gateway_auto_skip_handler

        d = Dispatcher(sovereign_prompt_handler=gateway_auto_skip_handler)
        assert d._sovereign_prompt_handler is gateway_auto_skip_handler

    def test_default_handler_used_when_none_injected(self):
        # When no handler is provided, the Dispatcher falls back to the
        # TTY ``_default_sovereign_prompt`` so interactive Sovereign
        # Prompts still work for CLI / oneshot callers.
        from grove.dispatcher import _default_sovereign_prompt

        d = Dispatcher()
        assert d._sovereign_prompt_handler is _default_sovereign_prompt

    def test_separate_dispatchers_hold_distinct_handlers(self):
        # Pre-Sprint-33 the per-Agent singleton pattern made this
        # implicit; post-Sprint-33 each Dispatcher instance is an
        # independent object, so the property is structural.
        from grove.sovereign_prompt_handlers import (
            batch_auto_skip_handler,
            gateway_auto_skip_handler,
        )

        batch_disp = Dispatcher(sovereign_prompt_handler=batch_auto_skip_handler)
        gateway_disp = Dispatcher(sovereign_prompt_handler=gateway_auto_skip_handler)
        assert batch_disp is not gateway_disp
        assert batch_disp._sovereign_prompt_handler is batch_auto_skip_handler
        assert gateway_disp._sovereign_prompt_handler is gateway_auto_skip_handler

    def test_handler_threads_through_agent_kwargs_construction(self):
        # The full inversion-of-construction path: the caller passes
        # the handler as a Dispatcher kwarg and the Agent's
        # ``_sovereign_prompt_handler`` field reflects the same
        # callable via the back-reference for in-Agent code paths
        # that read it (e.g., the inline lazy build inside
        # run_conversation when an Agent was constructed without
        # going through the Dispatcher).
        from unittest.mock import patch
        from grove.sovereign_prompt_handlers import batch_auto_skip_handler

        agent_kwargs = dict(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            sovereign_prompt_handler=batch_auto_skip_handler,
        )
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            d = Dispatcher(
                sovereign_prompt_handler=batch_auto_skip_handler,
                agent_kwargs=agent_kwargs,
            )
        assert d._sovereign_prompt_handler is batch_auto_skip_handler
        assert d.agent._sovereign_prompt_handler is batch_auto_skip_handler
        assert d.agent._dispatcher_singleton is d

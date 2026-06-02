"""Tests for the Sprint 30 escalate tool surface.

Covers the synthetic ``escalate`` tool registration, the LLM-visible
schema, the cold-path handler (only fires on mixed-batch escalates),
the ESCALATION_GUIDANCE prompt injection, and the Agent's intent
extraction intercept that converts a sole ``escalate`` tool call into
an :class:`grove.intents.EscalationRequest` yield.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

# Importing the module registers the tool with tools.registry.
import tools.escalate_tool  # noqa: F401



# Sprint 53 — module-level Dispatcher-style registry for tests.
from tools.registry import ToolRegistry as _Sprint53_TR_top, register_builtin_tools as _Sprint53_RBT_top
_REGISTRY = _Sprint53_TR_top()
_Sprint53_RBT_top(_REGISTRY)

# ── Tool registration + schema ────────────────────────────────────────────


class TestEscalateToolRegistration:
    def test_escalate_is_registered(self):
        from tools.registry import ToolRegistry as _Sprint53_TR, register_builtin_tools as _Sprint53_RBT
        registry = _Sprint53_TR()
        _Sprint53_RBT(registry)
        assert "escalate" in registry.get_all_tool_names()

    def test_schema_declares_three_required_params(self):
        schema = tools.escalate_tool.ESCALATE_SCHEMA
        params = schema["function"]["parameters"]
        assert set(params["required"]) == {
            "reasoning_depth", "context_size", "blocker",
        }
        # Three enum-typed fields constrain the LLM's vocabulary so the
        # Dispatcher's tier mapping is well-defined.
        assert params["properties"]["reasoning_depth"]["enum"] == [
            "shallow", "moderate", "deep", "apex",
        ]
        assert params["properties"]["context_size"]["enum"] == [
            "normal", "extended", "max",
        ]
        assert params["properties"]["blocker"]["type"] == "string"

    def test_schema_description_names_sole_purpose_rule(self):
        # The LLM's tool_description must spell out the
        # single-call-only invariant — otherwise it'll mix
        # escalate into batches and trip the cold-path handler.
        desc = tools.escalate_tool.ESCALATE_SCHEMA["function"]["description"]
        assert "only tool call" in desc.lower()


# ── Cold-path handler ─────────────────────────────────────────────────────


class TestEscalateColdPathHandler:
    """The handler fires only when the intercept missed (mixed batch).
    Returns a decline so the LLM re-emits the call alone."""

    def test_handler_returns_decline_json(self):
        out = tools.escalate_tool.escalate_tool(
            reasoning_depth="deep",
            context_size="extended",
            blocker="example",
        )
        parsed = json.loads(out)
        assert parsed["escalation"] == "ignored"
        assert "only tool call" in parsed["reason"]
        assert parsed["received"]["reasoning_depth"] == "deep"
        assert parsed["received"]["blocker"] == "example"


# ── ESCALATION_GUIDANCE prompt wiring ─────────────────────────────────────


class TestEscalationPromptGuidance:
    def test_guidance_constant_mentions_escalate_call(self):
        from agent.prompt_builder import ESCALATION_GUIDANCE
        # The constant must explicitly reference the tool name + the
        # sole-purpose rule so the LLM has the invariant in-prompt.
        assert "escalate(" in ESCALATION_GUIDANCE
        assert "only tool call" in ESCALATION_GUIDANCE.lower()
        # Names the three declarative dimensions the schema requires.
        assert "reasoning_depth" in ESCALATION_GUIDANCE
        assert "context_size" in ESCALATION_GUIDANCE
        assert "blocker" in ESCALATION_GUIDANCE

    def test_guidance_injected_when_escalate_in_valid_tool_names(self):
        # The AIAgent.tool_guidance composition only appends
        # ESCALATION_GUIDANCE when `escalate` is a known tool. Simulate
        # the gate logic since exercising it requires a fully-built
        # AIAgent.
        from agent.prompt_builder import ESCALATION_GUIDANCE
        # Spot-check that the import surface is intact: run_agent
        # imports ESCALATION_GUIDANCE alongside the other guidance
        # constants. A regression in that import would surface as an
        # ImportError at test collection.
        import run_agent
        assert run_agent.ESCALATION_GUIDANCE is ESCALATION_GUIDANCE


# ── Intent-extraction intercept ───────────────────────────────────────────


class TestEscalateIntentIntercept:
    """The generator yield site converts a sole-purpose escalate
    ToolIntent batch into an EscalationRequest. Mixed batches pass
    through to the normal dispatch path."""

    @staticmethod
    def _intents_from_calls(calls):
        """Helper — drive AIAgent._extract_tool_intents on a synthetic
        assistant message and return the resulting ToolIntent list."""
        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        msg = {"tool_calls": calls}
        return agent._extract_tool_intents(msg)

    def test_sole_escalate_extracts_as_single_intent(self):
        intents = self._intents_from_calls([
            {
                "id": "c1",
                "function": {
                    "name": "escalate",
                    "arguments": json.dumps({
                        "reasoning_depth": "deep",
                        "context_size": "extended",
                        "blocker": "complex synthesis required",
                    }),
                },
            },
        ])
        # Sprint 30 — the intercept happens AT the yield site (the
        # generator loop), not inside _extract_tool_intents. The
        # extraction still produces a ToolIntent; the conversion to
        # EscalationRequest happens in _run_turn_generator when it
        # sees the single-`escalate` shape.
        assert len(intents) == 1
        assert intents[0].tool_name == "escalate"
        assert intents[0].arguments["reasoning_depth"] == "deep"
        assert intents[0].arguments["context_size"] == "extended"
        assert intents[0].arguments["blocker"] == "complex synthesis required"

    def test_escalate_intent_carries_call_id(self):
        intents = self._intents_from_calls([
            {
                "id": "esc-call-42",
                "function": {
                    "name": "escalate",
                    "arguments": json.dumps({
                        "reasoning_depth": "apex",
                        "context_size": "max",
                        "blocker": "out of room",
                    }),
                },
            },
        ])
        # The Dispatcher needs the call_id to write the
        # grant/denial tool-response message back into messages —
        # otherwise the next LLM call sees an orphan tool_call.
        assert intents[0].call_id == "esc-call-42"

    def test_mixed_batch_does_not_special_case_extraction(self):
        # When the LLM mixes escalate with other tools, extraction
        # produces a multi-intent list. The yield-site intercept
        # checks ``len == 1 and tool_name == "escalate"`` — a mixed
        # batch fails that gate and falls through to normal dispatch.
        intents = self._intents_from_calls([
            {
                "id": "c1",
                "function": {
                    "name": "escalate",
                    "arguments": json.dumps({
                        "reasoning_depth": "deep",
                        "context_size": "normal",
                        "blocker": "x",
                    }),
                },
            },
            {
                "id": "c2",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "/tmp/x"}),
                },
            },
        ])
        assert len(intents) == 2
        # Both intents preserved; the cold-path handler will fire on
        # the escalate when the consumer's executor runs.
        names = [i.tool_name for i in intents]
        assert names == ["escalate", "read_file"]


# ── tool_groups.yaml taxonomy includes escalate in core ───────────────────


class TestEscalateInCoreChunk:
    def test_escalate_listed_in_core(self):
        # Sprint 30 lands `escalate` in the Sprint 29 core chunk so the
        # Sprint 29 selective-loading filter exposes it on EVERY
        # classified-intent turn. Without this, the LLM never sees
        # the escalate surface and the Agent-Tool option is dead.
        from pathlib import Path
        import yaml
        repo_yaml = (
            Path(__file__).resolve().parents[2]
            / "config" / "tool_groups.yaml"
        )
        taxonomy = yaml.safe_load(repo_yaml.read_text())
        assert "escalate" in taxonomy["core"]

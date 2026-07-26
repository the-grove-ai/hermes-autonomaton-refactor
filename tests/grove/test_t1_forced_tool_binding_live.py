"""GROVE_LIVE_TESTS-gated evidence that the T1 binding does forced-tool
structured output (researcher-retrieval-broker-v1 CO-1).

This exists so model-catalog.yaml's "reliable tool calling" note for the T1
model has evidence behind it instead of standing as a catalog claim alone
(M3's gap). It exercises the SAME call the broker's formulation makes.

Run:  GROVE_LIVE_TESTS=1 pytest tests/grove/test_t1_forced_tool_binding_live.py -q

NOT a latency assertion — latency is environmental (provider/routing) and would
flake. It asserts the binding RETURNS a forced tool call whose arguments parse
to the formulation schema.
"""

from __future__ import annotations

import json
import os

import pytest

LIVE = os.environ.get("GROVE_LIVE_TESTS") == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="live-only: set GROVE_LIVE_TESTS=1")


def test_t1_binding_returns_forced_tool_structured_output():
    from openai import OpenAI

    from grove.fleet.retrieval_broker import (
        MAX_QUERIES,
        QUERY_FORMULATION_MAX_TOKENS,
        QUERY_FORMULATION_TIER,
        _QUERY_SYSTEM,
        _QUERY_TOOL,
    )
    from grove.providers import openrouter_provider_pref
    from grove.t1_call import _resolve_t1_runtime, _to_openai_tool

    rt, _tc = _resolve_t1_runtime(QUERY_FORMULATION_TIER)
    client = OpenAI(api_key=rt.get("api_key") or "", base_url=rt.get("base_url") or None)
    kwargs = {
        "model": rt["model"],
        "max_tokens": QUERY_FORMULATION_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": _QUERY_SYSTEM},
            {
                "role": "user",
                "content": "Research topic: renewable energy grid storage\n"
                "Operator intent: {}\n\nPropose at most 3 search query strings for this topic.",
            },
        ],
        "tools": [_to_openai_tool(_QUERY_TOOL)],
        "tool_choice": {"type": "function", "function": {"name": _QUERY_TOOL["name"]}},
    }
    pp = openrouter_provider_pref(rt)
    if pp:
        kwargs["extra_body"] = {"provider": pp}

    resp = client.chat.completions.create(**kwargs)

    # The binding must FORCE the tool call (not emit free text / preamble).
    assert resp.choices[0].finish_reason == "tool_calls", resp.choices[0].finish_reason
    tool_calls = resp.choices[0].message.tool_calls
    assert tool_calls and tool_calls[0].function.name == _QUERY_TOOL["name"]

    # Its arguments must parse as the formulation schema: {"queries": [str, ...]}.
    args = json.loads(tool_calls[0].function.arguments)
    assert isinstance(args, dict) and "queries" in args
    assert isinstance(args["queries"], list) and args["queries"]
    assert all(isinstance(q, str) and q.strip() for q in args["queries"])
    assert len(args["queries"]) <= MAX_QUERIES

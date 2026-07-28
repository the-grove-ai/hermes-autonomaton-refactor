"""GROVE_LIVE_TESTS-gated evidence that the T2/T-QA and T3 bindings do
forced-tool structured output (instance-cold-start-parity-v1 P5).

Mirrors tests/grove/test_t1_forced_tool_binding_live.py exactly — the T1
precedent that put evidence behind model-catalog.yaml's "reliable tool calling"
note. These extend the same discipline to the two slugs whose tier-description
claims stood unevidenced:

  * z-ai/glm-5.2            — bound at T2 and T-QA (probe 2026-07-28)
  * google/gemini-3.6-flash — bound at T3        (probe 2026-07-28)

Each test forces ``tool_choice`` on a minimal tool schema and asserts the
binding RETURNS a wellformed tool call whose arguments parse. Like the
precedent, it is SINGLE-SHOT: a green call evidences the binding's CAPABILITY,
NOT its reliability — this is not latency or flake statistics.

Run:  GROVE_LIVE_TESTS=1 pytest tests/grove/test_tier_binding_evidence_live.py -q
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _load_user_env() -> None:
    """Load ``~/.grove/.env`` so live runs pick up ``OPENROUTER_API_KEY`` without
    the runner shell-sourcing it first (mirrors tests/run_agent/
    test_sequential_chats_live.py). Silent if absent; never clobbers an already-set
    var. Keeps the key in the walled secret file, out of the runner command line."""
    env_file = Path.home() / ".grove" / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        # Skip empty-valued lines: a bare ``KEY=`` placeholder must not shadow a
        # real assignment for the same key later in the file (setdefault locks in
        # the first value seen).
        if v:
            os.environ.setdefault(k.strip(), v)


_load_user_env()

LIVE = os.environ.get("GROVE_LIVE_TESTS") == "1"
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
pytestmark = [
    pytest.mark.skipif(not LIVE, reason="live-only: set GROVE_LIVE_TESTS=1"),
    pytest.mark.skipif(not OR_KEY, reason="OPENROUTER_API_KEY not configured"),
]

# A minimal forced-tool schema — one required string field. Deliberately not the
# broker's formulation tool: this evidences generic forced-tool capability, not a
# subsystem-specific contract.
_MINI_TOOL = {
    "name": "record_capital",
    "description": "Record the capital city of a country.",
    "input_schema": {
        "type": "object",
        "properties": {
            "capital": {"type": "string", "description": "The capital city name."},
        },
        "required": ["capital"],
    },
}


def _forced_tool_call(model: str, creds_tier: str):
    """One forced-tool call against ``model`` over the OpenRouter credentials the
    ``creds_tier`` binding resolves (same resolution path the precedent uses).
    Returns the raw response for the caller to assert on."""
    # Re-load the key HERE: tests/conftest.py's autouse ``_hermetic_environment``
    # strips every ``*_API_KEY`` from the env per-test (unit-test isolation) — it
    # runs AFTER the module-import load, deleting the key before this call. A
    # GROVE_LIVE_TESTS-gated test opts INTO the real credential, so we repopulate
    # it from the walled ~/.grove/.env; the next test's fixture re-strips it.
    _load_user_env()
    from openai import OpenAI

    from grove.providers import openrouter_provider_pref
    from grove.t1_call import _resolve_t1_runtime, _to_openai_tool

    rt, _tc = _resolve_t1_runtime(creds_tier)
    client = OpenAI(api_key=rt.get("api_key") or "", base_url=rt.get("base_url") or None)
    kwargs = {
        "model": model,
        "max_tokens": 256,
        "messages": [
            {
                "role": "system",
                "content": "You are a precise assistant. Answer only by calling the provided tool.",
            },
            {"role": "user", "content": "Record the capital city of France."},
        ],
        "tools": [_to_openai_tool(_MINI_TOOL)],
        "tool_choice": {"type": "function", "function": {"name": _MINI_TOOL["name"]}},
    }
    pp = openrouter_provider_pref(rt)
    if pp:
        kwargs["extra_body"] = {"provider": pp}
    return client.chat.completions.create(**kwargs)


def _assert_wellformed_tool_call(resp):
    """The binding must FORCE a wellformed ``record_capital`` call whose arguments
    parse to the schema — not free text, not a preamble."""
    assert resp.choices[0].finish_reason == "tool_calls", resp.choices[0].finish_reason
    tool_calls = resp.choices[0].message.tool_calls
    assert tool_calls and tool_calls[0].function.name == _MINI_TOOL["name"]
    args = json.loads(tool_calls[0].function.arguments)
    assert isinstance(args, dict) and "capital" in args
    assert isinstance(args["capital"], str) and args["capital"].strip()


def test_t2_tqa_glm_binding_returns_forced_tool_structured_output():
    # z-ai/glm-5.2 — the T2 daily-driver and T-QA judge binding.
    resp = _forced_tool_call("z-ai/glm-5.2", creds_tier="T2")
    _assert_wellformed_tool_call(resp)


def test_t3_gemini_binding_returns_forced_tool_structured_output():
    # google/gemini-3.6-flash — the T3 binding (shipped default).
    resp = _forced_tool_call("google/gemini-3.6-flash", creds_tier="T3")
    _assert_wellformed_tool_call(resp)

"""kaizen-synthesizer-provider-agnostic-v1 — behavioral pin.

Runs the REAL synthesis pass end-to-end per api_mode arm: a fake detector
supplies the candidate, the transport client is stubbed at its constructor
seam, and ``stage_proposal`` writes unmocked into the per-test GROVE_HOME
(the tests/grove/test_flywheel_full_loop.py shape). What the pin holds: each
arm carries a pattern from detection through free-text synthesis, the
forced-tool self-review verdict, and a real proposal-queue append — and the
verdict's tool arguments, not free text, are what the consumer judges.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace


_SKILL_MD = (
    "---\n"
    "name: prep-meeting-brief\n"
    "description: Pull a GitHub repo's recent activity before a meeting.\n"
    "---\n"
    "## When to use\n"
    "Before a meeting about {repo}.\n\n"
    "## Procedure\n"
    "1. Fetch {repo} activity.\n"
    "2. Summarize it.\n"
)

_VERDICT_OK = {
    "coherent": True, "parametrized": True, "safe": True, "reason": "ok",
}

_CANDIDATE = {
    "tool_sequence": ("github_fetch", "summarize"),
    "evidence_turns": ["s1#1", "s2#1"],
    "prompts": ["check the acme/widgets repo before my standup"],
}


class _AnthropicStub:
    """``messages.create`` stand-in: free text → SKILL.md; forced tool →
    verdict tool_use block. Records every call's kwargs."""

    def __init__(self, calls, verdict=None):
        self.calls = calls
        self._verdict = dict(verdict or _VERDICT_OK)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if "tools" in kwargs:
            block = SimpleNamespace(
                type="tool_use",
                name=kwargs["tool_choice"]["name"],
                input=dict(self._verdict),
            )
            return SimpleNamespace(content=[block])
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=_SKILL_MD)],
        )


class _OpenAIStub:
    """``chat.completions.create`` stand-in, mirror of _AnthropicStub."""

    def __init__(self, calls, verdict=None):
        self.calls = calls
        self._verdict = dict(verdict or _VERDICT_OK)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if "tools" in kwargs:
            fn = SimpleNamespace(
                name=kwargs["tool_choice"]["function"]["name"],
                arguments=json.dumps(self._verdict),
            )
            message = SimpleNamespace(
                tool_calls=[SimpleNamespace(function=fn)], content=None,
            )
        else:
            message = SimpleNamespace(tool_calls=None, content=_SKILL_MD)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _fake_detector():
    return SimpleNamespace(
        detect_skill_candidates=lambda **kw: [dict(_CANDIDATE)],
    )


def _run_pass_and_assert_staged(monkeypatch, runtime, calls):
    """Shared arm assertion: pass stages 1 proposal via the real queue."""
    import grove.kaizen.synthesizer as syn
    from grove.eval.proposal_queue import (
        PROPOSAL_TYPE_SKILL_SYNTHESIS, read_all,
    )

    monkeypatch.setattr(syn, "_resolve_t3_runtime", lambda: runtime)
    staged = syn.run_synthesis_pass(detector=_fake_detector())
    assert staged == 1

    queued = read_all()
    assert len(queued) == 1
    assert queued[0].type == PROPOSAL_TYPE_SKILL_SYNTHESIS
    # synthesize_skill_md strips the draft before staging (synthesizer.py).
    assert queued[0].payload["skill_md"] == _SKILL_MD.strip()
    assert queued[0].payload["skill_name"] == "prep-meeting-brief"
    # The append is a REAL file in the per-test GROVE_HOME, not a mock.
    assert (Path(os.environ["GROVE_HOME"]) / "proposals.jsonl").exists()

    # Two T3 calls: free-text synthesis (2048), forced-tool review (1024,
    # kaizen-review-cap-guard-v1 — 512 sat inside the observed reasoning
    # consumption range and cap-cut intermittently).
    assert len(calls) == 2
    assert "tools" not in calls[0]
    assert calls[0]["max_tokens"] == 2048
    assert calls[1]["max_tokens"] == 1024


def test_chat_completions_arm_stages_end_to_end(monkeypatch):
    calls = []
    monkeypatch.setattr("openai.OpenAI", lambda **kw: _OpenAIStub(calls))
    runtime = {
        "api_mode": "chat_completions", "model": "test/apex",
        "api_key": "k", "base_url": "https://example.invalid/v1",
    }
    _run_pass_and_assert_staged(monkeypatch, runtime, calls)
    assert calls[1]["tool_choice"] == {
        "type": "function", "function": {"name": "skill_review_verdict"},
    }
    # Anthropic tool shape reshaped into the OpenAI function envelope.
    assert calls[1]["tools"][0]["function"]["parameters"]["required"] == [
        "coherent", "parametrized", "safe", "reason",
    ]


def test_anthropic_messages_arm_stages_end_to_end(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client",
        lambda **kw: _AnthropicStub(calls),
    )
    runtime = {
        "api_mode": "anthropic_messages", "model": "claude-test",
        "api_key": "k", "base_url": None,
    }
    _run_pass_and_assert_staged(monkeypatch, runtime, calls)
    assert calls[1]["tool_choice"] == {
        "type": "tool", "name": "skill_review_verdict",
    }


def test_forced_tool_verdict_args_drive_the_consumer(monkeypatch):
    """A rejecting verdict returned as tool ARGUMENTS (not free text) must
    surface its reason through validate_skill_md's axis checks."""
    from grove.kaizen.synthesizer import validate_skill_md

    calls = []
    verdict = {
        "coherent": True, "parametrized": False, "safe": True,
        "reason": "hard-coded to one operator's repo",
    }
    monkeypatch.setattr(
        "openai.OpenAI", lambda **kw: _OpenAIStub(calls, verdict=verdict),
    )
    runtime = {
        "api_mode": "chat_completions", "model": "test/apex",
        "api_key": "k", "base_url": "https://example.invalid/v1",
    }
    ok, reason = validate_skill_md(_SKILL_MD, runtime=runtime)
    assert ok is False
    assert reason == "hard-coded to one operator's repo"
    # The review reached the transport as a forced tool call.
    assert calls and calls[-1]["tool_choice"]["function"]["name"] == (
        "skill_review_verdict"
    )


def test_unsupported_api_mode_is_warning_plus_none(caplog):
    """The best-effort floor: an api_mode neither arm speaks returns None
    with a WARNING — never a raise into the background daemon."""
    import logging

    from grove.kaizen.synthesizer import _t3_call

    with caplog.at_level(logging.WARNING, logger="grove.kaizen.synthesizer"):
        out = _t3_call(
            {"api_mode": "bedrock_converse", "model": "m"},
            "system", "user", max_tokens=16,
        )
    assert out is None
    assert any("T3 call failed" in r.message for r in caplog.records)


# ── kaizen-review-cap-guard-v1 — truncation-ladder pins ──────────────────
#
# Scripted stubs: each create() pops the next canned response, so a
# length-flagged first attempt followed by a clean second one exercises the
# ONE-retry-at-2x ladder inside _t3_call without a live transport.


class _ScriptedOpenAI:
    def __init__(self, calls, script):
        self.calls = calls
        self._script = list(script)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._script.pop(0)


class _ScriptedAnthropic:
    def __init__(self, calls, script):
        self.calls = calls
        self._script = list(script)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._script.pop(0)


def _oa_text(content, finish="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish, native_finish_reason=finish,
        message=SimpleNamespace(tool_calls=None, content=content),
    )])


def _oa_tool(args, finish="stop", name="skill_review_verdict", no_calls=False):
    if no_calls:
        message = SimpleNamespace(tool_calls=None, content=None)
    else:
        fn = SimpleNamespace(name=name, arguments=json.dumps(args))
        message = SimpleNamespace(
            tool_calls=[SimpleNamespace(function=fn)], content=None,
        )
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish, native_finish_reason=finish, message=message,
    )])


def _an_text(text, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
    )


def _an_tool(args, stop_reason="end_turn", name="skill_review_verdict",
             empty=False):
    content = [] if empty else [
        SimpleNamespace(type="tool_use", name=name, input=dict(args)),
    ]
    return SimpleNamespace(stop_reason=stop_reason, content=content)


_CHAT_RT = {
    "api_mode": "chat_completions", "model": "test/apex",
    "api_key": "k", "base_url": "https://example.invalid/v1",
}
_ANTH_RT = {
    "api_mode": "anthropic_messages", "model": "claude-test",
    "api_key": "k", "base_url": None,
}


def _ladder_warnings(caplog):
    return sum(
        "truncated at max_tokens" in r.message for r in caplog.records
    )


def test_chat_free_text_ladder_retries_once_at_2x(monkeypatch, caplog):
    import logging

    from grove.kaizen.synthesizer import _t3_call

    calls = []
    stub = _ScriptedOpenAI(calls, [
        _oa_text("## cut-short draft", finish="length"),
        _oa_text("full draft"),
    ])
    monkeypatch.setattr("openai.OpenAI", lambda **kw: stub)
    with caplog.at_level(logging.WARNING, logger="grove.kaizen.synthesizer"):
        out = _t3_call(_CHAT_RT, "s", "u", max_tokens=100)
    assert out == "full draft"
    assert [c["max_tokens"] for c in calls] == [100, 200]
    assert _ladder_warnings(caplog) == 1


def test_chat_forced_tool_ladder_pre_tool_cut(monkeypatch, caplog):
    import logging

    import grove.kaizen.synthesizer as syn

    calls = []
    stub = _ScriptedOpenAI(calls, [
        _oa_tool(None, finish="length", no_calls=True),  # run-1's shape
        _oa_tool(_VERDICT_OK),
    ])
    monkeypatch.setattr("openai.OpenAI", lambda **kw: stub)
    with caplog.at_level(logging.WARNING, logger="grove.kaizen.synthesizer"):
        out = syn._t3_call(
            _CHAT_RT, "s", "u", max_tokens=100, tool=syn._REVIEW_VERDICT_TOOL,
        )
    assert out == _VERDICT_OK
    assert [c["max_tokens"] for c in calls] == [100, 200]
    assert _ladder_warnings(caplog) == 1


def test_anthropic_free_text_ladder(monkeypatch, caplog):
    import logging

    from grove.kaizen.synthesizer import _t3_call

    calls = []
    stub = _ScriptedAnthropic(calls, [
        _an_text("cut", stop_reason="max_tokens"),
        _an_text("full draft"),
    ])
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client", lambda **kw: stub,
    )
    with caplog.at_level(logging.WARNING, logger="grove.kaizen.synthesizer"):
        out = _t3_call(_ANTH_RT, "s", "u", max_tokens=100)
    assert out == "full draft"
    assert [c["max_tokens"] for c in calls] == [100, 200]
    assert _ladder_warnings(caplog) == 1


def test_anthropic_forced_tool_ladder(monkeypatch, caplog):
    import logging

    import grove.kaizen.synthesizer as syn

    calls = []
    stub = _ScriptedAnthropic(calls, [
        _an_tool(None, stop_reason="max_tokens", empty=True),  # pre-block cut
        _an_tool(_VERDICT_OK),
    ])
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client", lambda **kw: stub,
    )
    with caplog.at_level(logging.WARNING, logger="grove.kaizen.synthesizer"):
        out = syn._t3_call(
            _ANTH_RT, "s", "u", max_tokens=100, tool=syn._REVIEW_VERDICT_TOOL,
        )
    assert out == _VERDICT_OK
    assert [c["max_tokens"] for c in calls] == [100, 200]
    assert _ladder_warnings(caplog) == 1


def test_second_truncation_is_none_never_raises(monkeypatch, caplog):
    """Two consecutive cap-hits: one ladder WARNING, one terminal WARNING,
    None returned — the best-effort boundary holds."""
    import logging

    from grove.kaizen.synthesizer import _t3_call

    calls = []
    stub = _ScriptedOpenAI(calls, [
        _oa_text("cut once", finish="length"),
        _oa_text("cut twice", finish="length"),
    ])
    monkeypatch.setattr("openai.OpenAI", lambda **kw: stub)
    with caplog.at_level(logging.WARNING, logger="grove.kaizen.synthesizer"):
        out = _t3_call(_CHAT_RT, "s", "u", max_tokens=100)
    assert out is None
    assert [c["max_tokens"] for c in calls] == [100, 200]
    assert _ladder_warnings(caplog) == 1
    assert sum("T3 call failed" in r.message for r in caplog.records) == 1


def test_truncated_free_text_never_reaches_strip(monkeypatch):
    """The draft silent class, loud: cap-cut content must not flow into
    _strip_code_fence as a plausible SKILL.md."""
    import grove.kaizen.synthesizer as syn

    seen = []
    real = syn._strip_code_fence
    monkeypatch.setattr(
        syn, "_strip_code_fence", lambda t: (seen.append(t), real(t))[1],
    )
    stub = _ScriptedOpenAI([], [
        _oa_text(_SKILL_MD, finish="length"),  # valid-looking but cap-cut
        _oa_text(_SKILL_MD, finish="length"),
    ])
    monkeypatch.setattr("openai.OpenAI", lambda **kw: stub)
    out = syn.synthesize_skill_md(dict(_CANDIDATE), runtime=_CHAT_RT)
    assert out is None
    assert seen == []


def test_parsed_valid_but_length_args_refused(monkeypatch):
    """Verdict args that parse cleanly but arrived cap-cut never reach the
    validate_skill_md consumer."""
    from grove.kaizen.synthesizer import validate_skill_md

    stub = _ScriptedOpenAI([], [
        _oa_tool(_VERDICT_OK, finish="length"),
        _oa_tool(_VERDICT_OK, finish="length"),
    ])
    monkeypatch.setattr("openai.OpenAI", lambda **kw: stub)
    ok, reason = validate_skill_md(_SKILL_MD, runtime=_CHAT_RT)
    assert ok is False
    assert reason == "T3 self-review call failed."

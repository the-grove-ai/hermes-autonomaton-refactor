"""Sprint 67 (kaizen-governance-parity-v1) — Gap 3: clarify on the web surface.

The clarify tool requires a callback to ask the operator a question. CLI
and Telegram inject blocking callbacks; the web /v1/chat/completions
surface had none, so clarify returned "not available". Gap 3 injects a
store-and-resume clarify callback: the first call raises
OperatorInputRequired (the question becomes the turn's response); the
operator's next message seeds the answer; the replay returns it.

The load-bearing pieces are the two tool-boundary passthrough guards —
without them the OperatorInputRequired raised by the callback would be
swallowed (clarify_tool's generic ``except Exception`` → error JSON;
the executor's per-tool catch → "Error executing tool" observation).
"""

from __future__ import annotations

import json
import time

import pytest

from grove.operator_input import (
    OperatorInputRequired,
    PendingOperatorRequest,
    clarify_answer_key,
    TIMEOUT_SECONDS,
)
from gateway.platforms.api_server import APIServerAdapter


class _FakeDB:
    def __init__(self):
        self.store = {}

    def get_meta(self, key):
        return self.store.get(key)

    def set_meta(self, key, value):
        self.store[key] = value


def _adapter(fake):
    a = object.__new__(APIServerAdapter)
    a._ensure_session_db = lambda: fake  # type: ignore[attr-assign]
    return a


# ── tool-boundary passthrough (the actual Gap 3 fix) ─────────────────


class TestClarifyToolPassthrough:
    def test_operator_input_required_propagates(self):
        """clarify_tool MUST NOT swallow OperatorInputRequired into an
        error JSON — it has to reach the surface's terminal catch."""
        from tools.clarify_tool import clarify_tool

        def _cb(question, choices):
            raise OperatorInputRequired(PendingOperatorRequest(
                kind="clarify", prompt_text=question,
                original_user_message="m", created_at=0.0, timeout_at=0.0,
            ))

        with pytest.raises(OperatorInputRequired):
            clarify_tool("Which environment?", None, callback=_cb)

    def test_ordinary_callback_error_still_becomes_error_json(self):
        """The passthrough must be surgical: a real callback failure is
        still caught and reported as an error, not propagated."""
        from tools.clarify_tool import clarify_tool

        def _cb(question, choices):
            raise RuntimeError("display backend exploded")

        result = json.loads(clarify_tool("Q?", None, callback=_cb))
        assert "error" in result
        assert "display backend exploded" in result["error"]


# ── web clarify callback (raise + replay) ────────────────────────────


class TestWebClarifyCallback:
    def test_first_call_raises_with_question(self):
        adapter = _adapter(_FakeDB())
        cb = adapter._make_web_clarify_callback("s1", "deploy the service")
        with pytest.raises(OperatorInputRequired) as ei:
            cb("Which environment?", ["dev", "prod"])
        pending = ei.value.pending
        assert pending.kind == "clarify"
        assert pending.question == "Which environment?"
        assert pending.choices == ["dev", "prod"]
        assert pending.original_user_message == "deploy the service"
        assert pending.timeout_at - pending.created_at == TIMEOUT_SECONDS
        # Numbered list rendered for choice-based questions.
        assert "1. dev" in pending.prompt_text and "2. prod" in pending.prompt_text

    def test_open_ended_question_has_no_numbered_list(self):
        adapter = _adapter(_FakeDB())
        cb = adapter._make_web_clarify_callback("s1", "msg")
        with pytest.raises(OperatorInputRequired) as ei:
            cb("What should I name it?", None)
        assert ei.value.pending.prompt_text == "What should I name it?"

    def test_replay_returns_seeded_answer_and_consumes(self):
        fake = _FakeDB()
        adapter = _adapter(fake)
        fake.set_meta(clarify_answer_key("s1"), "prod")
        cb = adapter._make_web_clarify_callback("s1", "deploy the service")
        # On replay the seeded answer is returned, NOT re-raised.
        assert cb("Which environment?", ["dev", "prod"]) == "prod"
        # Answer consumed (one-shot).
        assert not fake.get_meta(clarify_answer_key("s1"))

    def test_full_round_trip_raise_then_seed_then_resolve(self):
        fake = _FakeDB()
        adapter = _adapter(fake)
        cb = adapter._make_web_clarify_callback("s1", "deploy the service")
        # Turn 1: question raised.
        with pytest.raises(OperatorInputRequired):
            cb("Which environment?", ["dev", "prod"])
        # Operator answers; resolver seeds it.
        fake.set_meta(clarify_answer_key("s1"), "prod")
        # Turn 2 (replay): callback returns the answer, agent continues.
        assert cb("Which environment?", ["dev", "prod"]) == "prod"

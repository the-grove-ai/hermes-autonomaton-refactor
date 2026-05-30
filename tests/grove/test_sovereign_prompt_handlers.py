"""Tests for grove.sovereign_prompt_handlers — Sprint 32 sovereignty-ux-v1.

Sprint 32 replaced the v1.0 Skip/Drop binary with the Kaizen-register
four-choice prompt. v1.0 vocabulary is preserved through deprecated
aliases — both surfaces are pinned here.

Test categories:

* v1.1 surface (``tty_sovereign_prompt`` with four-choice prompt,
  ``batch_auto_allow_handler``, ``gateway_auto_allow_handler``,
  ``silent_allow_handler``, ``silent_deny_handler``,
  ``silent_promote_handler``).
* v1.0 deprecated aliases (``batch_auto_skip_handler``,
  ``gateway_auto_skip_handler``, ``silent_skip_handler``,
  ``silent_approve_handler``) — verifying they map to the new
  vocabulary cleanly.
* The Kaizen template (``describe_action_kaizen``) — the four
  template rows + the skill-name extraction.
"""

from __future__ import annotations

import logging

import pytest

from grove.dispatcher import AndonHalt
from grove.intents import ToolIntent
from grove.sovereign_prompt_handlers import (
    batch_auto_allow_handler,
    batch_auto_skip_handler,
    describe_action_kaizen,
    gateway_auto_allow_handler,
    gateway_auto_skip_handler,
    silent_allow_handler,
    silent_approve_handler,
    silent_deny_handler,
    silent_promote_handler,
    silent_skip_handler,
    tty_sovereign_prompt,
)
from grove.zones import ZoneResult


def _build_halt(
    tool_name: str = "x",
    zone: str = "red",
    arguments=None,
) -> AndonHalt:
    intents = [ToolIntent(
        tool_name=tool_name,
        arguments=arguments or {},
        call_id="c1",
    )]
    zr = [ZoneResult(zone=zone, matched_rule="r", source="s")]
    return AndonHalt(intents=intents, zone_results=zr, triggering_index=0)


# ── Kaizen template (Sprint 32 1a) ───────────────────────────────────


class TestKaizenTemplate:
    def test_terminal_with_skill_path_renders_skill_name(self):
        desc = describe_action_kaizen(
            "terminal",
            {"command": "python3 /Users/x/.grove/skills/google-workspace/cal.py today"},
        )
        assert desc == "run a skill (google-workspace)"

    def test_terminal_without_skill_path_renders_generic(self):
        desc = describe_action_kaizen("terminal", {"command": "ls -la /tmp"})
        assert desc == "run a command on your machine"

    def test_execute_code_renders_specific(self):
        desc = describe_action_kaizen("execute_code", {"code": "print(1)"})
        assert desc == "execute code"

    def test_unknown_tool_falls_through_to_default(self):
        desc = describe_action_kaizen("write_file", {"path": "/tmp/x"})
        assert desc == "perform an action (write_file)"

    def test_empty_arguments_handled(self):
        desc = describe_action_kaizen("terminal", {})
        assert desc == "run a command on your machine"

    def test_skill_path_with_trailing_slash(self):
        desc = describe_action_kaizen(
            "terminal",
            {"command": "bash /Users/x/.grove/skills/foo/run.sh"},
        )
        assert desc == "run a skill (foo)"


# ── tty_sovereign_prompt (Sprint 32 v1.1 four-choice) ────────────────


class TestTtySovereignPromptV11:
    """The Kaizen four-choice TTY prompt. The header is plain language;
    the four options are operator-facing. Zone names / regex /
    intent indices are absent from the prompt and move to the
    ledger."""

    def test_back_compat_alias_points_at_same_function(self):
        from grove.dispatcher import _default_sovereign_prompt
        assert _default_sovereign_prompt is tty_sovereign_prompt

    def test_choice_1_returns_once(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "1")
        assert tty_sovereign_prompt(_build_halt()) == "once"

    def test_choice_2_returns_session(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "2")
        assert tty_sovereign_prompt(_build_halt()) == "session"

    def test_choice_3_returns_always(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "3")
        assert tty_sovereign_prompt(_build_halt()) == "always"

    def test_choice_4_returns_deny(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "4")
        assert tty_sovereign_prompt(_build_halt()) == "deny"

    def test_word_alias_once(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "once")
        assert tty_sovereign_prompt(_build_halt()) == "once"

    def test_word_alias_deny(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "deny")
        assert tty_sovereign_prompt(_build_halt()) == "deny"

    def test_defaults_to_deny_on_eof(self, monkeypatch: pytest.MonkeyPatch):
        """Fail-safe: EOF / KeyboardInterrupt declines the action.
        v1.0 defaulted to ``drop``; Sprint 32 defaults to ``deny``
        so the absence-of-input is treated as "block" not "flush"."""
        def _eof(prompt=""):
            raise EOFError()
        monkeypatch.setattr("builtins.input", _eof)
        assert tty_sovereign_prompt(_build_halt()) == "deny"

    def test_prompt_text_is_kaizen_register(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ):
        """The operator-facing text MUST use plain language and MUST
        NOT mention zone names, regex patterns, or rule sources."""
        monkeypatch.setattr("builtins.input", lambda prompt="": "4")
        tty_sovereign_prompt(_build_halt(
            tool_name="terminal",
            arguments={"command": "python3 /Users/x/.grove/skills/cal/run.py"},
        ))
        captured = capsys.readouterr().err
        assert "The agent wants to run a skill (cal)" in captured
        assert "Allow this once" in captured
        assert "Allow for this session" in captured
        assert "Always allow this" in captured
        assert "Don't allow this" in captured
        # Forbidden tokens — operator MUST NOT see these.
        assert "zone" not in captured.lower()
        assert "andon halt" not in captured.lower()
        assert "sovereign disposition" not in captured.lower()
        assert "matched_rule" not in captured
        assert "pattern_key" not in captured


# ── v1.1 non-interactive handlers ────────────────────────────────────


class TestBatchAutoAllowHandler:
    def test_returns_once(self):
        assert batch_auto_allow_handler(_build_halt()) == "once"

    def test_logs_kaizen_description(self, caplog: pytest.LogCaptureFixture):
        halt = _build_halt(
            tool_name="terminal",
            zone="red",
            arguments={"command": "python3 /Users/x/.grove/skills/cal/run.py"},
        )
        with caplog.at_level(logging.INFO, logger="grove.sovereign_prompt_handlers"):
            batch_auto_allow_handler(halt)
        messages = [r.getMessage() for r in caplog.records]
        assert any("Kaizen auto-allow (batch)" in m for m in messages)
        assert any("tool=terminal" in m for m in messages)
        assert any("run a skill (cal)" in m for m in messages)


class TestGatewayAutoAllowHandler:
    def test_returns_once(self):
        assert gateway_auto_allow_handler(_build_halt()) == "once"

    def test_logs_with_gateway_label(self, caplog: pytest.LogCaptureFixture):
        halt = _build_halt(
            tool_name="mcp_notion_API_post_page",
            arguments={"page": "test"},
        )
        with caplog.at_level(logging.INFO, logger="grove.sovereign_prompt_handlers"):
            gateway_auto_allow_handler(halt)
        messages = [r.getMessage() for r in caplog.records]
        assert any("Kaizen auto-allow (gateway)" in m for m in messages)

    def test_is_distinct_callable_from_batch_handler(self):
        assert gateway_auto_allow_handler is not batch_auto_allow_handler


class TestSilentAllowHandler:
    def test_returns_once(self):
        assert silent_allow_handler(_build_halt()) == "once"

    def test_emits_no_log_records(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.DEBUG, logger="grove.sovereign_prompt_handlers"):
            silent_allow_handler(_build_halt())
        assert caplog.records == []


class TestSilentDenyHandler:
    def test_returns_deny(self):
        assert silent_deny_handler(_build_halt()) == "deny"

    def test_emits_no_log_records(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.DEBUG, logger="grove.sovereign_prompt_handlers"):
            silent_deny_handler(_build_halt())
        assert caplog.records == []


class TestSilentPromoteHandler:
    def test_returns_always(self):
        assert silent_promote_handler(_build_halt()) == "always"

    def test_emits_no_log_records(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.DEBUG, logger="grove.sovereign_prompt_handlers"):
            silent_promote_handler(_build_halt())
        assert caplog.records == []


# ── Legacy v1.0 aliases (deprecated but functional) ──────────────────


class TestLegacyAliases:
    """The v1.0 names continue to load. Behavior maps to the v1.1
    semantic the alias is documented to take."""

    def test_batch_auto_skip_returns_once(self):
        # Sprint 32 inverted batch from auto-deny to auto-allow.
        assert batch_auto_skip_handler(_build_halt()) == "once"

    def test_gateway_auto_skip_returns_once(self):
        assert gateway_auto_skip_handler(_build_halt()) == "once"

    def test_silent_skip_returns_deny(self):
        # silent_skip_handler maps to the v1.1 deny semantic (the
        # closest operational match to v1.0 "skip").
        assert silent_skip_handler(_build_halt()) == "deny"

    def test_silent_approve_returns_always(self):
        # silent_approve_handler now drives the always-promote flow.
        assert silent_approve_handler(_build_halt()) == "always"

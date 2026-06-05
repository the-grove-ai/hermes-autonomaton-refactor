"""Tests for grove.sovereign_prompt_handlers — GRV-005 § VI v1.1.

Sprint 32.1 removed the v1.0 disposition aliases (skip / drop /
shadow_approve) and the legacy alias functions. The only handler
surfaces are the v1.1 ones below; the only valid handler return
values are ``once`` / ``session`` / ``always`` / ``deny``.

Test categories:

* The Kaizen-register TTY prompt (``tty_sovereign_prompt``) —
  four-choice rendering and the four return values.
* Non-interactive handlers — ``batch_auto_allow_handler``,
  ``gateway_auto_allow_handler``, ``silent_allow_handler``,
  ``silent_deny_handler``, ``silent_promote_handler``.
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
    describe_action_kaizen,
    gateway_auto_allow_handler,
    normalize_command,
    peek,
    silent_allow_handler,
    silent_deny_handler,
    silent_promote_handler,
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
        assert desc == "run the google-workspace skill"

    def test_terminal_without_skill_path_shows_command(self):
        # Sprint 60 — the generic terminal row now Peek-shows the command.
        desc = describe_action_kaizen("terminal", {"command": "ls -la /tmp"})
        assert desc == "run this command: ls -la /tmp"

    def test_execute_code_renders_specific(self):
        desc = describe_action_kaizen("execute_code", {"code": "print(1)"})
        assert desc == "run this code snippet"

    def test_write_file_names_the_file(self):
        # Sprint 60 — write_file gets its own row; the path is Peek-shown.
        desc = describe_action_kaizen("write_file", {"path": "/tmp/x"})
        assert desc == "write the file /tmp/x"

    def test_empty_arguments_handled(self):
        # No command to show → graceful degradation to the bare phrase.
        desc = describe_action_kaizen("terminal", {})
        assert desc == "run a command on your machine"

    def test_write_file_without_path_degrades(self):
        desc = describe_action_kaizen("write_file", {})
        assert desc == "write a file"

    def test_unknown_tool_falls_through_to_default(self):
        desc = describe_action_kaizen("mcp_notion_post", {"page": "x"})
        assert desc == "use mcp_notion_post"

    def test_skill_path_with_trailing_slash(self):
        desc = describe_action_kaizen(
            "terminal",
            {"command": "bash /Users/x/.grove/skills/foo/run.sh"},
        )
        assert desc == "run the foo skill"


# ── Sprint 32.2 — normalize_command + category templates ─────────────


class TestNormalizeCommand:
    """The shared shell-variable normalizer reused by the template
    matcher and the zone-promotion regex generator."""

    def test_dollar_braces_home_expanded(self):
        import os
        home = os.path.expanduser("~")
        assert normalize_command("${HOME}/foo") == f"{home}/foo"

    def test_bare_dollar_home_expanded(self):
        import os
        home = os.path.expanduser("~")
        assert normalize_command("$HOME/foo") == f"{home}/foo"

    def test_leading_tilde_expanded(self):
        import os
        home = os.path.expanduser("~")
        assert normalize_command("~/foo") == f"{home}/foo"

    def test_non_leading_tilde_not_expanded(self):
        # The brief scopes ``~`` expansion to LEADING ``~/`` only —
        # don't mangle paths that happen to contain a tilde elsewhere.
        assert normalize_command("/tmp/~backup") == "/tmp/~backup"

    def test_already_expanded_idempotent(self):
        assert normalize_command("/Users/x/foo") == "/Users/x/foo"

    def test_empty_passes_through(self):
        assert normalize_command("") == ""

    def test_unknown_shell_variable_not_expanded(self):
        # Andon scope: only $HOME / ${HOME} / leading ~/ are handled.
        # Other shell vars stay literal so they fall through to the
        # generic template; the operator still sees a prompt.
        assert normalize_command("$XDG_CONFIG_HOME/foo") == "$XDG_CONFIG_HOME/foo"


class TestKaizenTemplateSprint322:
    """Sprint 32.2 — the bug repro + the new category templates.

    The repro case: a skill invocation written with ``${HOME}`` or
    ``$HOME`` previously fell through to the generic "run a command
    on your machine" template because the ``.grove/skills/``
    substring was split by the unexpanded shell variable.  After the
    fix, the matcher normalizes the command before the substring
    check and the skill template fires correctly.
    """

    # ── repro cases (Parts 1 + 3) ────────────────────────────────

    def test_dollar_braces_home_skill_path(self):
        desc = describe_action_kaizen(
            "terminal",
            {"command": (
                'GAPI="/opt/homebrew/bin/python3.13 '
                '${HOME}/.grove/skills/productivity/google-workspace/'
                'scripts/google_api.py" calendar today'
            )},
        )
        assert desc == "run the google-workspace skill"

    def test_bare_dollar_home_skill_path(self):
        desc = describe_action_kaizen(
            "terminal",
            {"command": (
                "python $HOME/.grove/skills/productivity/"
                "google-workspace/scripts/google_api.py"
            )},
        )
        assert desc == "run the google-workspace skill"

    def test_literal_home_skill_path_with_category(self):
        # No shell variables, but the path goes through a category
        # directory — the extractor must walk past ``productivity``
        # and return ``google-workspace``.
        import os
        home = os.path.expanduser("~")
        desc = describe_action_kaizen(
            "terminal",
            {"command": (
                f"python {home}/.grove/skills/productivity/"
                f"google-workspace/scripts/google_api.py"
            )},
        )
        assert desc == "run the google-workspace skill"

    # ── package-installation templates (Part 2) ──────────────────

    def test_brew_install_extracts_package(self):
        desc = describe_action_kaizen(
            "terminal", {"command": "brew install ripgrep"},
        )
        assert desc == "install the software ripgrep"

    def test_brew_uninstall_extracts_package(self):
        desc = describe_action_kaizen(
            "terminal", {"command": "brew uninstall ripgrep"},
        )
        assert desc == "uninstall the software ripgrep"

    def test_pip_install_extracts_package(self):
        desc = describe_action_kaizen(
            "terminal", {"command": "pip install pydantic"},
        )
        assert desc == "install the Python package pydantic"

    def test_npm_install_extracts_package(self):
        desc = describe_action_kaizen(
            "terminal", {"command": "npm install react"},
        )
        assert desc == "install the Node.js package react"

    def test_install_skips_leading_flags(self):
        # ``brew install -v ripgrep`` should pick ``ripgrep``, not ``-v``.
        desc = describe_action_kaizen(
            "terminal", {"command": "brew install -v ripgrep"},
        )
        assert desc == "install the software ripgrep"

    # ── destructive-operation templates ──────────────────────────

    def test_rm_rf_more_specific_than_plain_rm(self):
        # Row order matters: rm -rf MUST precede rm so the
        # "permanently delete" message wins on -rf invocations.
        # Sprint 60 — the command itself is Peek-shown.
        desc = describe_action_kaizen(
            "terminal", {"command": "rm -rf /tmp/stuff"},
        )
        assert desc == "permanently delete files (rm -rf /tmp/stuff)"

    def test_plain_rm(self):
        desc = describe_action_kaizen(
            "terminal", {"command": "rm /tmp/file.txt"},
        )
        assert desc == "delete files (rm /tmp/file.txt)"

    # ── network-operation templates ──────────────────────────────

    def test_curl_renders_network_request(self):
        desc = describe_action_kaizen(
            "terminal", {"command": "curl https://example.com"},
        )
        assert desc == "make a network request"

    def test_wget_renders_download(self):
        desc = describe_action_kaizen(
            "terminal", {"command": "wget https://example.com/x.tar.gz"},
        )
        assert desc == "download a file from the internet"

    def test_ssh_renders_remote_connect(self):
        desc = describe_action_kaizen(
            "terminal", {"command": "ssh user@host.example.com"},
        )
        assert desc == "connect to a remote machine"

    # ── git state-change templates ───────────────────────────────

    def test_git_push_renders_push(self):
        desc = describe_action_kaizen(
            "terminal", {"command": "git push origin main"},
        )
        assert desc == "push code to a remote repository"

    def test_git_reset_renders_reset(self):
        desc = describe_action_kaizen(
            "terminal", {"command": "git reset --hard HEAD~3"},
        )
        assert desc == "reset your git history"

    def test_git_status_falls_through_to_generic(self):
        # Read-only git commands aren't in the table — should
        # land on the generic terminal row, which Peek-shows the command.
        desc = describe_action_kaizen(
            "terminal", {"command": "git status"},
        )
        assert desc == "run this command: git status"


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
        assert "I'd like to run the cal skill" in captured
        assert "Just this once" in captured
        assert "For the rest of this session" in captured
        assert "Always — I'll remember it" in captured
        assert "Not this time" in captured
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
        assert any("run the cal skill" in m for m in messages)


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


# ── Sprint 60 — peek() display truncation ────────────────────────────


class TestPeek:
    def test_short_string_passes_through(self):
        assert peek("git status") == "git status"

    def test_none_renders_empty(self):
        assert peek(None) == ""

    def test_empty_renders_empty(self):
        assert peek("") == ""

    def test_long_string_center_truncated_within_limit(self):
        s = "a" * 50 + "b" * 50 + "c" * 50  # 150 chars
        out = peek(s)
        assert len(out) <= 100
        assert "…" in out
        # Both ends survive — head keeps the 'a's, tail keeps the 'c's.
        assert out.startswith("a")
        assert out.endswith("c")

    def test_exact_limit_not_truncated(self):
        s = "x" * 100
        assert peek(s) == s

    def test_describe_peeks_a_giant_command(self):
        # A multi-kilobyte command must not swamp the prompt surface.
        giant = "echo " + "z" * 5000
        desc = describe_action_kaizen("terminal", {"command": giant})
        assert desc.startswith("run this command: ")
        # The interpolated command is bounded by peek()'s 100-char limit.
        assert len(desc) < 160
        assert "…" in desc



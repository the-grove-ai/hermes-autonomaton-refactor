"""Tests for grove.sovereign_prompt_handlers — Sprint 27 Phase 2.

The four handler implementations the Dispatcher accepts via its
``sovereign_prompt_handler`` constructor argument. The TTY handler's
input-driven behavior already has coverage via the back-compat alias
in ``tests/grove/test_dispatch_turn.py::TestPhase5SovereignPromptDefault``;
this module adds coverage for the new module surface plus the three
auto-skip variants the Sprint 27 caller migration relies on.
"""

from __future__ import annotations

import logging

import pytest

from grove.dispatcher import AndonHalt
from grove.intents import ToolIntent
from grove.sovereign_prompt_handlers import (
    batch_auto_skip_handler,
    gateway_auto_skip_handler,
    silent_skip_handler,
    tty_sovereign_prompt,
)
from grove.zones import ZoneResult


def _build_halt(tool_name: str = "x", zone: str = "red") -> AndonHalt:
    intents = [ToolIntent(tool_name=tool_name, arguments={}, call_id="c1")]
    zr = [ZoneResult(zone=zone, matched_rule="r", source="s")]
    return AndonHalt(intents=intents, zone_results=zr, triggering_index=0)


# ── tty_sovereign_prompt (new module surface) ─────────────────────────────


class TestTtySovereignPrompt:
    """The interactive TTY handler. Behavior parity with the old
    ``_default_sovereign_prompt`` is verified by
    ``tests/grove/test_dispatch_turn.py::TestPhase5SovereignPromptDefault``
    (which imports the symbol via the back-compat alias in
    ``grove.dispatcher``). These tests pin the new public-import surface."""

    def test_back_compat_alias_points_at_same_function(self):
        # grove.dispatcher._default_sovereign_prompt is now an alias
        # for tty_sovereign_prompt. The old import path must keep
        # resolving to exactly the same callable so external test
        # imports (and any plugin imports) don't break.
        from grove.dispatcher import _default_sovereign_prompt
        assert _default_sovereign_prompt is tty_sovereign_prompt

    def test_returns_skip_on_skip_input(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "skip")
        assert tty_sovereign_prompt(_build_halt()) == "skip"

    def test_returns_drop_on_drop_input(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "drop")
        assert tty_sovereign_prompt(_build_halt()) == "drop"

    def test_drops_on_eof(self, monkeypatch: pytest.MonkeyPatch):
        def _eof(prompt=""):
            raise EOFError()
        monkeypatch.setattr("builtins.input", _eof)
        assert tty_sovereign_prompt(_build_halt()) == "drop"


# ── batch_auto_skip_handler ───────────────────────────────────────────────


class TestBatchAutoSkipHandler:
    def test_returns_skip(self):
        assert batch_auto_skip_handler(_build_halt()) == "skip"

    def test_logs_at_info_with_triggering_tool_and_zone(
        self, caplog: pytest.LogCaptureFixture
    ):
        halt = _build_halt(tool_name="terminal", zone="red")
        with caplog.at_level(logging.INFO, logger="grove.sovereign_prompt_handlers"):
            batch_auto_skip_handler(halt)
        messages = [r.getMessage() for r in caplog.records]
        assert any("Andon auto-skip (batch)" in m for m in messages)
        assert any("tool=terminal" in m for m in messages)
        assert any("zone=red" in m for m in messages)


# ── gateway_auto_skip_handler ─────────────────────────────────────────────


class TestGatewayAutoSkipHandler:
    def test_returns_skip(self):
        assert gateway_auto_skip_handler(_build_halt()) == "skip"

    def test_logs_at_info_with_gateway_label(
        self, caplog: pytest.LogCaptureFixture
    ):
        halt = _build_halt(tool_name="mcp_notion_API_post_page", zone="yellow")
        with caplog.at_level(logging.INFO, logger="grove.sovereign_prompt_handlers"):
            gateway_auto_skip_handler(halt)
        messages = [r.getMessage() for r in caplog.records]
        assert any("Andon auto-skip (gateway)" in m for m in messages)
        assert any("tool=mcp_notion_API_post_page" in m for m in messages)
        assert any("zone=yellow" in m for m in messages)

    def test_is_distinct_callable_from_batch_handler(self):
        # Same v1 semantics but distinct identity so Sprint 28 can evolve
        # the gateway handler (platform-mediated Sovereign Prompt) without
        # touching batch behavior.
        assert gateway_auto_skip_handler is not batch_auto_skip_handler


# ── silent_skip_handler ───────────────────────────────────────────────────


class TestSilentHandler:
    def test_returns_skip(self):
        assert silent_skip_handler(_build_halt()) == "skip"

    def test_emits_no_log_records(
        self, caplog: pytest.LogCaptureFixture
    ):
        # The fixture-grade handler must not pollute test output. The
        # upstream andon_halt ledger record is still written by the
        # Dispatcher — tests that want to assert on halt detail inspect
        # the ledger directly.
        halt = _build_halt()
        with caplog.at_level(logging.DEBUG, logger="grove.sovereign_prompt_handlers"):
            silent_skip_handler(halt)
        assert caplog.records == []

"""Tests for the /codex-runtime slash-command shared logic.

These cover the pure-Python state machine; CLI and gateway handlers are
tested separately because they involve config persistence and prompt
formatting that's surface-specific."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hermes_cli import codex_runtime_switch as crs


class TestParseArgs:
    @pytest.mark.parametrize("arg,expected", [
        ("", None),
        ("   ", None),
        ("auto", "auto"),
        ("codex_app_server", "codex_app_server"),
        ("on", "codex_app_server"),
        ("off", "auto"),
        ("codex", "codex_app_server"),
        ("default", "auto"),
        ("hermes", "auto"),
        ("ENABLE", "codex_app_server"),  # case-insensitive
        ("DiSaBlE", "auto"),
    ])
    def test_valid_args(self, arg, expected):
        value, errors = crs.parse_args(arg)
        assert errors == []
        assert value == expected

    def test_invalid_arg_returns_error(self):
        value, errors = crs.parse_args("turbo")
        assert value is None
        assert errors and "Unknown runtime" in errors[0]


class TestGetCurrentRuntime:
    def test_default_when_unset(self):
        assert crs.get_current_runtime({}) == "auto"
        assert crs.get_current_runtime({"model": {}}) == "auto"
        assert crs.get_current_runtime({"model": {"openai_runtime": ""}}) == "auto"

    def test_unrecognized_falls_back_to_auto(self):
        assert crs.get_current_runtime(
            {"model": {"openai_runtime": "garbage"}}
        ) == "auto"

    def test_explicit_codex(self):
        assert crs.get_current_runtime(
            {"model": {"openai_runtime": "codex_app_server"}}
        ) == "codex_app_server"

    def test_handles_non_dict_config(self):
        assert crs.get_current_runtime(None) == "auto"  # type: ignore[arg-type]
        assert crs.get_current_runtime("notadict") == "auto"  # type: ignore[arg-type]
        assert crs.get_current_runtime({"model": "notadict"}) == "auto"


class TestSetRuntime:
    def test_creates_model_section_if_missing(self):
        cfg = {}
        old = crs.set_runtime(cfg, "codex_app_server")
        assert old == "auto"
        assert cfg["model"]["openai_runtime"] == "codex_app_server"

    def test_returns_previous_value(self):
        cfg = {"model": {"openai_runtime": "codex_app_server"}}
        old = crs.set_runtime(cfg, "auto")
        assert old == "codex_app_server"
        assert cfg["model"]["openai_runtime"] == "auto"

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            crs.set_runtime({}, "garbage")


class TestApply:
    def test_read_only_call_reports_state(self):
        cfg = {"model": {"openai_runtime": "codex_app_server"}}
        with patch.object(crs, "check_codex_binary_ok",
                          return_value=(True, "0.130.0")):
            r = crs.apply(cfg, None)
        assert r.success
        assert r.new_value == "codex_app_server"
        assert r.old_value == "codex_app_server"
        assert "codex_app_server" in r.message
        assert "0.130.0" in r.message

    def test_no_change_when_already_set(self):
        cfg = {"model": {"openai_runtime": "auto"}}
        r = crs.apply(cfg, "auto")
        assert r.success
        assert r.message == "openai_runtime already set to auto"

    def test_enable_refused_runtime_disabled(self):
        """GRV-010 C1c-ii (Option c): enabling codex_app_server is refused —
        read-exfiltration is unconfinable at codex's read-blind approval
        callback (ANDON-EXFIL). The refusal does not depend on whether the
        codex binary is present; config never mutates."""
        cfg = {}
        # Binary check must NOT be reached — refusal precedes the gate.
        with patch.object(crs, "check_codex_binary_ok") as bin_check:
            r = crs.apply(cfg, "codex_app_server")
        assert r.success is False
        assert r.new_value is None
        assert "disabled" in r.message
        assert "C1c-ii" in r.message
        assert bin_check.call_count == 0
        # Config NOT mutated on refusal
        assert cfg.get("model", {}).get("openai_runtime") in (None, "")

    def test_enable_refused_does_not_persist(self):
        """The refusal must short-circuit before any persist or migration —
        a config that requested codex_app_server stays unwritten."""
        cfg = {}
        persisted = {}

        def persist(c):
            persisted.update(c)

        with patch.object(crs, "check_codex_binary_ok",
                          return_value=(True, "0.130.0")), \
             patch("hermes_cli.codex_runtime_plugin_migration.migrate") as mig:
            r = crs.apply(cfg, "codex_app_server", persist_callback=persist)
        assert r.success is False
        assert r.new_value is None
        assert "disabled" in r.message
        # Neither persist nor migration ran.
        assert persisted == {}
        assert not mig.called
        assert cfg.get("model", {}).get("openai_runtime") in (None, "")

    def test_disable_does_not_check_binary(self):
        cfg = {"model": {"openai_runtime": "codex_app_server"}}
        with patch.object(crs, "check_codex_binary_ok") as bin_check:
            r = crs.apply(cfg, "auto")
        assert r.success
        # Binary check is irrelevant when disabling — should not be called
        # with the codex_app_server enable-gate signature.
        assert r.new_value == "auto"
        assert r.old_value == "codex_app_server"

    def test_enable_refusal_precedes_persist_failure(self):
        """Even a persist_callback that would raise is never reached — the
        runtime is refused before any persist attempt."""
        cfg = {}

        def persist_boom(c):
            raise IOError("disk full")

        with patch.object(crs, "check_codex_binary_ok",
                          return_value=(True, "0.130.0")):
            r = crs.apply(cfg, "codex_app_server", persist_callback=persist_boom)
        assert r.success is False
        assert "disabled" in r.message
        # The persist boom never fired, so its message is absent.
        assert "disk full" not in r.message

    def test_enable_refused_does_not_trigger_mcp_migration(self):
        """GRV-010 C1c-ii: the disabled runtime must never reach MCP
        migration — refusal short-circuits before ~/.codex/ is touched."""
        cfg = {
            "mcp_servers": {
                "filesystem": {"command": "npx", "args": ["-y", "fs-server"]},
            }
        }

        with patch.object(crs, "check_codex_binary_ok",
                          return_value=(True, "0.130.0")), \
             patch("hermes_cli.codex_runtime_plugin_migration.migrate") as mig:
            r = crs.apply(cfg, "codex_app_server")
        assert r.success is False
        assert "disabled" in r.message
        assert not mig.called  # refusal precedes migration

    def test_disable_does_not_trigger_migration(self):
        """Switching back to auto must not write to ~/.codex/."""
        cfg = {
            "model": {"openai_runtime": "codex_app_server"},
            "mcp_servers": {"x": {"command": "y"}},
        }
        with patch("hermes_cli.codex_runtime_plugin_migration.migrate") as mig:
            r = crs.apply(cfg, "auto")
        assert r.success
        assert not mig.called  # disabling does not migrate

    def test_enable_refused_never_reaches_migration(self):
        """GRV-010 C1c-ii: a migration that would raise is irrelevant — the
        disabled runtime is refused before migration is ever invoked, so the
        side-effecting branch is unreachable."""
        cfg = {"mcp_servers": {"x": {"command": "y"}}}
        with patch.object(crs, "check_codex_binary_ok",
                          return_value=(True, "0.130.0")), \
             patch("hermes_cli.codex_runtime_plugin_migration.migrate",
                   side_effect=RuntimeError("disk full")) as mig:
            r = crs.apply(cfg, "codex_app_server")
        assert r.success is False
        assert "disabled" in r.message
        assert not mig.called
        # The migration's RuntimeError never surfaced.
        assert "disk full" not in r.message

    def test_enable_refusal_skips_binary_check(self):
        """GRV-010 C1c-ii: the disabled runtime is refused before the codex
        binary gate, so ``codex --version`` is never spawned on the enable
        path. (Previously the enable path cached one binary check; the refusal
        elides it entirely.)
        """
        cfg = {}
        with patch.object(crs, "check_codex_binary_ok",
                          return_value=(True, "0.130.0")) as bin_check, \
             patch("hermes_cli.codex_runtime_plugin_migration.migrate"):
            r = crs.apply(cfg, "codex_app_server")
        assert r.success is False
        assert "disabled" in r.message
        assert bin_check.call_count == 0, (
            f"check_codex_binary_ok was called {bin_check.call_count} time(s); "
            "the disabled-runtime refusal must precede the binary gate"
        )

    def test_binary_check_cached_on_read_only_call(self):
        """Read-only call (new_value=None) calls the binary check exactly
        once and reuses the result for the message."""
        cfg = {"model": {"openai_runtime": "codex_app_server"}}
        with patch.object(crs, "check_codex_binary_ok",
                          return_value=(True, "0.130.0")) as bin_check:
            crs.apply(cfg, None)
        assert bin_check.call_count == 1

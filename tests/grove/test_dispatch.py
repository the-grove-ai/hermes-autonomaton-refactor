"""Tests for grove.dispatch — command-to-action mapper, classify, descope, surface, Kaizen prompt."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from grove import dispatch as gdispatch
from grove import skills as gskills
from grove.zones import ZoneClassifier


_TEST_SCHEMA = """
    schema_version: 1
    zones:
      green:
        auto_approve:
          - calendar.read.*
      yellow:
        proposes:
          - command.dangerous.*
      red:
        sovereign:
          - command.execute.sudo
          - command.execute.su
          - command.execute.doas
    tool_zones:
      calendar.read: green
"""


@pytest.fixture
def fake_classifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Initialize a ZoneClassifier from a tmp schema; patch the dispatch singleton."""
    schema = tmp_path / "zones.schema.yaml"
    schema.write_text(textwrap.dedent(_TEST_SCHEMA).lstrip())
    classifier = ZoneClassifier(schema)
    monkeypatch.setattr(gdispatch, "_classifier", classifier)
    yield classifier
    gdispatch.reset_classifier()


# ----- command_to_action -----------------------------------------------------

@pytest.mark.parametrize(
    "command,expected",
    [
        ("sudo apt install foo", "command.execute.sudo"),
        ("/usr/bin/sudo whoami", "command.execute.sudo"),
        ("FOO=bar sudo ls", "command.execute.sudo"),
        ("ls -la", "command.execute.ls"),
        ("git push --force", "command.execute.git"),
        ("su - root", "command.execute.su"),
        ("doas pkg upgrade", "command.execute.doas"),
        ("", "command.execute.empty"),
        ("   ", "command.execute.empty"),
    ],
)
def test_command_to_action(command: str, expected: str) -> None:
    assert gdispatch.command_to_action(command) == expected


# ----- descope_command -------------------------------------------------------

@pytest.mark.parametrize(
    "command,expected",
    [
        ("sudo apt install foo", "apt install foo"),
        ("sudo -u root apt install", "apt install"),
        ("sudo -E -H bash -c 'x'", "bash -c 'x'"),
        ("FOO=bar sudo apt", "FOO=bar apt"),
        ("doas pkg upgrade", "pkg upgrade"),
        ("/usr/bin/sudo whoami", "whoami"),
        ("ls -la", None),
        ("git push", None),
        ("", None),
        ("sudo", None),
    ],
)
def test_descope_command(command: str, expected) -> None:
    assert gdispatch.descope_command(command) == expected


# ----- classify_command ------------------------------------------------------

def test_classify_command_red_sudo(fake_classifier) -> None:
    # GRV-010 C1a — classify_command routes shell commands to the bashlex-AST
    # effect classifier (grove/shell_effects.py), not the regex schema rules.
    # Privilege escalation still classifies RED, by effect.
    zr = gdispatch.classify_command("sudo apt install foo")
    assert zr.zone == "red"
    assert zr.source == "shell_effect"
    assert "priv:sudo" in (zr.pattern_key or "")


def test_classify_command_default_yellow(fake_classifier) -> None:
    zr = gdispatch.classify_command("ls -la")
    assert zr.zone == "yellow"
    assert zr.source == "shell_effect"


def test_classify_command_tool_zones_green(fake_classifier) -> None:
    # tool_zones has `calendar.read: green`. Our mapper produces
    # `command.execute.calendar.read` for the command "calendar.read",
    # which won't match — verify the tool_zones path directly via the
    # classifier instead of the command mapper.
    zr = fake_classifier.classify("calendar.read")
    assert zr.zone == "green"
    assert zr.source == "tool_zones"


# ----- render_red_surface ----------------------------------------------------

def test_render_red_surface_includes_register_and_command(fake_classifier) -> None:
    zr = gdispatch.classify_command("sudo apt install foo")
    surface = gdispatch.render_red_surface("sudo apt install foo", zr)
    assert "That's in your direct control — here's how." in surface
    assert "sudo apt install foo" in surface
    # Sprint 60 — butler structure kept, impl jargon ("tool dispatch") and
    # the raw rule id removed; the consequence + the fix path remain.
    assert "sudo / su / doas stay with you" in surface
    assert "Andon" not in surface
    assert "sovereignty" not in surface
    assert "tool dispatch" not in surface
    assert "zones.schema.yaml" in surface
    # Must not use forbidden language
    assert "access denied" not in surface.lower()
    assert "forbidden" not in surface.lower()


def test_render_red_surface_truncates_long_commands(fake_classifier) -> None:
    long_cmd = "sudo " + "x" * 200
    zr = gdispatch.classify_command(long_cmd)
    surface = gdispatch.render_red_surface(long_cmd, zr)
    # Truncation marker present somewhere in the surface
    assert "…" in surface


# ----- kaizen_sovereign_prompt -----------------------------------------------

def test_kaizen_prompt_cancel(monkeypatch: pytest.MonkeyPatch, capsys, fake_classifier) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "1")
    zr = gdispatch.classify_command("sudo apt install foo")
    choice = gdispatch.kaizen_sovereign_prompt(
        "sudo apt install foo", zr, descoped="apt install foo"
    )
    assert choice == "cancel"
    out = capsys.readouterr().out
    assert "Sovereign zone — Andon halted" in out
    assert "Here's how I'd move forward:" in out


def test_kaizen_prompt_operator_handles(
    monkeypatch: pytest.MonkeyPatch, fake_classifier
) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "2")
    zr = gdispatch.classify_command("sudo apt install foo")
    choice = gdispatch.kaizen_sovereign_prompt(
        "sudo apt install foo", zr, descoped="apt install foo"
    )
    assert choice == "operator_handles"


def test_kaizen_prompt_alternative(
    monkeypatch: pytest.MonkeyPatch, fake_classifier
) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "3")
    zr = gdispatch.classify_command("sudo apt install foo")
    choice = gdispatch.kaizen_sovereign_prompt(
        "sudo apt install foo", zr, descoped="apt install foo"
    )
    assert choice == "alternative"


def test_kaizen_prompt_alternative_not_offered_without_descope(
    monkeypatch: pytest.MonkeyPatch, fake_classifier
) -> None:
    """Picking '3' when only options 1 and 2 are offered should re-prompt."""
    inputs = iter(["3", "1"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    zr = gdispatch.classify_command("sudo apt install foo")
    choice = gdispatch.kaizen_sovereign_prompt(
        "sudo apt install foo", zr, descoped=None
    )
    assert choice == "cancel"


def test_kaizen_prompt_eof_cancels(
    monkeypatch: pytest.MonkeyPatch, fake_classifier
) -> None:
    def raise_eof(_):
        raise EOFError()
    monkeypatch.setattr("builtins.input", raise_eof)
    zr = gdispatch.classify_command("sudo apt install foo")
    choice = gdispatch.kaizen_sovereign_prompt(
        "sudo apt install foo", zr, descoped="apt install foo"
    )
    assert choice == "cancel"

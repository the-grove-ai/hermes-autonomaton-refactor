"""Tests for the Phase 4 zone-classifier integration into check_all_command_guards.

Focuses on the surface contract — green short-circuits, red hard-blocks in
non-interactive contexts, yellow / default fall through to the existing
approval flow. The interactive Kaizen prompt is covered separately in
``test_dispatch.py``; here we only assert the wiring around it.

Post-condition 9 (existing DANGEROUS_PATTERNS behavior preserved for
non-zone-classified actions) is exercised by leaving the system Python
environment intact and verifying that benign / undeclared commands fall
through to the existing pipeline.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from grove import dispatch as gdispatch
from grove.zones import ZoneClassifier


_SCHEMA = """
    schema_version: 1
    zones:
      green:
        auto_approve:
          - command.execute.echo
      yellow:
        proposes:
          - command.dangerous.*
      red:
        sovereign:
          - command.execute.sudo
          - command.execute.su
          - command.execute.doas
    tool_zones: {}
"""


@pytest.fixture
def fake_classifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    schema = tmp_path / "zones.schema.yaml"
    schema.write_text(textwrap.dedent(_SCHEMA).lstrip())
    classifier = ZoneClassifier(schema)
    monkeypatch.setattr(gdispatch, "_classifier", classifier)
    # Force interactive flags off so we exercise the non-interactive / strict
    # path; individual tests opt into interactive via monkeypatch.
    monkeypatch.delenv("GROVE_INTERACTIVE", raising=False)
    monkeypatch.delenv("GROVE_EXEC_ASK", raising=False)
    monkeypatch.delenv("GROVE_YOLO_MODE", raising=False)
    monkeypatch.delenv("GROVE_ZONE_STRICT", raising=False)
    yield classifier
    gdispatch.reset_classifier()


def test_green_zone_short_circuits(fake_classifier, tmp_path, monkeypatch) -> None:
    from tools.approval import check_all_command_guards

    # GRV-010 C1a — GREEN is now EFFECT-based (grove/shell_effects.py): a generic
    # command like "echo ..." is YELLOW (operator-gated); GREEN is reserved for a
    # promoted-skill invocation under ~/.grove/skills/ (not .andon). Stand up such
    # a skill and invoke it to exercise the green short-circuit.
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    skill = tmp_path / "skills" / "demo" / "run.py"
    skill.parent.mkdir(parents=True)
    skill.write_text("print('hi')\n")

    result = check_all_command_guards(f"python3 {skill}", env_type="local")
    assert result["approved"] is True
    assert result.get("zone_classified") == "green"
    assert result.get("matched_rule") == "shell.effect.green"


def test_red_zone_hard_blocks_non_interactive(fake_classifier) -> None:
    """No GROVE_INTERACTIVE set → no operator → hard block (Kaizen prompt skipped)."""
    from tools.approval import check_all_command_guards

    result = check_all_command_guards("sudo apt install foo", env_type="local")
    assert result["approved"] is False
    assert result.get("zone_classified") == "red"
    assert result.get("sovereign_red") is True
    # Sprint 60 — operator-friendly butler surface, no governance vocab.
    assert "That's in your direct control" in result["message"]
    assert "sudo / su / doas stay with you" in result["message"]
    assert "Andon" not in result["message"]


def test_red_zone_hard_blocks_in_strict_mode(
    fake_classifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GROVE_ZONE_STRICT=1 → hard block even in CLI."""
    from tools.approval import check_all_command_guards

    monkeypatch.setenv("GROVE_INTERACTIVE", "1")
    monkeypatch.setenv("GROVE_ZONE_STRICT", "1")
    result = check_all_command_guards("sudo apt install foo", env_type="local")
    assert result["approved"] is False
    assert result.get("strict") is True
    assert result.get("sovereign_red") is True


def test_red_zone_kaizen_prompt_cancel_cli(
    fake_classifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI + interactive + not strict → Kaizen prompts; pick cancel."""
    from tools.approval import check_all_command_guards

    monkeypatch.setenv("GROVE_INTERACTIVE", "1")
    monkeypatch.setattr("builtins.input", lambda _: "1")
    result = check_all_command_guards("sudo apt install foo", env_type="local")
    assert result["approved"] is False
    assert result.get("sovereign_choice") == "cancel"


def test_red_zone_kaizen_prompt_operator_handles_cli(
    fake_classifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools.approval import check_all_command_guards

    monkeypatch.setenv("GROVE_INTERACTIVE", "1")
    monkeypatch.setattr("builtins.input", lambda _: "2")
    result = check_all_command_guards("sudo apt install foo", env_type="local")
    assert result["approved"] is False
    assert result.get("sovereign_choice") == "operator_handles"
    assert "That's in your direct control" in result["message"]


def test_red_zone_kaizen_alternative_runs_descope(
    fake_classifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking the de-scoped alternative re-classifies → default → falls through to existing flow."""
    from tools.approval import check_all_command_guards

    monkeypatch.setenv("GROVE_INTERACTIVE", "1")
    monkeypatch.setattr("builtins.input", lambda _: "3")
    result = check_all_command_guards("sudo apt install foo", env_type="local")
    # The descoped command "apt install foo" classifies as default (yellow),
    # which falls through to the existing approval flow. With no
    # GROVE_EXEC_ASK / gateway and is_cli=True the non-interactive guard
    # at the top of check_all_command_guards is not the relevant path; the
    # existing DANGEROUS_PATTERNS check runs. `apt install` is not in
    # DANGEROUS_PATTERNS, so it should be approved with no further prompts.
    assert result["approved"] is True
    assert result.get("alternative_command") == "apt install foo"
    assert result.get("sovereign_choice") == "alternative"


def test_yellow_default_falls_through_to_existing_flow(fake_classifier) -> None:
    """Commands that don't match green or red use the existing pipeline (post-condition 9)."""
    from tools.approval import check_all_command_guards

    # `ls -la` is not in the test schema's red list, not in green,
    # and not a hardline pattern. With non-interactive (no GROVE_INTERACTIVE),
    # the existing non-interactive guard returns approved=True for safe
    # commands.
    result = check_all_command_guards("ls -la", env_type="local")
    assert result["approved"] is True


def test_container_envs_skip_zone_check(fake_classifier) -> None:
    """Container env_types short-circuit BEFORE the zone check, preserving prior behavior."""
    from tools.approval import check_all_command_guards

    for env in ("docker", "modal", "daytona", "vercel_sandbox"):
        result = check_all_command_guards("sudo apt install foo", env_type=env)
        assert result["approved"] is True
        # Zone classification should NOT have run for container envs.
        assert "zone_classified" not in result

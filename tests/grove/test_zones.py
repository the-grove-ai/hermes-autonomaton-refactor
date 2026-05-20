"""Tests for grove.zones — ZoneClassifier behavior and module-level wiring.

Every test builds a tiny schema inside ``tmp_path`` and points the classifier
at it. None of these tests touch ``~/.grove/`` or the repo default config —
the SPEC out-of-scope rule for this sprint forbids modifications outside the
new grove/ package and its tests.
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest

from grove.zones import ZoneClassifier


# ----- helpers ---------------------------------------------------------------

_BASE_SCHEMA = """
    schema_version: 1
    zones:
      green:
        auto_approve:
          - calendar.read.*
          - file.read.*
          - skill.invoke.*
      yellow:
        proposes:
          - skill.create.*
          - skill.edit.*
          - skill.promote.*
          - file.write.andon_quarantine.*
          - command.dangerous.*
      red:
        sovereign:
          - file.write.zones_schema
          - file.write.routing_config
          - skill.self_promote.*
          - command.execute.sudo
          - command.execute.su
          - command.execute.doas
    tool_zones:
      terminal: yellow
      skill_manage.promote: red
      calendar.read: green
"""


def _write_schema(tmp_path: Path, content: str = _BASE_SCHEMA) -> Path:
    schema = tmp_path / "zones.schema.yaml"
    schema.write_text(textwrap.dedent(content).lstrip())
    return schema


@pytest.fixture
def reset_singleton():
    """Clear the module-level singleton before and after each test that uses it."""
    from grove import zones as grove_zones
    grove_zones._singleton = None
    yield
    grove_zones._singleton = None


# ----- T1..T6: classify() precedence -----------------------------------------

def test_T1_green_action(tmp_path: Path) -> None:
    """T1: action matching a green auto_approve pattern returns green."""
    classifier = ZoneClassifier(_write_schema(tmp_path))
    result = classifier.classify("calendar.read.personal")
    assert result.zone == "green"
    assert result.source == "auto_approve"
    assert result.matched_rule == "calendar.read.*"


def test_T2_yellow_action(tmp_path: Path) -> None:
    """T2: action matching a yellow proposes pattern returns yellow."""
    classifier = ZoneClassifier(_write_schema(tmp_path))
    result = classifier.classify("skill.create.my_new_skill")
    assert result.zone == "yellow"
    assert result.source == "proposes"
    assert result.matched_rule == "skill.create.*"


def test_T3_red_action(tmp_path: Path) -> None:
    """T3: action matching a red sovereign pattern returns red."""
    classifier = ZoneClassifier(_write_schema(tmp_path))
    result = classifier.classify("skill.self_promote.via_andon")
    assert result.zone == "red"
    assert result.source == "sovereign"
    assert result.matched_rule == "skill.self_promote.*"


def test_T4_unmatched_defaults_to_yellow(tmp_path: Path) -> None:
    """T4: action that matches no rule and no tool_zones entry returns yellow default."""
    classifier = ZoneClassifier(_write_schema(tmp_path))
    result = classifier.classify("totally.unknown.action.path")
    assert result.zone == "yellow"
    assert result.source == "default"
    assert result.matched_rule == "default"


def test_T5_tool_zones_mapping_works(tmp_path: Path) -> None:
    """T5: an action whose identifier appears in tool_zones is classified by that map."""
    classifier = ZoneClassifier(_write_schema(tmp_path))
    result = classifier.classify("terminal")
    assert result.zone == "yellow"
    assert result.source == "tool_zones"
    assert result.matched_rule == "terminal"


def test_T6_tool_zones_precedes_zone_rules(tmp_path: Path) -> None:
    """T6: tool_zones exact match wins over zone-rule pattern match.

    Construct a schema where ``calendar.read`` would resolve green via
    auto_approve but tool_zones pins it to red — expect red.
    """
    schema_yaml = """
        schema_version: 1
        zones:
          green:
            auto_approve:
              - calendar.read.*
        tool_zones:
          calendar.read: red
    """
    classifier = ZoneClassifier(_write_schema(tmp_path, schema_yaml))
    result = classifier.classify("calendar.read")
    assert result.zone == "red"
    assert result.source == "tool_zones"
    assert result.matched_rule == "calendar.read"


# ----- T7..T8: reload() ------------------------------------------------------

def test_T7_reload_valid_schema_updates_map(tmp_path: Path) -> None:
    """T7: reload() with a valid new schema reflects the new patterns."""
    schema = _write_schema(tmp_path)
    classifier = ZoneClassifier(schema)
    # Baseline: action is unmatched, so yellow default.
    assert classifier.classify("brand.new.action.x").zone == "yellow"

    schema.write_text(textwrap.dedent("""
        schema_version: 1
        zones:
          green:
            auto_approve:
              - brand.new.action.*
    """).lstrip())
    classifier.reload()

    result = classifier.classify("brand.new.action.x")
    assert result.zone == "green"
    assert result.source == "auto_approve"
    assert result.matched_rule == "brand.new.action.*"


def test_T8_reload_invalid_schema_keeps_last_good(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """T8: reload() against broken YAML retains the last good map and logs loudly."""
    schema = _write_schema(tmp_path)
    classifier = ZoneClassifier(schema)
    before = classifier.classify("calendar.read.personal")
    assert before.zone == "green"

    # Write malformed YAML — unterminated flow mapping.
    schema.write_text("schema_version: 1\nzones: { THIS IS NOT VALID")

    with caplog.at_level(logging.ERROR, logger="grove.zones"):
        classifier.reload()

    # Same classification as before — last known good map retained.
    after = classifier.classify("calendar.read.personal")
    assert after.zone == "green"
    assert after.source == "auto_approve"

    # Loud log captured.
    assert any(
        "reload failed" in record.getMessage() for record in caplog.records
    ), f"expected 'reload failed' log; got: {[r.getMessage() for r in caplog.records]}"


# ----- T11: corrected sovereign command.execute patterns (Sprint 04 addition)

def test_T11_corrected_command_execute_patterns_classify_red(tmp_path: Path) -> None:
    """T11 (new in Sprint 04): the rewritten sovereign command.execute patterns
    classify sudo/su/doas as red via exact match (no substring magic, no
    ${SUDO} interpolation — Sprint 03 schema corrected to pure dot-notation)."""
    classifier = ZoneClassifier(_write_schema(tmp_path))
    for action in (
        "command.execute.sudo",
        "command.execute.su",
        "command.execute.doas",
    ):
        result = classifier.classify(action)
        assert result.zone == "red", f"{action!r} did not classify red: {result}"
        assert result.source == "sovereign"
        assert result.matched_rule == action


# ----- T12: load-time validator rejects invalid wildcard placements ----------

@pytest.mark.parametrize(
    "bad_pattern,expected_msg",
    [
        # Wildcard present but not at the end (does not match `.endswith('.*')`).
        ("command.execute.*.sudo", "only trailing"),
        # Trailing `.*` is fine, but the prefix also contains a `*`.
        ("foo.*.bar.*", "mid-pattern wildcards"),
    ],
)
def test_T12_invalid_wildcard_placements_raise_at_load(
    tmp_path: Path, bad_pattern: str, expected_msg: str
) -> None:
    """T12: invalid wildcard placements raise ValueError at load time.

    Two branches:
      * pattern contains `*` but does not end with `.*` (e.g. `a.*.b`)
      * pattern ends with `.*` but the prefix also contains `*` (mid-pattern)

    Both are fail-loud at load — no silent acceptance, no dead patterns.
    """
    bad_schema = f"""
        schema_version: 1
        zones:
          red:
            sovereign:
              - {bad_pattern}
    """
    schema = _write_schema(tmp_path, bad_schema)
    with pytest.raises(ValueError, match=expected_msg):
        ZoneClassifier(schema)


# ----- T13..T14: module-level singleton wiring -------------------------------

def test_T13_initialize_and_module_classify(
    tmp_path: Path, reset_singleton: None
) -> None:
    """T13: initialize(path) sets the singleton; module-level classify() delegates."""
    from grove import zones as grove_zones
    grove_zones.initialize(_write_schema(tmp_path))
    result = grove_zones.classify("calendar.read.personal")
    assert result.zone == "green"
    assert result.source == "auto_approve"


def test_T14_classify_before_initialize_raises(reset_singleton: None) -> None:
    """T14: module-level classify() raises if initialize() was never called."""
    from grove import zones as grove_zones
    with pytest.raises(RuntimeError, match="not initialized"):
        grove_zones.classify("anything")

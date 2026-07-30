"""Tests for grove.zones — ZoneClassifier behavior and module-level wiring.

zones-v2-scope-keying P2: the loader is v2-only (schema_version 2; effect classes
+ derivation). The v1 category-keyed machinery (action-type pattern lists,
hierarchical rules, classify_command_string) is retired. Derivation-matrix,
loader-version, and overlay-refusal coverage lives in
``test_zones_v2_scope_keying.py``; this file covers classify() lookup, reload
semantics, and the module singleton.

Every test builds a tiny v2 schema inside ``tmp_path`` and points the classifier
at it. GROVE_HOME is per-test-isolated by the autouse conftest fixture.
"""
from __future__ import annotations

import importlib
import logging
import textwrap
from pathlib import Path

import pytest

from grove.zones import ZoneClassifier


_V2_SCHEMA = """
    schema_version: 2
    effect_classes:
      read_only:       {derives: green}
      contained_write: {derives: green}
      workspace_write: {derives: yellow}
      external_effect: {derives: yellow}
      governance:      {derives: red}
      operator_only:   {derives: red}
    default_unmatched: yellow
    tool_effects:
      read_file:   read_only
      write_file:  workspace_write
      andon_promote: governance
"""


def _write_schema(tmp_path: Path, content: str = _V2_SCHEMA) -> Path:
    schema = tmp_path / "zones.schema.yaml"
    schema.write_text(textwrap.dedent(content).lstrip())
    return schema


# ----- classify() ------------------------------------------------------------

def test_classify_tool_effect_derives_zone(tmp_path: Path) -> None:
    clf = ZoneClassifier(_write_schema(tmp_path))
    assert clf.classify("read_file").zone == "green"
    assert clf.classify("write_file").zone == "yellow"
    assert clf.classify("andon_promote").zone == "red"
    r = clf.classify("read_file")
    assert r.source == "tool_zones" and r.matched_rule == "read_file"


def test_classify_unmatched_returns_declared_default(tmp_path: Path) -> None:
    clf = ZoneClassifier(_write_schema(tmp_path))
    r = clf.classify("totally.unknown.action")
    assert r.zone == "yellow"
    assert r.source == "default"
    assert r.matched_rule == "default"


# ----- reload() --------------------------------------------------------------

def test_reload_valid_schema_updates_map(tmp_path: Path) -> None:
    schema = _write_schema(tmp_path)
    clf = ZoneClassifier(schema)
    assert clf.classify("brand_new_tool").zone == "yellow"  # unmatched default

    schema.write_text(textwrap.dedent(_V2_SCHEMA).lstrip().replace(
        "  andon_promote: governance",
        "  andon_promote: governance\n  brand_new_tool: read_only",
    ))
    clf.reload()
    assert clf.classify("brand_new_tool").zone == "green"


def test_reload_invalid_schema_keeps_last_good(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    schema = _write_schema(tmp_path)
    clf = ZoneClassifier(schema)
    assert clf.classify("read_file").zone == "green"

    schema.write_text("schema_version: 2\neffect_classes: { THIS IS NOT VALID")
    with caplog.at_level(logging.ERROR, logger="grove.zones"):
        clf.reload()

    # Last known good retained.
    assert clf.classify("read_file").zone == "green"
    assert any("reload failed" in r.getMessage() for r in caplog.records)


# ----- module singleton wiring ----------------------------------------------

def test_module_classify_requires_initialize(monkeypatch) -> None:
    import grove.zones as gz
    monkeypatch.setattr(gz, "_singleton", None)
    with pytest.raises(RuntimeError, match="not initialized"):
        gz.classify("read_file")


def test_reload_picks_up_overlay(tmp_path, monkeypatch):
    # zones-v2-scope-keying P2: the overlay valid-key surface is now
    # {schema_version, red_denied_by_policy} (A3). A change to the valid deny-list
    # is picked up on reload; a retired-key overlay (tool_zones — the door
    # category keying walked back through) is REFUSED LOUD at load, never merged.
    #
    # instance-cold-start-parity-v1 D3: zones resolves the overlay via
    # get_hermes_home() (honors GROVE_HOME); red_policy reads the same overlay
    # via Path.home()/".grove". Drive both accordingly.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".grove").mkdir()
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / ".grove"))
    import grove.zones as _zones
    importlib.reload(_zones)
    classifier = _zones.initialize()  # no overlay yet — clean repo policy
    overlay = tmp_path / ".grove" / "zones.autonomaton.yaml"

    # (a) A valid overlay carrying only the deny-list key reloads cleanly and is
    #     picked up (red_policy reads the deny-list from the same overlay file).
    overlay.write_text('schema_version: 1\nred_denied_by_policy: ["priv:"]\n')
    classifier.reload()  # no raise — valid key surface
    from grove import red_policy
    assert "priv:" in red_policy.denied_patterns()

    # (b) A retired-key overlay (tool_zones) is refused loud at load — a fresh
    #     init raises A3 rather than silently merging category keying back in.
    overlay.write_text("schema_version: 1\ntool_zones:\n  x: green\n")
    with pytest.raises(ValueError, match="RETIRED key 'tool_zones'"):
        _zones.ZoneClassifier(_zones._resolve_schema_path(None))

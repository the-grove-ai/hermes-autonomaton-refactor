"""GRV-001 v2.0 split-config loaders (routing-v2-migration-v1, Phase 1).

Covers ``grove.router_merge.load_operational_routing_config`` and
``load_authority_routing_config``: the v2 version gate (rejects int / missing /
v1-shape with a migration-path error), the §V confused-deputy collision check
(fatal for each authority-reserved key, correct operator-vs-overlay source
attribution), and the authority loader's structural absence of an overlay path.

Hermetic: everything under ``tmp_path``; no ~/.grove read.
"""

from __future__ import annotations

import pytest

from grove.router_merge import (
    ConfigurationError,
    _AUTHORITY_RESERVED_KEYS,
    load_authority_routing_config,
    load_operational_routing_config,
)

_VALID_OPERATIONAL = """\
schema_version: "2.0"
surface_class: in_scope
writable_on: autonomous_loop
default_tier: T1
tier_preferences:
  T1:
    provider: openrouter
    model: deepseek/deepseek-v4-flash
    max_tokens: 4096
"""

_VALID_AUTHORITY = """\
schema_version: "2.0"
surface_class: scope_defining
writable_on: operator_authenticated
default_zone: red
escalation_threshold: 0.6
tier_budgets:
  T1:
    context: []
escalation_policy:
  enabled: false
"""


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


# ── version gate ─────────────────────────────────────────────────────────────


class TestVersionGate:
    def test_valid_v2_operational_loads(self, tmp_path):
        p = _write(tmp_path / "op.yaml", _VALID_OPERATIONAL)
        data = load_operational_routing_config(p)
        assert data["schema_version"] == "2.0"
        assert data["default_tier"] == "T1"

    def test_valid_v2_authority_loads(self, tmp_path):
        p = _write(tmp_path / "auth.yaml", _VALID_AUTHORITY)
        data = load_authority_routing_config(p)
        assert data["schema_version"] == "2.0"
        assert data["escalation_threshold"] == 0.6

    def test_int_schema_version_rejected(self, tmp_path):
        p = _write(tmp_path / "op.yaml", 'schema_version: 1\ndefault_tier: T1\n')
        with pytest.raises(ConfigurationError, match="non-string schema_version"):
            load_operational_routing_config(p)

    def test_missing_schema_version_rejected(self, tmp_path):
        p = _write(tmp_path / "op.yaml", "surface_class: in_scope\ndefault_tier: T1\n")
        with pytest.raises(ConfigurationError, match="no schema_version"):
            load_operational_routing_config(p)

    def test_v1_shape_rejected_with_migration_path(self, tmp_path):
        p = _write(tmp_path / "v1.yaml", "routing:\n  schema_version: 1\n  default_tier: T1\n")
        with pytest.raises(ConfigurationError, match="v1-shaped") as exc:
            load_operational_routing_config(p)
        assert "cold-start" in str(exc.value)

    def test_wrong_version_string_rejected(self, tmp_path):
        p = _write(tmp_path / "op.yaml", 'schema_version: "3.0"\ndefault_tier: T1\n')
        with pytest.raises(ConfigurationError, match="unsupported schema_version"):
            load_operational_routing_config(p)

    def test_authority_version_gate_applies(self, tmp_path):
        p = _write(tmp_path / "auth.yaml", 'schema_version: 1\ndefault_zone: red\n')
        with pytest.raises(ConfigurationError, match="non-string schema_version"):
            load_authority_routing_config(p)


# ── §V confused-deputy collision check ───────────────────────────────────────


class TestReservedKeyCollision:
    @pytest.mark.parametrize("key", sorted(_AUTHORITY_RESERVED_KEYS))
    def test_reserved_key_in_operator_file_is_fatal(self, tmp_path, key):
        text = _VALID_OPERATIONAL + f"{key}: {{}}\n"
        p = _write(tmp_path / "op.yaml", text)
        with pytest.raises(ConfigurationError, match=key) as exc:
            load_operational_routing_config(p)
        # source attribution names the operator file
        assert "operator file" in str(exc.value)
        assert "routing.authority.yaml" in str(exc.value)

    @pytest.mark.parametrize("key", sorted(_AUTHORITY_RESERVED_KEYS))
    def test_reserved_key_from_machine_overlay_is_fatal(self, tmp_path, key):
        op = _write(tmp_path / "op.yaml", _VALID_OPERATIONAL)  # clean operator
        machine = _write(tmp_path / "machine.yaml", f"{key}: {{}}\n")
        with pytest.raises(ConfigurationError, match=key) as exc:
            load_operational_routing_config(op, machine)
        # source attribution names the machine overlay, not the operator
        assert "machine overlay" in str(exc.value)

    def test_authority_loader_accepts_reserved_keys(self, tmp_path):
        # the reserved keys are the authority surface's legitimate home
        p = _write(tmp_path / "auth.yaml", _VALID_AUTHORITY)
        data = load_authority_routing_config(p)
        for key in _AUTHORITY_RESERVED_KEYS:
            assert key in data


# ── structural properties ────────────────────────────────────────────────────


def test_authority_loader_has_no_overlay_parameter():
    """R1: the authority loader must not accept a machine overlay — structurally
    absent, not defaulted-off."""
    import inspect

    sig = inspect.signature(load_authority_routing_config)
    assert list(sig.parameters) == ["authority_path"]


def test_operational_machine_overlay_merges_clean_additions(tmp_path):
    op = _write(tmp_path / "op.yaml", _VALID_OPERATIONAL)
    # a non-reserved operational addition merges through
    machine = _write(tmp_path / "machine.yaml", "routing_rules:\n  extra:\n    enabled: true\n")
    data = load_operational_routing_config(op, machine)
    assert data["routing_rules"]["extra"]["enabled"] is True
    assert data["default_tier"] == "T1"  # operator value preserved

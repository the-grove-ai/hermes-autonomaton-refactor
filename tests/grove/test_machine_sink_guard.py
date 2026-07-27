"""routing-v2-migration-v1 Phase 2b — machine-sink fail-closed guard.

``apply_diff_to_machine_config`` stages the merged candidate and VALIDATES BY
LOADING it through the runtime loader (``load_operational_routing_config``)
before any atomic replace. A diff that would produce a non-loadable machine
overlay — a v1-nested ``routing:`` wrapper, or an authority-reserved key
smuggled onto the machine surface — is rejected with ZERO bytes written (or the
prior machine file left byte-unchanged). A valid flat v2 diff writes atomically
and the loader accepts the result.

Hermetic: everything under ``tmp_path``; the operational sibling is written into
the same dir (the runtime pairing the guard resolves), no ~/.grove read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grove.router_merge import (
    ConfigurationError,
    apply_diff_to_machine_config,
    load_operational_routing_config,
)

_OP_SIBLING = (
    'schema_version: "2.0"\n'
    "surface_class: in_scope\n"
    "writable_on: autonomous_loop\n"
    "default_tier: T1\n"
    "tier_preferences:\n"
    "  T1:\n"
    "    provider: openrouter\n"
    "    model: deepseek/deepseek-v4-flash\n"
)


def _op(tmp_path: Path) -> Path:
    p = tmp_path / "routing.operational.yaml"
    p.write_text(_OP_SIBLING, encoding="utf-8")
    return p


def test_v1_nested_diff_rejected_with_zero_bytes(tmp_path):
    _op(tmp_path)
    mach = tmp_path / "routing.autonomaton.yaml"
    v1_nested = {"routing": {"routing_rules": {"downward": {"match": {"intents": ["x"]}}}}}
    with pytest.raises(ConfigurationError, match="v1-shaped"):
        apply_diff_to_machine_config(v1_nested, mach)
    assert not mach.exists()  # fail-closed: no bytes written
    assert not (tmp_path / "routing.autonomaton.yaml.tmp").exists()  # staged tmp cleaned


def test_v1_nested_diff_leaves_prior_machine_byte_unchanged(tmp_path):
    _op(tmp_path)
    mach = tmp_path / "routing.autonomaton.yaml"
    # a prior VALID flat machine overlay
    apply_diff_to_machine_config(
        {"routing_rules": {"downward": {"match": {"intents": ["creative_writing"]}}}}, mach
    )
    before = mach.read_bytes()
    v1_nested = {"routing": {"routing_rules": {"downward": {"match": {"intents": ["y"]}}}}}
    with pytest.raises(ConfigurationError, match="v1-shaped"):
        apply_diff_to_machine_config(v1_nested, mach)
    assert mach.read_bytes() == before  # fail-closed: prior content intact


def test_authority_reserved_key_rejected_no_write(tmp_path):
    _op(tmp_path)
    mach = tmp_path / "routing.autonomaton.yaml"
    # §V confused-deputy — an authority-reserved key must not reach the machine
    # (in_scope) surface; the loader's collision check rejects it.
    reserved = {"tier_budgets": {"T1": {"context": []}}}
    with pytest.raises(ConfigurationError, match="tier_budgets"):
        apply_diff_to_machine_config(reserved, mach)
    assert not mach.exists()


def test_valid_flat_diff_writes_atomically_and_loads(tmp_path):
    op = _op(tmp_path)
    mach = tmp_path / "routing.autonomaton.yaml"
    apply_diff_to_machine_config(
        {"routing_rules": {"downward": {"match": {"intents": ["creative_writing"]}}}}, mach
    )
    assert mach.exists()
    assert not (tmp_path / "routing.autonomaton.yaml.tmp").exists()  # no tmp left
    # the runtime loader accepts operational + the written machine overlay, and
    # the machine rule is present in the merged result.
    merged = load_operational_routing_config(op, mach)
    assert merged["routing_rules"]["downward"]["match"]["intents"] == ["creative_writing"]
    assert merged["default_tier"] == "T1"  # operator base preserved

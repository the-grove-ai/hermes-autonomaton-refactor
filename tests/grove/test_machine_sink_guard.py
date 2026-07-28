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
    # a prior VALID flat machine overlay. routing-v2-machine-overlay-migration-v1:
    # the rule now carries target_tier so it is runtime-constructible (the operator
    # sibling does not pre-declare 'downward'); an intents-only rule would be rejected
    # by the guard's parse step.
    apply_diff_to_machine_config(
        {"routing_rules": {"downward": {"match": {"intents": ["creative_writing"]},
                                        "target_tier": "T1"}}}, mach
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


def test_target_tier_less_rule_rejected_with_zero_bytes(tmp_path):
    # routing-v2-machine-overlay-migration-v1 R-A2c: this scenario previously PASSED
    # under the shallow guard (load_operational_routing_config accepts any v2-flat
    # mapping), pinning the shallow-guard defect — a set-tier rule with no target_tier
    # loaded clean but the RUNTIME router (_parse_routing_rules) would reject it at the
    # next init. The deepened guard now runs that parser at approval, so a rule the
    # runtime cannot construct is refused HERE with zero bytes.
    _op(tmp_path)
    mach = tmp_path / "routing.autonomaton.yaml"
    # 'downward' is NOT declared in the operator sibling, so a target_tier-less rule
    # is unconstructible in the merged config.
    intents_only = {"routing_rules": {"downward": {"match": {"intents": ["creative_writing"]}}}}
    with pytest.raises(ValueError, match="target_tier"):
        apply_diff_to_machine_config(intents_only, mach)
    assert not mach.exists()  # fail-closed: no bytes written
    assert not (tmp_path / "routing.autonomaton.yaml.tmp").exists()  # staged tmp cleaned


def test_complete_flat_rule_passes_guard_and_parse(tmp_path):
    # The positive twin: a COMPLETE flat set-tier rule (with target_tier) passes both
    # the shape loader AND the runtime parser, and writes atomically.
    op = _op(tmp_path)
    mach = tmp_path / "routing.autonomaton.yaml"
    apply_diff_to_machine_config(
        {"routing_rules": {"downward": {"match": {"intents": ["creative_writing"]},
                                        "target_tier": "T1"}}}, mach
    )
    assert mach.exists()
    assert not (tmp_path / "routing.autonomaton.yaml.tmp").exists()  # no tmp left
    # the runtime loader accepts operational + the written machine overlay, and
    # the machine rule is present in the merged result.
    merged = load_operational_routing_config(op, mach)
    assert merged["routing_rules"]["downward"]["match"]["intents"] == ["creative_writing"]
    assert merged["routing_rules"]["downward"]["target_tier"] == "T1"
    assert merged["default_tier"] == "T1"  # operator base preserved


def test_enabled_empty_intents_rule_rejected_guarded_class(tmp_path):
    # routing-v2-machine-overlay-migration-v1 ANDON 3 — match-all guarded class. An
    # ENABLED rule with no match criteria matches EVERY request (_rule_matches
    # router.py:737 skips an empty intents filter). The guard refuses a diff that would
    # ACTIVATE such a rule — zero bytes — even though it parses (target_tier present).
    _op(tmp_path)
    mach = tmp_path / "routing.autonomaton.yaml"
    match_all = {"routing_rules": {"downward": {"match": {"intents": []},
                                               "enabled": True, "target_tier": "T1"}}}
    with pytest.raises(ConfigurationError, match="every request"):
        apply_diff_to_machine_config(match_all, mach)
    assert not mach.exists()  # fail-closed: no bytes written
    assert not (tmp_path / "routing.autonomaton.yaml.tmp").exists()  # staged tmp cleaned

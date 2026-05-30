"""Sprint 47 — config merge tests (GRV-008 § III).

Operator wins on scalar key collisions. Lists merge as SET-UNION with
operator entries first. The dedicated set-union test the operator
added at GATE-A asserts both operator and machine intents survive.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from grove.router_merge import (
    _deep_merge,
    apply_diff_to_machine_config,
    load_merged_routing_config,
)


# ── Pure deep_merge ──────────────────────────────────────────────────


class TestDeepMergeScalars:
    def test_operator_scalar_wins_on_collision(self) -> None:
        operator = {"default_tier": "T2"}
        machine = {"default_tier": "T1"}
        assert _deep_merge(operator, machine) == {"default_tier": "T2"}

    def test_machine_only_key_survives(self) -> None:
        operator = {"default_tier": "T2"}
        machine = {"escalation_threshold": 0.6}
        assert _deep_merge(operator, machine) == {
            "default_tier": "T2",
            "escalation_threshold": 0.6,
        }

    def test_operator_only_key_survives(self) -> None:
        operator = {"default_tier": "T2"}
        machine = {}
        assert _deep_merge(operator, machine) == {"default_tier": "T2"}


class TestDeepMergeDicts:
    def test_recurses_into_nested_dicts(self) -> None:
        operator = {"routing_rules": {"downward": {"enabled": True}}}
        machine = {"routing_rules": {"upward": {"enabled": True}}}
        merged = _deep_merge(operator, machine)
        assert merged == {
            "routing_rules": {
                "downward": {"enabled": True},
                "upward": {"enabled": True},
            }
        }

    def test_scalar_at_nested_key_operator_wins(self) -> None:
        operator = {"routing_rules": {"downward": {"target_tier": "T1"}}}
        machine = {"routing_rules": {"downward": {"target_tier": "T2"}}}
        merged = _deep_merge(operator, machine)
        assert merged["routing_rules"]["downward"]["target_tier"] == "T1"


class TestDeepMergeListsSetUnion:
    def test_operator_intents_survive_machine_additions(self) -> None:
        """GATE-A revision: operator + machine intents both survive."""
        operator = {"intents": ["creative_writing"]}
        machine = {"intents": ["system_admin"]}
        merged = _deep_merge(operator, machine)
        assert merged == {"intents": ["creative_writing", "system_admin"]}

    def test_operator_order_preserved_machine_appended(self) -> None:
        operator = {"intents": ["a", "b", "c"]}
        machine = {"intents": ["x", "y"]}
        merged = _deep_merge(operator, machine)
        assert merged == {"intents": ["a", "b", "c", "x", "y"]}

    def test_duplicate_intents_dedupe(self) -> None:
        operator = {"intents": ["conversation"]}
        machine = {"intents": ["conversation", "creative_writing"]}
        merged = _deep_merge(operator, machine)
        assert merged == {"intents": ["conversation", "creative_writing"]}

    def test_empty_machine_list_does_not_remove_operator_entries(self) -> None:
        operator = {"intents": ["a", "b"]}
        machine = {"intents": []}
        merged = _deep_merge(operator, machine)
        assert merged == {"intents": ["a", "b"]}

    def test_empty_operator_list_inherits_machine_entries(self) -> None:
        operator = {"intents": []}
        machine = {"intents": ["a", "b"]}
        merged = _deep_merge(operator, machine)
        assert merged == {"intents": ["a", "b"]}


# ── load_merged_routing_config ───────────────────────────────────────


class TestLoadMergedRoutingConfig:
    def test_missing_operator_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_merged_routing_config(tmp_path / "nonexistent.yaml")

    def test_machine_absent_returns_operator(self, tmp_path: Path) -> None:
        op = tmp_path / "routing.config.yaml"
        op.write_text("routing:\n  default_tier: T2\n")
        merged = load_merged_routing_config(op, None)
        assert merged == {"routing": {"default_tier": "T2"}}

    def test_both_present_operator_wins(self, tmp_path: Path) -> None:
        op = tmp_path / "routing.config.yaml"
        mach = tmp_path / "routing.autonomaton.yaml"
        op.write_text("routing:\n  default_tier: T2\n")
        mach.write_text("routing:\n  default_tier: T3\n")
        merged = load_merged_routing_config(op, mach)
        assert merged["routing"]["default_tier"] == "T2"

    def test_machine_only_keys_survive_through_load(self, tmp_path: Path) -> None:
        op = tmp_path / "routing.config.yaml"
        mach = tmp_path / "routing.autonomaton.yaml"
        op.write_text(
            "routing:\n  default_tier: T2\n"
            "  routing_rules:\n    downward:\n      match:\n        intents: [creative_writing]\n"
        )
        mach.write_text(
            "routing:\n  routing_rules:\n    downward:\n      match:\n        intents: [system_admin]\n"
        )
        merged = load_merged_routing_config(op, mach)
        intents = merged["routing"]["routing_rules"]["downward"]["match"]["intents"]
        assert sorted(intents) == ["creative_writing", "system_admin"]


# ── apply_diff_to_machine_config ─────────────────────────────────────


class TestApplyDiffToMachineConfig:
    def test_creates_new_machine_file(self, tmp_path: Path) -> None:
        mach = tmp_path / "routing.autonomaton.yaml"
        diff = {"routing": {"routing_rules": {"downward": {"match": {"intents": ["creative_writing"]}}}}}
        apply_diff_to_machine_config(diff, mach)
        assert mach.exists()
        text = mach.read_text(encoding="utf-8")
        assert "Machine-authored routing additions" in text  # header banner present
        parsed = yaml.safe_load(text)
        assert parsed["routing"]["routing_rules"]["downward"]["match"]["intents"] == ["creative_writing"]

    def test_idempotent_on_re_application(self, tmp_path: Path) -> None:
        mach = tmp_path / "routing.autonomaton.yaml"
        diff = {"routing": {"routing_rules": {"downward": {"match": {"intents": ["creative_writing"]}}}}}
        apply_diff_to_machine_config(diff, mach)
        text_a = mach.read_text(encoding="utf-8")
        apply_diff_to_machine_config(diff, mach)
        text_b = mach.read_text(encoding="utf-8")
        assert text_a == text_b

    def test_subsequent_diff_unions_intents(self, tmp_path: Path) -> None:
        mach = tmp_path / "routing.autonomaton.yaml"
        diff_a = {"routing": {"routing_rules": {"downward": {"match": {"intents": ["creative_writing"]}}}}}
        diff_b = {"routing": {"routing_rules": {"downward": {"match": {"intents": ["system_admin"]}}}}}
        apply_diff_to_machine_config(diff_a, mach)
        apply_diff_to_machine_config(diff_b, mach)
        parsed = yaml.safe_load(mach.read_text(encoding="utf-8"))
        intents = parsed["routing"]["routing_rules"]["downward"]["match"]["intents"]
        assert sorted(intents) == ["creative_writing", "system_admin"]

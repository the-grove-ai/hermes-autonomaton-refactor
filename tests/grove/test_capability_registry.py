"""Tests for the Capability registry + the real Workspace records (GRV-009 E2 C2)."""

import pytest
import yaml

from grove.capability import (
    CapabilityKind,
    LifecycleState,
    Provenance,
    Zone,
)
from grove.capability_registry import (
    CapabilityLoadError,
    default_capabilities_dir,
    load_capabilities,
)

REPO_CAPS = default_capabilities_dir()
WORKSPACE_IDS = {"workspace_read", "workspace_write", "workspace_destructive"}
WORKSPACE_INTENTS = ["memory_operation", "scheduling", "messaging", "retrieval"]


# ── The real records load + dry-run validate ─────────────────────────────────


def test_registry_loads_the_three_real_records():
    # E4 (mcp-migration-v1) added notion_read/notion_write to the same dir;
    # the three Workspace records must still be present and VERB-kind.
    caps = load_capabilities()
    assert WORKSPACE_IDS <= set(caps)
    for c in (caps[i] for i in WORKSPACE_IDS):
        assert c.kind is CapabilityKind.VERB
        assert c.lifecycle.state is LifecycleState.APPROVED
        assert c.lifecycle.provenance is Provenance.MIGRATED
        assert c.lifecycle.flywheel_eligible is True


def test_locked_zone_map():
    caps = load_capabilities()
    assert caps["workspace_read"].zone is Zone.GREEN
    assert caps["workspace_write"].zone is Zone.YELLOW
    assert caps["workspace_destructive"].zone is Zone.YELLOW


def test_locked_tier_parity():
    # Workspace records are T1/T2/T3-eligible; the E4 MCP records differ
    # ([3]-only) and are asserted in test_mcp_capability_records.py.
    caps = load_capabilities()
    for c in (caps[i] for i in WORKSPACE_IDS):
        assert c.tier_rule.eligible == [1, 2, 3]
        assert c.tier_rule.preferred == 1


def test_locked_intent_parity():
    caps = load_capabilities()
    for c in (caps[i] for i in WORKSPACE_IDS):
        assert c.trigger.intents == WORKSPACE_INTENTS


def test_records_seed_one_migration_decision_log_entry():
    caps = load_capabilities()
    for c in caps.values():
        assert len(c.lineage.decision_log) == 1
        rec = c.lineage.decision_log[0]
        assert rec.actor == "operator"
        assert rec.to_state == "approved"
        assert rec.evidence  # carries the sprint-page evidence id


def test_telemetry_feed_binding():
    # feed binding is shared by all migrated records; disclosure/dock_composition
    # are Workspace-specific (eager native verbs) — the E4 MCP records are
    # pull/none (asserted in test_mcp_capability_records.py).
    caps = load_capabilities()
    for c in caps.values():
        assert c.telemetry.feed == "intent_feed"
    for c in (caps[i] for i in WORKSPACE_IDS):
        assert c.context.disclosure.value == "eager"
        assert c.context.dock_composition.value == "goal_context"


# ── Fail-loud dry-run validation (Amendment A3) ──────────────────────────────


def test_invalid_record_fails_loud_with_filename_and_field(tmp_path):
    # one valid record + one invalid (preferred tier not in eligible).
    good = (REPO_CAPS / "workspace_read.yaml").read_text(encoding="utf-8")
    (tmp_path / "good.yaml").write_text(good, encoding="utf-8")

    bad = yaml.safe_load(good)
    bad["tier_rule"]["preferred"] = 9  # not in eligible -> validate() raises
    (tmp_path / "bad.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")

    with pytest.raises(CapabilityLoadError) as ei:
        load_capabilities(tmp_path)
    msg = str(ei.value)
    assert "bad.yaml" in msg          # names the file
    assert "preferred" in msg          # names the field


def test_no_partial_registry_on_failure(tmp_path):
    # A governance-bearing field emptied -> the whole load aborts loud.
    bad = yaml.safe_load((REPO_CAPS / "workspace_read.yaml").read_text(encoding="utf-8"))
    bad["telemetry"]["feed"] = ""  # governance field -> validate() raises
    (tmp_path / "x.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")

    with pytest.raises(CapabilityLoadError) as ei:
        load_capabilities(tmp_path)
    assert "x.yaml" in str(ei.value)


def test_missing_directory_fails_loud(tmp_path):
    with pytest.raises(CapabilityLoadError):
        load_capabilities(tmp_path / "does_not_exist")


def test_empty_directory_fails_loud(tmp_path):
    with pytest.raises(CapabilityLoadError):
        load_capabilities(tmp_path)

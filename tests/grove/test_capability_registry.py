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
    _configurable_toolset_keys,
    _validate_toolset_keys,
    default_capabilities_dir,
    load_capabilities,
)

REPO_CAPS = default_capabilities_dir()
WORKSPACE_IDS = {"workspace_read", "workspace_write", "workspace_destructive"}
# GRV-009 E5 C-RESOLVE parity fix — the workspace records' intents were extended
# to the full tool_groups reverse-map of the 24 verbs (+ system_admin, planning);
# E2 authored only the 4 daily-driver intents, but the verbs were also aliased
# into the system_admin / planning groups, so the registry-driven resolver needs
# those intents to reproduce the golden offered surface there.
WORKSPACE_INTENTS = [
    "memory_operation", "scheduling", "messaging", "retrieval",
    "system_admin", "planning",
]


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


def test_binding_collision_fails_loud(tmp_path):
    # GRV-009 E5 A4 — strict 1:1: two records claiming the same tool aborts the
    # load, naming both records and the colliding tool.
    base = yaml.safe_load((REPO_CAPS / "workspace_read.yaml").read_text(encoding="utf-8"))

    a = dict(base)
    a["id"] = "owner_a"
    a["bindings"] = {"tools": ["gmail_search"], "credentials": "google", "toolset_key": "google-workspace"}
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(a), encoding="utf-8")

    b = dict(base)
    b["id"] = "owner_b"
    b["bindings"] = {"tools": ["gmail_search"], "credentials": "google", "toolset_key": "google-workspace"}
    (tmp_path / "b.yaml").write_text(yaml.safe_dump(b), encoding="utf-8")

    with pytest.raises(CapabilityLoadError) as ei:
        load_capabilities(tmp_path)
    msg = str(ei.value)
    assert "gmail_search" in msg
    assert "owner_a" in msg and "owner_b" in msg


def test_disjoint_bindings_load_clean(tmp_path):
    # Two records with disjoint tool sets coexist — the 1:1 invariant only fires
    # on a genuine collision.
    base = yaml.safe_load((REPO_CAPS / "workspace_read.yaml").read_text(encoding="utf-8"))
    a = dict(base); a["id"] = "owner_a"
    a["bindings"] = {"tools": ["gmail_search"], "credentials": "google", "toolset_key": "google-workspace"}
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(a), encoding="utf-8")
    b = dict(base); b["id"] = "owner_b"
    b["bindings"] = {"tools": ["gmail_get"], "credentials": "google", "toolset_key": "google-workspace"}
    (tmp_path / "b.yaml").write_text(yaml.safe_dump(b), encoding="utf-8")

    caps = load_capabilities(tmp_path)
    assert {"owner_a", "owner_b"} <= set(caps)


def test_real_records_carry_backfilled_bindings():
    # GRV-009 E5 C-BACKFILL — the 5 live records explicitly claim their tool
    # subsets (locked D3 table). Counts + credentials + toolset_key per record.
    caps = load_capabilities()
    expected = {
        "workspace_read": (10, "google", "google-workspace"),
        "workspace_write": (9, "google", "google-workspace"),
        "workspace_destructive": (5, "google", "google-workspace"),
        "notion_read": (7, "notion-oauth", None),
        "notion_write": (9, "notion-oauth", None),
    }
    for rid, (n, cred, tk) in expected.items():
        b = caps[rid].bindings
        assert len(b.tools) == n, (rid, len(b.tools))
        assert b.credentials == cred
        assert b.toolset_key == tk


def test_real_records_bindings_are_strictly_one_to_one():
    # The 40 governed tools are disjoint across records — the load-time 1:1
    # invariant passed, and no tool is double-owned.
    caps = load_capabilities()
    governed = [
        t
        for rid in (
            "workspace_read", "workspace_write", "workspace_destructive",
            "notion_read", "notion_write",
        )
        for t in caps[rid].bindings.tools
    ]
    assert len(governed) == 40
    assert len(set(governed)) == 40  # no collision


def test_real_workspace_zone_tool_parity_with_zones_schema():
    # The bound tool sets match zones.schema.yaml exactly: read=green verbs,
    # write+destructive=the yellow verbs. Guards against drift between the
    # binding and the zone map the Dispatcher gate reads.
    caps = load_capabilities()
    assert set(caps["workspace_read"].bindings.tools) == {
        "gmail_search", "gmail_get", "gmail_labels", "calendar_list",
        "drive_search", "drive_get", "drive_download", "contacts_list",
        "sheets_get", "docs_get",
    }
    yellow = set(caps["workspace_write"].bindings.tools) | set(
        caps["workspace_destructive"].bindings.tools
    )
    assert yellow == {
        "gmail_send", "gmail_reply", "gmail_modify", "calendar_create",
        "calendar_delete", "drive_upload", "drive_create_folder", "drive_share",
        "drive_delete", "sheets_update", "sheets_append", "sheets_create",
        "docs_create", "docs_append",
    }


def test_real_notion_bindings_match_zones_schema():
    caps = load_capabilities()
    assert set(caps["notion_read"].bindings.tools) == {
        "mcp_notion_notion_search", "mcp_notion_notion_fetch",
        "mcp_notion_notion_get_comments", "mcp_notion_notion_get_users",
        "mcp_notion_notion_get_teams", "mcp_notion_notion_query_database_view",
        "mcp_notion_notion_query_meeting_notes",
    }
    assert set(caps["notion_write"].bindings.tools) == {
        "mcp_notion_notion_create_pages", "mcp_notion_notion_create_database",
        "mcp_notion_notion_update_page", "mcp_notion_notion_move_pages",
        "mcp_notion_notion_duplicate_page", "mcp_notion_notion_create_comment",
        "mcp_notion_notion_update_data_source", "mcp_notion_notion_create_view",
        "mcp_notion_notion_update_view",
    }


# ── D2<->D3 mutual check (GRV-009 E5 C-SEAM4) ────────────────────────────────


def test_unknown_toolset_key_on_record_fails_loud(tmp_path):
    # record -> key direction: a non-null toolset_key that is not a known
    # CONFIGURABLE_TOOLSETS key aborts the load, naming record + bad key.
    base = yaml.safe_load((REPO_CAPS / "workspace_read.yaml").read_text(encoding="utf-8"))
    base["id"] = "phantom"
    base["bindings"] = {
        "tools": ["gmail_search"],
        "credentials": "google",
        "toolset_key": "not_a_real_toolset",
    }
    (tmp_path / "phantom.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")

    with pytest.raises(CapabilityLoadError) as ei:
        load_capabilities(tmp_path)
    msg = str(ei.value)
    assert "not_a_real_toolset" in msg
    assert "phantom" in msg


def test_hosted_mcp_null_toolset_key_does_not_fail(tmp_path):
    # A null toolset_key (hosted MCP) is skipped by the record->key check.
    base = yaml.safe_load((REPO_CAPS / "notion_read.yaml").read_text(encoding="utf-8"))
    (tmp_path / "n.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
    caps = load_capabilities(tmp_path)
    assert "notion_read" in caps


def test_real_registry_has_zero_uncovered_after_verbs():
    # GRV-009 E5 C-VERBS drove the migration-coverage gap to zero: every
    # CONFIGURABLE_TOOLSETS key now has a governing capability record.
    caps = load_capabilities()
    uncovered = _validate_toolset_keys(caps)
    assert uncovered == frozenset(), f"residual uncovered keys: {sorted(uncovered)}"


def test_uncovered_keys_reported_not_raised_synthetic(tmp_path):
    # key -> record direction (mechanism): a record set that covers only one key
    # leaves the rest uncovered — returned for reporting, NEVER raised.
    base = yaml.safe_load((REPO_CAPS / "workspace_read.yaml").read_text(encoding="utf-8"))
    base["bindings"] = {"tools": ["gmail_search"], "credentials": "google",
                        "toolset_key": "google-workspace"}
    (tmp_path / "only.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")

    caps = load_capabilities(tmp_path)          # loads clean — no raise on uncovered
    uncovered = _validate_toolset_keys(caps)
    valid = _configurable_toolset_keys()
    assert "google-workspace" not in uncovered  # the lone covered key
    assert "web" in uncovered                    # the rest are reported, not fatal
    assert uncovered < valid


# ── A4t disclosure modes (derived from the golden snapshot) ──────────────────


def test_real_records_disclosure_modes_match_golden():
    from grove.capability import TriggerDisclosure as TD
    caps = load_capabilities()
    # exploratory cohort -> complexity (present on complex-known T3, absent simple).
    for rid in ("browser_read", "browser_write", "delegate_task", "mixture_of_agents",
                "vision_analyze", "video_analyze", "feishu_doc_read", "ha_get_state",
                "ha_call_service"):
        assert caps[rid].trigger.disclosure is TD.COMPLEXITY, rid
    # never-grouped integrations -> fallback (only on maximal unknown fallback).
    for rid in ("spotify_write", "kanban_read", "kanban_write", "yuanbao_read",
                "yuanbao_write", "discord", "discord_admin", "computer_use", "todo",
                "invoke_skill", "send_message", "feishu_read", "feishu_write",
                "homeassistant_read"):
        assert caps[rid].trigger.disclosure is TD.FALLBACK, rid
        # carve-out holds on the real records: no proactive trigger.
        assert not caps[rid].trigger.always and not caps[rid].trigger.intents
    # core + intent records stay proactive.
    for rid in ("clarify", "memory", "read_file", "terminal", "escalate",
                "web_search", "search_files", "execute_code", "x_search"):
        assert caps[rid].trigger.disclosure is TD.PROACTIVE, rid


def test_missing_directory_fails_loud(tmp_path):
    with pytest.raises(CapabilityLoadError):
        load_capabilities(tmp_path / "does_not_exist")


def test_empty_directory_fails_loud(tmp_path):
    with pytest.raises(CapabilityLoadError):
        load_capabilities(tmp_path)

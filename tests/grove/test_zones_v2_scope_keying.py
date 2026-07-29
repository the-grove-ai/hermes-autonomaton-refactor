"""zones-v2-scope-keying P1b — the scope-keyed effect-class model.

Covers the P1b test matrix:
  * derivation matrix (six effect classes × derived zone / target class)
  * loader version acceptance (v1 loads, v2 loads, other hard-rejects)
  * malformed-overlay loud refusal (absent loads; malformed raises) [A2]
  * retired-key loud refusal, per key class [A3]
  * merge-idiom regression (no silent yellow default) [zones.py:702 / R-4]
  * governed-target set-diff: the repo v2 schema derives byte-identical zones
    vs the v1 baseline over the unchanged 136-tool collection.

Every test builds its schema in ``tmp_path`` (or reads the repo default via the
module singleton) — GROVE_HOME is per-test-isolated by the autouse conftest
fixture, so no operator overlay interferes.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from grove.zones import (
    ZoneClassifier,
    _load_and_validate_overlay,
    merge_zone_schemas,
)


# ── a minimal, valid v2 schema (six classes + one tool per class) ─────────────
_V2_MIN = """
schema_version: 2
red_denied_by_policy: []
effect_classes:
  read_only:       {derives: green}
  contained_write: {derives: green}
  workspace_write: {derives: yellow}
  external_effect: {derives: yellow}
  governance:      {derives: red}
  operator_only:   {derives: red}
default_unmatched: yellow
tool_effects:
  a_read:   read_only
  a_jailed:
    class: contained_write
    containment: "some/module.py:1 (cited jail)"
  a_fswrite: workspace_write
  a_extern:  external_effect
  a_gov:     governance
  a_secure:  operator_only
"""


def _write(tmp_path: Path, content: str, name: str = "zones.schema.yaml") -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content).lstrip())
    return p


# ── Derivation matrix — each class derives its declared zone ──────────────────
@pytest.mark.parametrize(
    "tool, expected_zone",
    [
        ("a_read", "green"),      # read_only → green
        ("a_jailed", "green"),    # contained_write → green (cited)
        ("a_fswrite", "yellow"),  # workspace_write → yellow at bare gate
        ("a_extern", "yellow"),   # external_effect → yellow
        ("a_gov", "red"),         # governance → red
        ("a_secure", "red"),      # operator_only → red
    ],
)
def test_derivation_matrix(tmp_path: Path, tool: str, expected_zone: str) -> None:
    clf = ZoneClassifier(_write(tmp_path, _V2_MIN))
    assert clf.classify(tool).zone == expected_zone


def test_unmatched_derives_declared_default(tmp_path: Path) -> None:
    clf = ZoneClassifier(_write(tmp_path, _V2_MIN))
    r = clf.classify("no_such_tool")
    assert r.zone == "yellow"
    assert r.source == "default"


def test_default_unmatched_is_enforced_not_hardcoded(tmp_path: Path) -> None:
    """A schema that declares a different default posture is honored (enforced)."""
    schema = _V2_MIN.replace("default_unmatched: yellow", "default_unmatched: red")
    clf = ZoneClassifier(_write(tmp_path, schema))
    assert clf.classify("no_such_tool").zone == "red"


# ── contained_write REQUIRES a cited containment primitive (A1) ───────────────
def test_contained_write_without_citation_refused(tmp_path: Path) -> None:
    schema = _V2_MIN.replace(
        '  a_jailed:\n    class: contained_write\n'
        '    containment: "some/module.py:1 (cited jail)"',
        "  a_jailed: contained_write",
    )
    with pytest.raises(ValueError, match="contained_write.*containment"):
        ZoneClassifier(_write(tmp_path, schema))


# ── effect-class enum is CLOSED (six exactly) ─────────────────────────────────
def test_effect_classes_must_be_the_six_closed_set(tmp_path: Path) -> None:
    schema = _V2_MIN.replace(
        "  operator_only:   {derives: red}",
        "  operator_only:   {derives: red}\n  rogue_class:     {derives: green}",
    )
    with pytest.raises(ValueError, match="six closed classes"):
        ZoneClassifier(_write(tmp_path, schema))


def test_tool_declaring_unknown_class_refused(tmp_path: Path) -> None:
    schema = _V2_MIN.replace("  a_read:   read_only", "  a_read:   made_up_class")
    with pytest.raises(ValueError, match="not one of the six declared classes"):
        ZoneClassifier(_write(tmp_path, schema))


# ── Loader version acceptance: v1 loads, v2 loads, other hard-rejects ─────────
_V1_MIN = """
schema_version: 1
zones:
  green:  {auto_approve: [file.read.*]}
  yellow: {proposes: [skill.create.*]}
  red:    {sovereign: [command.execute.sudo]}
tool_zones:
  read_file: green
  write_file: yellow
"""


def test_loader_accepts_v1(tmp_path: Path) -> None:
    clf = ZoneClassifier(_write(tmp_path, _V1_MIN))
    assert clf.classify("read_file").zone == "green"
    assert clf.classify("write_file").zone == "yellow"


def test_loader_accepts_v2(tmp_path: Path) -> None:
    clf = ZoneClassifier(_write(tmp_path, _V2_MIN))
    assert clf.classify("a_read").zone == "green"


def test_loader_rejects_other_version(tmp_path: Path) -> None:
    schema = _V2_MIN.replace("schema_version: 2", "schema_version: 3")
    with pytest.raises(ValueError, match="expected 1 or 2"):
        ZoneClassifier(_write(tmp_path, schema))


# ── Overlay malformed refusal [A2] — absent loads, malformed raises ───────────
def test_overlay_absent_returns_none(tmp_path: Path) -> None:
    # Nonexistent overlay path is never passed to the validator in production
    # (_resolve_overlay_path returns None first); an EMPTY file == absent.
    empty = _write(tmp_path, "", name="zones.autonomaton.yaml")
    assert _load_and_validate_overlay(empty) is None


def test_overlay_malformed_yaml_raises(tmp_path: Path) -> None:
    bad = tmp_path / "zones.autonomaton.yaml"
    bad.write_text("key: [unclosed\n:::not yaml:::\n")
    with pytest.raises(ValueError, match="is not valid YAML"):
        _load_and_validate_overlay(bad)


def test_overlay_non_mapping_raises(tmp_path: Path) -> None:
    seq = _write(tmp_path, "- a\n- b\n", name="zones.autonomaton.yaml")
    with pytest.raises(ValueError, match="did not parse to a mapping.*REFUSING"):
        _load_and_validate_overlay(seq)


# ── Overlay retired-key refusal [A3] — per key class, NAMED, never dropped ────
def test_overlay_retired_tool_zones_refused(tmp_path: Path) -> None:
    ov = _write(
        tmp_path,
        "schema_version: 1\ntool_zones:\n  terminal:\n    default_zone: yellow\n",
        name="zones.autonomaton.yaml",
    )
    with pytest.raises(ValueError, match="RETIRED key 'tool_zones'"):
        _load_and_validate_overlay(ov)


def test_overlay_retired_zones_block_refused(tmp_path: Path) -> None:
    ov = _write(
        tmp_path,
        "schema_version: 1\nzones:\n  green: {auto_approve: [file.read.*]}\n",
        name="zones.autonomaton.yaml",
    )
    with pytest.raises(ValueError, match="RETIRED key 'zones'"):
        _load_and_validate_overlay(ov)


def test_overlay_unknown_key_refused(tmp_path: Path) -> None:
    ov = _write(
        tmp_path, "schema_version: 1\nwidgets: 3\n", name="zones.autonomaton.yaml"
    )
    with pytest.raises(ValueError, match="unrecognized key 'widgets'"):
        _load_and_validate_overlay(ov)


def test_overlay_valid_keys_accepted(tmp_path: Path) -> None:
    ov = _write(
        tmp_path,
        'schema_version: 1\nred_denied_by_policy: ["priv:"]\n',
        name="zones.autonomaton.yaml",
    )
    data = _load_and_validate_overlay(ov)
    assert data == {"schema_version": 1, "red_denied_by_policy": ["priv:"]}


# ── Merge-idiom regression [zones.py:702 / R-4] — no silent yellow default ────
def test_merge_refuses_overlay_tool_without_default_zone() -> None:
    repo = {"schema_version": 1, "tool_zones": {}}
    overlay = {
        "schema_version": 1,
        "tool_zones": {"newtool": {"rules": [{"match_pattern": "x", "zone": "green"}]}},
    }
    with pytest.raises(ValueError, match="no resolvable default_zone"):
        merge_zone_schemas(repo, overlay)


def test_merge_accepts_overlay_tool_with_explicit_default() -> None:
    repo = {"schema_version": 1, "tool_zones": {}}
    overlay = {"schema_version": 1, "tool_zones": {"newtool": {"default_zone": "green"}}}
    merged = merge_zone_schemas(repo, overlay)
    assert merged["tool_zones"]["newtool"]["default_zone"] == "green"


# ── Governed set-diff: repo v2 derives byte-identical vs v1 baseline ──────────
# The v1 baseline zone of every tool in the UNCHANGED collection (137 bare v1
# entries minus the RETIRED `memory` entry = 136). Frozen here as the parity
# oracle; the v2 repo schema must derive each identically.
_BASELINE_GREEN = frozenset({
    "andon_list", "browser_back", "browser_console", "browser_get_images",
    "browser_navigate", "browser_scroll", "browser_snapshot", "browser_vision",
    "calendar_list", "cellar_search", "clarify", "contacts_list", "docs_get",
    "drive_download", "drive_get", "drive_search", "emit_package",
    "feishu_doc_read", "feishu_drive_list_comment_replies",
    "feishu_drive_list_comments", "gmail_get", "gmail_labels", "gmail_search",
    "ha_get_state", "ha_list_entities", "ha_list_services", "honcho.dialectic.read",
    "kanban_list", "kanban_show", "mcp_grove_browser_browser_extract",
    "mcp_grove_browser_browser_read_page", "mcp_grove_browser_browser_screenshot",
    "mcp_grove_browser_browser_search", "mcp_grove_browser_browser_session",
    "mcp_notion_notion_fetch", "mcp_notion_notion_get_comments",
    "mcp_notion_notion_get_teams", "mcp_notion_notion_get_users",
    "mcp_notion_notion_query_database_view", "mcp_notion_notion_query_meeting_notes",
    "mcp_notion_notion_search", "read_capability_state", "read_file",
    "review_grants", "review_proposals", "search_files", "session_search",
    "sheets_get", "skill_view", "skills_list", "todo", "video_analyze",
    "vision_analyze", "web_extract", "web_search", "x_search",
    "yb_query_group_info", "yb_query_group_members", "yb_search_sticker",
})
_BASELINE_YELLOW = frozenset({
    "approve_proposal", "browser_cdp", "browser_click", "browser_dialog",
    "browser_press", "browser_type", "calendar_create", "calendar_delete",
    "cron.create", "cron.delete", "cronjob", "delegate_task", "discord",
    "dismiss_proposal", "docs_append", "docs_create", "drive_create_folder",
    "drive_delete", "drive_share", "drive_upload", "execute_code",
    "feishu_drive_add_comment", "feishu_drive_reply_comment",
    "file_operations.write", "gmail_modify", "gmail_reply", "gmail_send",
    "ha_call_service", "image_generate", "invoke_skill", "kanban_block",
    "kanban_comment", "kanban_complete", "kanban_create", "kanban_heartbeat",
    "kanban_link", "kanban_unblock", "mcp_notion_notion_create_comment",
    "mcp_notion_notion_create_database", "mcp_notion_notion_create_pages",
    "mcp_notion_notion_create_view", "mcp_notion_notion_duplicate_page",
    "mcp_notion_notion_move_pages", "mcp_notion_notion_update_data_source",
    "mcp_notion_notion_update_page", "mcp_notion_notion_update_view",
    "mixture_of_agents", "patch", "process", "reject_proposal", "revoke_grant",
    "send_message", "set_publication_state", "sheets_append", "sheets_create",
    "sheets_update", "skill_manage", "skill_manage.create", "spotify_albums",
    "spotify_devices", "spotify_library", "spotify_playback", "spotify_playlists",
    "spotify_queue", "spotify_search", "text_to_speech", "video_generate",
    "write_file", "yb_send_dm", "yb_send_sticker",
})
_BASELINE_RED = frozenset({
    "andon_promote", "andon_reject", "andon_revoke", "computer_use",
    "discord_admin", "fleet_purge", "skill_manage.promote",
})


def _repo_v2_classifier() -> ZoneClassifier:
    from grove.zones import _resolve_schema_path

    return ZoneClassifier(_resolve_schema_path(None))


def test_baseline_counts_preserved() -> None:
    assert len(_BASELINE_GREEN) == 59
    assert len(_BASELINE_YELLOW) == 70
    assert len(_BASELINE_RED) == 7


def test_repo_v2_set_diff_byte_identical() -> None:
    clf = _repo_v2_classifier()
    mism = []
    for tool in _BASELINE_GREEN:
        if clf.classify(tool).zone != "green":
            mism.append((tool, "green", clf.classify(tool).zone))
    for tool in _BASELINE_YELLOW:
        if clf.classify(tool).zone != "yellow":
            mism.append((tool, "yellow", clf.classify(tool).zone))
    for tool in _BASELINE_RED:
        if clf.classify(tool).zone != "red":
            mism.append((tool, "red", clf.classify(tool).zone))
    assert not mism, f"v2 derivation diverged from v1 baseline: {mism}"


def test_memory_entry_retired() -> None:
    """The v1 `memory: green` entry is deleted; nothing declares it now."""
    clf = _repo_v2_classifier()
    assert "memory" not in clf._tool_effects
    # No live tool emits tool_name 'memory' (registry invariant); classify falls
    # to the declared default posture — behaviorally null.
    assert clf.classify("memory").source == "default"


def test_repo_schema_is_v2_with_six_classes() -> None:
    clf = _repo_v2_classifier()
    assert set(clf._effect_derivations) == {
        "read_only", "contained_write", "workspace_write",
        "external_effect", "governance", "operator_only",
    }

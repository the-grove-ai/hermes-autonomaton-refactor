"""zone-rekey-v2-scope-model — GRV-001 v2.0 scope-keyed shell zone enforcement.

The shell-effect classifier no longer treats all of ~/.grove as RED. A write's
zone is keyed on SCOPE (positive allowlist as of
workspace-governance-unification-v1):
  * scope-defining surface (zone schema, routing/prompt config, dock goals,
    operator secrets, the live skills tree, the capability registry) -> RED
  * operator-granted workspace (declared in workspaces.yaml) -> GREEN (autonomous)
  * under ~/.grove but NOT granted -> RED (fail-closed; this superseded the v2
    blanket-GREEN complement and closes the credential-overwrite path)
  * outside ~/.grove -> YELLOW (four-choice operator grant)

is_governed_path (the blanket wall for generic file tools) is intentionally
NOT touched by this sprint; see tests/grove/test_c1b_substrate_write.py for its
unchanged contract.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    """Redirect GROVE_HOME to a materialized tmp tree so scope keying is testable
    without touching the operator's real ~/.grove. Mirrors the C1b fixture."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    # Materialize the tree so realpath resolves symlink/.. escapes cleanly.
    (tmp_path / "dock").mkdir(parents=True, exist_ok=True)
    (tmp_path / "skills" / ".andon").mkdir(parents=True, exist_ok=True)
    (tmp_path / "skills" / "my-skill").mkdir(parents=True, exist_ok=True)
    (tmp_path / "skills" / "active" / "foo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "capabilities").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "research").mkdir(parents=True, exist_ok=True)
    for fname in ("zones.schema.yaml", "routing.config.yaml", "prompt.config.yaml", ".env"):
        (tmp_path / fname).write_text("x\n")
    (tmp_path / "dock" / "dock.yaml").write_text("x\n")
    (tmp_path / "skills" / "my-skill" / "SKILL.md").write_text("x\n")
    (tmp_path / "skills" / "active" / "foo" / "SKILL.md").write_text("x\n")
    (tmp_path / "capabilities" / "bar.yaml").write_text("x\n")
    (tmp_path / "memory" / "records.jsonl").write_text("x\n")
    (tmp_path / "research" / "doc.md").write_text("x\n")
    (tmp_path / "notes").mkdir(parents=True, exist_ok=True)
    # workspace-governance-unification-v1: research/ and notes/ are GREEN only
    # because the operator granted them here. Un-granted paths (memory/, etc.)
    # are RED under the positive-allowlist model.
    (tmp_path / "workspaces.yaml").write_text(
        "granted_workspaces:\n  - path: research/\n  - path: notes/\n"
    )
    return tmp_path


# ── is_scope_defining (SPEC tests 1-10) ──────────────────────────────────────


class TestIsScopeDefining:
    def test_1_zones_schema(self, grove_home):
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "zones.schema.yaml") is True

    def test_2_routing_config(self, grove_home):
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "routing.config.yaml") is True

    def test_3_prompt_config(self, grove_home):
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "prompt.config.yaml") is True

    def test_4_dock_yaml(self, grove_home):
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "dock" / "dock.yaml") is True

    def test_5_skills_active_subtree(self, grove_home):
        # Whole skills tree is scope-defining (skills/active/<name>/SKILL.md too).
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "skills" / "active" / "foo" / "SKILL.md") is True

    def test_6_capabilities_subtree(self, grove_home):
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "capabilities" / "bar.yaml") is True

    def test_7_memory_is_granted_workspace(self, grove_home):
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "memory" / "records.jsonl") is False

    def test_8_research_is_granted_workspace(self, grove_home):
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "research" / "doc.md") is False

    def test_9_dotdot_traversal_to_scope_defining(self, grove_home):
        # realpath collapses ../.. back to the grove root → scope-defining.
        from grove.utils.fs_utils import is_scope_defining
        escape = grove_home / "skills" / ".andon" / ".." / ".." / "zones.schema.yaml"
        assert is_scope_defining(escape) is True

    def test_10_symlink_to_scope_defining(self, grove_home, tmp_path):
        from grove.utils.fs_utils import is_scope_defining
        link = tmp_path.parent / "sneaky_zones_link.yaml"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(grove_home / "zones.schema.yaml")
        assert is_scope_defining(link) is True

    # ── Inline-amendment regression guards ──
    def test_live_skill_name_is_scope_defining(self, grove_home):
        # The real live tree is skills/<name> (not skills/active) — also RED.
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "skills" / "my-skill" / "SKILL.md") is True

    def test_env_secrets_is_scope_defining(self, grove_home):
        # Amendment #2: secrets must not be an autonomous workspace write.
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / ".env") is True

    def test_outside_grove_is_not_scope_defining(self, grove_home, tmp_path):
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(tmp_path.parent / "elsewhere.txt") is False

    def test_grove_root_is_scope_defining(self, grove_home):
        # The root contains every scope-defining surface — mutating it is RED.
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home) is True

    def test_ancestor_of_surface_is_scope_defining(self, grove_home):
        # `dock` is the parent of the scope-defining dock/dock.yaml.
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "dock") is True


# ── routing-scope-wall-v1 R-W1 — set additions + prefix strictness ───────────


class TestScopeWallAdditions:
    def test_routing_autonomaton_yaml(self, grove_home):
        # the machine routing overlay — sibling of routing.config.yaml, authority.
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "routing.autonomaton.yaml") is True

    def test_zones_autonomaton_yaml(self, grove_home):
        # runtime zone overlay — grants GREEN zones, so it is authority.
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "zones.autonomaton.yaml") is True

    def test_fleet_workers_override_yaml(self, grove_home):
        # per-worker enable overrides — controls autonomous fleet execution.
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "fleet_workers.override.yaml") is True

    def test_routing_profiles_subtree(self, grove_home):
        # alternate tier presets — routing authority; the whole subtree is RED.
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "routing-profiles" / "gemma-mac.yaml") is True

    def test_routing_profiles_dir_itself(self, grove_home):
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "routing-profiles") is True

    def test_routing_profiles_backups_not_trapped(self, grove_home):
        # strict path-separator matching: a sibling dir that merely SHARES the
        # prefix string must NOT be trapped by the routing-profiles subtree.
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "routing-profiles-backups" / "old.yaml") is False

    def test_dock_autonomaton_yaml_stays_out(self, grove_home):
        # the machine half of the Dock is a GREEN workspace, NOT scope-defining.
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "dock" / "dock.autonomaton.yaml") is False


class TestCapabilitiesStatePin:
    # Finding-2 ruling: no separate capabilities/state/ entry — the existing
    # "capabilities" prefix already covers it. Pin both directions.
    def test_capabilities_state_subtree_is_scope_defining(self, grove_home):
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining(grove_home / "capabilities" / "state" / "s.yaml") is True

    def test_repo_config_capabilities_outside_grove_is_not(self, grove_home, tmp_path):
        # the repo config/capabilities/ tree resolves OUTSIDE ~/.grove → not scope-defining.
        from grove.utils.fs_utils import is_scope_defining
        outside = tmp_path.parent / "repo_config" / "capabilities" / "x.yaml"
        assert is_scope_defining(outside) is False


class TestQuarantineOnlyDoor:
    # routing-scope-wall-v1 R-W1/R-W3/R-W5 — the FILE-TOOL wall fires on the
    # composed predicate is_scope_defining(t) AND is_governed_path(t). This pins
    # that the composed predicate's ONLY exception is the .andon authoring
    # quarantine: every scope-defining member/prefix is walled, .andon is the sole
    # survivor. Tripwire — if is_governed_path ever grows another carve-out, the
    # wall fails loud here instead of silently inheriting it.
    def test_every_scope_defining_member_walled_andon_sole_survivor(self, grove_home):
        from grove.utils import fs_utils as fu

        def _walled(rel):
            t = grove_home / rel
            return fu.is_scope_defining(t) and fu.is_governed_path(t)

        for f in fu._SCOPE_DEFINING_FILES:
            assert _walled(f), f"scope-defining file not walled by composed predicate: {f}"
        for p in fu._SCOPE_DEFINING_DIR_PREFIXES:
            assert _walled(f"{p}/x.yaml"), f"scope-defining prefix not walled: {p}"

        # The .andon quarantine is the SOLE survivor: scope-defining, yet the
        # composed predicate lets it through (is_governed_path allowlists it).
        andon = grove_home / "skills" / ".andon" / "draft" / "SKILL.md"
        assert fu.is_scope_defining(andon) is True
        assert fu.is_governed_path(andon) is False
        assert not (fu.is_scope_defining(andon) and fu.is_governed_path(andon))


class TestScopeWallPrecondition:
    # Ruling (B): plain unset GROVE_HOME keeps the ~/.grove default; fail closed
    # ONLY when the effective grove root is degenerate (empty/relative/unresolvable).
    def test_unset_env_uses_grove_default_normal_classification(self, monkeypatch):
        monkeypatch.delenv("GROVE_HOME", raising=False)
        from grove.utils.fs_utils import is_scope_defining
        # a clearly-outside path stays False — NOT fail-closed-all-True.
        assert is_scope_defining("/tmp/definitely-outside-grove-xyz-rw1") is False

    def test_relative_grove_root_fails_closed(self):
        from grove.utils.fs_utils import is_scope_defining
        # a relative root is CWD-dependent and untrustworthy → every path RED.
        assert is_scope_defining("/tmp/anything", grove_home="relative/grove") is True
        assert is_scope_defining("/etc/passwd", grove_home="./foo") is True

    def test_empty_grove_root_fails_closed(self):
        from grove.utils.fs_utils import is_scope_defining
        assert is_scope_defining("/tmp/anything", grove_home="") is True

    def test_unresolvable_grove_root_fails_closed(self):
        from grove.utils.fs_utils import is_scope_defining
        # absolute but unresolvable (embedded NUL) → realpath raises → fail closed.
        assert is_scope_defining("/tmp/x", grove_home="/tmp/\x00bad") is True


# ── _classify_write_zone (SPEC tests 11-13) ──────────────────────────────────


class TestClassifyWriteZone:
    def test_11_workspace_green(self, grove_home):
        from grove.shell_effects import _classify_write_zone
        assert _classify_write_zone(str(grove_home / "research" / "doc.md")) == "green"

    def test_12_scope_defining_red(self, grove_home):
        from grove.shell_effects import _classify_write_zone
        assert _classify_write_zone(str(grove_home / "zones.schema.yaml")) == "red"

    def test_13_outside_yellow(self, grove_home, tmp_path):
        from grove.shell_effects import _classify_write_zone
        assert _classify_write_zone(str(tmp_path.parent / "Desktop_output.txt")) == "yellow"


# ── classify_shell_effect end-to-end zone (SPEC tests 14-16) ─────────────────


class TestClassifyShellEffectScope:
    def test_14_green_workspace_write_no_red(self, grove_home):
        # The reported bug: mkdir into a workspace now classifies GREEN (no halt).
        from grove.shell_effects import classify_shell_effect
        zr = classify_shell_effect(f"mkdir -p {grove_home / 'research' / 'x'}")
        assert zr.zone == "green"

    def test_15_red_scope_defining_write(self, grove_home):
        from grove.shell_effects import classify_shell_effect
        zr = classify_shell_effect(f"echo data > {grove_home / 'zones.schema.yaml'}")
        assert zr.zone == "red"

    def test_16_yellow_outside_write(self, grove_home, tmp_path):
        from grove.shell_effects import classify_shell_effect
        zr = classify_shell_effect(f"echo data > {tmp_path.parent / 'output.txt'}")
        assert zr.zone == "yellow"

    # ── Inline-amendment + safety regression guards ──
    def test_read_secrets_is_red(self, grove_home):
        # shell-grove-access-v1: secret reads via shell are RED (parity with the
        # file tools' reject_governed_agent_read). Closes the secret-read hole —
        # was YELLOW (operator-approvable), now hard RED.
        from grove.shell_effects import classify_shell_effect
        zr = classify_shell_effect(f"cat {grove_home / '.env'}")
        assert zr.zone == "red"
        assert "secret" in (zr.pattern_key or "")

    def test_overwrite_secrets_is_red(self, grove_home):
        # Amendment #2: `echo x > ~/.grove/.env` must stay RED (was RED in v1).
        from grove.shell_effects import classify_shell_effect
        zr = classify_shell_effect(f"echo x > {grove_home / '.env'}")
        assert zr.zone == "red"

    def test_code_interp_redirect_is_red(self, grove_home):
        # Phase-2 Change 1 (supersedes operational-toolkit-v1): `python -c` is
        # bucket-3 RED (UNRESOLVED_WRITER) — the opaque payload can write anywhere
        # the AST cannot see, so it dominates regardless of the visible redirect
        # target (even a granted workspace). Redirect-target zoning for a
        # transparent verb is covered in test_shell_effects.
        from grove.shell_effects import classify_shell_effect
        zr = classify_shell_effect(f"python3 -c 'print(1)' > {grove_home / 'research' / 'x'}")
        assert zr.zone == "red"
        assert "UNRESOLVED_WRITER" in (zr.matched_rule or "")

    def test_transparent_verb_redirect_into_grove_path(self, grove_home):
        # shell-grove-access-v1 + Phase-2 Change 1: a TRANSPARENT verb (echo) whose
        # redirect target the AST can extract is zoned by that target — a non-secret
        # ~/.grove path is YELLOW (operator-approvable, matches write_file), a SECRET
        # stays RED (the secret wall runs before any benign-zone shortcut). (The old
        # test used `python -c`, which is now bucket-3 RED regardless of target.)
        from grove.shell_effects import classify_shell_effect
        assert classify_shell_effect(
            f"echo x > {grove_home / 'memory' / 'x'}"
        ).zone == "yellow"
        assert classify_shell_effect(
            f"echo x > {grove_home / '.env'}"
        ).zone == "red"

    def test_rm_rf_grove_root_is_red(self, grove_home):
        # Deleting the grove root as a unit destroys scope-defining surfaces.
        from grove.shell_effects import classify_shell_effect
        zr = classify_shell_effect(f"rm -rf {grove_home}")
        assert zr.zone == "red"

"""write-routing-coherence-v1 fix-part-1 — the running tree's ``config/`` meta-
surfaces classify RED, coherently across BOTH file-tool write doors and the shell
door.

The forge-arming misfire granted the deployed repo clone as a write workspace and
``patch``-ed ``config/capabilities/skill__fleet__forge_jobsearch.yaml`` — the
deployed DEFINITION file. Before this fix that landed as a generic Yellow write
(``is_scope_defining`` was anchored to GROVE_HOME only), while the identical file
under ``~/.grove/capabilities/`` was RED. These tests pin the closure:

  * ``is_scope_defining`` now walls the ``<module_root>/config/`` twins (module_root
    anchor), so a patch OR a shell sed to any repo meta-surface is RED.
  * The file-tool doors ride ``is_scope_defining AND not is_andon_quarantine``
    (retargeted from the stale ``AND is_governed_path``, which re-imposed the
    ~/.grove anchor and silently demoted the repo surfaces back to Yellow). The
    LOAD-BEARING assertion is the file_tools ``patch`` case resolving RED end-to-end.
  * GROVE_HOME twins are unchanged (still RED).
  * Over-match guard: a granted workspace OUTSIDE both anchors that merely contains
    a ``capabilities/`` / ``dock.yaml`` component is NOT trapped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grove.utils.fs_utils import (
    _MODULE_CONFIG_ROOT,
    is_andon_quarantine,
    is_scope_defining,
)
from grove.shell_effects import _classify_write_zone
from grove.dispatch import classify_command
from grove.dispatcher import Dispatcher
from grove.governance_halt import TerminalGovernanceHalt
from tools.file_tools import _verify_scope_defining_execution

CONFIG = Path(_MODULE_CONFIG_ROOT)

# The six running-tree meta-surfaces the SPEC enumerates. Classification is a pure
# path predicate — NO file is created; the probe basenames need not exist on disk.
REPO_SURFACES = [
    CONFIG / "capabilities" / "skill__fleet__forge_jobsearch.yaml",
    CONFIG / "routing-profiles" / "conservative.yaml",
    CONFIG / "dock" / "dock.yaml",
    CONFIG / "zones.schema.yaml",
    CONFIG / "routing.config.yaml",
    CONFIG / "write_workspaces.yaml",
]
_ids = [str(p.relative_to(CONFIG)) for p in REPO_SURFACES]


def _patch_args(target: str) -> dict:
    return {"mode": "replace", "path": target, "old_string": "a", "new_string": "b"}


# ── the shared predicate ─────────────────────────────────────────────────────
@pytest.mark.parametrize("surface", REPO_SURFACES, ids=_ids)
def test_repo_config_surface_is_scope_defining(surface):
    assert is_scope_defining(str(surface)) is True
    # never the .andon authoring quarantine — so the retargeted file-tool doors fire.
    assert is_andon_quarantine(str(surface)) is False


# ── shell door (sed) ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("surface", REPO_SURFACES, ids=_ids)
def test_repo_config_surface_shell_sed_red(surface):
    # the direct predicate the shell write-target classifier consults …
    assert _classify_write_zone(str(surface)) == "red"
    # … and an end-to-end shell write through the AST effect classifier. Uses a
    # REDIRECT (the govwrite:redirect path that consults the same
    # _classify_write_zone), NOT ``sed -i``: sed -i is a pre-existing
    # shell.effect.default (Yellow even for GROVE_HOME scope-defining twins), a
    # gap in the sed effect classifier orthogonal to this sprint (see HANDOFF).
    zr = classify_command(f"echo x > {surface}")
    assert zr.zone == "red"


# ── file-tool door (patch) — LOAD-BEARING (was silently Yellow) ──────────────
@pytest.mark.parametrize("surface", REPO_SURFACES, ids=_ids)
def test_repo_config_surface_file_tools_patch_red(surface):
    target = str(surface)
    # (a) Stage-04 classification door (Seam β) → RED via the scope wall.
    intent = type("I", (), {"tool_name": "patch", "arguments": _patch_args(target)})()
    zr = Dispatcher._classify_one_intent(intent, None)
    assert zr.zone == "red"
    assert zr.source == "scope_wall"
    # (b) execution-time TOCTOU guard → halts LOUD before any physical write
    #     (no approved-effect token in this context).
    with pytest.raises(TerminalGovernanceHalt):
        _verify_scope_defining_execution("patch", _patch_args(target))


# ── GROVE_HOME twins unchanged ───────────────────────────────────────────────
@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    g = tmp_path / "grove"
    g.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(g))
    return g


GROVE_TWINS = [
    "capabilities/skill__fleet__forge_jobsearch.yaml",
    "routing-profiles/conservative.yaml",
    "dock/dock.yaml",
    "zones.schema.yaml",
    "routing.config.yaml",
    "write_workspaces.yaml",
    # skills/ has NO config/ twin but stays RED under GROVE_HOME (unchanged).
    "skills/my_skill/SKILL.md",
]


@pytest.mark.parametrize("rel", GROVE_TWINS)
def test_grove_home_twins_still_red(grove_home, rel):
    target = str(grove_home / rel)
    assert is_scope_defining(target) is True
    assert _classify_write_zone(target) == "red"


# ── over-match guard (P3) ────────────────────────────────────────────────────
OVER_MATCH_COMPONENTS = [
    "skills/x.py",
    "capabilities/y.yaml",
    "routing-profiles/z.yaml",
    "dock/dock.yaml",
    "zones.schema.yaml",
]


@pytest.mark.parametrize("rel", OVER_MATCH_COMPONENTS)
def test_over_match_guard_outside_both_anchors_not_scope_defining(grove_home, tmp_path, rel):
    # A project dir OUTSIDE ~/.grove AND outside <module_root>/config that merely
    # CONTAINS a scope-defining-looking component must NOT be scope-defining.
    ws = tmp_path / "some_project"
    target = str(ws / rel)
    assert is_scope_defining(target) is False


def test_over_match_granted_workspace_classifies_yellow_not_red(grove_home, tmp_path):
    # The door rides is_scope_defining: an operator-granted workspace outside both
    # anchors, whose subtree names collide with meta-surfaces, stays operator-
    # approvable (YELLOW), never trapped RED-by-scope.
    ws = tmp_path / "some_project"
    (ws / "capabilities").mkdir(parents=True)
    target = ws / "capabilities" / "y.yaml"
    (grove_home / "write_workspaces.yaml").write_text(
        f"write_workspaces:\n  - path: {ws.resolve()}\n"
    )
    assert _classify_write_zone(str(target)) == "yellow"


# ── third door: propose_governance_change (retargeted off is_governed_path) ───
def _pgc_intent(target: str):
    return type(
        "I", (),
        {"tool_name": "propose_governance_change",
         "arguments": {"target_file": target, "content": "x"}},
    )()


def test_propose_governance_change_repo_target_now_red():
    # The LAST is_scope_defining AND is_governed_path composition is retargeted:
    # a running-tree config/ target is no longer filtered out by the stale
    # ~/.grove anchor, so this door classifies it RED (was Yellow pre-fix).
    surface = str(CONFIG / "zones.schema.yaml")
    zr = Dispatcher._classify_one_intent(_pgc_intent(surface), None)
    assert zr.zone == "red"
    assert zr.source == "governance_change"
    # Defense-in-depth intact (capability-mutation-surface-v1 P5 —
    # classify_governance_target retired): the viability seam refuses any
    # repo-definition target, so no repo-edit path is opened and nothing can
    # even be store-pended.
    from grove.red_pending_store import is_viable_red_target
    _viable, _reason = is_viable_red_target(
        "propose_governance_change",
        {"target_file": surface, "content": "x", "rationale": "r"},
    )
    assert _viable is False and "deploy" in _reason.lower()


def test_propose_governance_change_grove_target_behavior_identical(grove_home):
    # A valid in-grove scope-defining target stays RED through this door — the
    # retarget is behavior-identical here (is_governed_path ≡ not-andon in-grove).
    surface = str(grove_home / "zones.schema.yaml")
    zr = Dispatcher._classify_one_intent(_pgc_intent(surface), None)
    assert zr.zone == "red"


def test_propose_governance_change_andon_draft_not_red(grove_home):
    # The .andon authoring quarantine carve-out is preserved: a draft target is
    # NOT red-by-scope through this door (it falls through to the yellow default),
    # exactly as under the prior is_governed_path predicate.
    draft = grove_home / "skills" / ".andon" / "my_skill" / "SKILL.md"
    zr = Dispatcher._classify_one_intent(_pgc_intent(str(draft)), None)
    assert zr.zone != "red"

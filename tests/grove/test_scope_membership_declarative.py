"""capability-mutation-surface-v1 M1 (P3) — declarative scope membership.

Parity proof + loader fail-loud validation for the move of scope-defining
MEMBERSHIP from Python literals to ``config/scope_surfaces.yaml``.

GOLDEN BASELINE: the exact P2-era literal membership (fs_utils.py at commit
d6864d8f2 working tree, pre-consolidation). The consolidation changes WHERE
membership is declared, not WHAT is walled — the set-diff against this golden
must be EMPTY, with exactly ONE mandated addition: the membership file itself
(confused-deputy closure, P3 scope item 1).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

import grove.utils.fs_utils as fu

# guard-set-self-declaring: this whole module is a defect-class guard suite.
pytestmark = pytest.mark.guard

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ── The golden: P2-era literals, verbatim ──
_GOLDEN_GROVE_HOME_FILES = frozenset({
    "zones.schema.yaml",
    "routing.config.yaml",
    "prompt.config.yaml",
    ".env",
    os.path.join("dock", "dock.yaml"),
    "workspaces.yaml",
    "write_workspaces.yaml",
    "grants.yaml",
    "routing.autonomaton.yaml",
    "zones.autonomaton.yaml",
    "fleet_workers.override.yaml",
})
_GOLDEN_GROVE_HOME_PREFIXES = ("skills", "capabilities", "routing-profiles")
_GOLDEN_REPO_FILES = frozenset({
    "zones.schema.yaml",
    "routing.config.yaml",
    os.path.join("dock", "dock.yaml"),
    "write_workspaces.yaml",
})
_GOLDEN_REPO_PREFIXES = ("capabilities", "routing-profiles")

# The single mandated addition — the membership file's self-membership.
_MANDATED_ADDITION = "scope_surfaces.yaml"


# ── Parity: membership sets ──


def test_grove_home_membership_parity_is_exact():
    assert fu._SCOPE_DEFINING_FILES == _GOLDEN_GROVE_HOME_FILES, (
        f"GROVE_HOME file membership drifted from the golden:\n"
        f"  added:   {sorted(fu._SCOPE_DEFINING_FILES - _GOLDEN_GROVE_HOME_FILES)}\n"
        f"  removed: {sorted(_GOLDEN_GROVE_HOME_FILES - fu._SCOPE_DEFINING_FILES)}"
    )
    assert set(fu._SCOPE_DEFINING_DIR_PREFIXES) == set(
        _GOLDEN_GROVE_HOME_PREFIXES
    ), (
        f"GROVE_HOME dir-prefix membership drifted: "
        f"{fu._SCOPE_DEFINING_DIR_PREFIXES!r} vs golden "
        f"{_GOLDEN_GROVE_HOME_PREFIXES!r}"
    )


def test_repo_config_membership_parity_modulo_mandated_self_member():
    added = fu._REPO_CONFIG_SCOPE_DEFINING_FILES - _GOLDEN_REPO_FILES
    removed = _GOLDEN_REPO_FILES - fu._REPO_CONFIG_SCOPE_DEFINING_FILES
    assert removed == frozenset(), (
        f"repo-config membership LOST members vs golden: {sorted(removed)}"
    )
    assert added == {_MANDATED_ADDITION}, (
        "repo-config additions must be EXACTLY the mandated self-membership "
        f"({_MANDATED_ADDITION!r}); got {sorted(added)}"
    )
    assert set(fu._REPO_CONFIG_SCOPE_DEFINING_DIR_PREFIXES) == set(
        _GOLDEN_REPO_PREFIXES
    )


# ── Parity: behavioral probes through the live predicate ──


def _probe_targets(grove_home: Path):
    """(target, expected) pairs spanning every golden member + controls."""
    pairs = []
    for f in sorted(_GOLDEN_GROVE_HOME_FILES):
        pairs.append((str(grove_home / f), True))
    for p in _GOLDEN_GROVE_HOME_PREFIXES:
        pairs.append((str(grove_home / p / "nested" / "leaf.yaml"), True))
    # Ancestor-of-a-surface and the root itself.
    pairs.append((str(grove_home / "dock"), True))
    pairs.append((str(grove_home), True))
    # capabilities/state (the T2 pin surface).
    pairs.append((str(grove_home / "capabilities" / "state" / "x.yaml"), True))
    # Negative controls.
    pairs.append((str(grove_home / "research" / "notes.md"), False))
    pairs.append((str(grove_home / "dock.autonomaton.yaml"), False))
    # Repo-config twins.
    cfg = Path(fu._MODULE_CONFIG_ROOT)
    for f in sorted(_GOLDEN_REPO_FILES):
        pairs.append((str(cfg / f), True))
    for p in _GOLDEN_REPO_PREFIXES:
        pairs.append((str(cfg / p / "browser_read.yaml"), True))
    pairs.append((str(cfg), True))
    # Repo negative controls: a config sibling outside config/, and a
    # basename collision outside both anchors.
    pairs.append((str(cfg.parent / "README.md"), False))
    pairs.append(("/tmp/elsewhere/capabilities/anything.yaml", False))
    return pairs


def test_classified_scope_defining_set_parity(tmp_path, monkeypatch):
    grove_home = tmp_path / ".grove"
    grove_home.mkdir()
    diverged = []
    for target, expected in _probe_targets(grove_home):
        got = fu.is_scope_defining(target, grove_home)
        if got is not expected:
            diverged.append((target, expected, got))
    assert not diverged, (
        "scope-wall behavioral parity broke (target, golden-expected, got):\n"
        + "\n".join(f"  {t} expected={e} got={g}" for t, e, g in diverged)
    )


def test_mandated_addition_membership_file_is_walled():
    """The confused-deputy closure, behaviorally: the membership file itself
    classifies scope-defining under the repo-config anchor."""
    target = str(Path(fu._MODULE_CONFIG_ROOT) / _MANDATED_ADDITION)
    assert fu.is_scope_defining(target), (
        "config/scope_surfaces.yaml must be inside the wall it defines"
    )


# ── Loader fail-loud validation (house idiom: explicit faults, loud) ──


def _write_membership(root: Path, doc) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / fu._SCOPE_MEMBERSHIP_FILENAME
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return p


def _valid_doc():
    return {
        "version": 1,
        "grove_home": {
            "files": sorted(_GOLDEN_GROVE_HOME_FILES),
            "dir_prefixes": list(_GOLDEN_GROVE_HOME_PREFIXES),
        },
        "repo_config": {
            "files": sorted(_GOLDEN_REPO_FILES | {_MANDATED_ADDITION}),
            "dir_prefixes": list(_GOLDEN_REPO_PREFIXES),
        },
    }


def test_loader_accepts_the_live_shape(tmp_path):
    _write_membership(tmp_path, _valid_doc())
    loaded = fu._load_scope_membership(str(tmp_path))
    assert loaded["grove_home_files"] == _GOLDEN_GROVE_HOME_FILES
    assert loaded["repo_config_files"] == (
        _GOLDEN_REPO_FILES | {_MANDATED_ADDITION}
    )


def test_loader_missing_file_is_loud(tmp_path):
    with pytest.raises(fu.ScopeMembershipConfigError, match="unreadable"):
        fu._load_scope_membership(str(tmp_path / "nowhere"))


def test_loader_empty_member_list_is_loud(tmp_path):
    doc = _valid_doc()
    doc["grove_home"]["files"] = []
    _write_membership(tmp_path, doc)
    with pytest.raises(fu.ScopeMembershipConfigError, match="non-empty"):
        fu._load_scope_membership(str(tmp_path))


def test_loader_missing_self_membership_is_loud(tmp_path):
    doc = _valid_doc()
    doc["repo_config"]["files"] = sorted(_GOLDEN_REPO_FILES)  # no self
    _write_membership(tmp_path, doc)
    with pytest.raises(fu.ScopeMembershipConfigError, match="ITSELF"):
        fu._load_scope_membership(str(tmp_path))


def test_loader_rejects_absolute_and_traversal_entries(tmp_path):
    for bad in ("/etc/passwd", "../outside.yaml"):
        doc = _valid_doc()
        doc["grove_home"]["files"] = sorted(_GOLDEN_GROVE_HOME_FILES) + [bad]
        _write_membership(tmp_path, doc)
        with pytest.raises(
            fu.ScopeMembershipConfigError, match="relative, non-traversing"
        ):
            fu._load_scope_membership(str(tmp_path))


def test_loader_version_mismatch_is_loud(tmp_path):
    doc = _valid_doc()
    doc["version"] = 2
    _write_membership(tmp_path, doc)
    with pytest.raises(fu.ScopeMembershipConfigError, match="version"):
        fu._load_scope_membership(str(tmp_path))


def test_loader_unparseable_yaml_is_loud(tmp_path):
    p = tmp_path / fu._SCOPE_MEMBERSHIP_FILENAME
    tmp_path.mkdir(parents=True, exist_ok=True)
    p.write_text("grove_home: [unclosed", encoding="utf-8")
    with pytest.raises(fu.ScopeMembershipConfigError, match="not valid YAML"):
        fu._load_scope_membership(str(tmp_path))

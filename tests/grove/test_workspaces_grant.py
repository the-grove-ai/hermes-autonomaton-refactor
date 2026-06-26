"""Tests for the config/workspaces.yaml grant reconcile.

Sprint K1 (living-cellar-v1) Phase 6. The repo copy is reconciled UP to the
canonical runtime grant set (captured read-only from the VM this phase) plus
the wiki cellar grant. Additive against the runtime: no runtime grant dropped.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# The canonical runtime grant set, read read-only from the VM
# (~/.grove/workspaces.yaml) during Phase 6. The repo must be a superset.
_RUNTIME_FLEET_GRANTS = {"scout", "researcher", "drafter", "cultivator"}


def _grant_names():
    data = yaml.safe_load(Path("config/workspaces.yaml").read_text(encoding="utf-8"))
    return {p["path"].rstrip("/") for p in data["granted_workspaces"]}


def test_workspaces_yaml_parses():
    data = yaml.safe_load(Path("config/workspaces.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert isinstance(data.get("granted_workspaces"), list)
    assert all("path" in g for g in data["granted_workspaces"])


def test_wiki_grant_present():
    assert "wiki" in _grant_names()


def test_superset_of_runtime_no_grant_dropped():
    names = _grant_names()
    missing = _RUNTIME_FLEET_GRANTS - names
    assert not missing, f"reconcile dropped runtime grant(s): {missing}"


def test_reconciled_set_is_runtime_plus_wiki():
    assert _grant_names() == _RUNTIME_FLEET_GRANTS | {"wiki"}

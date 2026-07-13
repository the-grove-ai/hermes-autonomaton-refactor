"""fleet-hygiene-sweep P4 — the node-local enable-flag overlay.

Per-worker {enabled: bool} overrides from ~/.grove/fleet_workers.override.yaml
layer over the repo-bundled operational registry (deploy-immune, the D5 seam).
Cases:

* MERGE — an override flips a named worker's enabled; unnamed workers keep
  their bundled default (anti-masking).
* GHOST — an override for an unknown worker is warned + ignored.
* UNPARSEABLE — a broken override disables ALL workers, loud, gateway lives
  (no raise) (R-B3).
* MALFORMED ENTRY — a per-worker entry without {enabled: bool} disables THAT
  worker (fail-closed), others unaffected.
* ABSENT — no override file = bundled defaults unchanged.
"""
from __future__ import annotations

import pytest

from grove.fleet.config import load_fleet_workers

_REGISTRY = """\
workers:
  - id: forge
    skill: skill.fleet.forge-jobsearch
    enabled: true
  - id: drafter
    skill: skill.fleet.drafter
    enabled: true
  - id: scout
    skill: skill.fleet.scout
    enabled: false
"""


@pytest.fixture()
def registry(tmp_path):
    p = tmp_path / "fleet_workers.yaml"
    p.write_text(_REGISTRY, encoding="utf-8")
    return p


def _override(tmp_path, body: str):
    p = tmp_path / "override.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _load(registry, override=None):
    return load_fleet_workers(path=registry, override_path=override)


# ── ABSENT ────────────────────────────────────────────────────────────────────


def test_absent_override_bundled_defaults(registry):
    w = _load(registry)
    assert w["forge"].enabled is True
    assert w["drafter"].enabled is True
    assert w["scout"].enabled is False


# ── MERGE + anti-masking ──────────────────────────────────────────────────────


def test_override_flips_named_worker_only(registry, tmp_path):
    ov = _override(tmp_path, "workers:\n  forge: {enabled: false}\n  scout: {enabled: true}\n")
    w = _load(registry, ov)
    assert w["forge"].enabled is False   # flipped off
    assert w["scout"].enabled is True    # flipped on
    assert w["drafter"].enabled is True  # UNNAMED — bundled default preserved


def test_newly_shipped_worker_keeps_bundled_default(registry, tmp_path):
    # override names only forge; drafter is "newly shipped" w.r.t. the override
    ov = _override(tmp_path, "workers:\n  forge: {enabled: false}\n")
    w = _load(registry, ov)
    assert w["forge"].enabled is False
    assert w["drafter"].enabled is True  # anti-masking: unnamed = bundled


# ── GHOST ─────────────────────────────────────────────────────────────────────


def test_ghost_override_unknown_worker_ignored(registry, tmp_path, caplog):
    ov = _override(tmp_path, "workers:\n  nonexistent: {enabled: true}\n  forge: {enabled: false}\n")
    with caplog.at_level("WARNING"):
        w = _load(registry, ov)
    assert "nonexistent" not in w
    assert w["forge"].enabled is False   # the real override still applied
    assert any("ghost override" in r.message for r in caplog.records)


# ── UNPARSEABLE (R-B3 fail-closed) ────────────────────────────────────────────


def test_unparseable_override_disables_all_gateway_lives(registry, tmp_path, caplog):
    ov = _override(tmp_path, "workers:\n  forge: {enabled: false\n  {{{ broken yaml")
    with caplog.at_level("CRITICAL"):
        w = _load(registry, ov)  # must NOT raise
    assert all(cfg.enabled is False for cfg in w.values())  # ALL disabled
    assert set(w) == {"forge", "drafter", "scout"}          # registry still loaded
    assert any("ALL fleet workers DISABLED" in r.message for r in caplog.records)


def test_workers_block_not_mapping_disables_all(registry, tmp_path, caplog):
    ov = _override(tmp_path, "workers:\n  - forge\n  - drafter\n")  # list, not mapping
    with caplog.at_level("CRITICAL"):
        w = _load(registry, ov)
    assert all(cfg.enabled is False for cfg in w.values())


# ── MALFORMED PER-WORKER ENTRY (fail-closed for that worker) ──────────────────


def test_malformed_entry_disables_that_worker_only(registry, tmp_path, caplog):
    ov = _override(tmp_path, "workers:\n  forge: {enabled: yes-please}\n  scout: {enabled: true}\n")
    with caplog.at_level("WARNING"):
        w = _load(registry, ov)
    assert w["forge"].enabled is False   # malformed value → fail-closed
    assert w["scout"].enabled is True    # valid override applied
    assert w["drafter"].enabled is True  # untouched
    assert any("malformed" in r.message for r in caplog.records)


def test_empty_workers_override_is_noop(registry, tmp_path):
    ov = _override(tmp_path, "workers:\n")  # present but empty
    w = _load(registry, ov)
    assert w["forge"].enabled is True  # unchanged

"""GRV-001 v2.0 migration tool (routing-v2-migration-v1, Phase 1).

Covers ``grove.config.routing_migrate.migrate_v1_to_v2``: disposition matches
the census exactly (set-diff on keys), zone_overrides dropped + reported,
crash-atomicity (a failure between the two os.replace calls leaves v1 intact and
a re-run completes), dry-run writes nothing, comments survive the round-trip,
both-v2-present is a no-op, and the surface markers land per spec.

Hermetic: everything under ``tmp_path``; no ~/.grove read.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import grove.config.routing_migrate as mig
from grove.config.routing_migrate import migrate_v1_to_v2
from grove.router_merge import (
    _AUTHORITY_RESERVED_KEYS,
    load_authority_routing_config,
    load_operational_routing_config,
)

# A full v1 file carrying every censused key, with inline comment markers inside
# two moved blocks (they must survive the split).
_V1 = """\
routing:
  schema_version: 1
  default_tier: T1
  zone_overrides: {}
  tier_preferences:
    T1:
      provider: openrouter
      model: deepseek/deepseek-v4-flash
      max_tokens: 4096
  model_facts:
    "z-ai/glm-5.2":
      context_window: 1048576
  routing_rules:
    premium:
      enabled: true
      target_tier: T2
  telemetry:
    tier: T1
  provider_routing:
    openrouter:
      order: [DeepInfra]
  escalation:
    threshold: 0.6
    description: prose that is not carried
  escalation_policy:
    enabled: false
    ceiling_tier: T3
tier_budgets:
  T2:
    context: [cellar_context]  # TIER_BUDGET_COMMENT_MARKER
  T1:
    context: [cellar_context]
pattern_cache:
  enabled: true  # PATTERN_CACHE_COMMENT_MARKER
goal_attachment:
  adjudicator_tier: T-GA
"""

_EXPECTED_OPERATIONAL = {
    "default_tier",
    "tier_preferences",
    "model_facts",
    "routing_rules",
    "telemetry",
    "provider_routing",
    "pattern_cache",
    "goal_attachment",
}
_EXPECTED_AUTHORITY = {"escalation_threshold", "tier_budgets", "escalation_policy"}
_EXPECTED_DROPPED = {"zone_overrides"}


@pytest.fixture
def v1_file(tmp_path):
    p = tmp_path / "routing.config.yaml"
    p.write_text(_V1, encoding="utf-8")
    return p


def _outs(tmp_path):
    return tmp_path / "routing.operational.yaml", tmp_path / "routing.authority.yaml"


def _by_dest(disposition, dest):
    return {k for k, v in disposition.items() if v == dest}


# ── disposition / census ─────────────────────────────────────────────────────


def test_disposition_matches_census_set_diff(v1_file, tmp_path):
    op_out, auth_out = _outs(tmp_path)
    report = migrate_v1_to_v2(v1_file, op_out, auth_out, dry_run=True)
    disp = report["disposition"]
    assert _by_dest(disp, "operational") == _EXPECTED_OPERATIONAL
    assert _by_dest(disp, "authority") == _EXPECTED_AUTHORITY
    assert _by_dest(disp, "dropped") == _EXPECTED_DROPPED


def test_zone_overrides_dropped_and_reported(v1_file, tmp_path):
    op_out, auth_out = _outs(tmp_path)
    report = migrate_v1_to_v2(v1_file, op_out, auth_out, dry_run=True)
    assert report["disposition"]["zone_overrides"] == "dropped"


def test_authority_destination_equals_reserved_keyset(v1_file, tmp_path):
    """Coherence: the keys the migration routes to authority are exactly the
    keys the operational loader treats as reserved (confused-deputy set)."""
    op_out, auth_out = _outs(tmp_path)
    report = migrate_v1_to_v2(v1_file, op_out, auth_out, dry_run=True)
    assert _by_dest(report["disposition"], "authority") == set(_AUTHORITY_RESERVED_KEYS)


# ── dry-run ──────────────────────────────────────────────────────────────────


def test_dry_run_writes_nothing(v1_file, tmp_path):
    op_out, auth_out = _outs(tmp_path)
    report = migrate_v1_to_v2(v1_file, op_out, auth_out, dry_run=True)
    assert report["wrote"] is False
    assert not op_out.exists()
    assert not auth_out.exists()
    assert v1_file.exists()  # untouched


# ── real migration: outputs, markers, round-trip through the loaders ─────────


def test_real_migration_produces_loadable_split(v1_file, tmp_path):
    op_out, auth_out = _outs(tmp_path)
    report = migrate_v1_to_v2(v1_file, op_out, auth_out)
    assert report["wrote"] is True
    assert op_out.exists() and auth_out.exists()
    # v1 renamed to a .bak_*, not deleted
    assert not v1_file.exists()
    assert Path(report["v1_backup"]).exists()

    # the split loads cleanly through the new v2 loaders
    op = load_operational_routing_config(op_out)
    auth = load_authority_routing_config(auth_out)

    assert op["surface_class"] == "in_scope"
    assert op["writable_on"] == "autonomous_loop"
    assert op["schema_version"] == "2.0"
    assert op["default_tier"] == "T1"

    assert auth["surface_class"] == "scope_defining"
    assert auth["writable_on"] == "operator_authenticated"
    assert auth["default_zone"] == "red"
    assert auth["schema_version"] == "2.0"
    assert auth["escalation_threshold"] == 0.6

    # R3: no reader-less authority claims are written
    assert "approval_requirements" not in auth
    assert "zone_assignment" not in auth
    # zone_overrides is gone from both
    assert "zone_overrides" not in op and "zone_overrides" not in auth


def test_comments_survive_round_trip(v1_file, tmp_path):
    op_out, auth_out = _outs(tmp_path)
    migrate_v1_to_v2(v1_file, op_out, auth_out)
    assert "PATTERN_CACHE_COMMENT_MARKER" in op_out.read_text(encoding="utf-8")
    assert "TIER_BUDGET_COMMENT_MARKER" in auth_out.read_text(encoding="utf-8")


# ── atomicity / crash safety ─────────────────────────────────────────────────


def test_crash_between_replaces_leaves_v1_intact_and_reruns(v1_file, tmp_path, monkeypatch):
    op_out, auth_out = _outs(tmp_path)

    real_replace = os.replace
    state = {"n": 0}

    def flaky_replace(src, dst):
        state["n"] += 1
        if state["n"] == 2:  # fail the SECOND replace (authority)
            raise OSError("simulated crash between replaces")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(OSError, match="simulated crash"):
        migrate_v1_to_v2(v1_file, op_out, auth_out)

    # v1 is the recovery anchor — never moved before both v2 files land
    assert v1_file.exists()
    assert op_out.exists()          # first replace succeeded
    assert not auth_out.exists()    # second never landed

    # re-run from the intact v1 completes idempotently
    monkeypatch.setattr(os, "replace", real_replace)
    report = migrate_v1_to_v2(v1_file, op_out, auth_out)
    assert report["state"] == "partial"
    assert report["wrote"] is True
    assert op_out.exists() and auth_out.exists()
    assert not v1_file.exists()
    assert Path(report["v1_backup"]).exists()


# ── state detection ──────────────────────────────────────────────────────────


def test_both_v2_present_is_noop(v1_file, tmp_path):
    op_out, auth_out = _outs(tmp_path)
    migrate_v1_to_v2(v1_file, op_out, auth_out)  # first run migrates (moves v1)
    # both v2 present now; a second call is a no-op
    report = migrate_v1_to_v2(v1_file, op_out, auth_out)
    assert report["state"] == "both-v2-present"
    assert report["wrote"] is False


def test_unrecoverable_state_raises(tmp_path):
    op_out, auth_out = _outs(tmp_path)
    # v1 gone and pair incomplete → unrecoverable
    op_out.write_text('schema_version: "2.0"\n', encoding="utf-8")
    with pytest.raises(mig.MigrationError, match="unrecoverable"):
        migrate_v1_to_v2(tmp_path / "routing.config.yaml", op_out, auth_out)

"""router-merge-wiring-v1 — the live router loads the *merged* operator +
machine routing config.

GATE-A confirmed the merge helper (``load_merged_routing_config``), the
single-path loader it replaces, the snapshot/restore ``reload()`` contract,
and the ``_machine_config_path`` resolver. These tests cover the four DoD
items for wiring that helper into ``CognitiveRouter``:

1. merged-load wiring — a machine file merges on top of the operator root;
   absent, the operator routes alone; the default machine path resolves via
   the flywheel resolver.
2. merged validation fails loud — an undeclared tier introduced by the
   machine file raises at construction (no silent degradation).
3. the load-bearing integration: build → reload-grows → reload-rejects, with
   last-known-good-merged retained and a fault-attribution event emitted.
4. fault attribution — a successful load emits ``routing_config_load``
   (outcome ``loaded``) carrying sha256 source hashes, the machine-absent
   sentinel, and the changed-file attribution.

Only item 3's wording is fixed by the SPEC; items 1/2/4 are derived from
build steps C1/C2/C4 to give each a direct regression test.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest

import grove.flywheel_cli
from grove.router import _MACHINE_ABSENT_SENTINEL, CognitiveRouter

# Operator root: default tier T1 (cheap), with one Ratchet-promotions rule
# pinning the ``seed_promoted`` intent to T2. The machine file set-unions
# additional intents into this rule — the merge is observable because an
# un-merged sink intent falls through to the T1 default.
OPERATOR_CONFIG = """\
routing:
  schema_version: 1
  default_tier: T1
  tier_preferences:
    T0:
      handler: pattern_cache
      description: Deterministic recall.
      max_latency_ms: 50
    T1:
      provider: anthropic
      model: claude-haiku-4-5-20251001
      description: Cheap cognition.
      max_tokens: 4096
    T2:
      provider: anthropic
      model: claude-sonnet-4-6
      description: Premium cognition.
      max_tokens: 8192
    T3:
      provider: anthropic
      model: claude-opus-4-6
      description: Apex cognition.
      max_tokens: 16384
  routing_rules:
    ratchet_promotions:
      enabled: true
      match:
        intents: [seed_promoted]
      target_tier: T2
  escalation:
    threshold: 0.6
    description: Confidence dial.
  telemetry:
    tier: T1
    description: Scoring tier.
"""

# Machine additions set-union a sink intent into ratchet_promotions.match.
# target_tier is NOT restated — the operator owns that scalar (operator-wins).
MACHINE_ONE_SINK = """\
routing:
  routing_rules:
    ratchet_promotions:
      match:
        intents: [ratchet_promoted_t2]
"""

MACHINE_TWO_SINKS = """\
routing:
  routing_rules:
    ratchet_promotions:
      match:
        intents: [ratchet_promoted_t2, ratchet_promoted_t2_b]
"""

# A machine-only rule key (no operator collision, so it survives the merge)
# targeting a tier the operator never declared — trips validation.
MACHINE_UNDECLARED_TIER = """\
routing:
  routing_rules:
    bogus_machine_rule:
      enabled: true
      match:
        intents: [anything]
      target_tier: T9
"""

# Confidence above the 0.6 escalation threshold so the synthesized step_up
# never fires — route() resolves purely on the merged set-tier rule.
_HIGH_CONF = dict(confidence=0.9)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _operator(tmp_path: Path) -> Path:
    return _write(tmp_path / "routing.config.yaml", OPERATOR_CONFIG)


def _load_events(caplog) -> list[dict]:
    """Parsed ``routing_config_load`` event payloads, in emission order."""
    events = []
    for record in caplog.records:
        message = record.getMessage()
        if message.startswith("routing_config_load "):
            events.append(json.loads(message[len("routing_config_load ") :]))
    return events


# ── DoD 1 — merged-load wiring (C1) ──────────────────────────────────


def test_item1_machine_merges_onto_operator(tmp_path):
    """A present machine file set-unions its sink intent into the operator
    rule, so the merged router routes the sink intent to T2."""
    op = _operator(tmp_path)
    mc = _write(tmp_path / "routing.autonomaton.yaml", MACHINE_ONE_SINK)
    router = CognitiveRouter(op, mc)
    assert router.route(intent="ratchet_promoted_t2", **_HIGH_CONF).tier == "T2"


def test_item1_machine_absent_operator_routes_alone(tmp_path):
    """With no machine file, only the operator rule applies: the sink intent
    falls through to the T1 default; the operator's own intent still hits T2."""
    op = _operator(tmp_path)
    missing = tmp_path / "absent.autonomaton.yaml"
    router = CognitiveRouter(op, missing)
    assert router.route(intent="ratchet_promoted_t2", **_HIGH_CONF).tier == "T1"
    assert router.route(intent="seed_promoted", **_HIGH_CONF).tier == "T2"


def test_item1_default_machine_path_uses_flywheel_resolver(tmp_path, monkeypatch):
    """When machine_path is omitted, __init__ resolves it via the flywheel
    CLI's _machine_config_path — not a re-defined path."""
    op = _operator(tmp_path)
    resolved = tmp_path / "resolved.autonomaton.yaml"  # absent → operator-only
    monkeypatch.setattr(
        grove.flywheel_cli, "_machine_config_path", lambda: resolved
    )
    router = CognitiveRouter(op)
    assert router._machine_path == resolved


# ── DoD 2 — merged validation fails loud (C2) ────────────────────────


def test_item2_undeclared_tier_in_merge_raises_at_construction(tmp_path):
    """A machine rule targeting an undeclared tier makes the *merged* config
    invalid; construction raises loudly rather than degrading."""
    op = _operator(tmp_path)
    mc = _write(tmp_path / "routing.autonomaton.yaml", MACHINE_UNDECLARED_TIER)
    with pytest.raises(ValueError, match="T9"):
        CognitiveRouter(op, mc)


def test_item2_absent_operator_raises_file_not_found(tmp_path):
    """The operator-absent contract is preserved through the merge helper."""
    missing_op = tmp_path / "nope.config.yaml"
    mc = _write(tmp_path / "routing.autonomaton.yaml", MACHINE_ONE_SINK)
    with pytest.raises(FileNotFoundError):
        CognitiveRouter(missing_op, mc)


# ── DoD 3 — load-bearing integration: build → grow → reject ──────────


def test_item3_merge_load_reload_grow_and_last_known_good(tmp_path, caplog):
    op = _operator(tmp_path)
    mc = _write(tmp_path / "routing.autonomaton.yaml", MACHINE_ONE_SINK)

    # 3a — fresh router over operator + machine routes the merged sink to T2.
    router = CognitiveRouter(op, mc)
    assert router.route(intent="ratchet_promoted_t2", **_HIGH_CONF).tier == "T2"
    # the second sink is not present yet
    assert router.route(intent="ratchet_promoted_t2_b", **_HIGH_CONF).tier == "T1"

    # 3b — append a second sink intent on disk, reload, assert it now routes.
    _write(tmp_path / "routing.autonomaton.yaml", MACHINE_TWO_SINKS)
    router.reload()
    assert router.route(intent="ratchet_promoted_t2_b", **_HIGH_CONF).tier == "T2"
    # the original sink still routes (merge is cumulative, not replacing)
    assert router.route(intent="ratchet_promoted_t2", **_HIGH_CONF).tier == "T2"

    # 3c — overwrite the machine file with an undeclared-tier rule; reload must
    # keep the last-known-good *merged* state and attribute the fault.
    _write(tmp_path / "routing.autonomaton.yaml", MACHINE_UNDECLARED_TIER)
    with caplog.at_level(logging.INFO, logger="grove.telemetry"):
        router.reload()

    # last-known-good-merged: both sinks from 3b still resolve to T2.
    assert router.route(intent="ratchet_promoted_t2", **_HIGH_CONF).tier == "T2"
    assert router.route(intent="ratchet_promoted_t2_b", **_HIGH_CONF).tier == "T2"

    kept = [e for e in _load_events(caplog) if e["outcome"] == "kept_last_known_good"]
    assert len(kept) == 1
    assert kept[0]["changed_file"] == "machine"
    assert kept[0]["error"]  # repr of the validation exception is recorded


# ── DoD 4 — fault attribution telemetry (C4) ─────────────────────────


def test_item4_loaded_event_carries_source_hashes(tmp_path, caplog):
    op = _operator(tmp_path)
    mc = _write(tmp_path / "routing.autonomaton.yaml", MACHINE_ONE_SINK)
    with caplog.at_level(logging.INFO, logger="grove.telemetry"):
        CognitiveRouter(op, mc)

    loaded = [e for e in _load_events(caplog) if e["outcome"] == "loaded"]
    assert len(loaded) == 1
    event = loaded[0]
    assert event["operator_hash"] == hashlib.sha256(op.read_bytes()).hexdigest()
    assert event["machine_hash"] == hashlib.sha256(mc.read_bytes()).hexdigest()
    # first load: both source hashes diverge from the empty retained state.
    assert event["changed_file"] == "both"
    assert event["error"] is None


def test_item4_machine_absent_emits_sentinel_hash(tmp_path, caplog):
    op = _operator(tmp_path)
    missing = tmp_path / "absent.autonomaton.yaml"
    with caplog.at_level(logging.INFO, logger="grove.telemetry"):
        CognitiveRouter(op, missing)

    loaded = [e for e in _load_events(caplog) if e["outcome"] == "loaded"]
    assert len(loaded) == 1
    assert loaded[0]["machine_hash"] == _MACHINE_ABSENT_SENTINEL
    assert loaded[0]["operator_hash"] == hashlib.sha256(op.read_bytes()).hexdigest()

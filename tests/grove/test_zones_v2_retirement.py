"""zones-v2-scope-keying P2 — retirement + migration + deny-all coverage.

Covers the P2b test additions:
  * D1 — a non-governance halt offers no "always" store (resolve_always_store →
    None); a SUBMITTED "always" on a non-governance halt is REFUSED (re-prompt →
    deny), never a silent no-op, never a crash.
  * D1 — a governance-mutation halt still resolves a standing_grant store (the
    "always" affordance survives there, unchanged).
  * D5(b′) — a malformed deny-config denies ALL RED at halt-time and does NOT
    raise (fail-closed).
  * Migration — prune tool_zones + stamp v2 + .bak; malformed refusal (zero
    writes); idempotency (byte-identical, nothing-to-do); prune-report content.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from grove.config import zones_overlay_migrate as migrate
from grove.grant_recognition import resolve_always_store


# ── D1: resolve_always_store — the central "always" affordance gate ──────────

def _halt(tool: str, args: dict, *, is_promotable: bool = True):
    zr = SimpleNamespace(is_promotable=is_promotable)
    intent = SimpleNamespace(tool_name=tool, arguments=args)
    return SimpleNamespace(intents=[intent], zone_results=[zr], triggering_index=0)


def test_non_governance_halt_offers_no_always_store():
    # A yellow generic (terminal command) — the zone_rule store is retired, so
    # no store applies and the "always" affordance must not render.
    assert resolve_always_store(_halt("terminal", {"command": "echo hi"})) is None
    assert resolve_always_store(_halt("write_file", {"path": "/tmp/x"})) is None


def test_governance_halt_still_offers_standing_grant():
    # A native governance-mutation verb still resolves a standing_grant store —
    # the "always" affordance survives for governance (untouched by D1).
    from grove.grant_recognition import WRITE_CLASS_DECLARATION

    gov = next(
        (t for t, e in WRITE_CLASS_DECLARATION.items() if e.routing_class == "native"),
        None,
    )
    assert gov is not None, "expected at least one native governance verb"
    entry = WRITE_CLASS_DECLARATION[gov]
    args = {} if entry.scope_policy == "global" else {"skill_name": "tgt"}
    store = resolve_always_store(_halt(gov, args))
    assert store is not None and store[0] == "standing_grant"


# ── D1: submitted "always" on a non-governance halt is refused ───────────────

def _dispatcher(monkeypatch, **kwargs):
    from grove.dispatcher import Dispatcher

    d = Dispatcher(**kwargs)
    d._write_pending_andon = lambda agent, halt: None  # type: ignore[method-assign]
    d._clear_pending_andon = lambda agent, marker: None  # type: ignore[method-assign]
    d._current_turn_id = "s_test#1"
    return d


def _real_halt(command="echo hello"):
    from grove.dispatcher import AndonHalt
    from grove.intents import ToolIntent
    from grove.zones import ZoneResult

    intents = [ToolIntent(tool_name="terminal", arguments={"command": command}, call_id="c1")]
    zr = [ZoneResult(zone="yellow", matched_rule="shell.effect.default", source="shell_effect")]
    return AndonHalt(intents=intents, zone_results=zr, triggering_index=0)


def test_submitted_always_on_non_governance_halt_is_refused(monkeypatch):
    # unrendered ≠ unsubmittable: a stale client / direct API submitting "always"
    # on a non-governance halt must hit an explicit refusal (re-prompt → deny),
    # never a silent accept, never a crash.
    d = _dispatcher(monkeypatch, sovereign_prompt_handler=lambda h: "always")
    result = d._handle_andon_halt(agent=MagicMock(), halt=_real_halt(), ledger=MagicMock())
    assert result == "deny"  # refused (invalid input) — NOT "always", no crash


def test_submitted_once_on_non_governance_halt_is_honored(monkeypatch):
    # The refusal is specific to "always" — the three valid choices pass through.
    d = _dispatcher(monkeypatch, sovereign_prompt_handler=lambda h: "once")
    result = d._handle_andon_halt(agent=MagicMock(), halt=_real_halt(), ledger=MagicMock())
    assert result == "once"


# ── D5(b′): malformed deny-config → deny-all, no raise ───────────────────────

def test_malformed_deny_config_denies_all_and_does_not_raise(tmp_path, monkeypatch):
    import grove.red_policy as rp

    bad = tmp_path / "bad.yaml"
    bad.write_text("red_denied_by_policy: [unclosed\n:::not yaml")
    monkeypatch.setattr(rp, "_overlay_path", lambda: bad)
    monkeypatch.setattr(rp, "_repo_schema_path", lambda: bad)

    # Denies every effect — including a halt with no pattern_key — and never raises.
    assert rp.is_denied_by_policy("some:random:effect") is True
    assert rp.is_denied_by_policy(None) is True
    assert "safety measure" in rp.denial_message("some:random:effect")


def test_valid_empty_deny_config_denies_only_the_floor(tmp_path, monkeypatch):
    import grove.red_policy as rp

    good = tmp_path / "good.yaml"
    good.write_text("red_denied_by_policy: []\n")
    monkeypatch.setattr(rp, "_overlay_path", lambda: good)
    monkeypatch.setattr(rp, "_repo_schema_path", lambda: good)
    assert rp.is_denied_by_policy("random:effect") is False
    assert rp.is_denied_by_policy("rm:catastrophic") is True  # hardcoded floor


# ── Migration: prune / refusal / idempotency / report ────────────────────────

_LEGACY_OVERLAY = """schema_version: 1
tool_zones:
  write_file:
    default_zone: green
  review_proposals:
    default_zone: green
  terminal:
    default_zone: yellow
    rules:
      - match_pattern: ^date$
        zone: green
        reason: op-approved
      - match_pattern: ^ls$
        zone: green
        reason: op-approved
"""


def _overlay(tmp_path: Path, body: str = _LEGACY_OVERLAY) -> Path:
    p = tmp_path / "zones.autonomaton.yaml"
    p.write_text(body)
    return p


def test_migration_prunes_tool_zones_stamps_v2_and_backs_up(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    ov = _overlay(tmp_path)
    report = migrate.migrate_overlay(ov, queue_path=tmp_path / "q.jsonl")
    assert report.status == "migrated"
    # tool_zones gone; schema_version stamped to 2.
    import yaml
    data = yaml.safe_load(ov.read_text())
    assert data == {"schema_version": 2}
    # .bak preserved (recovery anchor).
    assert report.backup_path and Path(report.backup_path).exists()
    assert "tool_zones" in Path(report.backup_path).read_text()


def test_migration_report_enumerates_prune_set(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    report = migrate.migrate_overlay(_overlay(tmp_path), queue_path=tmp_path / "q.jsonl")
    # 2 default_zone promotions + 2 terminal.rules enumerated with v2-derived zones.
    assert len(report.pruned_default_zone_promotions) == 2
    assert len(report.pruned_terminal_rules) == 2
    tools = {p["tool"]: p for p in report.pruned_default_zone_promotions}
    assert tools["write_file"]["overlay_zone"] == "green"
    assert tools["write_file"]["v2_derived_zone"] == "yellow"      # workspace_write
    assert tools["review_proposals"]["v2_derived_zone"] == "green"  # read_only


def test_migration_idempotent_second_run_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    ov = _overlay(tmp_path)
    migrate.migrate_overlay(ov, queue_path=tmp_path / "q.jsonl")
    after_first = ov.read_bytes()
    report2 = migrate.migrate_overlay(ov, queue_path=tmp_path / "q.jsonl")
    assert report2.status == "nothing-to-do"
    assert ov.read_bytes() == after_first  # byte-identical


def test_migration_refuses_malformed_overlay_zero_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    ov = _overlay(tmp_path, "tool_zones: [unclosed\n:::not yaml")
    before = ov.read_bytes()
    with pytest.raises(migrate.OverlayMigrationError, match="not valid YAML"):
        migrate.migrate_overlay(ov, queue_path=tmp_path / "q.jsonl")
    assert ov.read_bytes() == before  # zero writes
    assert not list(tmp_path.glob("*.bak_*"))  # no backup written


def test_migration_absent_overlay_is_nothing_to_do(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    report = migrate.migrate_overlay(tmp_path / "nope.yaml")
    assert report.status == "absent"

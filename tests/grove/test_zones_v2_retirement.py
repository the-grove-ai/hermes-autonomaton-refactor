"""zones-v2-scope-keying P2 — retirement + deny-all coverage.

Covers the P2b test additions:
  * D1 — a non-governance halt offers no "always" store (resolve_always_store →
    None); a SUBMITTED "always" on a non-governance halt is REFUSED (re-prompt →
    deny), never a silent no-op, never a crash.
  * D1 — a governance-mutation halt still resolves a standing_grant store (the
    "always" affordance survives there, unchanged).
  * D5(b′) — a malformed deny-config denies ALL RED at halt-time and does NOT
    raise (fail-closed).

hermes-severance-v1: the overlay-migration coverage (prune / refusal /
idempotency / report) was removed with grove/config/zones_overlay_migrate.py.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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



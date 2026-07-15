"""operator-mutable-admission-v1 Phase 5 (builds) — cadence + retention wiring.

* run_admission_friction_scan is the FIFTH signal on ``flywheel scan --propose``.
* ledger retention covers the capability_refusals feed dir (one seam for all
  feeds; run_retention itself untouched, called once per feed dir).
"""
from __future__ import annotations

from types import SimpleNamespace

import grove.flywheel_cli as fc
import grove.ledger_retention as lr
from grove.capability_refusals import refusals_dir


def test_scan_propose_runs_admission_friction_signal(monkeypatch, capsys):
    called = {}

    def _af_spy(*, queue_path=None):
        called["queue_path"] = queue_path
        return (2, 1)

    monkeypatch.setattr(fc, "run_admission_friction_scan", _af_spy)
    # neutralize the other propose signals so the test isolates the 5th
    for name in ("run_tier_ratchet_scan", "run_disposition_promotion_scan",
                 "run_fault_triage_scan", "run_binding_scan"):
        monkeypatch.setattr(fc, name, lambda **k: (0, 0))
    # short-circuit the always-on pattern-cache scan after the propose block
    import grove.eval.pattern_compiler as pc
    monkeypatch.setattr(pc, "load_pattern_cache_config", lambda: {"enabled": False})

    rc = fc.cli_scan(propose=True, store=object())
    assert rc == 0
    assert "queue_path" in called, "admission_friction scan must run under --propose"
    out = capsys.readouterr().out
    assert "Admission friction: queued 2 admission_friction proposal(s)" in out
    assert "1 already pending (deduped)" in out


def test_retention_entrypoint_covers_refusals_feed(monkeypatch):
    seen_dirs = []

    def _spy(*, ledger_dir=None, **kw):
        seen_dirs.append(ledger_dir)
        return lr.RunReport()

    monkeypatch.setattr(lr, "run_retention", _spy)
    monkeypatch.setattr(
        lr, "load_retention_config",
        lambda: SimpleNamespace(
            enabled=True, retention_days=30, cold_buffer_hours=24,
            batch_max_files=100,
        ),
    )

    rc = fc.cli_maintain_retention()
    assert rc == 0
    # one seam, both feeds: the default kaizen-ledger dir (None) AND the
    # capability_refusals dir are each swept in the same pass.
    assert None in seen_dirs, "default ledger dir must still be covered"
    assert refusals_dir() in seen_dirs, "the refusals feed dir must be covered"

"""model-catalog-v1 P2 Task 8 — load-time catalog coherence Andon (M-2/G-5).

An off-catalog ACTIVE tier binding must surface an operator-facing, recurring,
NON-FATAL Andon (Kaizen card + portal badge) and never degrade a tier or block
boot. Reconcile clears it; boot always proceeds.
"""

from __future__ import annotations

import pytest

import grove.config.catalog_coherence as cc


# ── pure detector ─────────────────────────────────────────────────────────────


class TestDetector:
    def test_off_catalog_binding_detected(self):
        prefs = {"T1": {"model": "a/x"}, "T2": {"model": "ghost/9000"}}
        v = cc.off_catalog_active_bindings(prefs, {"a/x"})
        assert v == [{"tier": "T2", "model": "ghost/9000"}]

    def test_all_on_catalog_is_clean(self):
        prefs = {"T1": {"model": "a/x"}, "T2": {"model": "b/y"}}
        assert cc.off_catalog_active_bindings(prefs, {"a/x", "b/y"}) == []

    def test_modelless_and_nonmapping_entries_ignored(self):
        prefs = {"T0": {"handler": "builtin"}, "telemetry": "T1", "T1": {"model": "a/x"}}
        assert cc.off_catalog_active_bindings(prefs, {"a/x"}) == []


# ── portal badge (recurring surface, clears on reconcile) ────────────────────


class TestBadge:
    _cat = [{"slug": "a/x"}]

    def test_badge_names_violations(self):
        html = cc.coherence_badge_html({"T2": {"model": "ghost/9000"}}, self._cat)
        assert "badge-red" in html and "ghost/9000" in html and "T2" in html

    def test_badge_empty_when_coherent(self):
        assert cc.coherence_badge_html({"T1": {"model": "a/x"}}, self._cat) == ""

    def test_badge_escapes_content(self):
        html = cc.coherence_badge_html({"T2": {"model": "<script>/x"}}, self._cat)
        assert "<script>" not in html  # escaped

    def test_routing_fragment_shows_badge_when_incoherent(self, monkeypatch):
        # Integration: the routing panel embeds the badge for an off-catalog bind.
        from grove.api import fragments

        monkeypatch.setattr(fragments, "_swappable_tiers", lambda: ["T2"])
        config = {"T2": {"model": "ghost/9000", "provider": "openrouter"}}
        catalog = [{
            "slug": "a/x", "display_name": "A X", "provider": "openrouter",
            "input_cost_per_mtok": 1, "output_cost_per_mtok": 2,
        }]
        html = fragments.render_routing_fragment(config, catalog)
        assert "badge-red" in html and "ghost/9000" in html


# ── boot Andon (non-fatal, files card, clears on reconcile) ──────────────────


class TestBootAndon:
    def _capture_ledger(self, monkeypatch):
        events = []
        from grove import kaizen_ledger as kl

        monkeypatch.setattr(
            kl.KaizenLedger, "record",
            lambda self, event_type, **f: events.append((event_type, f)),
        )
        return events

    def test_incoherent_boot_files_card_and_returns_violations(self, monkeypatch):
        events = self._capture_ledger(monkeypatch)
        monkeypatch.setattr(
            cc, "evaluate_coherence",
            lambda: {"coherent": False, "violations": [{"tier": "T2", "model": "ghost/9000"}]},
        )
        report = cc.check_catalog_coherence_at_boot()
        assert report["coherent"] is False
        card = [e for e in events if e[0] == "catalog_coherence_violation"]
        assert len(card) == 1
        assert card[0][1]["violations"] == [{"tier": "T2", "model": "ghost/9000"}]

    def test_reconcile_clears_no_card(self, monkeypatch):
        events = self._capture_ledger(monkeypatch)
        monkeypatch.setattr(cc, "evaluate_coherence", lambda: {"coherent": True, "violations": []})
        report = cc.check_catalog_coherence_at_boot()
        assert report["coherent"] is True
        assert [e for e in events if e[0] == "catalog_coherence_violation"] == []

    def test_boot_proceeds_when_config_unreadable(self, monkeypatch):
        # Never raise — boot must survive an unreadable catalog/config.
        def _boom():
            raise RuntimeError("catalog gone")

        monkeypatch.setattr(cc, "evaluate_coherence", _boom)
        report = cc.check_catalog_coherence_at_boot()  # must not raise
        assert report == {"coherent": True, "violations": [], "skipped": True}

    def test_ledger_failure_does_not_break_boot(self, monkeypatch):
        from grove import kaizen_ledger as kl

        def _boom(self, *a, **k):
            raise RuntimeError("ledger down")

        monkeypatch.setattr(kl.KaizenLedger, "record", _boom)
        monkeypatch.setattr(
            cc, "evaluate_coherence",
            lambda: {"coherent": False, "violations": [{"tier": "T2", "model": "ghost/9000"}]},
        )
        # filing failure is swallowed (error-log floor) — no raise
        report = cc.check_catalog_coherence_at_boot()
        assert report["coherent"] is False


def test_coherence_lives_outside_dispatch_isolation():
    # catalog_coherence may import model_catalog (it is NOT on the dispatch path);
    # the router/dispatcher still must not — belt-and-suspenders with the G-1b test.
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    for rel in ("grove/router.py", "grove/dispatcher.py"):
        assert "model_catalog" not in (repo / rel).read_text(encoding="utf-8")

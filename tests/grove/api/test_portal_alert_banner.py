"""portal-action-error-surfacing-v1 P2 — the non-destructive #alert-banner.

Covers the server-side OOB fragment, the base-template slot + neutered
responseError, and inline-path non-regression. Standalone: reads the fragment
function and the static index.html; no gateway, no deploy.
"""

from __future__ import annotations

from pathlib import Path

from grove.api.fragments import render_alert_banner, render_forge_publish_card

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INDEX = _REPO_ROOT / "gateway" / "assets" / "portal" / "index.html"


class TestAlertBannerFragment:
    def test_renders_alert_banner_oob(self):
        html = render_alert_banner("Drive publish failed", status=422)
        assert 'id="alert-banner"' in html
        assert 'hx-swap-oob="true"' in html
        assert "Drive publish failed" in html
        assert "422" in html

    def test_targets_banner_only_never_center_panel(self):
        # Non-destructive: the fragment touches ONLY #alert-banner.
        html = render_alert_banner("boom", status=500, detail="stack")
        assert "center-panel" not in html
        assert "stack" in html

    def test_escapes_message_and_detail(self):
        html = render_alert_banner("<script>alert(1)</script>", detail="<b>x</b>")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html
        assert "<b>x</b>" not in html

    def test_no_status_omits_prefix(self):
        html = render_alert_banner("plain message")
        assert "plain message" in html
        # No "None:" leaking when status is unset.
        assert "None" not in html


class TestResponseErrorRepoint:
    def _index(self) -> str:
        return _INDEX.read_text(encoding="utf-8")

    def test_persistent_slot_present(self):
        idx = self._index()
        assert 'id="alert-banner"' in idx
        # Always-present and empty by default (an OOB target that takes no space).
        assert 'class="alert-banner"' in idx

    def test_destructive_center_panel_blank_is_gone(self):
        idx = self._index()
        # The destructive swap (blanking #center-panel with an error card) is gone.
        assert "getElementById('center-panel').innerHTML" not in idx
        assert 'class="error-card"' not in idx

    def test_response_error_listener_preserved_and_repointed(self):
        idx = self._index()
        # Neutered, NOT deleted — the shared chokepoint listener still exists...
        assert "htmx:responseError" in idx
        # ...and now drives the banner instead of the center panel.
        assert "getElementById('alert-banner')" in idx

    def test_listener_fail_safe_floor(self):
        # The listener is the delivery hook on the failure surface — it must never
        # silently no-op. The generic floor is unconditional (banner is set to
        # lifted-OR-fallback, so an empty/thrown lift falls through), the lift
        # only wins on NON-EMPTY content, and the status read is guarded.
        idx = self._index()
        assert "banner.innerHTML = lifted || fallback" in idx  # floor guaranteed
        assert "oob.innerHTML.trim()" in idx                    # empty lift excluded
        assert "try { status = evt.detail.xhr.status" in idx    # status read guarded


class TestInlinePathNonRegression:
    def test_forge_inline_error_card_still_returned(self):
        # P2 is additive: the inline failure card the handler returns is unchanged
        # and independent of the banner path.
        card = render_forge_publish_card("acme", error="Notion MCP is cold.")
        assert 'id="forge-publish-acme"' in card
        assert "Notion MCP is cold." in card
        assert "alert-banner" not in card

    def test_forge_success_card_unchanged(self):
        card = render_forge_publish_card("acme", published=True, folder_link="http://x")
        assert "Drafted" in card
        assert "alert-banner" not in card

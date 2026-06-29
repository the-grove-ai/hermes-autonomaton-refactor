"""portal-link-provider-v1 — tests.

Phase 2: ``resolve_portal_base_url`` (I2 config-derivation chain).
Phase 3 will append the provider tests to this module.
"""

from grove.prompt.composer import build_default_composer
from grove.prompt.portal_links import (
    build_portal_links_provider,
    resolve_portal_base_url,
)


def test_base_url_explicit_from_portal_config():
    """(1) An operator-set portal.base_url wins outright."""
    config = {"portal": {"base_url": "http://100.102.6.70:8642"}}
    assert resolve_portal_base_url(config) == "http://100.102.6.70:8642"


def test_base_url_trailing_slash_stripped():
    """The trailing slash is stripped so callers append /portal#... cleanly."""
    config = {"portal": {"base_url": "http://100.102.6.70:8642/"}}
    assert resolve_portal_base_url(config) == "http://100.102.6.70:8642"


def test_base_url_derived_from_api_server():
    """(2) No portal.base_url — derive http://{host}:{port} from api_server."""
    config = {"platforms": {"api_server": {"host": "192.168.1.50", "port": 9000}}}
    assert resolve_portal_base_url(config) == "http://192.168.1.50:9000"


def test_base_url_zero_host_falls_back_to_loopback():
    """0.0.0.0 is a bind-any wildcard, not dialable — map it to loopback."""
    config = {"platforms": {"api_server": {"host": "0.0.0.0", "port": 8642}}}
    assert resolve_portal_base_url(config) == "http://127.0.0.1:8642"


def test_base_url_default_when_empty_dict():
    """(3) An explicit empty dict (no portal/api_server) — loopback default.
    The explicit-dict path is pure: it does NOT load the sovereign config."""
    assert resolve_portal_base_url({}) == "http://127.0.0.1:8642"


def test_base_url_none_loads_full_sovereign_config(monkeypatch):
    """config=None reads the FULL sovereign config via load_config() — the
    production fix: the composer hands providers only the ``prompt`` sub-block
    (no portal/platforms), so None must reach past it to the real config."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"portal": {"base_url": "http://100.102.6.70:8642"}},
    )
    assert resolve_portal_base_url() == "http://100.102.6.70:8642"
    assert resolve_portal_base_url(None) == "http://100.102.6.70:8642"


def test_base_url_none_loaded_config_empty_falls_to_default(monkeypatch):
    """If the loaded config carries neither key, still the loopback default."""
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    assert resolve_portal_base_url() == "http://127.0.0.1:8642"


# ── Phase 3 — provider ────────────────────────────────────────────────


def test_provider_returns_portal_links_section():
    """The provider returns a SectionResult labelled 'portal_links'."""
    provider = build_portal_links_provider(base_url="http://t.test:8642")
    result = provider({})
    assert result is not None
    assert result.label == "portal_links"
    assert result.text.strip()


def test_provider_uses_hash_routed_urls():
    """Every link is hash-routed (#fragments); the raw /portal/fragments URL
    form (which returns an unstyled fragment) never appears (I5)."""
    provider = build_portal_links_provider(base_url="http://t.test:8642")
    text = provider({}).text
    assert "#fragments" in text
    assert "/portal#fragments/" in text
    # The bare fragment-route URL form must NOT appear — that would bypass the
    # shell and serve raw HTML.
    assert "/portal/fragments" not in text


def test_provider_base_url_resolved_from_config():
    """With no explicit base_url, the provider resolves it from config (I2)."""
    config = {"portal": {"base_url": "http://cfg.test:9000"}}
    provider = build_portal_links_provider(config=config)
    text = provider({}).text
    assert "http://cfg.test:9000/portal" in text
    # portal-link-reliability-v1 (P3): cellar/pages template removed (now ready-
    # link embedded); witness base_url resolution via a surviving standing link.
    assert "http://cfg.test:9000/portal#fragments/dock/goals" in text


def test_provider_loads_full_config_when_no_args(monkeypatch):
    """REGRESSION (base_url-empty bug): with neither base_url nor an explicit
    config (the live composer path), the provider resolves via the FULL
    sovereign config — load_config() — not the prompt sub-block. The rendered
    section must carry the sovereign portal.base_url, not a bare /portal."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"portal": {"base_url": "http://100.102.6.70:8642"}},
    )
    provider = build_portal_links_provider()  # no base_url, no config
    text = provider({}).text
    assert "http://100.102.6.70:8642/portal" in text
    # portal-link-reliability-v1 (P3): witness via a surviving standing link.
    assert "http://100.102.6.70:8642/portal#fragments/dock/goals" in text


def test_provider_section_under_token_budget():
    """I4: the rendered section stays under ~300 tokens (~1200 chars)."""
    # A long-ish Tailscale-style host is the realistic worst case for length.
    provider = build_portal_links_provider(base_url="http://100.102.6.70:8642")
    text = provider({}).text
    assert len(text) < 1200


def test_provider_caches_resolved_url_across_turns():
    """The base URL resolves once and is reused (session-stable)."""
    provider = build_portal_links_provider(base_url="http://t.test:8642")
    first = provider({}).text
    second = provider({}).text
    assert first == second
    assert "http://t.test:8642" in first


def test_provider_returns_none_when_base_url_unusable():
    """Defensive: a base_url that resolves to nothing usable → None (the
    composer treats that as a skip). Not a normal path — config defaults."""
    provider = build_portal_links_provider(base_url="   ")
    assert provider({}) is None


def test_registration_in_default_composer_at_volatile_17():
    """build_default_composer registers portal_links at tier=volatile, order=17."""
    composer = build_default_composer()
    reg = composer._sections["portal_links"]
    assert reg.tier == "volatile"
    assert reg.order == 17
    assert reg.enabled is True

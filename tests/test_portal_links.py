"""portal-link-provider-v1 — tests.

Phase 2: ``resolve_portal_base_url`` (I2 config-derivation chain).
Phase 3 will append the provider tests to this module.
"""

from grove.prompt.portal_links import resolve_portal_base_url


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


def test_base_url_default_when_neither():
    """(3) Neither source present — the sensible loopback default."""
    assert resolve_portal_base_url({}) == "http://127.0.0.1:8642"
    assert resolve_portal_base_url(None) == "http://127.0.0.1:8642"

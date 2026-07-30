"""hermes-severance-v1 T2 — retirement pin for excised platform adapters.

Every platform adapter except telegram, api_server, and tty/local (LOCAL) was
excised at severance. Their ``Platform`` enum members are RETAINED as inert
vocabulary — kept-code guards throughout gateway/run.py reference them — but no
adapter exists for them any more. ``_create_adapter`` therefore returns ``None``
for each retired member, and the boot loop logs a loud "No adapter available"
skip (gateway/run.py).

This pin asserts that contract for the explicit retired SET. A retired member
gaining an adapter, or a new builtin platform appearing without being accounted
for, is a loud diff here rather than a silent regression.
"""
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner

# The retired SET is the single source of truth in tests/_retired_platforms.py
# (shared with the platform↔toolset parity guard). Keep it in sync with the
# comment block on the Platform enum in gateway/config.py.
from tests._retired_platforms import RETIRED, KEPT


@pytest.fixture
def runner():
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    return GatewayRunner(config)


@pytest.mark.parametrize("platform", RETIRED, ids=[p.value for p in RETIRED])
def test_retired_platform_creates_no_adapter(runner, platform):
    """Every retired member falls through _create_adapter to ``return None``."""
    assert runner._create_adapter(platform, PlatformConfig(enabled=True)) is None


def test_retired_set_is_exhaustive():
    """retired ∪ kept == the in-source builtin Platform value set.

    Bound to ``gateway.config._BUILTIN_PLATFORM_VALUES`` (config.py:212) — the
    frozen snapshot of builtin platform values taken at import, before any
    dynamic ``_missing_`` lookups. Dynamic plugin members (e.g. ``irc``) are
    DELIBERATELY excluded: on-demand plugin platforms are the extensibility
    design, not builtin vocabulary, so a plugin member registered by another
    test must not perturb this guard (that order-dependence is why comparing
    against the live ``Platform.__members__`` was wrong). If a builtin enum
    member is added (or a retired one removed) without updating the retirement
    SET, the sets diverge and this fails loudly — the SET is the guard.
    """
    from gateway.config import _BUILTIN_PLATFORM_VALUES

    retired_and_kept = {p.value for p in RETIRED} | {p.value for p in KEPT}
    assert retired_and_kept == _BUILTIN_PLATFORM_VALUES, (
        "Platform enum membership drift — a builtin platform value is neither "
        "retired nor kept. Update the retirement SET (tests/_retired_platforms.py) "
        "and the gateway/config.py enum note."
    )

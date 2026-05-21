"""Tests for the grove.kaizen package — Sprint 06b foundation.

Verifies the package exports, the three stubs raise NotImplementedError
with the design-contract link, and the Curator copy exposes the same
public interface as the canonical agent.curator (which must stay
unmodified per the Sprint 06b out-of-scope guard).
"""

from __future__ import annotations

import pytest


# ----- package exports -------------------------------------------------------

def test_package_exports_three_stub_classes() -> None:
    from grove import kaizen
    assert hasattr(kaizen, "IntentPatternDetector")
    assert hasattr(kaizen, "TierRatchet")
    assert hasattr(kaizen, "UsageRefiner")
    assert set(kaizen.__all__) == {
        "IntentPatternDetector", "TierRatchet", "UsageRefiner",
    }


# ----- stubs raise NotImplementedError ---------------------------------------

def test_detector_stub_raises() -> None:
    from grove.kaizen import IntentPatternDetector
    with pytest.raises(NotImplementedError, match="the-grove.ai/standards/001"):
        IntentPatternDetector().detect()


def test_detector_stub_raises_with_explicit_args() -> None:
    from grove.kaizen import IntentPatternDetector
    with pytest.raises(NotImplementedError, match="the-grove.ai/standards/001"):
        IntentPatternDetector().detect(window_days=30, threshold=5)


def test_ratchet_stub_raises() -> None:
    from grove.kaizen import TierRatchet
    with pytest.raises(NotImplementedError, match="the-grove.ai/standards/001"):
        TierRatchet().ratchet()


def test_refiner_stub_raises() -> None:
    from grove.kaizen import UsageRefiner
    with pytest.raises(NotImplementedError, match="the-grove.ai/standards/001"):
        UsageRefiner().refine()


# ----- curator copy ----------------------------------------------------------

_CURATOR_PUBLIC_API = (
    "load_state",
    "save_state",
    "set_paused",
    "is_paused",
    "is_enabled",
    "get_interval_hours",
    "get_min_idle_hours",
    "get_stale_after_days",
    "get_archive_after_days",
    "should_run_now",
    "apply_automatic_transitions",
    "DEFAULT_INTERVAL_HOURS",
    "CURATOR_REVIEW_PROMPT",
)


def test_kaizen_curator_importable() -> None:
    from grove.kaizen import curator as kaizen_curator
    assert kaizen_curator is not None


def test_kaizen_curator_matches_agent_curator_interface() -> None:
    """The Kaizen-namespace curator exposes the same public surface as the
    canonical agent.curator — it is a verbatim copy."""
    from grove.kaizen import curator as kaizen_curator
    from agent import curator as agent_curator
    for symbol in _CURATOR_PUBLIC_API:
        assert hasattr(kaizen_curator, symbol), f"grove.kaizen.curator missing {symbol}"
        assert hasattr(agent_curator, symbol), f"agent.curator missing {symbol}"


def test_agent_curator_still_importable() -> None:
    """Sprint 06b out-of-scope guard: agent/curator.py is unmodified and
    remains the canonical implementation for existing consumers."""
    from agent import curator as agent_curator
    assert callable(agent_curator.load_state)
    assert callable(agent_curator.should_run_now)

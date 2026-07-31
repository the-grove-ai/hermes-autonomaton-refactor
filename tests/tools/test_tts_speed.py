"""Tests for TTS speed configuration across providers."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in (
        "OPENAI_API_KEY",
        "MINIMAX_API_KEY",
        "MINIMAX_GROUP_ID",
        "GROVE_SESSION_PLATFORM",
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Edge TTS speed
# ---------------------------------------------------------------------------

class TestEdgeTtsSpeed:
    def _run(self, tts_config, tmp_path):
        mock_comm = MagicMock()
        mock_comm.save = AsyncMock()
        mock_edge = MagicMock()
        mock_edge.Communicate = MagicMock(return_value=mock_comm)

        with patch("tools.tts_tool._import_edge_tts", return_value=mock_edge):
            from tools.tts_tool import _generate_edge_tts
            asyncio.run(_generate_edge_tts("Hello", str(tmp_path / "out.mp3"), tts_config))
        return mock_edge.Communicate

    def test_default_no_rate_kwarg(self, tmp_path):
        """No speed config => no rate kwarg passed to Communicate."""
        comm_cls = self._run({}, tmp_path)
        kwargs = comm_cls.call_args[1]
        assert "rate" not in kwargs

    def test_global_speed_applied(self, tmp_path):
        """Global tts.speed used as fallback."""
        comm_cls = self._run({"speed": 1.5}, tmp_path)
        kwargs = comm_cls.call_args[1]
        assert kwargs["rate"] == "+50%"

    def test_provider_speed_overrides_global(self, tmp_path):
        """tts.edge.speed takes precedence over tts.speed."""
        comm_cls = self._run({"speed": 1.5, "edge": {"speed": 2.0}}, tmp_path)
        kwargs = comm_cls.call_args[1]
        assert kwargs["rate"] == "+100%"

    def test_speed_below_one(self, tmp_path):
        """Speed < 1.0 produces a negative rate string."""
        comm_cls = self._run({"speed": 0.5}, tmp_path)
        kwargs = comm_cls.call_args[1]
        assert kwargs["rate"] == "-50%"

    def test_speed_exactly_one_no_rate(self, tmp_path):
        """Explicit speed=1.0 should not pass rate kwarg."""
        comm_cls = self._run({"speed": 1.0}, tmp_path)
        kwargs = comm_cls.call_args[1]
        assert "rate" not in kwargs

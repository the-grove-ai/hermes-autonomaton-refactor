"""Tests for per-provider TTS input-character limits.

After the voice-backend severance, ``edge`` is the only selectable built-in
TTS provider; user-declared command providers resolve their own caps.
"""

import json
import logging
from unittest.mock import patch

import pytest

from tools.tts_tool import (
    FALLBACK_MAX_TEXT_LENGTH,
    PROVIDER_MAX_TEXT_LENGTH,
    _resolve_max_text_length,
)


class TestResolveMaxTextLength:
    def test_edge_default(self):
        assert _resolve_max_text_length("edge", {}) == PROVIDER_MAX_TEXT_LENGTH["edge"]

    def test_unknown_provider_falls_back(self):
        assert _resolve_max_text_length("does-not-exist", {}) == FALLBACK_MAX_TEXT_LENGTH

    def test_empty_provider_falls_back(self):
        assert _resolve_max_text_length("", {}) == FALLBACK_MAX_TEXT_LENGTH
        assert _resolve_max_text_length(None, {}) == FALLBACK_MAX_TEXT_LENGTH

    def test_case_insensitive(self):
        assert _resolve_max_text_length("EDGE", {}) == PROVIDER_MAX_TEXT_LENGTH["edge"]
        assert _resolve_max_text_length("  Edge  ", {}) == PROVIDER_MAX_TEXT_LENGTH["edge"]

    # --- Overrides ---

    def test_override_wins(self):
        cfg = {"edge": {"max_text_length": 9999}}
        assert _resolve_max_text_length("edge", cfg) == 9999

    def test_override_zero_falls_through(self):
        # A broken/zero override must not disable truncation
        cfg = {"edge": {"max_text_length": 0}}
        assert _resolve_max_text_length("edge", cfg) == PROVIDER_MAX_TEXT_LENGTH["edge"]

    def test_override_negative_falls_through(self):
        cfg = {"edge": {"max_text_length": -1}}
        assert _resolve_max_text_length("edge", cfg) == PROVIDER_MAX_TEXT_LENGTH["edge"]

    def test_override_non_int_falls_through(self):
        cfg = {"edge": {"max_text_length": "lots"}}
        assert _resolve_max_text_length("edge", cfg) == PROVIDER_MAX_TEXT_LENGTH["edge"]

    def test_override_bool_falls_through(self):
        # bool is technically an int; make sure we don't treat True as 1 char
        cfg = {"edge": {"max_text_length": True}}
        assert _resolve_max_text_length("edge", cfg) == PROVIDER_MAX_TEXT_LENGTH["edge"]

    def test_missing_provider_section_uses_default(self):
        cfg = {"provider": "edge"}  # no "edge" key
        assert _resolve_max_text_length("edge", cfg) == PROVIDER_MAX_TEXT_LENGTH["edge"]

    def test_provider_config_not_a_dict(self):
        cfg = {"edge": "not-a-dict"}
        assert _resolve_max_text_length("edge", cfg) == PROVIDER_MAX_TEXT_LENGTH["edge"]

    # --- Sanity: the surviving providers have defaults ---

    def test_surviving_providers_have_defaults(self):
        # edge is the only selectable built-in TTS provider after severance.
        assert {"edge"}.issubset(PROVIDER_MAX_TEXT_LENGTH.keys())


class TestTextToSpeechToolTruncation:
    """End-to-end: verify the resolver actually drives the text_to_speech_tool
    truncation path rather than the old 4000-char global."""

    def test_edge_truncates_at_5000(self, tmp_path, monkeypatch, caplog):
        caplog.set_level(logging.WARNING, logger="tools.tts_tool")

        # 6000 chars -- over Edge TTS's 5000-char practical cap
        text = "A" * 6000
        captured_text = {}

        async def fake_edge(t, out, cfg):
            captured_text["text"] = t
            with open(out, "wb") as f:
                f.write(b"\x00")
            return out

        monkeypatch.setattr("tools.tts_tool._generate_edge_tts", fake_edge)
        monkeypatch.setattr("tools.tts_tool._convert_to_opus", lambda p: None)
        monkeypatch.setattr("tools.tts_tool._load_tts_config",
                            lambda: {"provider": "edge"})

        from tools.tts_tool import text_to_speech_tool
        out = str(tmp_path / "out.mp3")
        result = json.loads(text_to_speech_tool(text=text, output_path=out))

        assert result["success"] is True
        assert len(captured_text["text"]) == 5000
        assert any("edge" in rec.message.lower() for rec in caplog.records)

    def test_user_override_is_respected(self, tmp_path, monkeypatch):
        # User says "cap edge at 100 chars" -- we must honor it
        text = "C" * 500
        captured_text = {}

        async def fake_edge(t, out, cfg):
            captured_text["text"] = t
            with open(out, "wb") as f:
                f.write(b"\x00")
            return out

        monkeypatch.setattr("tools.tts_tool._generate_edge_tts", fake_edge)
        monkeypatch.setattr("tools.tts_tool._convert_to_opus", lambda p: None)
        monkeypatch.setattr("tools.tts_tool._load_tts_config",
                            lambda: {"provider": "edge",
                                     "edge": {"max_text_length": 100}})

        from tools.tts_tool import text_to_speech_tool
        out = str(tmp_path / "out.mp3")
        result = json.loads(text_to_speech_tool(text=text, output_path=out))

        assert result["success"] is True
        assert len(captured_text["text"]) == 100

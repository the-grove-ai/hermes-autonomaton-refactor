"""Tests for tools.transcription_tools — local faster-whisper STT pipeline.

Covers local provider resolution, config loading, validation edge cases, and
end-to-end dispatch.  All external dependencies are mocked.
"""

import os
import struct
import subprocess
import wave
from unittest.mock import MagicMock, patch

import pytest


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def sample_wav(tmp_path):
    """Create a minimal valid WAV file (1 second of silence at 16kHz)."""
    wav_path = tmp_path / "test.wav"
    n_frames = 16000
    silence = struct.pack(f"<{n_frames}h", *([0] * n_frames))

    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(silence)

    return str(wav_path)


@pytest.fixture
def sample_ogg(tmp_path):
    """Create a fake OGG file for validation tests."""
    ogg_path = tmp_path / "test.ogg"
    ogg_path.write_bytes(b"fake audio data")
    return str(ogg_path)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure no real API keys leak into tests."""
    monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("GROVE_LOCAL_STT_COMMAND", raising=False)
    monkeypatch.delenv("GROVE_LOCAL_STT_LANGUAGE", raising=False)


class TestGetProviderFallbackPriority:
    """Auto-detect and provider resolution — local faster-whisper only."""

    def test_auto_detect_prefers_local(self):
        """Auto-detect resolves to local when faster-whisper is available."""
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True):
            from tools.transcription_tools import _get_provider
            assert _get_provider({}) == "local"

    def test_unknown_provider_falls_back_to_local(self):
        """An unsupported provider name resolves to local when faster-whisper
        is available — no silent cloud dispatch."""
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True):
            from tools.transcription_tools import _get_provider
            assert _get_provider({"provider": "custom-endpoint"}) == "local"

    def test_unknown_provider_no_local_returns_none(self):
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", False):
            from tools.transcription_tools import _get_provider
            assert _get_provider({"provider": "custom-endpoint"}) == "none"

    def test_empty_config_defaults_to_local(self):
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True):
            from tools.transcription_tools import _get_provider
            assert _get_provider({}) == "local"


# ============================================================================
# Explicit provider config respected  (GH-1774)
# ============================================================================

class TestExplicitProviderRespected:
    """The local faster-whisper backend is authoritative. No silent fallback
    to any cloud provider even when cloud API keys are present."""

    def test_explicit_local_no_fallback_to_openai(self, monkeypatch):
        """GH-1774: provider=local must not silently fall back to openai
        even when an OpenAI API key is set; with no local backend, none."""
        monkeypatch.setenv("OPENAI_API_KEY", "***")
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", False):
            from tools.transcription_tools import _get_provider
            result = _get_provider({"provider": "local"})
            assert result == "none", f"Expected 'none' but got {result!r}"

    def test_explicit_local_no_fallback_to_groq(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", False):
            from tools.transcription_tools import _get_provider
            result = _get_provider({"provider": "local"})
            assert result == "none"

    def test_unsupported_provider_no_silent_cloud(self, monkeypatch):
        """An unsupported cloud provider name does not dispatch to the cloud;
        it resolves to local when available, else none."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", False):
            from tools.transcription_tools import _get_provider
            assert _get_provider({"provider": "openai"}) == "none"
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True):
            from tools.transcription_tools import _get_provider
            assert _get_provider({"provider": "openai"}) == "local"


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("faster_whisper"),
    reason="faster_whisper not installed",
)
class TestTranscribeLocalExtended:
    def test_model_reuse_on_second_call(self, tmp_path):
        """Second call with same model should NOT reload the model."""
        audio = tmp_path / "test.ogg"
        audio.write_bytes(b"fake")

        mock_segment = MagicMock()
        mock_segment.text = "hi"
        mock_info = MagicMock()
        mock_info.language = "en"
        mock_info.duration = 1.0

        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([mock_segment], mock_info)
        mock_whisper_cls = MagicMock(return_value=mock_model)

        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True), \
             patch("faster_whisper.WhisperModel", mock_whisper_cls), \
             patch("tools.transcription_tools._local_model", None), \
             patch("tools.transcription_tools._local_model_name", None):
            from tools.transcription_tools import _transcribe_local
            _transcribe_local(str(audio), "base")
            _transcribe_local(str(audio), "base")

        # WhisperModel should be created only once
        assert mock_whisper_cls.call_count == 1

    def test_model_reloaded_on_change(self, tmp_path):
        """Switching model name should reload the model."""
        audio = tmp_path / "test.ogg"
        audio.write_bytes(b"fake")

        mock_segment = MagicMock()
        mock_segment.text = "hi"
        mock_info = MagicMock()
        mock_info.language = "en"
        mock_info.duration = 1.0

        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([mock_segment], mock_info)
        mock_whisper_cls = MagicMock(return_value=mock_model)

        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True), \
             patch("faster_whisper.WhisperModel", mock_whisper_cls), \
             patch("tools.transcription_tools._local_model", None), \
             patch("tools.transcription_tools._local_model_name", None):
            from tools.transcription_tools import _transcribe_local
            _transcribe_local(str(audio), "base")
            _transcribe_local(str(audio), "small")

        assert mock_whisper_cls.call_count == 2

    def test_exception_returns_failure(self, tmp_path):
        audio = tmp_path / "test.ogg"
        audio.write_bytes(b"fake")

        mock_whisper_cls = MagicMock(side_effect=RuntimeError("CUDA out of memory"))

        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True), \
             patch("faster_whisper.WhisperModel", mock_whisper_cls), \
             patch("tools.transcription_tools._local_model", None):
            from tools.transcription_tools import _transcribe_local
            result = _transcribe_local(str(audio), "large-v3")

        assert result["success"] is False
        assert "CUDA out of memory" in result["error"]

    def test_multiple_segments_joined(self, tmp_path):
        audio = tmp_path / "test.ogg"
        audio.write_bytes(b"fake")

        seg1 = MagicMock()
        seg1.text = "Hello"
        seg2 = MagicMock()
        seg2.text = " world"
        mock_info = MagicMock()
        mock_info.language = "en"
        mock_info.duration = 3.0

        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([seg1, seg2], mock_info)

        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True), \
             patch("faster_whisper.WhisperModel", return_value=mock_model), \
             patch("tools.transcription_tools._local_model", None):
            from tools.transcription_tools import _transcribe_local
            result = _transcribe_local(str(audio), "base")

        assert result["success"] is True
        assert result["transcript"] == "Hello world"

    def test_load_time_cuda_lib_failure_falls_back_to_cpu(self, tmp_path):
        """Missing libcublas at load time → reload on CPU, succeed."""
        audio = tmp_path / "test.ogg"
        audio.write_bytes(b"fake")

        seg = MagicMock()
        seg.text = "hi"
        info = MagicMock()
        info.language = "en"
        info.duration = 1.0

        cpu_model = MagicMock()
        cpu_model.transcribe.return_value = ([seg], info)

        call_args = []

        def fake_whisper(model_name, device, compute_type):
            call_args.append((device, compute_type))
            if device == "auto":
                raise RuntimeError("Library libcublas.so.12 is not found or cannot be loaded")
            return cpu_model

        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True), \
             patch("faster_whisper.WhisperModel", side_effect=fake_whisper), \
             patch("tools.transcription_tools._local_model", None), \
             patch("tools.transcription_tools._local_model_name", None):
            from tools.transcription_tools import _transcribe_local
            result = _transcribe_local(str(audio), "base")

        assert result["success"] is True
        assert result["transcript"] == "hi"
        assert call_args == [("auto", "auto"), ("cpu", "int8")]

    def test_runtime_cuda_lib_failure_evicts_cache_and_retries_on_cpu(self, tmp_path):
        """libcublas dlopen fails at transcribe() → evict cache, reload CPU, retry."""
        audio = tmp_path / "test.ogg"
        audio.write_bytes(b"fake")

        seg = MagicMock()
        seg.text = "recovered"
        info = MagicMock()
        info.language = "en"
        info.duration = 1.0

        # First model loads fine (auto), but transcribe() blows up on dlopen
        gpu_model = MagicMock()
        gpu_model.transcribe.side_effect = RuntimeError(
            "Library libcublas.so.12 is not found or cannot be loaded"
        )
        # Second model (forced CPU) works
        cpu_model = MagicMock()
        cpu_model.transcribe.return_value = ([seg], info)

        models = [gpu_model, cpu_model]
        call_args = []

        def fake_whisper(model_name, device, compute_type):
            call_args.append((device, compute_type))
            return models.pop(0)

        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True), \
             patch("faster_whisper.WhisperModel", side_effect=fake_whisper), \
             patch("tools.transcription_tools._local_model", None), \
             patch("tools.transcription_tools._local_model_name", None):
            from tools.transcription_tools import _transcribe_local
            result = _transcribe_local(str(audio), "base")

        assert result["success"] is True
        assert result["transcript"] == "recovered"
        # First load is auto, retry forces CPU.
        assert call_args == [("auto", "auto"), ("cpu", "int8")]
        # Cached-bad-model eviction: the broken GPU model was called once,
        # then discarded; the CPU model served the retry.
        assert gpu_model.transcribe.call_count == 1
        assert cpu_model.transcribe.call_count == 1

    def test_cuda_out_of_memory_does_not_trigger_cpu_fallback(self, tmp_path):
        """'CUDA out of memory' is a real error, not a missing lib — surface it."""
        audio = tmp_path / "test.ogg"
        audio.write_bytes(b"fake")

        mock_whisper_cls = MagicMock(side_effect=RuntimeError("CUDA out of memory"))

        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True), \
             patch("faster_whisper.WhisperModel", mock_whisper_cls), \
             patch("tools.transcription_tools._local_model", None), \
             patch("tools.transcription_tools._local_model_name", None):
            from tools.transcription_tools import _transcribe_local
            result = _transcribe_local(str(audio), "base")

        # Single call — no CPU retry, because OOM isn't a missing-lib symptom.
        assert mock_whisper_cls.call_count == 1
        assert result["success"] is False
        assert "CUDA out of memory" in result["error"]


# ============================================================================
# _load_stt_config
# ============================================================================

class TestLoadSttConfig:
    def test_returns_dict_when_import_fails(self):
        with patch("tools.transcription_tools._load_stt_config") as mock_load:
            mock_load.return_value = {}
            from tools.transcription_tools import _load_stt_config
            assert _load_stt_config() == {}

    def test_real_load_returns_dict(self):
        """_load_stt_config should always return a dict, even on import error."""
        with patch.dict("sys.modules", {"hermes_cli": None, "hermes_cli.config": None}):
            from tools.transcription_tools import _load_stt_config
            result = _load_stt_config()
        assert isinstance(result, dict)


# ============================================================================
# _validate_audio_file — edge cases
# ============================================================================

class TestValidateAudioFileEdgeCases:
    def test_directory_is_not_a_file(self, tmp_path):
        from tools.transcription_tools import _validate_audio_file
        # tmp_path itself is a directory with an .ogg-ish name? No.
        # Create a directory with a valid audio extension
        d = tmp_path / "audio.ogg"
        d.mkdir()
        result = _validate_audio_file(str(d))
        assert result is not None
        assert "not a file" in result["error"]

    def test_stat_oserror(self, tmp_path):
        f = tmp_path / "test.ogg"
        f.write_bytes(b"data")
        from tools.transcription_tools import _validate_audio_file

        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.stat", side_effect=OSError("disk error")):
            result = _validate_audio_file(str(f))

        assert result is not None
        assert "Failed to access" in result["error"]

    def test_all_supported_formats_accepted(self, tmp_path):
        from tools.transcription_tools import _validate_audio_file, SUPPORTED_FORMATS
        for fmt in SUPPORTED_FORMATS:
            f = tmp_path / f"test{fmt}"
            f.write_bytes(b"data")
            assert _validate_audio_file(str(f)) is None, f"Format {fmt} should be accepted"

    def test_case_insensitive_extension(self, tmp_path):
        from tools.transcription_tools import _validate_audio_file
        f = tmp_path / "test.MP3"
        f.write_bytes(b"data")
        assert _validate_audio_file(str(f)) is None


# ============================================================================
# transcribe_audio — end-to-end dispatch
# ============================================================================

class TestTranscribeAudioDispatch:
    def test_dispatches_to_local(self, sample_ogg):
        with patch("tools.transcription_tools._load_stt_config", return_value={}), \
             patch("tools.transcription_tools._get_provider", return_value="local"), \
             patch("tools.transcription_tools._transcribe_local",
                   return_value={"success": True, "transcript": "hi"}) as mock_local:
            from tools.transcription_tools import transcribe_audio
            result = transcribe_audio(sample_ogg)

        assert result["success"] is True
        mock_local.assert_called_once()

    def test_no_provider_returns_error(self, sample_ogg):
        with patch("tools.transcription_tools._load_stt_config", return_value={}), \
             patch("tools.transcription_tools._get_provider", return_value="none"):
            from tools.transcription_tools import transcribe_audio
            result = transcribe_audio(sample_ogg)

        assert result["success"] is False
        assert "No STT provider" in result["error"]
        assert "faster-whisper" in result["error"]

    def test_invalid_file_short_circuits(self):
        from tools.transcription_tools import transcribe_audio
        result = transcribe_audio("/nonexistent/audio.wav")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_model_override_passed_to_local(self, sample_ogg):
        with patch("tools.transcription_tools._load_stt_config", return_value={}), \
             patch("tools.transcription_tools._get_provider", return_value="local"), \
             patch("tools.transcription_tools._transcribe_local",
                   return_value={"success": True, "transcript": "hi"}) as mock_local:
            from tools.transcription_tools import transcribe_audio
            transcribe_audio(sample_ogg, model="large-v3")

        assert mock_local.call_args[0][1] == "large-v3"

    def test_config_local_model_used(self, sample_ogg):
        config = {"local": {"model": "small"}}
        with patch("tools.transcription_tools._load_stt_config", return_value=config), \
             patch("tools.transcription_tools._get_provider", return_value="local"), \
             patch("tools.transcription_tools._transcribe_local",
                   return_value={"success": True, "transcript": "hi"}) as mock_local:
            from tools.transcription_tools import transcribe_audio
            transcribe_audio(sample_ogg, model=None)

        assert mock_local.call_args[0][1] == "small"



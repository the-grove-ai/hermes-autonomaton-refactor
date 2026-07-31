#!/usr/bin/env python3
"""
Transcription Tools Module

Provides speech-to-text transcription via the local provider:

  - **local** (default, free) — faster-whisper running locally, no API key needed.
    Auto-downloads the model (~150 MB for ``base``) on first use.

Used by the messaging gateway to automatically transcribe voice messages
sent by users on Telegram, Discord, WhatsApp, Slack, and Signal.

Supported input formats: mp3, mp4, mpeg, mpga, m4a, wav, webm, ogg, aac

Usage::

    from tools.transcription_tools import transcribe_audio

    result = transcribe_audio("/path/to/audio.ogg")
    if result["success"]:
        print(result["transcript"])
"""

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any

from utils import is_truthy_value

logger = logging.getLogger(__name__)

def get_env_value(name, default=None):
    """Read env values through the live config module.

    Tests may monkeypatch and later restore ``hermes_cli.config.get_env_value``
    before this module is imported. Resolve the helper at call time so STT does
    not keep a stale imported function for the rest of the test process.
    """
    try:
        from hermes_cli.config import get_env_value as _get_env_value
    except ImportError:
        return os.getenv(name, default)
    value = _get_env_value(name)
    return default if value is None else value

# ---------------------------------------------------------------------------
# Optional imports — graceful degradation
# ---------------------------------------------------------------------------

import importlib.util as _ilu


def _safe_find_spec(module_name: str) -> bool:
    try:
        return _ilu.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return module_name in globals() or module_name in os.sys.modules


_HAS_FASTER_WHISPER = _safe_find_spec("faster_whisper")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PROVIDER = "local"
DEFAULT_LOCAL_MODEL = "base"
DEFAULT_LOCAL_STT_LANGUAGE = "en"
LOCAL_STT_LANGUAGE_ENV = "GROVE_LOCAL_STT_LANGUAGE"
COMMON_LOCAL_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")

SUPPORTED_FORMATS = {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".aac", ".flac"}
LOCAL_NATIVE_AUDIO_FORMATS = {".wav", ".aiff", ".aif"}
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB

# Cloud model names retained only so the local-model normalizer can detect
# and reject them (mapping to the default faster-whisper size).
OPENAI_MODELS = {"whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"}
GROQ_MODELS = {"whisper-large-v3", "whisper-large-v3-turbo", "distil-whisper-large-v3-en"}

# Singleton for the local model — loaded once, reused across calls
_local_model: Optional[object] = None
_local_model_name: Optional[str] = None

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------



def _load_stt_config() -> dict:
    """Load the ``stt`` section from user config, falling back to defaults."""
    try:
        from hermes_cli.config import load_config
        return load_config().get("stt", {})
    except Exception:
        return {}


def is_stt_enabled(stt_config: Optional[dict] = None) -> bool:
    """Return whether STT is enabled in config."""
    if stt_config is None:
        stt_config = _load_stt_config()
    enabled = stt_config.get("enabled", True)
    return is_truthy_value(enabled, default=True)


def _find_binary(binary_name: str) -> Optional[str]:
    """Find a local binary, checking common Homebrew/local prefixes as well as PATH."""
    for directory in COMMON_LOCAL_BIN_DIRS:
        candidate = Path(directory) / binary_name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(binary_name)


def _find_ffmpeg_binary() -> Optional[str]:
    return _find_binary("ffmpeg")


def _normalize_local_model(model_name: Optional[str]) -> str:
    """Return a valid faster-whisper model size, mapping cloud-only names to the default.

    Cloud providers like OpenAI use names such as ``whisper-1`` which are not
    valid for faster-whisper (which expects ``tiny``, ``base``, ``small``,
    ``medium``, or ``large-v*``).  When such a name is detected we fall back to
    the default local model and emit a warning so the user knows what happened.
    """
    if not model_name or model_name in OPENAI_MODELS or model_name in GROQ_MODELS:
        if model_name and (model_name in OPENAI_MODELS or model_name in GROQ_MODELS):
            logger.warning(
                "STT model '%s' is a cloud-only name and cannot be used with the local "
                "provider. Falling back to '%s'. Set stt.local.model to a valid "
                "faster-whisper size (tiny, base, small, medium, large-v3).",
                model_name,
                DEFAULT_LOCAL_MODEL,
            )
        return DEFAULT_LOCAL_MODEL
    return model_name


def _get_provider(stt_config: dict) -> str:
    """Determine which STT provider to use.

    The local faster-whisper backend is the only supported provider. An
    explicit ``stt.provider`` other than ``local`` is honoured only if it
    resolves to a working local backend; otherwise transcription reports
    ``none`` rather than silently falling back to a cloud service.
    """
    if not is_stt_enabled(stt_config):
        return "none"

    provider = stt_config.get("provider", DEFAULT_PROVIDER)

    if provider == "local" or not stt_config.get("provider"):
        if _HAS_FASTER_WHISPER:
            return "local"
        logger.warning(
            "STT provider 'local' configured but unavailable "
            "(install faster-whisper)"
        )
        return "none"

    # Any non-local provider is no longer supported. Fall back to local
    # when faster-whisper is available, otherwise report none.
    if _HAS_FASTER_WHISPER:
        logger.info(
            "STT provider '%s' is not supported; using local faster-whisper",
            provider,
        )
        return "local"
    logger.warning(
        "STT provider '%s' is not supported and faster-whisper is not "
        "installed", provider,
    )
    return "none"

# ---------------------------------------------------------------------------
# Shared validation
# ---------------------------------------------------------------------------


def _validate_audio_file(file_path: str) -> Optional[Dict[str, Any]]:
    """Validate the audio file.  Returns an error dict or None if OK."""
    audio_path = Path(file_path)

    if not audio_path.exists():
        return {"success": False, "transcript": "", "error": f"Audio file not found: {file_path}"}
    if not audio_path.is_file():
        return {"success": False, "transcript": "", "error": f"Path is not a file: {file_path}"}
    if audio_path.suffix.lower() not in SUPPORTED_FORMATS:
        return {
            "success": False,
            "transcript": "",
            "error": f"Unsupported format: {audio_path.suffix}. Supported: {', '.join(sorted(SUPPORTED_FORMATS))}",
        }
    try:
        file_size = audio_path.stat().st_size
        if file_size > MAX_FILE_SIZE:
            return {
                "success": False,
                "transcript": "",
                "error": f"File too large: {file_size / (1024*1024):.1f}MB (max {MAX_FILE_SIZE / (1024*1024):.0f}MB)",
            }
    except OSError as e:
        return {"success": False, "transcript": "", "error": f"Failed to access file: {e}"}

    return None

# ---------------------------------------------------------------------------
# Provider: local (faster-whisper)
# ---------------------------------------------------------------------------


# Substrings that identify a missing/unloadable CUDA runtime library.  When
# ctranslate2 (the backend for faster-whisper) cannot dlopen one of these, the
# "auto" device picker has already committed to CUDA and the model can no
# longer be used — we fall back to CPU and reload.
#
# Deliberately narrow: we match on library-name tokens and dlopen phrasing so
# we DO NOT accidentally catch legitimate runtime failures like "CUDA out of
# memory" — those should surface to the user, not silently fall back to CPU
# (a 32GB audio clip on CPU at int8 isn't useful either).
_CUDA_LIB_ERROR_MARKERS = (
    "libcublas",
    "libcudnn",
    "libcudart",
    "cannot be loaded",
    "cannot open shared object",
    "no kernel image is available",
    "no CUDA-capable device",
    "CUDA driver version is insufficient",
)


def _looks_like_cuda_lib_error(exc: BaseException) -> bool:
    """Heuristic: is this exception a missing/broken CUDA runtime library?

    ctranslate2 raises plain RuntimeError with messages like
    ``Library libcublas.so.12 is not found or cannot be loaded``.  We want to
    catch missing/unloadable shared libs and driver-mismatch errors, NOT
    legitimate runtime failures ("CUDA out of memory", model bugs, etc.).
    """
    msg = str(exc)
    return any(marker in msg for marker in _CUDA_LIB_ERROR_MARKERS)


def _load_local_whisper_model(model_name: str):
    """Load faster-whisper with graceful CUDA → CPU fallback.

    faster-whisper's ``device="auto"`` picks CUDA when the ctranslate2 wheel
    ships CUDA shared libs, even on hosts where the NVIDIA runtime
    (``libcublas.so.12`` / ``libcudnn*``) isn't installed — common on WSL2
    without CUDA-on-WSL, headless servers, and CPU-only developer machines.
    On those hosts the load itself sometimes succeeds and the dlopen failure
    only surfaces at first ``transcribe()`` call.

    We try ``auto`` first (fast CUDA path when it works), and on any CUDA
    library load failure fall back to CPU + int8.
    """
    from faster_whisper import WhisperModel
    try:
        return WhisperModel(model_name, device="auto", compute_type="auto")
    except Exception as exc:
        if not _looks_like_cuda_lib_error(exc):
            raise
        logger.warning(
            "faster-whisper CUDA load failed (%s) — falling back to CPU (int8). "
            "Install the NVIDIA CUDA runtime (libcublas/libcudnn) to use GPU.",
            exc,
        )
        return WhisperModel(model_name, device="cpu", compute_type="int8")


def _transcribe_local(file_path: str, model_name: str) -> Dict[str, Any]:
    """Transcribe using faster-whisper (local, free)."""
    global _local_model, _local_model_name

    if not _HAS_FASTER_WHISPER:
        return {"success": False, "transcript": "", "error": "faster-whisper not installed"}

    try:
        # Lazy-load the model (downloads on first use, ~150 MB for 'base')
        if _local_model is None or _local_model_name != model_name:
            logger.info("Loading faster-whisper model '%s' (first load downloads the model)...", model_name)
            _local_model = _load_local_whisper_model(model_name)
            _local_model_name = model_name

        # Language: config.yaml (stt.local.language) > env var > auto-detect.
        _forced_lang = (
            _load_stt_config().get("local", {}).get("language")
            or os.getenv(LOCAL_STT_LANGUAGE_ENV)
            or None
        )
        transcribe_kwargs = {"beam_size": 5}
        if _forced_lang:
            transcribe_kwargs["language"] = _forced_lang

        try:
            segments, info = _local_model.transcribe(file_path, **transcribe_kwargs)
            transcript = " ".join(segment.text.strip() for segment in segments)
        except Exception as exc:
            # CUDA runtime libs sometimes only fail at dlopen-on-first-use,
            # AFTER the model loaded successfully.  Evict the broken cached
            # model, reload on CPU, retry once.  Without this the module-
            # global `_local_model` is poisoned and every subsequent voice
            # message on this process fails identically until restart.
            if not _looks_like_cuda_lib_error(exc):
                raise
            logger.warning(
                "faster-whisper CUDA runtime failed mid-transcribe (%s) — "
                "evicting cached model and retrying on CPU (int8).",
                exc,
            )
            _local_model = None
            _local_model_name = None
            from faster_whisper import WhisperModel
            _local_model = WhisperModel(model_name, device="cpu", compute_type="int8")
            _local_model_name = model_name
            segments, info = _local_model.transcribe(file_path, **transcribe_kwargs)
            transcript = " ".join(segment.text.strip() for segment in segments)

        logger.info(
            "Transcribed %s via local whisper (%s, lang=%s, %.1fs audio)",
            Path(file_path).name, model_name, info.language, info.duration,
        )

        return {"success": True, "transcript": transcript, "provider": "local"}

    except Exception as e:
        logger.error("Local transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Local transcription failed: {e}"}


def _prepare_local_audio(file_path: str, work_dir: str) -> tuple[Optional[str], Optional[str]]:
    """Normalize audio for local CLI STT when needed."""
    audio_path = Path(file_path)
    if audio_path.suffix.lower() in LOCAL_NATIVE_AUDIO_FORMATS:
        return file_path, None

    ffmpeg = _find_ffmpeg_binary()
    if not ffmpeg:
        return None, "Local STT fallback requires ffmpeg for non-WAV inputs, but ffmpeg was not found"

    converted_path = os.path.join(work_dir, f"{audio_path.stem}.wav")
    command = [ffmpeg, "-y", "-i", file_path, converted_path]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        return converted_path, None
    except subprocess.CalledProcessError as e:
        details = e.stderr.strip() or e.stdout.strip() or str(e)
        logger.error("ffmpeg conversion failed for %s: %s", file_path, details)
        return None, f"Failed to convert audio for local STT: {details}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def transcribe_audio(file_path: str, model: Optional[str] = None) -> Dict[str, Any]:
    """
    Transcribe an audio file using the configured STT provider.

    The local faster-whisper backend is the only supported provider.

    Args:
        file_path: Absolute path to the audio file to transcribe.
        model:     Override the model. If None, uses config or provider default.

    Returns:
        dict with keys:
          - "success" (bool): Whether transcription succeeded
          - "transcript" (str): The transcribed text (empty on failure)
          - "error" (str, optional): Error message if success is False
          - "provider" (str, optional): Which provider was used
    """
    # Validate input
    error = _validate_audio_file(file_path)
    if error:
        return error

    # Load config and determine provider
    stt_config = _load_stt_config()
    if not is_stt_enabled(stt_config):
        return {
            "success": False,
            "transcript": "",
            "error": "STT is disabled in config.yaml (stt.enabled: false).",
        }

    provider = _get_provider(stt_config)

    if provider == "local":
        local_cfg = stt_config.get("local", {})
        model_name = _normalize_local_model(
            model or local_cfg.get("model", DEFAULT_LOCAL_MODEL)
        )
        return _transcribe_local(file_path, model_name)

    # No provider available
    return {
        "success": False,
        "transcript": "",
        "error": (
            "No STT provider available. Install faster-whisper for free local "
            "transcription (pip install faster-whisper)."
        ),
    }

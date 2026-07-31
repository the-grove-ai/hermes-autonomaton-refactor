#!/usr/bin/env python3
"""
Text-to-Speech Tool Module

Built-in TTS providers:
- Edge TTS (default, free, no API key): Microsoft Edge neural voices

Custom command providers:
- Users can declare any number of named providers with ``type: command``
  under ``tts.providers.<name>`` in ``~/.grove/config.yaml``. Hermes
  writes the input text to a temp file and runs the configured shell
  command, which must produce the audio file at the expected path.
  See the Local Command section of ``website/docs/user-guide/features/tts.md``.

Output formats:
- Opus (.ogg) for Telegram voice bubbles (requires ffmpeg for Edge TTS)
- MP3 (.mp3) for everything else (CLI, Discord, WhatsApp)

Configuration is loaded from ~/.grove/config.yaml under the 'tts:' key.
The user chooses the provider and voice; the model just sends text.

Usage:
    from tools.tts_tool import text_to_speech_tool, check_tts_requirements

    result = text_to_speech_tool(text="Hello world")
"""

import asyncio
import base64
import datetime
import json
import logging
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Callable, Dict, Any, Optional

from hermes_constants import display_hermes_home

logger = logging.getLogger(__name__)
def get_env_value(name, default=None):
    """Read env values through the live config module.

    Tests may monkeypatch and later restore ``hermes_cli.config.get_env_value``
    before this module is imported. Resolve the helper at call time so TTS does
    not keep a stale imported function for the rest of the test process.
    """
    try:
        from hermes_cli.config import get_env_value as _get_env_value
    except ImportError:
        return os.getenv(name, default)
    value = _get_env_value(name)
    return default if value is None else value
# ---------------------------------------------------------------------------
# Lazy imports -- providers are imported only when actually used to avoid
# crashing in headless environments (SSH, Docker, WSL, no PortAudio).
# ---------------------------------------------------------------------------

def _import_edge_tts():
    """Lazy import edge_tts. Returns the module or raises ImportError."""
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("tts.edge", prompt=False)
    except ImportError:
        pass
    except Exception as e:
        raise ImportError(str(e))
    import edge_tts
    return edge_tts

def _import_sounddevice():
    """Lazy import sounddevice. Returns the module or raises ImportError/OSError."""
    import sounddevice as sd
    return sd


# ===========================================================================
# Defaults
# ===========================================================================
DEFAULT_PROVIDER = "edge"
DEFAULT_EDGE_VOICE = "en-US-AriaNeural"

def _get_default_output_dir() -> str:
    from hermes_constants import get_hermes_dir
    return str(get_hermes_dir("cache/audio", "audio_cache"))

DEFAULT_OUTPUT_DIR = _get_default_output_dir()

# ---------------------------------------------------------------------------
# Per-provider input-character limits (from official provider docs).
# A single global cap was wrong: the built-in edge provider caps at ~5000
# chars.  Users can override any of these via
# ``tts.<provider>.max_text_length`` in config.yaml.
# ---------------------------------------------------------------------------
PROVIDER_MAX_TEXT_LENGTH: Dict[str, int] = {
    "edge": 5000,         # edge-tts practical sync limit
}

# Final fallback when provider isn't recognised at all.
FALLBACK_MAX_TEXT_LENGTH = 4000

# Back-compat alias. Prefer ``_resolve_max_text_length()`` for new code.
MAX_TEXT_LENGTH = FALLBACK_MAX_TEXT_LENGTH


def _resolve_max_text_length(
    provider: Optional[str],
    tts_config: Optional[Dict[str, Any]] = None,
) -> int:
    """Return the input-character cap for *provider*.

    Resolution order:
      1. ``tts.<provider>.max_text_length`` (user override in config.yaml)
      2. ``tts.providers.<provider>.max_text_length`` for user-declared
         command providers
      3. ``PROVIDER_MAX_TEXT_LENGTH`` default
      4. ``DEFAULT_COMMAND_TTS_MAX_TEXT_LENGTH`` when the provider is a
         command-type user provider without an explicit cap
      5. ``FALLBACK_MAX_TEXT_LENGTH`` (4000)

    Non-positive or non-integer overrides fall through to the default so a
    broken config can't accidentally disable truncation entirely.
    """
    if not provider:
        return FALLBACK_MAX_TEXT_LENGTH
    key = provider.lower().strip()
    cfg = tts_config or {}

    # Built-in-style override at tts.<provider>.max_text_length wins first,
    # matching historical behavior.
    prov_cfg = cfg.get(key) if isinstance(cfg.get(key), dict) else {}
    override = prov_cfg.get("max_text_length") if prov_cfg else None
    if isinstance(override, bool):
        override = None
    if isinstance(override, int) and override > 0:
        return override

    if key in PROVIDER_MAX_TEXT_LENGTH:
        return PROVIDER_MAX_TEXT_LENGTH[key]

    # User-declared command provider (under tts.providers.<name>)
    if key not in BUILTIN_TTS_PROVIDERS:
        named = _get_named_provider_config(cfg, key)
        if _is_command_provider_config(named):
            named_override = named.get("max_text_length")
            if isinstance(named_override, bool):
                named_override = None
            if isinstance(named_override, int) and named_override > 0:
                return named_override
            return DEFAULT_COMMAND_TTS_MAX_TEXT_LENGTH

    return FALLBACK_MAX_TEXT_LENGTH


# ===========================================================================
# Config loader -- reads tts: section from ~/.grove/config.yaml
# ===========================================================================
def _load_tts_config() -> Dict[str, Any]:
    """
    Load TTS configuration from ~/.grove/config.yaml.

    Returns a dict with provider settings. Falls back to defaults
    for any missing fields.
    """
    try:
        from hermes_cli.config import load_config
        config = load_config()
        return config.get("tts", {})
    except ImportError:
        logger.debug("hermes_cli.config not available, using default TTS config")
        return {}
    except Exception as e:
        logger.warning("Failed to load TTS config: %s", e, exc_info=True)
        return {}


def _get_provider(tts_config: Dict[str, Any]) -> str:
    """Get the configured TTS provider name."""
    return (tts_config.get("provider") or DEFAULT_PROVIDER).lower().strip()


# ===========================================================================
# Custom command providers (type: command under tts.providers.<name>)
# ===========================================================================
#
# Users can declare any number of command-type providers alongside the
# built-ins so they can plug any local CLI (Piper, VoxCPM, Kokoro CLIs,
# custom voice-cloning scripts, etc.) into Hermes without any Python code
# changes. The config shape is::
#
#     tts:
#       provider: piper-en
#       providers:
#         piper-en:
#           type: command
#           command: "piper -m ~/model.onnx -f {output_path} < {input_path}"
#           output_format: wav
#
# Hermes writes the input text to a temp UTF-8 file, runs the command with
# placeholder substitution, and reads the audio file the command wrote to
# ``{output_path}``. Supported placeholders: ``{input_path}``,
# ``{text_path}`` (alias for input_path), ``{output_path}``, ``{format}``,
# ``{voice}``, ``{model}``, ``{speed}``. Use ``{{`` / ``}}`` for literal braces.
#
# Built-in provider names always win over an entry with the same name under
# ``tts.providers``, so user config can't silently shadow ``edge`` etc.
#
# Placeholder values are shell-quoted for their surrounding context
# (bare / single / double quote), so paths with spaces work transparently.

# Built-in provider names. Any ``tts.provider`` value NOT in this set is
# interpreted as a reference to ``tts.providers.<name>``.
BUILTIN_TTS_PROVIDERS = frozenset({
    "edge",
})

DEFAULT_COMMAND_TTS_TIMEOUT_SECONDS = 120
DEFAULT_COMMAND_TTS_OUTPUT_FORMAT = "mp3"
COMMAND_TTS_OUTPUT_FORMATS = frozenset({"mp3", "wav", "ogg", "flac"})
DEFAULT_COMMAND_TTS_MAX_TEXT_LENGTH = 5000


def _get_provider_section(tts_config: Dict[str, Any], name: str) -> Dict[str, Any]:
    """Return a provider config block if it's a dict, else an empty dict."""
    if not isinstance(tts_config, dict):
        return {}
    section = tts_config.get(name)
    return section if isinstance(section, dict) else {}


def _get_named_provider_config(
    tts_config: Dict[str, Any],
    name: str,
) -> Dict[str, Any]:
    """Return the config dict for a user-declared provider.

    Looks up ``tts.providers.<name>`` first (the canonical location), and
    falls back to ``tts.<name>`` so users who followed the built-in layout
    still work. Returns an empty dict when the provider is not declared.
    """
    providers = _get_provider_section(tts_config, "providers")
    section = providers.get(name) if isinstance(providers, dict) else None
    if isinstance(section, dict):
        return section
    # Back-compat: allow ``tts.<name>`` for user-declared providers too,
    # but only when the name is not a built-in (so a user's ``tts.openai``
    # block still means the OpenAI provider, not a custom command).
    if name.lower() not in BUILTIN_TTS_PROVIDERS:
        legacy = _get_provider_section(tts_config, name)
        if legacy:
            return legacy
    return {}


def _is_command_provider_config(config: Dict[str, Any]) -> bool:
    """Return True when *config* declares a command-type provider."""
    if not isinstance(config, dict):
        return False
    ptype = str(config.get("type") or "").strip().lower()
    if ptype and ptype != "command":
        return False
    command = config.get("command")
    return isinstance(command, str) and bool(command.strip())


def _resolve_command_provider_config(
    provider: str,
    tts_config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Return the provider config if *provider* resolves to a command type.

    Built-in provider names are rejected (they have native handlers).
    Returns None when the name is a built-in, unknown, or not a command
    type.
    """
    if not provider:
        return None
    key = provider.lower().strip()
    if key in BUILTIN_TTS_PROVIDERS:
        return None
    config = _get_named_provider_config(tts_config, key)
    if _is_command_provider_config(config):
        return config
    return None


def _iter_command_providers(tts_config: Dict[str, Any]):
    """Yield (name, config) pairs for every declared command-type provider."""
    if not isinstance(tts_config, dict):
        return
    providers = _get_provider_section(tts_config, "providers")
    for name, cfg in (providers or {}).items():
        if isinstance(name, str) and name.lower() not in BUILTIN_TTS_PROVIDERS:
            if _is_command_provider_config(cfg):
                yield name, cfg


def _get_command_tts_timeout(config: Dict[str, Any]) -> float:
    """Return timeout in seconds, falling back when invalid."""
    raw = config.get("timeout", config.get("timeout_seconds", DEFAULT_COMMAND_TTS_TIMEOUT_SECONDS))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_COMMAND_TTS_TIMEOUT_SECONDS)
    if value <= 0:
        return float(DEFAULT_COMMAND_TTS_TIMEOUT_SECONDS)
    return value


def _get_command_tts_output_format(
    config: Dict[str, Any],
    output_path: Optional[str] = None,
) -> str:
    """Return the validated output format (mp3/wav/ogg/flac)."""
    if output_path:
        suffix = Path(output_path).suffix.lower().strip().lstrip(".")
        if suffix in COMMAND_TTS_OUTPUT_FORMATS:
            return suffix
    raw = (
        config.get("format")
        or config.get("output_format")
        or DEFAULT_COMMAND_TTS_OUTPUT_FORMAT
    )
    fmt = str(raw).lower().strip().lstrip(".")
    return fmt if fmt in COMMAND_TTS_OUTPUT_FORMATS else DEFAULT_COMMAND_TTS_OUTPUT_FORMAT


def _is_command_tts_voice_compatible(config: Dict[str, Any]) -> bool:
    """Return True only when the user explicitly opted in to voice delivery."""
    value = config.get("voice_compatible", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _shell_quote_context(command_template: str, position: int) -> Optional[str]:
    """Return the shell quote character active right before *position*.

    Returns ``"'"`` / ``'"'`` when inside a single- / double-quoted region
    of the template, ``None`` for bare context.
    """
    quote: Optional[str] = None
    escaped = False
    i = 0
    while i < position:
        char = command_template[i]
        if quote == "'":
            if char == "'":
                quote = None
        elif quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
        elif char == "'":
            quote = "'"
        elif char == '"':
            quote = '"'
        elif char == "\\":
            i += 1
        i += 1
    return quote


def _quote_command_tts_placeholder(value: str, quote_context: Optional[str]) -> str:
    """Quote a placeholder value for its position in a shell command template."""
    if quote_context == "'":
        return value.replace("'", r"'\''")
    if quote_context == '"':
        return (
            value
            .replace("\\", "\\\\")
            .replace('"', r'\"')
            .replace("$", r"\$")
            .replace("`", r"\`")
        )
    if os.name == "nt":
        return subprocess.list2cmdline([value])
    return shlex.quote(value)


def _render_command_tts_template(
    command_template: str,
    placeholders: Dict[str, str],
) -> str:
    """Replace supported placeholders while preserving ``{{`` / ``}}``."""
    names = "|".join(re.escape(name) for name in placeholders)
    pattern = re.compile(
        rf"(?<!\$)(?:\{{\{{(?P<double>{names})\}}\}}|\{{(?P<single>{names})\}})"
    )
    replacements: list[tuple[str, str]] = []

    def replace_match(match: re.Match[str]) -> str:
        name = match.group("double") or match.group("single")
        token = f"__GROVE_TTS_PLACEHOLDER_{len(replacements)}__"
        replacements.append((
            token,
            _quote_command_tts_placeholder(
                placeholders[name],
                _shell_quote_context(command_template, match.start()),
            ),
        ))
        return token

    rendered = pattern.sub(replace_match, command_template)
    rendered = rendered.replace("{{", "{").replace("}}", "}")
    for token, value in replacements:
        rendered = rendered.replace(token, value)
    return rendered


def _terminate_command_tts_process_tree(proc: subprocess.Popen) -> None:
    """Best-effort termination of a shell process and all of its children."""
    if proc.poll() is not None:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            proc.kill()
        return

    import psutil
    try:
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        parent.terminate()
    except psutil.NoSuchProcess:
        return
    except Exception:
        proc.terminate()

    try:
        proc.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
    except psutil.NoSuchProcess:
        return
    except Exception:
        proc.kill()


def _run_command_tts(command: str, timeout: float) -> subprocess.CompletedProcess:
    """Run a command-provider shell command with process-tree timeout cleanup."""
    popen_kwargs: Dict[str, Any] = {
        "shell": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(command, **popen_kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_command_tts_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=1)
        except Exception:
            stdout = getattr(exc, "output", None)
            stderr = getattr(exc, "stderr", None)
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=stdout,
            stderr=stderr,
        ) from exc

    if proc.returncode:
        raise subprocess.CalledProcessError(
            proc.returncode,
            command,
            output=stdout,
            stderr=stderr,
        )
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def _configured_command_tts_output_path(path: Path, config: Dict[str, Any]) -> Path:
    """Return an output path whose extension matches the provider's output_format."""
    fmt = _get_command_tts_output_format(config)
    return path.with_suffix(f".{fmt}")


def _generate_command_tts(
    text: str,
    output_path: str,
    provider_name: str,
    config: Dict[str, Any],
    tts_config: Dict[str, Any],
) -> str:
    """Generate speech by running a user-configured shell command.

    Returns the absolute path of the audio file the command wrote.
    Raises ``ValueError`` when the provider config is invalid, and
    ``RuntimeError`` for timeouts / non-zero exits / empty output.
    """
    command_template = str(config.get("command") or "").strip()
    if not command_template:
        raise ValueError(
            f"tts.providers.{provider_name}.command is not configured"
        )

    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    timeout = _get_command_tts_timeout(config)
    output_format = _get_command_tts_output_format(config, str(output))
    speed = config.get("speed", tts_config.get("speed", ""))

    with tempfile.TemporaryDirectory() as tmpdir:
        text_path = Path(tmpdir) / "input.txt"
        text_path.write_text(text, encoding="utf-8")

        placeholders = {
            "input_path": str(text_path),
            "text_path": str(text_path),
            "output_path": str(output),
            "format": output_format,
            "voice": str(config.get("voice", "")),
            "model": str(config.get("model", "")),
            "speed": str(speed),
        }
        command = _render_command_tts_template(command_template, placeholders)

        try:
            _run_command_tts(command, timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"TTS provider '{provider_name}' timed out after {timeout:g}s"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail_parts = []
            if exc.stderr:
                detail_parts.append(f"stderr: {exc.stderr.strip()}")
            if exc.stdout:
                detail_parts.append(f"stdout: {exc.stdout.strip()}")
            detail = "; ".join(detail_parts) or "no command output"
            raise RuntimeError(
                f"TTS provider '{provider_name}' exited with code "
                f"{exc.returncode}: {detail}"
            ) from exc

    if not output.exists() or output.stat().st_size <= 0:
        raise RuntimeError(
            f"TTS provider '{provider_name}' produced no output at {output}"
        )
    return str(output)


def _has_any_command_tts_provider(tts_config: Optional[Dict[str, Any]] = None) -> bool:
    """Return True when any command-type TTS provider is configured."""
    if tts_config is None:
        tts_config = _load_tts_config()
    for _name, _cfg in _iter_command_providers(tts_config):
        return True
    return False


# ===========================================================================
# ffmpeg Opus conversion (Edge TTS MP3 -> OGG Opus for Telegram)
# ===========================================================================
def _has_ffmpeg() -> bool:
    """Check if ffmpeg is available on the system."""
    return shutil.which("ffmpeg") is not None


def _convert_to_opus(mp3_path: str) -> Optional[str]:
    """
    Convert an MP3 file to OGG Opus format for Telegram voice bubbles.

    Args:
        mp3_path: Path to the input MP3 file.

    Returns:
        Path to the .ogg file, or None if conversion fails.
    """
    if not _has_ffmpeg():
        return None

    ogg_path = mp3_path.rsplit(".", 1)[0] + ".ogg"
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", mp3_path, "-acodec", "libopus",
             "-ac", "1", "-b:a", "64k", "-vbr", "off", ogg_path, "-y"],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            logger.warning("ffmpeg conversion failed with return code %d: %s", 
                          result.returncode, result.stderr.decode('utf-8', errors='ignore')[:200])
            return None
        if os.path.exists(ogg_path) and os.path.getsize(ogg_path) > 0:
            return ogg_path
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg OGG conversion timed out after 30s")
    except FileNotFoundError:
        logger.warning("ffmpeg not found in PATH")
    except Exception as e:
        logger.warning("ffmpeg OGG conversion failed: %s", e, exc_info=True)
    return None


# ===========================================================================
# Provider: Edge TTS (free)
# ===========================================================================
async def _generate_edge_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """
    Generate audio using Edge TTS.

    Args:
        text: Text to convert.
        output_path: Where to save the MP3 file.
        tts_config: TTS config dict.

    Returns:
        Path to the saved audio file.
    """
    _edge_tts = _import_edge_tts()
    edge_config = tts_config.get("edge", {})
    voice = edge_config.get("voice", DEFAULT_EDGE_VOICE)
    speed = float(edge_config.get("speed", tts_config.get("speed", 1.0)))

    kwargs = {"voice": voice}
    if speed != 1.0:
        pct = round((speed - 1.0) * 100)
        kwargs["rate"] = f"{pct:+d}%"

    communicate = _edge_tts.Communicate(text, **kwargs)
    await communicate.save(output_path)
    return output_path


# ===========================================================================
# Main tool function
# ===========================================================================
def text_to_speech_tool(
    text: str,
    output_path: Optional[str] = None,
) -> str:
    """
    Convert text to speech audio.

    Reads provider/voice config from ~/.grove/config.yaml (tts: section).
    The model sends text; the user configures voice and provider.

    On messaging platforms, the returned MEDIA:<path> tag is intercepted
    by the send pipeline and delivered as a native voice message.
    In CLI mode, the file is saved to ~/voice-memos/.

    Args:
        text: The text to convert to speech.
        output_path: Optional custom save path. Defaults to ~/voice-memos/<timestamp>.mp3

    Returns:
        str: JSON result with success, file_path, and optionally MEDIA tag.
    """
    if not text or not text.strip():
        return tool_error("Text is required", success=False)

    tts_config = _load_tts_config()
    provider = _get_provider(tts_config)

    # User-declared command provider (type: command under tts.providers.<name>)
    # resolves BEFORE the built-in dispatch. The built-in ``edge`` name
    # short-circuits here so a user's ``tts.providers.edge.command`` can't
    # override the real Edge handler.
    command_provider_config = _resolve_command_provider_config(provider, tts_config)

    # Truncate very long text with a warning. The cap is per-provider
    # (Edge TTS ~5000 chars; command providers via their own max_text_length).
    max_len = _resolve_max_text_length(provider, tts_config)
    if len(text) > max_len:
        logger.warning(
            "TTS text too long for provider %s (%d chars), truncating to %d",
            provider, len(text), max_len,
        )
        text = text[:max_len]

    # Determine output path
    if output_path:
        file_path = Path(output_path).expanduser()
        if command_provider_config is not None:
            # Respect caller-supplied path but align the extension with the
            # provider's configured output_format so the command writes to a
            # path the caller actually expects.
            file_path = _configured_command_tts_output_path(
                file_path, command_provider_config
            )
    else:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(DEFAULT_OUTPUT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        if command_provider_config is not None:
            fmt = _get_command_tts_output_format(command_provider_config)
            file_path = out_dir / f"tts_{timestamp}.{fmt}"
        else:
            # Edge TTS always outputs MP3; ffmpeg converts to Opus (.ogg)
            # downstream when a Telegram voice bubble is needed.
            file_path = out_dir / f"tts_{timestamp}.mp3"

    # Ensure parent directory exists
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_str = str(file_path)

    try:
        # Generate audio with the configured provider
        if command_provider_config is not None:
            logger.info(
                "Generating speech with command TTS provider '%s'...", provider,
            )
            file_str = _generate_command_tts(
                text, file_str, provider, command_provider_config, tts_config,
            )

        else:
            # Default: Edge TTS (free). The only built-in provider.
            try:
                _import_edge_tts()
            except ImportError:
                return json.dumps({
                    "success": False,
                    "error": "No TTS provider available. Install edge-tts (pip install edge-tts)."
                }, ensure_ascii=False)

            logger.info("Generating speech with Edge TTS...")
            try:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(
                        lambda: asyncio.run(_generate_edge_tts(text, file_str, tts_config))
                    ).result(timeout=60)
            except RuntimeError:
                asyncio.run(_generate_edge_tts(text, file_str, tts_config))

        # Check the file was actually created
        if not os.path.exists(file_str) or os.path.getsize(file_str) == 0:
            return json.dumps({
                "success": False,
                "error": f"TTS generation produced no output (provider: {provider})"
            }, ensure_ascii=False)

        # Try Opus conversion for Telegram compatibility.
        # Edge TTS outputs MP3 and needs ffmpeg conversion for voice bubbles.
        voice_compatible = False
        if command_provider_config is not None:
            # Command providers are documents by default. Voice-bubble
            # delivery only kicks in when the user explicitly opts in
            # via ``voice_compatible: true`` in their provider config.
            if _is_command_tts_voice_compatible(command_provider_config):
                if not file_str.endswith(".ogg"):
                    opus_path = _convert_to_opus(file_str)
                    if opus_path:
                        file_str = opus_path
                voice_compatible = file_str.endswith(".ogg")
        elif provider == "edge" and not file_str.endswith(".ogg"):
            opus_path = _convert_to_opus(file_str)
            if opus_path:
                file_str = opus_path
                voice_compatible = True

        file_size = os.path.getsize(file_str)
        logger.info("TTS audio saved: %s (%s bytes, provider: %s)", file_str, f"{file_size:,}", provider)

        # Build response with MEDIA tag for platform delivery
        media_tag = f"MEDIA:{file_str}"
        if voice_compatible:
            media_tag = f"[[audio_as_voice]]\n{media_tag}"

        return json.dumps({
            "success": True,
            "file_path": file_str,
            "media_tag": media_tag,
            "provider": provider,
            "voice_compatible": voice_compatible,
        }, ensure_ascii=False)

    except ValueError as e:
        # Configuration errors (missing API keys, etc.)
        error_msg = f"TTS configuration error ({provider}): {e}"
        logger.error("%s", error_msg)
        return tool_error(error_msg, success=False)
    except FileNotFoundError as e:
        # Missing dependencies or files
        error_msg = f"TTS dependency missing ({provider}): {e}"
        logger.error("%s", error_msg, exc_info=True)
        return tool_error(error_msg, success=False)
    except Exception as e:
        # Unexpected errors
        error_msg = f"TTS generation failed ({provider}): {e}"
        logger.error("%s", error_msg, exc_info=True)
        return tool_error(error_msg, success=False)


# ===========================================================================
# Requirements check
# ===========================================================================
def check_tts_requirements() -> bool:
    """
    Check if at least one TTS provider is available.

    Edge TTS needs no API key and is the default, so if the package
    is installed, TTS is available. A user-declared command provider
    also satisfies the requirement.

    Returns:
        bool: True if at least one provider can work.
    """
    # Any configured command provider counts as available.
    if _has_any_command_tts_provider():
        return True
    try:
        _import_edge_tts()
        return True
    except ImportError:
        pass
    return False


# ===========================================================================
# Markdown stripping for spoken text (used by the text_to_speech path)
# ===========================================================================
# Markdown stripping patterns (same as cli.py _voice_speak_response)
_MD_CODE_BLOCK = re.compile(r'```[\s\S]*?```')
_MD_LINK = re.compile(r'\[([^\]]+)\]\([^)]+\)')
_MD_URL = re.compile(r'https?://\S+')
_MD_BOLD = re.compile(r'\*\*(.+?)\*\*')
_MD_ITALIC = re.compile(r'\*(.+?)\*')
_MD_INLINE_CODE = re.compile(r'`(.+?)`')
_MD_HEADER = re.compile(r'^#+\s*', flags=re.MULTILINE)
_MD_LIST_ITEM = re.compile(r'^\s*[-*]\s+', flags=re.MULTILINE)
_MD_HR = re.compile(r'---+')
_MD_EXCESS_NL = re.compile(r'\n{3,}')


def _strip_markdown_for_tts(text: str) -> str:
    """Remove markdown formatting that shouldn't be spoken aloud."""
    text = _MD_CODE_BLOCK.sub(' ', text)
    text = _MD_LINK.sub(r'\1', text)
    text = _MD_URL.sub('', text)
    text = _MD_BOLD.sub(r'\1', text)
    text = _MD_ITALIC.sub(r'\1', text)
    text = _MD_INLINE_CODE.sub(r'\1', text)
    text = _MD_HEADER.sub('', text)
    text = _MD_LIST_ITEM.sub('', text)
    text = _MD_HR.sub('', text)
    text = _MD_EXCESS_NL.sub('\n\n', text)
    return text.strip()


# ===========================================================================
# Main -- quick diagnostics
# ===========================================================================
if __name__ == "__main__":
    print("🔊 Text-to-Speech Tool Module")
    print("=" * 50)

    def _check(importer, label):
        try:
            importer()
            return True
        except ImportError:
            return False

    print("\nProvider availability:")
    print(f"  Edge TTS:   {'installed' if _check(_import_edge_tts, 'edge') else 'not installed (pip install edge-tts)'}")
    print(f"  ffmpeg:     {'✅ found' if _has_ffmpeg() else '❌ not found (needed for Telegram Opus)'}")
    print(f"\n  Output dir: {DEFAULT_OUTPUT_DIR}")

    config = _load_tts_config()
    provider = _get_provider(config)
    print(f"  Configured provider: {provider}")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
from tools.registry import tool_error
TTS_SCHEMA = {
    "name": "text_to_speech",
    "description": "Convert text to speech audio. Returns a MEDIA: path that the platform delivers as native audio. Compatible providers render as a voice bubble on Telegram; otherwise audio is sent as a regular attachment. In CLI mode, saves to ~/voice-memos/. Voice and provider are user-configured (the built-in edge provider or custom command providers under tts.providers.<name>), not model-selected.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to convert to speech. A per-provider character cap applies and is enforced automatically (Edge TTS ~5000 chars); over-long input is truncated."
            },
            "output_path": {
                "type": "string",
                "description": f"Optional custom file path to save the audio. Defaults to {display_hermes_home()}/audio_cache/<timestamp>.mp3"
            }
        },
        "required": ["text"]
    }
}

def register(reg):
    """Sprint 53 — Dispatcher-driven registration entrypoint."""
    reg.register(
        name="text_to_speech",
        toolset="tts",
        schema=TTS_SCHEMA,
        handler=lambda args, **kw: text_to_speech_tool(
            text=args.get("text", ""),
            output_path=args.get("output_path")),
        check_fn=check_tts_requirements,
        emoji="🔊",
    )

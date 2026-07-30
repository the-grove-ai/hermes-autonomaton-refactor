"""Send Message Tool -- cross-channel messaging via platform APIs.

Sends a message to a user or channel on a connected messaging platform
(Telegram is the built-in target; other live platforms route through their
gateway plugin adapter). Supports listing available targets and resolving
human-friendly channel names to IDs. Works in both CLI and gateway contexts.
"""

import asyncio
import json
import logging
import os
import re
import ssl
import time
from email.utils import formatdate
from typing import Dict, Optional

from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)

_TELEGRAM_TOPIC_TARGET_RE = re.compile(r"^\s*(-?\d+)(?::(\d+))?\s*$")
_FEISHU_TARGET_RE = re.compile(r"^\s*((?:oc|ou|on|chat|open)_[-A-Za-z0-9]+)(?::([-A-Za-z0-9_]+))?\s*$")
# Slack conversation IDs: C (public channel), G (private/group channel), D (DM).
# Must be uppercase alphanumeric, 9+ chars. User IDs (U...) and workspace IDs
# (W...) are NOT valid chat.postMessage channel values — posting to them fails
# because the API requires a conversation ID. To DM a user you must first call
# conversations.open to obtain a D... ID. Without this gate, Slack IDs fall
# through to channel-name resolution, which only matches by name and fails.
_SLACK_TARGET_RE = re.compile(r"^\s*([CGD][A-Z0-9]{8,})\s*$")
_WEIXIN_TARGET_RE = re.compile(r"^\s*((?:wxid|gh|v\d+|wm|wb)_[A-Za-z0-9_-]+|[A-Za-z0-9._-]+@chatroom|filehelper)\s*$")
_YUANBAO_TARGET_RE = re.compile(r"^\s*((?:group|direct):[^:]+)\s*$")
# Discord snowflake IDs are numeric, same regex pattern as Telegram topic targets.
_NUMERIC_TOPIC_RE = _TELEGRAM_TOPIC_TARGET_RE
# Platforms that address recipients by phone number and accept E.164 format
# (with a leading '+'). Without this, "+15551234567" fails the isdigit() check
# below and falls through to channel-name resolution, which has no way to
# resolve a raw phone number. Keeping the '+' preserves the E.164 form that
# downstream adapters (signal, etc.) expect.
_PHONE_PLATFORMS = frozenset({"signal", "sms", "whatsapp"})
_E164_TARGET_RE = re.compile(r"^\s*\+(\d{7,15})\s*$")
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".3gp"}
_AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac"}
_VOICE_EXTS = {".ogg", ".opus"}
# Telegram's Bot API sendAudio only accepts MP3 / M4A. Other audio
# formats either route through sendVoice (Opus/OGG) or fall back to
# document delivery.
_TELEGRAM_SEND_AUDIO_EXTS = {".mp3", ".m4a"}
_URL_SECRET_QUERY_RE = re.compile(
    r"([?&](?:access_token|api[_-]?key|auth[_-]?token|token|signature|sig)=)([^&#\s]+)",
    re.IGNORECASE,
)
_GENERIC_SECRET_ASSIGN_RE = re.compile(
    r"\b(access_token|api[_-]?key|auth[_-]?token|signature|sig)\s*=\s*([^\s,;]+)",
    re.IGNORECASE,
)


def _sanitize_error_text(text) -> str:
    """Redact secrets from error text before surfacing it to users/models."""
    redacted = redact_sensitive_text(text)
    redacted = _URL_SECRET_QUERY_RE.sub(lambda m: f"{m.group(1)}***", redacted)
    redacted = _GENERIC_SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=***", redacted)
    return redacted


def _error(message: str) -> dict:
    """Build a standardized error payload with redacted content."""
    return {"error": _sanitize_error_text(message)}


def _telegram_retry_delay(exc: Exception, attempt: int) -> float | None:
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        try:
            return max(float(retry_after), 0.0)
        except (TypeError, ValueError):
            return 1.0

    text = str(exc).lower()
    if "timed out" in text or "timeout" in text:
        return None
    if (
        "bad gateway" in text
        or "502" in text
        or "too many requests" in text
        or "429" in text
        or "service unavailable" in text
        or "503" in text
        or "gateway timeout" in text
        or "504" in text
    ):
        return float(2 ** attempt)
    return None


async def _send_telegram_message_with_retry(bot, *, attempts: int = 3, **kwargs):
    for attempt in range(attempts):
        try:
            return await bot.send_message(**kwargs)
        except Exception as exc:
            delay = _telegram_retry_delay(exc, attempt)
            if delay is None or attempt >= attempts - 1:
                raise
            logger.warning(
                "Transient Telegram send failure (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1,
                attempts,
                delay,
                _sanitize_error_text(exc),
            )
            await asyncio.sleep(delay)


SEND_MESSAGE_SCHEMA = {
    "name": "send_message",
    "description": (
        "Send a message to a connected messaging platform, or list available targets.\n\n"
        "IMPORTANT: When the user asks to send to a specific channel or person "
        "(not just a bare platform name), call send_message(action='list') FIRST to see "
        "available targets, then send to the correct one.\n"
        "If the user just says a platform name like 'send to telegram', send directly "
        "to the home channel without listing first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "list"],
                "description": "Action to perform. 'send' (default) sends a message. 'list' returns all available channels/contacts across connected platforms."
            },
            "target": {
                "type": "string",
                "description": "Delivery target. Format: 'platform' (uses home channel), 'platform:#channel-name', 'platform:chat_id', or 'platform:chat_id:thread_id' for Telegram topics. Examples: 'telegram', 'telegram:-1001234567890:17585', 'telegram:#bot-home'. Other live platforms are addressed by their gateway plugin adapter's target format."
            },
            "message": {
                "type": "string",
                "description": "The message text to send. To send an image or file, include MEDIA:<local_path> (e.g. 'MEDIA:/tmp/hermes/cache/img_xxx.jpg') in the message — the platform will deliver it as a native media attachment."
            }
        },
        "required": []
    }
}


def send_message_tool(args, **kw):
    """Handle cross-channel send_message tool calls."""
    action = args.get("action", "send")

    if action == "list":
        return _handle_list()

    return _handle_send(args)


def _handle_list():
    """Return formatted list of available messaging targets."""
    try:
        from gateway.channel_directory import format_directory_for_display
        return json.dumps({"targets": format_directory_for_display()})
    except Exception as e:
        return json.dumps(_error(f"Failed to load channel directory: {e}"))


def _handle_send(args):
    """Send a message to a platform target."""
    target = args.get("target", "")
    message = args.get("message", "")
    if not target or not message:
        return tool_error("Both 'target' and 'message' are required when action='send'")

    parts = target.split(":", 1)
    platform_name = parts[0].strip().lower()
    target_ref = parts[1].strip() if len(parts) > 1 else None
    chat_id = None
    thread_id = None

    if target_ref:
        chat_id, thread_id, is_explicit = _parse_target_ref(platform_name, target_ref)
    else:
        is_explicit = False

    # Resolve human-friendly channel names to numeric IDs
    if target_ref and not is_explicit:
        try:
            from gateway.channel_directory import resolve_channel_name
            resolved = resolve_channel_name(platform_name, target_ref)
            if resolved:
                chat_id, thread_id, _ = _parse_target_ref(platform_name, resolved)
            else:
                return json.dumps({
                    "error": f"Could not resolve '{target_ref}' on {platform_name}. "
                    f"Use send_message(action='list') to see available targets."
                })
        except Exception:
            return json.dumps({
                "error": f"Could not resolve '{target_ref}' on {platform_name}. "
                f"Try using a numeric channel ID instead."
            })

    from tools.interrupt import is_interrupted
    if is_interrupted():
        return tool_error("Interrupted")

    try:
        from gateway.config import load_gateway_config, Platform
        config = load_gateway_config()
    except Exception as e:
        return json.dumps(_error(f"Failed to load gateway config: {e}"))

    # Accept any platform name — built-in names resolve to their enum
    # member, plugin platform names create dynamic members via _missing_().
    try:
        platform = Platform(platform_name)
    except (ValueError, KeyError):
        return tool_error(f"Unknown platform: {platform_name}")

    pconfig = config.platforms.get(platform)
    if not pconfig or not pconfig.enabled:
        # Weixin can be configured purely via .env; synthesize a pconfig so
        # send_message and cron delivery work without a gateway.yaml entry.
        if platform_name == "weixin":
            wx_token = os.getenv("WEIXIN_TOKEN", "").strip()
            wx_account = os.getenv("WEIXIN_ACCOUNT_ID", "").strip()
            if wx_token and wx_account:
                from gateway.config import PlatformConfig
                pconfig = PlatformConfig(
                    enabled=True,
                    token=wx_token,
                    extra={
                        "account_id": wx_account,
                        "base_url": os.getenv("WEIXIN_BASE_URL", "").strip(),
                        "cdn_base_url": os.getenv("WEIXIN_CDN_BASE_URL", "").strip(),
                    },
                )
            else:
                return tool_error(f"Platform '{platform_name}' is not configured. Set up credentials in ~/.grove/config.yaml or environment variables.")
        else:
            return tool_error(f"Platform '{platform_name}' is not configured. Set up credentials in ~/.grove/config.yaml or environment variables.")

    from gateway.platforms.base import BasePlatformAdapter

    # Capture [[as_document]] directive before extract_media strips it.
    # Image-extension files in this batch will route through send_document
    # instead of send_photo so the original bytes survive (e.g. info-graph
    # JPGs where Telegram's sendPhoto recompresses to 1280px).
    force_document_attachments = "[[as_document]]" in message

    media_files, cleaned_message = BasePlatformAdapter.extract_media(message)
    mirror_text = cleaned_message.strip() or _describe_media_for_mirror(media_files)

    used_home_channel = False
    if not chat_id:
        home = config.get_home_channel(platform)
        if not home and platform_name == "weixin":
            wx_home = os.getenv("WEIXIN_HOME_CHANNEL", "").strip()
            if wx_home:
                from gateway.config import HomeChannel
                home = HomeChannel(platform=platform, chat_id=wx_home, name="Weixin Home")
        if home:
            chat_id = home.chat_id
            used_home_channel = True
        else:
            return json.dumps({
                "error": f"No home channel set for {platform_name} to determine where to send the message. "
                f"Either specify a channel directly with '{platform_name}:CHANNEL_NAME', "
                f"or set a home channel via: hermes config set {platform_name.upper()}_HOME_CHANNEL <channel_id>"
            })

    duplicate_skip = _maybe_skip_cron_duplicate_send(platform_name, chat_id, thread_id)
    if duplicate_skip:
        return json.dumps(duplicate_skip)

    try:
        from model_tools import _run_async
        result = _run_async(
            _send_to_platform(
                platform,
                pconfig,
                chat_id,
                cleaned_message,
                thread_id=thread_id,
                media_files=media_files,
                force_document=force_document_attachments,
            )
        )
        if used_home_channel and isinstance(result, dict) and result.get("success"):
            result["note"] = f"Sent to {platform_name} home channel (chat_id: {chat_id})"

        # Mirror the sent message into the target's gateway session
        if isinstance(result, dict) and result.get("success") and mirror_text:
            try:
                from gateway.mirror import mirror_to_session
                from gateway.session_context import get_session_env
                source_label = get_session_env("GROVE_SESSION_PLATFORM", "cli")
                user_id = get_session_env("GROVE_SESSION_USER_ID", "") or None
                if mirror_to_session(
                    platform_name,
                    chat_id,
                    mirror_text,
                    source_label=source_label,
                    thread_id=thread_id,
                    user_id=user_id,
                ):
                    result["mirrored"] = True
            except Exception:
                pass

        if isinstance(result, dict) and "error" in result:
            result["error"] = _sanitize_error_text(result["error"])
        return json.dumps(result)
    except Exception as e:
        return json.dumps(_error(f"Send failed: {e}"))


def _parse_target_ref(platform_name: str, target_ref: str):
    """Parse a tool target into chat_id/thread_id and whether it is explicit."""
    if platform_name == "telegram":
        match = _TELEGRAM_TOPIC_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), match.group(2), True
    if platform_name == "feishu":
        match = _FEISHU_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), match.group(2), True
    if platform_name == "discord":
        match = _NUMERIC_TOPIC_RE.fullmatch(target_ref)
        if match:
            return match.group(1), match.group(2), True
    if platform_name == "slack":
        match = _SLACK_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), None, True
    if platform_name == "weixin":
        match = _WEIXIN_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), None, True
    if platform_name == "yuanbao":
        match = _YUANBAO_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), None, True
        if target_ref.strip().isdigit():
            return f"group:{target_ref.strip()}", None, True
        return None, None, False
    if platform_name in _PHONE_PLATFORMS:
        match = _E164_TARGET_RE.fullmatch(target_ref)
        if match:
            # Preserve the leading '+' — signal-cli and sms/whatsapp adapters
            # expect E.164 format for direct recipients.
            return target_ref.strip(), None, True
    if target_ref.lstrip("-").isdigit():
        return target_ref, None, True
    # Matrix room IDs (start with !) and user IDs (start with @) are explicit
    if platform_name == "matrix" and (target_ref.startswith("!") or target_ref.startswith("@")):
        return target_ref, None, True
    # XMPP JIDs (user@server or room@conference.server) are explicit
    if platform_name == "xmpp" and "@" in target_ref:
        return target_ref, None, True
    return None, None, False


def _describe_media_for_mirror(media_files):
    """Return a human-readable mirror summary when a message only contains media."""
    if not media_files:
        return ""
    if len(media_files) == 1:
        media_path, is_voice = media_files[0]
        ext = os.path.splitext(media_path)[1].lower()
        if is_voice and ext in _VOICE_EXTS:
            return "[Sent voice message]"
        if ext in _IMAGE_EXTS:
            return "[Sent image attachment]"
        if ext in _VIDEO_EXTS:
            return "[Sent video attachment]"
        if ext in _AUDIO_EXTS:
            return "[Sent audio attachment]"
        return "[Sent document attachment]"
    return f"[Sent {len(media_files)} media attachments]"


def _get_cron_auto_delivery_target():
    """Return the cron scheduler's auto-delivery target for the current run, if any."""
    from gateway.session_context import get_session_env
    platform = get_session_env("GROVE_CRON_AUTO_DELIVER_PLATFORM", "").strip().lower()
    chat_id = get_session_env("GROVE_CRON_AUTO_DELIVER_CHAT_ID", "").strip()
    if not platform or not chat_id:
        return None
    thread_id = get_session_env("GROVE_CRON_AUTO_DELIVER_THREAD_ID", "").strip() or None
    return {
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": thread_id,
    }


def _maybe_skip_cron_duplicate_send(platform_name: str, chat_id: str, thread_id: str | None):
    """Skip redundant cron send_message calls when the scheduler will auto-deliver there."""
    auto_target = _get_cron_auto_delivery_target()
    if not auto_target:
        return None

    same_target = (
        auto_target["platform"] == platform_name
        and str(auto_target["chat_id"]) == str(chat_id)
        and auto_target.get("thread_id") == thread_id
    )
    if not same_target:
        return None

    target_label = f"{platform_name}:{chat_id}"
    if thread_id is not None:
        target_label += f":{thread_id}"

    return {
        "success": True,
        "skipped": True,
        "reason": "cron_auto_delivery_duplicate_target",
        "target": target_label,
        "note": (
            f"Skipped send_message to {target_label}. This cron job will already auto-deliver "
            "its final response to that same target. Put the intended user-facing content in "
            "your final response instead, or use a different target if you want an additional message."
        ),
    }


async def _send_via_adapter(
    platform,
    pconfig,
    chat_id,
    chunk,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Send a message via a live gateway adapter, with a standalone fallback
    for out-of-process callers (e.g. cron running separately from the gateway).

    Order of attempts:
      1. Live in-process adapter via ``_gateway_runner_ref()`` (the path that
         existed before this change).
      2. The plugin's ``standalone_sender_fn`` registered on its
         ``PlatformEntry`` (used when the gateway is not in this process, so
         the runner weakref is ``None``).
      3. A descriptive error explaining both options.
    """
    runner = None
    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
    except Exception:
        runner = None

    if runner is not None:
        try:
            adapter = runner.adapters.get(platform)
        except Exception:
            adapter = None
        if adapter is not None:
            try:
                metadata = {"thread_id": thread_id} if thread_id else None
                result = await adapter.send(chat_id=chat_id, content=chunk, metadata=metadata)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return {"error": f"Plugin platform send failed: {e}"}
            if result.success:
                return {"success": True, "message_id": result.message_id}
            return {"error": f"Adapter send failed: {result.error}"}

    platform_name = platform.value if hasattr(platform, "value") else str(platform)
    entry = None
    try:
        from gateway.platform_registry import platform_registry
        entry = platform_registry.get(platform_name)
    except Exception:
        entry = None

    if entry is not None and entry.standalone_sender_fn is not None:
        try:
            result = await entry.standalone_sender_fn(
                pconfig,
                chat_id,
                chunk,
                thread_id=thread_id,
                media_files=media_files,
                force_document=force_document,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Plugin standalone send for %s raised", platform_name, exc_info=True)
            return {"error": f"Plugin standalone send failed: {e}"}

        if isinstance(result, dict) and (result.get("success") or result.get("error")):
            return result
        return {
            "error": (
                f"Plugin standalone send for '{platform_name}' returned an "
                f"invalid result: expected a dict with 'success' or 'error' "
                f"keys, got {type(result).__name__}"
            )
        }

    return {
        "error": (
            f"No live adapter for platform '{platform_name}'. Is the gateway "
            f"running with this platform connected? For out-of-process delivery "
            f"(e.g. cron in a separate process), the platform plugin must "
            f"register a standalone_sender_fn on its PlatformEntry."
        )
    }


async def _send_to_platform(platform, pconfig, chat_id, message, thread_id=None, media_files=None, force_document=False):
    """Route a message to the appropriate platform sender.

    Long messages are automatically chunked to fit within platform limits
    using the same smart-splitting algorithm as the gateway adapters
    (preserves code-block boundaries, adds part indicators).
    """
    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter, utf16_len

    # hermes-severance-v1 T2: telegram is the only built-in send target after
    # the adapter severance. Its import is optional (requires
    # python-telegram-bot). Every other live platform (plugin adapters) routes
    # through the generic gateway-adapter path (_send_via_adapter).
    try:
        from gateway.platforms.telegram import TelegramAdapter
        _telegram_available = True
    except ImportError:
        _telegram_available = False

    media_files = media_files or []

    # Platform message length limits (from adapter class attributes)
    _MAX_LENGTHS = {
        Platform.TELEGRAM: TelegramAdapter.MAX_MESSAGE_LENGTH if _telegram_available else 4096,
    }

    # Check plugin registry for max_message_length
    if platform not in _MAX_LENGTHS:
        try:
            from gateway.platform_registry import platform_registry
            entry = platform_registry.get(platform.value)
            if entry and entry.max_message_length > 0:
                _MAX_LENGTHS[platform] = entry.max_message_length
        except Exception:
            pass

    # Smart-chunk the message to fit within platform limits.
    # For short messages or platforms without a known limit this is a no-op.
    # Telegram measures length in UTF-16 code units, not Unicode codepoints.
    max_len = _MAX_LENGTHS.get(platform)
    if max_len:
        _len_fn = utf16_len if platform == Platform.TELEGRAM else None
        chunks = BasePlatformAdapter.truncate_message(message, max_len, len_fn=_len_fn)
    else:
        chunks = [message]

    # --- Telegram: special handling for media attachments ---
    if platform == Platform.TELEGRAM:
        last_result = None
        disable_link_previews = bool(getattr(pconfig, "extra", {}) and pconfig.extra.get("disable_link_previews"))
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            result = await _send_telegram(
                pconfig.token,
                chat_id,
                chunk,
                media_files=media_files if is_last else [],
                thread_id=thread_id,
                disable_link_previews=disable_link_previews,
                force_document=force_document,
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            last_result = result
        return last_result

    # --- Non-telegram: route through the gateway's live adapter (plugin
    #     platforms). Built-in media delivery is telegram-only post-severance. ---
    if media_files and not message.strip():
        return {
            "error": (
                f"send_message MEDIA delivery is currently only supported for telegram; "
                f"target {platform.value} had only media attachments"
            )
        }
    warning = None
    if media_files:
        warning = (
            f"MEDIA attachments were omitted for {platform.value}; "
            "native send_message media delivery is currently only supported for telegram"
        )

    last_result = None
    for chunk in chunks:
        # Plugin platform: route through the gateway's live adapter if
        # available, otherwise the plugin's standalone_sender_fn.
        result = await _send_via_adapter(
            platform,
            pconfig,
            chat_id,
            chunk,
            thread_id=thread_id,
            media_files=media_files,
            force_document=force_document,
        )
        if isinstance(result, dict) and result.get("error"):
            return result
        last_result = result

    if warning and isinstance(last_result, dict) and last_result.get("success"):
        warnings = list(last_result.get("warnings", []))
        warnings.append(warning)
        last_result["warnings"] = warnings
    return last_result


async def _send_telegram(token, chat_id, message, media_files=None, thread_id=None, disable_link_previews=False, force_document=False):
    """Send via Telegram Bot API (one-shot, no polling needed).

    Applies markdown→MarkdownV2 formatting (same as the gateway adapter)
    so that bold, links, and headers render correctly.  If the message
    already contains HTML tags, it is sent with ``parse_mode='HTML'``
    instead, bypassing MarkdownV2 conversion.
    """
    try:
        from telegram import Bot
        from telegram.constants import ParseMode

        # Auto-detect HTML tags — if present, skip MarkdownV2 and send as HTML.
        # Inspired by github.com/ashaney — PR #1568.
        _has_html = bool(re.search(r'<[a-zA-Z/][^>]*>', message))

        if _has_html:
            formatted = message
            send_parse_mode = ParseMode.HTML
        else:
            # Reuse the gateway adapter's format_message for markdown→MarkdownV2
            try:
                from gateway.platforms.telegram import TelegramAdapter
                _adapter = TelegramAdapter.__new__(TelegramAdapter)
                formatted = _adapter.format_message(message)
            except Exception:
                # Fallback: send as-is if formatting unavailable
                formatted = message
            send_parse_mode = ParseMode.MARKDOWN_V2

        bot = Bot(token=token)
        int_chat_id = int(chat_id)
        media_files = media_files or []
        thread_kwargs = {}
        if thread_id is not None:
            # Reuse the gateway adapter's General-topic mapping: in Telegram
            # forum supergroups, the General topic is addressed as
            # message_thread_id="1" on incoming updates, but Bot API
            # sendMessage rejects message_thread_id=1 with "Message thread
            # not found". The adapter's helper maps "1" to None for that
            # reason; the send_message tool needs the same mapping or a
            # send to a forum group's General topic always errors out
            # (see issue #22267).
            try:
                from gateway.platforms.telegram import TelegramAdapter
                effective_thread_id = TelegramAdapter._message_thread_id_for_send(
                    str(thread_id)
                )
            except Exception:
                # Fallback: explicit mapping in case the adapter import
                # fails (e.g. python-telegram-bot missing in this venv).
                effective_thread_id = (
                    None if str(thread_id) == "1" else int(thread_id)
                )
            if effective_thread_id is not None:
                thread_kwargs["message_thread_id"] = effective_thread_id
        if disable_link_previews:
            thread_kwargs["disable_web_page_preview"] = True

        last_msg = None
        warnings = []

        if formatted.strip():
            try:
                last_msg = await _send_telegram_message_with_retry(
                    bot,
                    chat_id=int_chat_id, text=formatted,
                    parse_mode=send_parse_mode, **thread_kwargs
                )
            except Exception as md_error:
                # Parse failed, fall back to plain text
                if "parse" in str(md_error).lower() or "markdown" in str(md_error).lower() or "html" in str(md_error).lower():
                    logger.warning(
                        "Parse mode %s failed in _send_telegram, falling back to plain text: %s",
                        send_parse_mode,
                        _sanitize_error_text(md_error),
                    )
                    if not _has_html:
                        try:
                            from gateway.platforms.telegram import _strip_mdv2
                            plain = _strip_mdv2(formatted)
                        except Exception:
                            plain = message
                    else:
                        plain = message
                    last_msg = await _send_telegram_message_with_retry(
                        bot,
                        chat_id=int_chat_id, text=plain,
                        parse_mode=None, **thread_kwargs
                    )
                else:
                    raise

        for media_path, is_voice in media_files:
            if not os.path.exists(media_path):
                warning = f"Media file not found, skipping: {media_path}"
                logger.warning(warning)
                warnings.append(warning)
                continue

            ext = os.path.splitext(media_path)[1].lower()
            try:
                with open(media_path, "rb") as f:
                    if ext in _IMAGE_EXTS and not force_document:
                        last_msg = await bot.send_photo(
                            chat_id=int_chat_id, photo=f, **thread_kwargs
                        )
                    elif ext in _VIDEO_EXTS:
                        last_msg = await bot.send_video(
                            chat_id=int_chat_id, video=f, **thread_kwargs
                        )
                    elif ext in _VOICE_EXTS and is_voice:
                        last_msg = await bot.send_voice(
                            chat_id=int_chat_id, voice=f, **thread_kwargs
                        )
                    elif ext in _TELEGRAM_SEND_AUDIO_EXTS:
                        last_msg = await bot.send_audio(
                            chat_id=int_chat_id, audio=f, **thread_kwargs
                        )
                    else:
                        last_msg = await bot.send_document(
                            chat_id=int_chat_id, document=f, **thread_kwargs
                        )
            except Exception as e:
                warning = _sanitize_error_text(f"Failed to send media {media_path}: {e}")
                logger.error(warning)
                warnings.append(warning)

        if last_msg is None:
            error = "No deliverable text or media remained after processing MEDIA tags"
            if warnings:
                return {"error": error, "warnings": warnings}
            return {"error": error}

        result = {
            "success": True,
            "platform": "telegram",
            "chat_id": chat_id,
            "message_id": str(last_msg.message_id),
        }
        if warnings:
            result["warnings"] = warnings
        return result
    except ImportError:
        return {"error": "python-telegram-bot not installed. Run: pip install python-telegram-bot"}
    except Exception as e:
        return _error(f"Telegram send failed: {e}")


def _derive_forum_thread_name(message: str) -> str:
    """Derive a thread name from the first line of the message, capped at 100 chars."""
    first_line = message.strip().split("\n", 1)[0].strip()
    # Strip common markdown heading prefixes
    first_line = first_line.lstrip("#").strip()
    if not first_line:
        first_line = "New Post"
    return first_line[:100]


# Process-local cache for Discord channel-type probes.  Avoids re-probing the
# same channel on every send when the directory cache has no entry (e.g. fresh
# install, or channel created after the last directory build).
_DISCORD_CHANNEL_TYPE_PROBE_CACHE: Dict[str, bool] = {}


def _remember_channel_is_forum(chat_id: str, is_forum: bool) -> None:
    _DISCORD_CHANNEL_TYPE_PROBE_CACHE[str(chat_id)] = bool(is_forum)


def _probe_is_forum_cached(chat_id: str) -> Optional[bool]:
    return _DISCORD_CHANNEL_TYPE_PROBE_CACHE.get(str(chat_id))


def _check_send_message():
    """Gate send_message on gateway running (always available on messaging platforms).

    Also passes for kanban workers — the dispatcher sets ``GROVE_KANBAN_TASK``
    on every spawned worker, but those workers run with the assignee profile's
    ``GROVE_HOME`` which has no ``gateway.pid``, so the gateway-running check
    would fail even though the parent gateway is alive. Honoring the env var
    lets workers call ``send_message`` to deliver rich content directly to the
    originating chat (paired with ``kanban_complete`` for the short notifier
    summary), which is the canonical pattern for any worker that needs to
    reply with more than the ~200-char first-line truncation the kanban
    notifier applies.
    """
    if os.environ.get("GROVE_KANBAN_TASK"):
        return True
    from gateway.session_context import get_session_env
    platform = get_session_env("GROVE_SESSION_PLATFORM", "")
    if platform and platform != "local":
        return True
    try:
        from gateway.status import is_gateway_running
        return is_gateway_running()
    except Exception:
        return False


from tools.registry import tool_error
def register(reg):
    """Sprint 53 — Dispatcher-driven registration entrypoint."""
    reg.register(
        name="send_message",
        toolset="messaging",
        schema=SEND_MESSAGE_SCHEMA,
        handler=send_message_tool,
        check_fn=_check_send_message,
        emoji="📨",
    )

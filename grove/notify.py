"""Agentless, surface-agnostic operator broadcast.

portal-action-error-surfacing-v1 (Phase 1). The one public path a
NON-agent, server-side caller (an aiohttp portal action handler, a cron
job — anything with no LLM turn, no ``send_message`` tool, no session
env) uses to reach the operator when something has already failed.

Two legs, in this order:

1. **The always-on CLI/substrate leg.** A single ``logger.<severity>``
   line, prefixed ``[ACTION FAILURE]``, emitted *unconditionally* —
   before any surface lookup, regardless of what surfaces are connected.
   This is journalctl-tailable, so "works at CLI first" is literal, not
   aspirational: the operator watching the log always sees the failure
   even with zero messaging surfaces up.

2. **The fan-out leg.** Lazily resolve the live gateway runner via the
   ``_gateway_runner_ref`` weakref (the same pattern
   ``tools/send_message_tool.py:_send_via_adapter`` uses) and, for each
   connected platform, deliver to its home channel. The caller never
   names a channel — the connected-surface set is resolved here.

FAIL-SAFE by design (prime directive nuance for the reporter path): this
runs precisely when something is *already* broken, so a broadcast that
itself failed must never crash the caller. A missing runner, no adapters,
or a single ``adapter.send`` raising are all logged and swallowed — the
log leg is already out, and the fan-out continues past a bad surface.
This is correct handling for the reporter, not silent degradation.

NO top-level ``grove`` → ``gateway`` import: the gateway dependency is
resolved lazily inside :func:`_resolve_gateway_runner`, so importing this
module never drags the gateway in and the resolution point is patchable
in isolation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _resolve_gateway_runner() -> Optional[Any]:
    """Lazily resolve the live ``GatewayRunner`` (or ``None``).

    Deferred import — ``grove`` must not import ``gateway`` at module load
    (mirrors ``_send_via_adapter``). Returns ``None`` on any failure (the
    gateway not in this process, the weakref dead, an import error): the
    caller treats ``None`` as "fan-out unavailable" and relies on the log
    leg that already fired.
    """
    try:
        from gateway.run import _gateway_runner_ref
    except Exception:  # noqa: BLE001 — gateway absent/out-of-process is expected
        return None
    try:
        return _gateway_runner_ref()
    except Exception:  # noqa: BLE001 — dead weakref, never fatal to the reporter
        return None


def _platform_name(platform: Any) -> str:
    return platform.value if hasattr(platform, "value") else str(platform)


async def broadcast_to_operator(
    content: str,
    *,
    severity: str = "error",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Reach the operator on every connected surface; never raise.

    Args:
        content: The operator-facing message (a failure report).
        severity: ``logger`` level name for the always-on log leg
            (``"error"``, ``"warning"``, ``"info"``, …). Unknown names
            fall back to ``error``.
        metadata: Optional per-send platform options, passed through to
            ``adapter.send``.

    Returns:
        A summary dict: ``{"logged": True, "surfaces_reached": [...],
        "surfaces_failed": [...]}``. ``logged`` is always ``True`` — the
        log leg is the guaranteed floor.
    """
    # ── Leg 1: the always-on CLI/substrate log. Fires first, unconditionally.
    # getattr-with-default so an unknown ``severity`` can never AttributeError
    # the log floor — it falls back to ``logger.error``.
    # forge-unattended-publish-v1 P3 (mechanism 2) — the prefix is severity-
    # conditional: a non-failure notice (info/debug, e.g. a fleet published-event)
    # gets a clean line; error/warning KEEP ``[ACTION FAILURE]`` byte-for-byte, so
    # every existing failure caller (all at the default ``error`` severity) is
    # unchanged.
    _prefix = "" if severity in ("info", "debug") else "[ACTION FAILURE] "
    getattr(logger, severity, logger.error)("%s%s", _prefix, content)

    reached: List[str] = []
    failed: List[str] = []
    summary: Dict[str, Any] = {
        "logged": True,
        "surfaces_reached": reached,
        "surfaces_failed": failed,
    }

    # ── Leg 2: fan-out. Absent runner → log is already out; return quietly.
    runner = _resolve_gateway_runner()
    if runner is None:
        return summary

    try:
        config = runner.config
        platforms = config.get_connected_platforms()
    except Exception as exc:  # noqa: BLE001 — malformed runner: log leg stands, do not raise
        logger.error("[ACTION FAILURE] surface resolution failed: %r", exc)
        return summary

    for platform in platforms:
        name = _platform_name(platform)
        try:
            home = config.get_home_channel(platform)
            adapter = runner.adapters.get(platform)
            if home is None or adapter is None:
                failed.append(name)
                continue
            result = await adapter.send(
                chat_id=home.chat_id, content=content, metadata=metadata
            )
            if getattr(result, "success", False):
                reached.append(name)
            else:
                err = getattr(result, "error", None)
                logger.error(
                    "[ACTION FAILURE] delivery to %s failed: %s", name, err
                )
                failed.append(name)
        except Exception as exc:  # noqa: BLE001 — one bad surface never aborts the fan-out
            logger.error(
                "[ACTION FAILURE] delivery to %s raised: %r", name, exc
            )
            failed.append(name)
            continue

    return summary

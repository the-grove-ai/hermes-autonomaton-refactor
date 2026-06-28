"""Composer observability sink (composer-observability-v1, Wave 1).

A read-side ledger of *how the system prompt was assembled* on each turn —
structure and token math only, never prompt text. The composer's behaviour is
UNCHANGED; this module makes it OBSERVABLE.

Storage: a single append-only JSON Lines file at
``~/.grove/composer_events.jsonl``. One record per ``compose()`` call, written
synchronously under an in-process lock — the SAME I/O model as
:class:`grove.intent_store.IntentStore` (intent_store.py:202-227): no async, no
buffering, POSIX ``O_APPEND`` atomicity for one-line records.

Why a sibling sink (not an IntentRecord extension): the two feeds have
different write lifecycles. ``IntentStore.append`` fires at turn *outcome*
(finalize); a composer event fires at *compose* time. Keeping them separate
keeps the compose-time write off the outcome-time record. The shared
``correlation_key`` (the dispatcher ``turn_id``, format ``session_id#counter``)
lets a downstream reader join the two on demand.

HARD INVARIANT — NO prompt text in any payload. Every field is an id, a band,
an order index, a token count, a status reason, a bool, or a small structured
``detail``. Section content never enters this file. See AC-3.

Schema (envelope), ``schema_version`` 1::

    {
      "schema_version": 1,
      "correlation_key": "<turn_id: session_id#turn_counter>",
      "compose_seq": <monotonic int, process-lifetime>,
      "compose_tier": "T0|T1|T2|T3",
      "total_tokens": <int>,
      "budget_ceiling": <int>,
      "providers": [
        {
          "provider_id": "<stable registry name, e.g. 'identity'>",
          "band": "stable|context|volatile",
          "order_index": <int>,
          "measured_tokens": <int>,           # estimate_tokens_rough
          "status_reason": "included|tier_gated|exception_dropped|budget_truncated",
          "is_gateable": <bool>,              # derived from GATEABLE_CONTEXT_BLOCKS
          "detail": null | {
            "exception_class": "<str>",       # F1 only
            "dropped_blocks": <int>,          # F2 only
            "dropped_tokens": <int>           # F2 only (provider's own _approx_tokens)
          }
        }
      ],
      "timestamp": "<ISO 8601>"
    }
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# The envelope contract version. Bump only on a breaking shape change; readers
# key off this to tolerate older lines (mirrors the IntentStore schema-skip
# discipline — a future field must not crash a current reader).
SCHEMA_VERSION = 1


class ComposerEventWriter:
    """Synchronous, lock-guarded, append-only JSONL sink for composer events.

    One file, shared across sessions in a process. Thread-safe writes via an
    in-process lock; cross-process safety relies on POSIX ``O_APPEND``
    atomicity for short one-line records, exactly as
    :class:`grove.intent_store.IntentStore`.

    The writer owns the ``compose_seq`` counter (process-lifetime monotonic,
    starts at 0, never resets except on process restart). ``emit`` stamps the
    authoritative ``compose_seq`` onto each event UNDER THE LOCK, so sequence
    order is identical to file-append order — two concurrent ``compose()``
    calls can never interleave a higher seq ahead of a lower one in the file.

    Tests pass an explicit ``sink_path``; production callers use
    :func:`get_writer` to acquire the module-level singleton bound to
    ``~/.grove/composer_events.jsonl``. A per-turn instance would reset the
    counter every turn — the singleton is what makes ``compose_seq`` monotonic.
    """

    def __init__(self, sink_path: Optional[Path] = None) -> None:
        if sink_path is None:
            from hermes_constants import get_hermes_home
            sink_path = Path(get_hermes_home()) / "composer_events.jsonl"
        self._path = Path(sink_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Process-lifetime monotonic. First emit stamps compose_seq == 0.
        self._compose_seq = 0

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Stamp ``compose_seq`` and append ``event`` as one JSON line.

        Fail-loud: a payload missing ``schema_version`` is a programming error
        (the envelope was built wrong) and raises ``ValueError`` rather than
        writing a malformed line that a reader would later silently skip
        (Architectural Prime Directive — surface, don't swallow).

        Returns the written dict (with the authoritative ``compose_seq``) so a
        caller wanting to forward the same payload to a logger avoids a
        re-build round-trip.
        """
        if "schema_version" not in event:
            raise ValueError(
                "composer event missing 'schema_version'; refusing to write a "
                "malformed envelope (composer-observability-v1)"
            )
        with self._lock:
            # Counter increment + serialize + append are ALL under the one lock
            # so compose_seq order == file order. Stamp authoritative value.
            event["compose_seq"] = self._compose_seq
            self._compose_seq += 1
            line = json.dumps(event, sort_keys=True, default=str) + "\n"
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return event


def build_composer_event(
    *,
    result: Any,
    provider_views: Any,
    correlation_key: Optional[str],
    compose_tier: Optional[str],
    budget_ceiling: Optional[int],
    timestamp: str,
) -> Dict[str, Any]:
    """Assemble the schema-1 envelope from a resident ``ComposedPrompt`` plus
    the composer's registry view — structure + token math only, NO prompt text.

    ``provider_views`` is the ``(name, band, order_index, gateable_block)``
    sequence from :meth:`PromptComposer.registered_provider_views`. One record
    is emitted per registered provider. ``compose_seq`` is left unset here —
    :meth:`ComposerEventWriter.emit` stamps it under the lock.

    Token math (deliberately TWO measures, so the ledger agrees with the gate):
    * ``measured_tokens`` for section content → ``estimate_tokens_rough``
      (``(len+3)//4``), the SAME measure ``build_context_report`` uses.
    * ``detail.dropped_tokens`` for F2 → the PROVIDER'S OWN ``_approx_tokens``
      (``max(1, len//4)``), recorded upstream at the drop site (Phase 4), so the
      dropped-token count matches the budget decision that dropped it.
    """
    # Lazy imports avoid any import cycle through the dispatcher hot path.
    from grove.tier_budget import GATEABLE_CONTEXT_BLOCKS
    from agent.model_metadata import estimate_tokens_rough

    sections: Dict[str, str] = getattr(result, "sections", None) or {}
    gated = set(getattr(result, "gated_context_blocks", None) or ())
    exc_drops: Dict[str, str] = dict(getattr(result, "exception_drops", None) or {})
    bud_drops: Dict[str, Dict[str, int]] = dict(
        getattr(result, "budget_drops", None) or {}
    )

    providers = []
    total_tokens = 0
    for name, band, order_index, gateable_block in provider_views:
        is_gateable = gateable_block in GATEABLE_CONTEXT_BLOCKS  # None → False
        detail: Optional[Dict[str, Any]] = None
        # Order matters: exception_dropped / budget_truncated are checked BEFORE
        # tier_gated. F1 adds a thrown gateable provider's block to the excluded
        # set (for /context, AC-8), so it would ALSO match the tier_gated test;
        # the drop must win. Genuine tier-gated providers never ran, so they are
        # absent from both drop maps — the reorder is correct for them too.
        if name in exc_drops:
            status_reason = "exception_dropped"
            measured_tokens = 0
            detail = {"exception_class": exc_drops[name]}
        elif name in bud_drops:
            status_reason = "budget_truncated"
            measured_tokens = estimate_tokens_rough(sections.get(name, "") or "")
            drop = bud_drops[name]
            detail = {
                "dropped_blocks": int(drop.get("dropped_blocks", 0)),
                "dropped_tokens": int(drop.get("dropped_tokens", 0)),
            }
        elif gateable_block is not None and gateable_block in gated:
            status_reason = "tier_gated"
            measured_tokens = 0
        else:
            status_reason = "included"
            measured_tokens = estimate_tokens_rough(sections.get(name, "") or "")

        # total_tokens is the truthful sum of what actually rode the prompt:
        # tier_gated / exception_dropped contribute 0, so summing every
        # provider's measured_tokens == sum over included + budget-truncated.
        total_tokens += measured_tokens
        providers.append(
            {
                "provider_id": name,
                "band": band,
                "order_index": order_index,
                "measured_tokens": measured_tokens,
                "status_reason": status_reason,
                "is_gateable": is_gateable,
                "detail": detail,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "correlation_key": correlation_key,
        "compose_tier": compose_tier,
        "total_tokens": total_tokens,
        "budget_ceiling": budget_ceiling,
        "providers": providers,
        "timestamp": timestamp,
    }


_default_writer: Optional[ComposerEventWriter] = None


def get_writer() -> ComposerEventWriter:
    """Return the module-level default writer, constructing on first call.

    Production callers (the Dispatcher emission site) acquire the writer
    through this accessor so they share a single instance — and thus a single
    monotonic ``compose_seq`` — bound to ``~/.grove/composer_events.jsonl``.
    Tests monkeypatch ``_default_writer`` with a tmp-path instance to isolate
    the file they write to from any other test or runtime state.
    """
    global _default_writer
    if _default_writer is None:
        _default_writer = ComposerEventWriter()
    return _default_writer

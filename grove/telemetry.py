"""Grove telemetry — structured event logging for sovereignty decisions.

Per Sprint 05 design D6: each sovereignty decision (promote / reject / revoke)
emits a structured ``sovereignty_decision`` event. v0.1 logs as JSON via the
standard ``logging`` module under the ``grove.telemetry`` logger. A future
sprint migrates the event store to SQL rows on the stages table.

The event schema is fixed in the design doc and treated as a public contract;
downstream tooling (the Kaizen recommender in Sprint 06b, dashboards later)
consumes these events directly.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from grove.skills import utc_now_iso

logger = logging.getLogger("grove.telemetry")


def log_sovereignty_decision(
    *,
    action: str,
    skill_name: str,
    skill_hash: str = "",
    scan_verdict: str = "unknown",
    operator: str = "unknown",
    source_path: Optional[str] = None,
    dest_path: Optional[str] = None,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """Emit a ``sovereignty_decision`` event and return the event dict.

    The return value lets callers chain (e.g. CLI renderers report what was
    logged) without re-constructing the dict.
    """
    event: dict[str, Any] = {
        "event_type": "sovereignty_decision",
        "action": action,
        "skill_name": skill_name,
        "skill_hash": skill_hash,
        "scan_verdict": scan_verdict,
        "operator": operator,
        "reason": reason,
        "timestamp": utc_now_iso(),
        "source_path": source_path,
        "dest_path": dest_path,
    }
    logger.info("sovereignty_decision %s", json.dumps(event, sort_keys=True))
    return event

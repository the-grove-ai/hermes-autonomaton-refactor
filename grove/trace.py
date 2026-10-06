"""One turn, read across the whole feed.

Every store that records a turn carries its ``turn_uid``: the intent record
(with its stage summary), the capability feed's tool rows and a goal's
decision log. This joins them, read-only, so a trace or a scorecard reads one
turn as one thing. Pipeline stage: Telemetry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def _jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue  # the chain verifier reports unreadable lines
    return rows


def turn_trace(turn_uid: str, *, home: Optional[Path] = None) -> Dict[str, Any]:
    """Everything on record for one turn: ``intent`` (its latest intent
    record, stage summary included), ``tool_rows`` (capability feed) and
    ``decisions`` (proposed and decided records from every goal's log)."""
    if home is None:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    home = Path(home)
    intent = None
    for row in _jsonl(home / "intent_records.jsonl"):
        if row.get("turn_uid") == turn_uid:
            intent = row      # later lines supersede (pending -> final)
    tool_rows = []
    feed_dir = home / ".capability_feed"
    if feed_dir.is_dir():
        for path in sorted(feed_dir.glob("feed*.jsonl")):
            tool_rows += [r for r in _jsonl(path) if r.get("turn_uid") == turn_uid]
    decisions = []
    log_dir = home / "decisions"
    if log_dir.is_dir():
        for path in sorted(log_dir.glob("*.jsonl")):
            decisions += [r for r in _jsonl(path) if r.get("turn_uid") == turn_uid]
    return {"turn_uid": turn_uid, "intent": intent,
            "tool_rows": tool_rows, "decisions": decisions}

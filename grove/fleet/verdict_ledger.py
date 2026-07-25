"""grove/fleet/verdict_ledger.py — the append-only quality-verdict feed
(artifact-review-v1 P4, R-7 FEED-FIRST).

Every fleet quality-gate evaluation writes ONE append-only verdict record
here; the fleet terminal event REFERENCES the record by ``run_id`` rather
than embedding the verdict. A drift detector reads ACROSS many verdicts to
see trend — one stream per run, globbed across runs, serves that.

Family 2 — the Kaizen-ledger idiom (grove/kaizen_ledger.py): one ``.jsonl``
per key under a single GROVE_HOME dotdir constant, a single sanctioned
writer (``threading.Lock`` + ``open("a")`` + ``json.dumps(sort_keys=True,
default=str)`` + fail-loud on an unknown/reserved field), enrolled in
writer-conformance-guard-v1 by a dirname-toucher pin. Here the key is the
fleet ``run_id`` (Kaizen keys on ``session_id``); a run's first evaluation
and its redraft re-score are SEPARATE records in the same file, keyed by
``attempt`` (R-3a) — a redraft never amends the first record.

Retention: the volume grows per evaluation, so a reaper mirroring
grove/ledger_retention.py is required before this runs at production volume.
That reaper is not built here (it is not in P4's field list); it is flagged
as a follow-on so the growth is a known, ruled deferral rather than a
silent unbounded feed.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = ["VERDICT_LEDGER_DIRNAME", "VerdictLedger", "default_verdict_dir"]


# The ONE spelling of the verdict feed directory name — the dotted literal
# appears nowhere else, so writer-conformance-guard-v1's dirname-toucher pin
# freezes the population to this module (sole writer) plus any future
# reaper/reader added in a reviewed diff.
VERDICT_LEDGER_DIRNAME = ".verdict_ledger"

# The verdict record's required fields (R-7). ``run_id`` / ``attempt`` /
# ``timestamp`` are populated by :meth:`VerdictLedger.record` and are reserved.
_REQUIRED_FIELDS = frozenset({
    "artifact_id",         # artifact identity the verdict is about
    "rubric_key",          # class@version (the immutable, hash-pinned rubric id)
    "criteria_ids",        # the stable criterion ids evaluated
    "effective_threshold",  # the threshold actually applied
    "threshold_source",    # rubric_default | record_override
    "status",              # pass | fail | skipped_oversize
    "complete",
    "accurate",
    "quality_score",
    "issues",
    "evaluator_tier",
    "evaluator_model",
})

_RESERVED_FIELDS = frozenset({"run_id", "attempt", "timestamp"})

_VALID_THRESHOLD_SOURCES = frozenset({"rubric_default", "record_override"})


def default_verdict_dir() -> Path:
    """Resolve ``~/.grove/.verdict_ledger`` via the standard hermes_home."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / VERDICT_LEDGER_DIRNAME


class VerdictLedger:
    """Append-only verdict feed for one fleet run.

    Thread-safe via a single ``threading.Lock`` around appends; the jsonl
    format tolerates concurrent read-while-write because each record is one
    complete line and ``open(..., "a")`` is atomic for short records on POSIX
    (the KaizenLedger contract, verbatim).
    """

    def __init__(self, run_id: str, ledger_dir: Optional[Path] = None) -> None:
        if ledger_dir is None:
            ledger_dir = default_verdict_dir()
        self._dir = Path(ledger_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._run_id = str(run_id)
        safe_id = "".join(
            c if c.isalnum() or c in ("-", "_") else "_" for c in self._run_id
        )[:128]
        self._path = self._dir / f"{safe_id}.jsonl"
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def run_id(self) -> str:
        return self._run_id

    def record(self, attempt: int, **fields: Any) -> Dict[str, Any]:
        """Append one verdict record for ``attempt`` and return it.

        ``attempt`` is 0 for the first evaluation and increments per redraft
        (R-3a); each attempt is a SEPARATE, append-only record — a redraft
        never amends the first. Fail-loud on a missing required field, an
        unknown/reserved field, or an out-of-domain ``threshold_source``
        (the Architectural Prime Directive — a malformed verdict record must
        not silently enter the feed a drift detector trusts).
        """
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
            raise ValueError(f"attempt must be a non-negative int, got {attempt!r}")
        collisions = _RESERVED_FIELDS & set(fields)
        if collisions:
            raise ValueError(
                f"reserved fields cannot be overridden: {sorted(collisions)}"
            )
        missing = _REQUIRED_FIELDS - set(fields)
        if missing:
            raise ValueError(
                f"verdict record is missing required field(s): {sorted(missing)}"
            )
        unknown = set(fields) - _REQUIRED_FIELDS
        if unknown:
            raise ValueError(
                f"verdict record carries unknown field(s): {sorted(unknown)} — "
                f"the feed schema is fixed (R-7); extend it in a reviewed diff."
            )
        if fields["threshold_source"] not in _VALID_THRESHOLD_SOURCES:
            raise ValueError(
                f"threshold_source must be one of {sorted(_VALID_THRESHOLD_SOURCES)}, "
                f"got {fields['threshold_source']!r}"
            )
        record: Dict[str, Any] = {
            "run_id": self._run_id,
            "attempt": attempt,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return record

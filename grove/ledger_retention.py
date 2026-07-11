"""kaizen-ledger-retention-v1 P2 — the ledger retention engine.

Prunes aged, window-bounded telemetry from ``~/.grove/.kaizen_ledger/``
while structurally preserving the event types downstream readers depend on
UNBOUNDED (GATE-A D3): ``kaizen_disposition`` (fault-triage acknowledge
baselines, fleet artifact terminal states) and
``quarantine_skill_disposition`` (the strict-promotion gate). Pruned lines
are archived — never destroyed: append to the sibling archive dir under the
SAME filename, fsync, and only THEN atomically rewrite the source (the
quarantine-before-rewrite discipline from proposal_queue).

Safety properties (each pinned by a unit test):

* **Cold-file stricture.** Only files whose mtime is older than
  ``retention_days + cold_buffer_hours`` are touched. Hot files — anything a
  live writer might still append to — are never read, never rewritten. This
  also makes the scan-state skip sound: an ELIGIBLE file's mtime bounds its
  newest event, so a file once verdicted ``fully-retained`` (every kept line
  is preserved-type or unparseable) can never age into prunability while its
  (mtime, size) key is unchanged.
* **Keep on any doubt.** A line that fails JSON parse, is not a dict, has an
  unknown ``event_type`` (outside ``KaizenLedger.EVENT_TYPES``), or has no
  recognizable timestamp is KEPT, counted, and reported — retention never
  guesses.
* **Archive-before-replace.** Pruned lines land in the archive (fsync'd)
  before the source is rewritten; a crash between the two duplicates data,
  never loses it. Zero kept lines → the whole file MOVES to the archive (no
  empty stubs left behind).
* **Fail loud.** Any exception propagates. The engine never swallows; the
  CLI layer (P3) owns the Andon filing.

The engine prints nothing and returns a :class:`RunReport`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from grove.kaizen_ledger import KaizenLedger, default_ledger_dir

__all__ = [
    "PRESERVE_EVENT_TYPES",
    "ARCHIVE_DIRNAME",
    "STATE_FILENAME",
    "FileReport",
    "RetentionConfig",
    "RunReport",
    "default_archive_dir",
    "default_state_path",
    "load_retention_config",
    "run_retention",
]


# GATE-A D3 — event types with UNBOUNDED reader completeness assumptions:
# fault_triage._latest_dispositions (ack-then-quiet baselines),
# portal._iter_ledger_terminal_events (artifact terminal states), and
# flywheel_cli._has_successful_quarantine_execution (strict promotion).
# These are NEVER pruned regardless of age.
PRESERVE_EVENT_TYPES = frozenset({
    "kaizen_disposition",
    "quarantine_skill_disposition",
})

ARCHIVE_DIRNAME = ".kaizen_ledger_archive"
STATE_FILENAME = ".kaizen_ledger_retention_state.json"

_VERDICT_FULLY_RETAINED = "fully-retained"


@dataclass(frozen=True)
class RetentionConfig:
    """Declarative retention knobs (``ledger_retention`` block in
    ``~/.grove/flywheel.config.yaml``; template in ``config/``).

    Fail-loud contract per the fault_triage loader precedent: an absent
    file or block uses these documented defaults; a present-but-invalid
    value raises LOUD in :func:`load_retention_config`.
    """

    enabled: bool = True
    retention_days: int = 30
    cold_buffer_hours: int = 24
    batch_max_files: int = 100
    sidecar_max_bytes: int = 1048576


def _require_positive_int(block: Dict[str, object], key: str, default: int) -> int:
    """Read ``key`` from a present config block, fail loud on a bad value."""
    if key not in block:
        return default
    value = block[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"flywheel.config.yaml ledger_retention.{key} must be an "
            f"integer, got {value!r} ({type(value).__name__})."
        )
    if value < 1:
        raise ValueError(
            f"flywheel.config.yaml ledger_retention.{key} must be >= 1, "
            f"got {value}."
        )
    return value


def load_retention_config(config_path: Optional[Path] = None) -> RetentionConfig:
    """Load the ``ledger_retention`` block from the operator's
    ``flywheel.config.yaml``.

    Mirrors :func:`grove.eval.fault_triage.load_fault_triage_thresholds`:
    absent file / absent block → documented defaults; a present block is
    validated key-by-key and any malformed value raises LOUD. Malformed
    YAML propagates from the parser.
    """
    if config_path is None:
        from hermes_constants import get_hermes_home
        config_path = Path(get_hermes_home()) / "flywheel.config.yaml"
    if not config_path.exists():
        return RetentionConfig()

    import yaml

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if raw is None:
        return RetentionConfig()
    if not isinstance(raw, dict):
        raise ValueError(
            f"{config_path} must be a YAML mapping, got {type(raw).__name__}."
        )
    block = raw.get("ledger_retention")
    if block is None:
        return RetentionConfig()
    if not isinstance(block, dict):
        raise ValueError(
            f"{config_path} ledger_retention must be a mapping, got "
            f"{type(block).__name__}."
        )

    enabled_raw = block.get("enabled", True)
    if not isinstance(enabled_raw, bool):
        raise ValueError(
            f"flywheel.config.yaml ledger_retention.enabled must be a "
            f"boolean, got {enabled_raw!r} ({type(enabled_raw).__name__})."
        )
    return RetentionConfig(
        enabled=enabled_raw,
        retention_days=_require_positive_int(block, "retention_days", 30),
        cold_buffer_hours=_require_positive_int(block, "cold_buffer_hours", 24),
        batch_max_files=_require_positive_int(block, "batch_max_files", 100),
        sidecar_max_bytes=_require_positive_int(
            block, "sidecar_max_bytes", 1048576
        ),
    )


def default_archive_dir() -> Path:
    """The archive sibling: ``~/.grove/.kaizen_ledger_archive``."""
    return default_ledger_dir().parent / ARCHIVE_DIRNAME


def default_state_path() -> Path:
    """The scan-state cache: ``~/.grove/.kaizen_ledger_retention_state.json``."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / STATE_FILENAME


@dataclass
class FileReport:
    """Per-file plan/outcome — the --dry-run plan line and the live audit."""

    path: str
    action: str  # "retained" | "rewritten" | "moved" | "skipped"
    lines_kept: int = 0
    lines_pruned: int = 0
    lines_unparseable: int = 0
    bytes_pruned: int = 0


@dataclass
class RunReport:
    """Aggregate outcome of one retention run. The engine never prints."""

    dry_run: bool = False
    cutoff: str = ""
    files_total: int = 0
    files_scanned: int = 0
    files_skipped: int = 0     # scan-state cache skips
    files_hot: int = 0         # inside the cold buffer — never touched
    files_rewritten: int = 0
    files_moved: int = 0
    lines_kept: int = 0
    lines_pruned: int = 0
    lines_unparseable: int = 0
    bytes_archived: int = 0
    file_reports: List[FileReport] = field(default_factory=list)


def _parse_timestamp(ts_raw: object) -> Optional[datetime]:
    """ISO-8601 → tz-aware UTC; naive treated as UTC; unparseable → None."""
    if not isinstance(ts_raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts_raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _classify_line(line: str, cutoff: datetime) -> str:
    """``"keep"`` / ``"prune"`` / ``"unparseable"`` for one raw ledger line.

    Keep-on-any-doubt: only a parseable dict with a KNOWN window-bounded
    event_type and a recognizable timestamp older than the cutoff prunes.
    """
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return "unparseable"
    if not isinstance(event, dict):
        return "unparseable"
    event_type = event.get("event_type")
    if event_type in PRESERVE_EVENT_TYPES:
        return "keep"
    if event_type not in KaizenLedger.EVENT_TYPES:
        return "unparseable"  # unknown type — never guess
    ts = _parse_timestamp(event.get("timestamp"))
    if ts is None:
        return "unparseable"  # no recognizable ts — never guess
    return "keep" if ts >= cutoff else "prune"


def _read_state(state_path: Path) -> Dict[str, Dict[str, object]]:
    """Load the scan-state cache; a missing or damaged cache reads as empty
    (the cache is a pure skip-optimization — losing it only costs a rescan)."""
    if not state_path.exists():
        return {}
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_state(state_path: Path, state: Dict[str, Dict[str, object]]) -> None:
    """Atomic (tmp + replace) rewrite of the scan-state cache."""
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    tmp.replace(state_path)


def _archive_append(archive_path: Path, lines: List[str]) -> int:
    """Append *lines* to the archive file and fsync BEFORE the caller
    touches the source. Returns bytes written."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(l + "\n" for l in lines)
    with open(archive_path, "a", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    return len(payload.encode("utf-8"))


def run_retention(
    *,
    ledger_dir: Optional[Path] = None,
    archive_dir: Optional[Path] = None,
    state_path: Optional[Path] = None,
    retention_days: int = 30,
    cold_buffer_hours: int = 24,
    batch_max_files: int = 100,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> RunReport:
    """One retention pass over the ledger directory. Returns a RunReport.

    ``dry_run=True`` computes the full plan and writes NOTHING — no archive,
    no rewrite, no scan-state. Any exception propagates (fail loud); the CLI
    layer owns surfacing.
    """
    ledger_dir = Path(ledger_dir) if ledger_dir is not None else default_ledger_dir()
    archive_dir = Path(archive_dir) if archive_dir is not None else default_archive_dir()
    state_path = Path(state_path) if state_path is not None else default_state_path()
    if now is None:
        now = datetime.now(timezone.utc)

    cutoff = now - timedelta(days=retention_days)
    cold_line = cutoff - timedelta(hours=cold_buffer_hours)

    report = RunReport(dry_run=dry_run, cutoff=cutoff.isoformat())
    if not ledger_dir.is_dir():
        return report

    state = _read_state(state_path)
    processed = 0

    for src in sorted(ledger_dir.glob("*.jsonl")):
        report.files_total += 1
        stat = src.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

        # Cold-file stricture: a file inside the buffer may still be written.
        if mtime >= cold_line:
            report.files_hot += 1
            continue

        # Scan-state skip: identical (mtime, size) + fully-retained verdict.
        # Sound because eligibility bounds the newest event by mtime — a
        # fully-retained ELIGIBLE file holds only preserved/unparseable lines.
        cached = state.get(str(src))
        if (
            cached
            and cached.get("mtime") == stat.st_mtime
            and cached.get("size") == stat.st_size
            and cached.get("verdict") == _VERDICT_FULLY_RETAINED
        ):
            report.files_skipped += 1
            report.file_reports.append(
                FileReport(path=str(src), action="skipped")
            )
            continue

        if processed >= batch_max_files:
            break
        processed += 1
        report.files_scanned += 1

        raw_lines = [
            l for l in src.read_text(encoding="utf-8").splitlines() if l.strip()
        ]
        kept: List[str] = []
        pruned: List[str] = []
        unparseable = 0
        for line in raw_lines:
            verdict = _classify_line(line, cutoff)
            if verdict == "prune":
                pruned.append(line)
            else:
                kept.append(line)
                if verdict == "unparseable":
                    unparseable += 1

        bytes_pruned = sum(len(l.encode("utf-8")) + 1 for l in pruned)
        fr = FileReport(
            path=str(src),
            action="retained",
            lines_kept=len(kept),
            lines_pruned=len(pruned),
            lines_unparseable=unparseable,
            bytes_pruned=bytes_pruned,
        )
        report.lines_kept += len(kept)
        report.lines_pruned += len(pruned)
        report.lines_unparseable += unparseable

        if not pruned:
            # Nothing to do — record the fully-retained verdict for the skip.
            report.file_reports.append(fr)
            if not dry_run:
                state[str(src)] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "verdict": _VERDICT_FULLY_RETAINED,
                }
            continue

        fr.action = "rewritten" if kept else "moved"
        report.file_reports.append(fr)
        if dry_run:
            if kept:
                report.files_rewritten += 1
            else:
                report.files_moved += 1
            report.bytes_archived += bytes_pruned
            continue

        archive_path = archive_dir / src.name
        if not kept and not archive_path.exists():
            # Whole-file move — the no-empty-stubs fast path.
            archive_dir.mkdir(parents=True, exist_ok=True)
            os.replace(src, archive_path)
            report.files_moved += 1
            report.bytes_archived += bytes_pruned
        else:
            # Archive-before-replace: pruned lines are durably in the
            # archive BEFORE the source rewrite. A crash between the two
            # duplicates lines; it never loses them.
            report.bytes_archived += _archive_append(archive_path, pruned)
            if kept:
                tmp = src.with_suffix(src.suffix + ".tmp")
                tmp.write_text(
                    "".join(l + "\n" for l in kept), encoding="utf-8"
                )
                os.replace(tmp, src)
                report.files_rewritten += 1
            else:
                src.unlink()
                report.files_moved += 1
        state.pop(str(src), None)  # rewritten/moved — stale key either way
        time.sleep(0.05)  # pace the I/O between rewrites

    if not dry_run:
        # Drop state entries for files that no longer exist (moved/purged).
        state = {p: v for p, v in state.items() if Path(p).exists()}
        _write_state(state_path, state)
    return report

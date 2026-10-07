"""Checkpoints — a consistent copy of the node's records at one moment, and
putting it back.

For rehearsal: do the work once on real records, save a checkpoint, and start
again from that exact point as often as needed. A checkpoint is every store
the work's state lives in (:data:`STATE`), copied together while nothing is in
flight.

What this never does:

  * It never alters a record. Files are copied whole; every record keeps the
    time it was made and the hash it was written with. A restored chain is the
    chain as it stood at the checkpoint, byte for byte.
  * It never deletes. A restore first MOVES the current state to a dated
    archive folder beside the checkpoints, then copies the checkpoint in.
  * It never writes to the goal's records, the ledger or the chain. Saves and
    restores are logged in an admin log of their own (:func:`admin_log_path`),
    which is not part of any checkpoint and is never restored over.

A restore is applied at gateway START-UP, before any store is open
(:func:`apply_pending`). Asking for one writes a small request file and the
gateway restarts; nothing is swapped under a running process, so no cached
chain head, open database handle or in-memory session can disagree with the
files. A save needs no restart: SQLite stores are copied through SQLite's own
backup, so the copy is consistent even while the gateway reads them.

Scope, plainly: these stores belong to the NODE, not to one goal. Restoring a
checkpoint puts back every turn record, session and proposal as of that
moment, for all work on this node. What was there is in the archive.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Every store a checkpoint holds, relative to the node's home. A path that
# does not exist at save time is recorded as absent, and is absent again after
# a restore. Each goal's work queue is added at save time, from the Dock.
STATE = (
    "intent_records.jsonl",                 # the turn records: the hash chain
    "telemetry.db",                         # sessions, their chain anchors and latches
    "sessions",                             # each chat's session index and transcripts
    "decisions",                            # each goal's decision log
    ".kaizen_ledger",                       # the Kaizen ledger
    ".kaizen_ledger_archive",               # ...and what retention moved out of it
    ".kaizen_ledger_retention_state.json",
    "pattern_cache.db",                     # kegs and their versions; watches
    "proposals.jsonl",                      # proposals waiting for a signature
    "memory_proposals.jsonl",               # memory suggestions waiting
    "dock/dock.yaml",                       # the goals and their declared work
    "grants.yaml",                          # signatures in force, the session rule among them
    "vocabulary",                           # phrases learned in conversation
    ".reissue",                             # transient notes (empty when nothing is in flight)
    "routing.operational.yaml",             # tier bindings and prices: the scorecard's inputs
    "memory_records.jsonl",                 # memory the operator has accepted
    "memory_index.json",
    ".pushed_memory_ids.json",              # which suggestions were already offered
    ".push_cadence.json",
    ".last_offered_proposal.json",
    ".pending_andon",                       # halts waiting for the operator
    "red_pending.db",
    "state",                                # readers' cursors into the records above
    ".capability_feed",                     # the feed the detectors read
    "composer_events.jsonl",
)
SQLITE = frozenset({"telemetry.db", "pattern_cache.db", "red_pending.db"})
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
# Transient notes that mean work is under way. A goal note (a backlog stage
# just released) and a turn note are state, not motion.
_IN_FLIGHT_NOTES = ("hold-", "cards-", "actions-", "tier-", "absorbed-", "pause-")


class CheckpointRefused(Exception):
    """A save or restore that was not carried out, with the reason to show."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _home(home: Any = None) -> Path:
    if home is not None:
        return Path(home)
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def root(home: Any = None) -> Path:
    return _home(home) / "checkpoints"


def admin_log_path(home: Any = None) -> Path:
    """The admin log: outside every goal's records, never chained, never part
    of a checkpoint."""
    return root(home) / "admin-log.jsonl"


def _log(home: Any, action: str, **fields: Any) -> Dict[str, Any]:
    entry = {"at": _now(), "action": action, **fields}
    path = admin_log_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return entry


def admin_log(home: Any = None, limit: int = 20) -> List[Dict[str, Any]]:
    path = admin_log_path(home)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows[-limit:][::-1]


# ── what is in flight ─────────────────────────────────────────────────


def _work_goals() -> List[Any]:
    from grove.decision_work import DecisionWork, load_config
    from grove.dock import load_dock

    out = []
    dock = load_dock()
    for goal in (getattr(dock, "goals", None) or ()):
        cfg = load_config(goal)
        if cfg is not None:
            out.append(DecisionWork(cfg))
    return out


def in_flight(home: Any = None) -> List[str]:
    """Why a checkpoint cannot be saved or restored right now: one plain
    reason per thing that is under way. Empty when nothing is."""
    base, reasons = _home(home), []
    try:
        for work in _work_goals():
            waiting = work.pending()
            if waiting is not None:
                reasons.append(
                    f"{work.config.goal_id}: {waiting['item_id']} is waiting for your decision")
    except Exception as exc:  # noqa: BLE001 — an unreadable Dock is itself a reason
        reasons.append(f"the Dock could not be read ({type(exc).__name__})")
    notes = base / ".reissue"
    if notes.is_dir():
        for path in sorted(notes.iterdir()):
            name = path.name
            if name.startswith(_IN_FLIGHT_NOTES):
                reasons.append(f"a work session has something under way ({name.split('-')[0]})")
            elif not name.startswith(("goal-", "note-", "hop-", "stop-")) and path.is_file():
                reasons.append("a request is about to be re-issued")
    return list(dict.fromkeys(reasons))


# ── copying ───────────────────────────────────────────────────────────


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _files(path: Path) -> List[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*") if p.is_file())


def _copy_sqlite(source: Path, target: Path) -> None:
    """A consistent copy of a live SQLite store, through SQLite itself."""
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _copy(source: Path, target: Path, *, sqlite: bool) -> None:
    if sqlite:
        _copy_sqlite(source, target)
    elif source.is_dir():
        shutil.copytree(source, target, symlinks=False)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _state_paths(base: Path) -> List[str]:
    """STATE plus each goal's work queue and backlog stage folders, as paths relative to the home."""
    paths = list(STATE)
    real = base.resolve()
    try:
        for work in _work_goals():
            # The queue, and each backlog stage's own folder: which invoices a
            # stage holds decides what a release puts in the queue, so a
            # checkpoint that left the folders out could be restored onto a
            # different set.
            from grove.decision_work import _stages
            folders = [Path(work.config.queue)] + [Path(f) for f, _label in _stages(work.config)]
            for folder in folders:
                try:
                    paths.append(str(folder.resolve().relative_to(real)))
                except ValueError:
                    continue      # a folder outside the home is not this node's to copy
    except Exception:  # noqa: BLE001 — reported by in_flight; nothing extra to copy
        pass
    return list(dict.fromkeys(paths))


def _summary() -> Dict[str, Any]:
    """What the checkpoint holds, in the operator's terms."""
    out: Dict[str, Any] = {"goals": []}
    try:
        from grove.decision_work import backlog_state
        for work in _work_goals():
            run = work.log.current_run() or {}
            decided = sum(1 for r in work.log.run_records() if r.get("kind") == "decided"
                          and not r.get("after"))
            stages = backlog_state(work.config)
            out["goals"].append({
                "goal": work.config.goal_id, "run": run.get("run_number"),
                "decided": decided, "queued": len(work.queue_items()),
                "backlog": {"items": stages.get("items"), "released": stages.get("released")},
            })
    except Exception:  # noqa: BLE001 — the summary is a convenience
        pass
    return out


# ── save ──────────────────────────────────────────────────────────────


def save(name: str, note: str = "", *, home: Any = None, surface: str = "cli",
         replace: bool = False) -> Dict[str, Any]:
    """Save a checkpoint. Refused while anything is in flight, for a name that
    is not a plain slug, and for a name already taken (unless ``replace``, in
    which case the earlier one is moved to the archive, not deleted)."""
    base = _home(home)
    name = str(name or "").strip().lower()
    if not NAME_RE.fullmatch(name):
        raise CheckpointRefused(
            "A checkpoint name is lower-case letters, digits and hyphens, such as "
            "before-month-3.")
    reasons = in_flight(base)
    if reasons:
        _log(base, "save_refused", name=name, surface=surface, reasons=reasons)
        raise CheckpointRefused("Nothing was saved: " + "; ".join(reasons) + ".")
    target = root(base) / name
    if target.exists():
        if not replace:
            raise CheckpointRefused(
                f"A checkpoint named {name} already exists. Choose another name, or "
                f"replace it (the earlier one is kept in the archive).")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        kept = root(base) / "_archive" / f"{stamp}-replaced-{name}"
        kept.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, kept)
    staging = root(base) / f".saving-{name}"
    shutil.rmtree(staging, ignore_errors=True)
    entries = []
    try:
        for rel in _state_paths(base):
            source = base / rel
            if not source.exists():
                entries.append({"path": rel, "present": False})
                continue
            _copy(source, staging / "state" / rel, sqlite=rel in SQLITE)
            copied = staging / "state" / rel
            entries.append({
                "path": rel, "present": True, "dir": copied.is_dir(),
                "files": {str(f.relative_to(copied)) if copied.is_dir() else "": _digest(f)
                          for f in _files(copied)},
            })
        manifest = {"name": name, "note": str(note or "").strip()[:500], "saved_at": _now(),
                    "surface": surface, "entries": entries, **_summary()}
        (staging / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=1), encoding="utf-8")
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _log(base, "saved", name=name, note=manifest["note"], surface=surface,
         files=sum(len(e.get("files") or {}) for e in entries))
    return manifest


def rename(old: str, new: str, *, home: Any = None, surface: str = "cli") -> Dict[str, Any]:
    """Give a saved checkpoint another name. Its files are not touched; only
    the folder and the name in its manifest change. Logged in the admin log."""
    base = _home(home)
    old, new = str(old or "").strip().lower(), str(new or "").strip().lower()
    source, target = root(base) / old, root(base) / new
    if not NAME_RE.fullmatch(old) or not (source / "manifest.json").exists():
        raise CheckpointRefused(f"There is no checkpoint named {old}.")
    if not NAME_RE.fullmatch(new):
        raise CheckpointRefused(
            "A checkpoint name is lower-case letters, digits and hyphens.")
    if target.exists():
        raise CheckpointRefused(f"A checkpoint named {new} already exists.")
    if (pending_restore(base) or {}).get("name") == old:
        raise CheckpointRefused(f"A restore of {old} is waiting; it cannot be renamed now.")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifest["name"], manifest["renamed_from"] = new, old
    os.replace(source, target)
    (target / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=1), encoding="utf-8")
    _log(base, "renamed", name=new, renamed_from=old, surface=surface)
    return manifest


def listing(home: Any = None) -> List[Dict[str, Any]]:
    out = []
    base = root(home)
    if base.is_dir():
        for path in sorted(base.iterdir()):
            manifest = path / "manifest.json"
            if path.is_dir() and not path.name.startswith(("_", ".")) and manifest.exists():
                try:
                    out.append(json.loads(manifest.read_text(encoding="utf-8")))
                except ValueError:
                    out.append({"name": path.name, "unreadable": True})
    return sorted(out, key=lambda m: str(m.get("saved_at") or ""), reverse=True)


def verify(name: str, home: Any = None) -> List[str]:
    """Check a saved checkpoint against its own manifest. Returns what is
    wrong, file by file; empty when it is exactly as saved."""
    base = root(home) / name
    try:
        manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [f"checkpoint {name} has no readable manifest"]
    problems = []
    for entry in manifest.get("entries") or []:
        if not entry.get("present"):
            continue
        copied = base / "state" / entry["path"]
        for rel, digest in (entry.get("files") or {}).items():
            path = copied / rel if rel else copied
            if not path.is_file():
                problems.append(f"{entry['path']}/{rel}: missing")
            elif _digest(path) != digest:
                problems.append(f"{entry['path']}/{rel}: changed since it was saved")
    return problems


# ── restore ───────────────────────────────────────────────────────────


def _pending_path(home: Any = None) -> Path:
    return root(home) / ".restore-request.json"


def last_restore_path(home: Any = None) -> Path:
    return root(home) / "last-restore.json"


def request_restore(name: str, *, home: Any = None, surface: str = "cli") -> Dict[str, Any]:
    """Ask for a checkpoint to be put back. It is applied when the gateway
    next starts, before any store is open. Refused while anything is in
    flight, and for a checkpoint that is not exactly as it was saved."""
    base = _home(home)
    name = str(name or "").strip().lower()
    if not NAME_RE.fullmatch(name) or not (root(base) / name / "manifest.json").exists():
        raise CheckpointRefused(f"There is no checkpoint named {name}.")
    reasons = in_flight(base)
    if reasons:
        _log(base, "restore_refused", name=name, surface=surface, reasons=reasons)
        raise CheckpointRefused("Nothing was restored: " + "; ".join(reasons) + ".")
    damaged = verify(name, base)
    if damaged:
        _log(base, "restore_refused", name=name, surface=surface, reasons=damaged[:5])
        raise CheckpointRefused(
            f"Checkpoint {name} is not as it was saved ({damaged[0]}). Nothing was restored.")
    request = {"name": name, "requested_at": _now(), "surface": surface}
    _pending_path(base).write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
    _log(base, "restore_requested", name=name, surface=surface)
    return request


def pending_restore(home: Any = None) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(_pending_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def restore_now(name: str, *, home: Any = None, surface: str = "cli") -> Dict[str, Any]:
    """Put a checkpoint back, now. ONLY for a moment when no process has the
    stores open (gateway start-up, or the gateway stopped). The current state
    is moved to a dated archive folder first; then the checkpoint is copied
    in, checked file by file against its manifest, and the audit check is run.
    Returns the result, which is also kept as the last restore."""
    base = _home(home)
    source = root(base) / name
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = root(base) / "_archive" / f"{stamp}-before-restore-{name}"
    archive.mkdir(parents=True, exist_ok=True)
    moved = []
    for entry in manifest.get("entries") or []:
        rel = entry["path"]
        current = base / rel
        if current.exists() or current.is_symlink():
            (archive / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(current), str(archive / rel))
            moved.append(rel)
        # SQLite side files belong to the store that was just moved away.
        for side in ("-wal", "-shm"):
            extra = base / (rel + side)
            if rel in SQLITE and extra.exists():
                shutil.move(str(extra), str(archive / (rel + side)))
        if entry.get("present"):
            _copy(source / "state" / rel, current, sqlite=False)
    mismatches = []
    for entry in manifest.get("entries") or []:
        if not entry.get("present"):
            if (base / entry["path"]).exists():
                mismatches.append(f"{entry['path']}: present, though the checkpoint has none")
            continue
        restored = base / entry["path"]
        for rel, digest in (entry.get("files") or {}).items():
            path = restored / rel if rel else restored
            if not path.is_file() or _digest(path) != digest:
                mismatches.append(f"{entry['path']}/{rel}: not identical to the checkpoint")
    try:
        from grove import audit
        report = audit.chain_report(base)
        check = {"result": report["result"], "records": report.get("records"),
                 "chained": report.get("chained"),
                 "problems": [p.get("problem") for p in (report.get("problems") or [])][:10]}
    except Exception as exc:  # noqa: BLE001 — the restore is reported with the check's failure
        check = {"result": "could_not_run", "problems": [type(exc).__name__]}
    result = {
        "name": name, "restored_at": _now(), "surface": surface,
        "saved_at": manifest.get("saved_at"), "archive": str(archive),
        "moved_to_archive": moved, "identical": not mismatches, "mismatches": mismatches[:10],
        "audit_check": check,
    }
    last_restore_path(base).write_text(json.dumps(result, sort_keys=True, indent=1),
                                       encoding="utf-8")
    _log(base, "restored", name=name, surface=surface, archive=str(archive),
         identical=not mismatches, audit_check=check["result"])
    return result


def apply_pending(home: Any = None) -> Optional[Dict[str, Any]]:
    """At gateway start-up: carry out a restore that was asked for, if any.
    The request is consumed first, so a restore that fails cannot run again on
    every start; its failure is logged and kept as the last restore."""
    base = _home(home)
    request = pending_restore(base)
    if request is None:
        return None
    _pending_path(base).unlink(missing_ok=True)
    try:
        return restore_now(str(request.get("name")), home=home,
                           surface=str(request.get("surface") or "cli"))
    except Exception as exc:  # noqa: BLE001 — loud, recorded, and the gateway still starts
        failed = {"name": request.get("name"), "restored_at": _now(), "identical": False,
                  "failed": f"The restore stopped part-way ({type(exc).__name__}). What had "
                            f"been moved is in the archive folder; the gateway log has the detail.",
                  "audit_check": {"result": "not_run", "problems": []}}
        last_restore_path(base).write_text(json.dumps(failed, sort_keys=True, indent=1),
                                           encoding="utf-8")
        _log(base, "restore_failed", name=request.get("name"), kind=type(exc).__name__)
        import logging
        logging.getLogger(__name__).critical(
            "[checkpoints] restore of %s FAILED: %r — see %s", request.get("name"), exc,
            admin_log_path(base))
        return failed


def last_restore(home: Any = None) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(last_restore_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

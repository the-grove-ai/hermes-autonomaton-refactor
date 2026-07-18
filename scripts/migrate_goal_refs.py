#!/usr/bin/env python3
"""One-off migration: dock-goal-ref-integrity-v1 M5 — remediate poisoned goal refs.

Phase 1 (commit 7b5bcd421) closed the three writer doors so no NEW invalid
goal ref can be emitted. This script remediates the EXISTING poisoned state,
once, in three targets (ruling R5 — one sweep, three targets):

  (a) ``pages/session_compacted/*.md`` — ``dock_goal_refs`` → ``[]``.
      Session-page refs were ``goal_alignment`` CATEGORY strings
      (direct/indirect/...) that can never match a ``goal.id``; honest-empty
      is the remediation (R1). attachment-projection-v1 later upgrades
      ``[]`` → derived refs.
  (b) every OTHER page class — apply the Phase 1 adapter-door logic
      STATICALLY (``grove.wiki.adapters._validated_dock_goal_refs`` is
      imported, not reimplemented): keep-on-id / exact-goal-NAME-map-to-id /
      drop-loud.
  (c) ``memory_records.jsonl`` events whose ``dock_goal_ref`` is the literal
      STRING ``"None"`` (model-authored at birth, M7 trace) → ``null``.
      The ``career-transition`` record is EXPLICITLY EXCLUDED (R3): it is
      the dangling-later / tolerant-degrade exemplar — the matcher only
      touches ``== "None"``, and a belt guard hard-skips it besides.

SAFETY:
  * ``--dry-run`` is the DEFAULT: prints the full change census (per-file
    old refs → new refs; per-record for (c)), writes NOTHING.
  * ``--execute`` REFUSES to run without ``--backup-tar <path>`` naming an
    existing tar that (1) lists every file this run will modify and
    (2) is fresher than every one of them (i.e. was created after the last
    mutation — a stale backup refuses).
  * Page rewrites touch ONLY the ``dock_goal_refs`` frontmatter entry;
    every other byte is preserved. The rewrite bumps mtime, which IS the
    FTS re-index trigger (grove/wiki/index.py mtime-incremental refresh) —
    the index is never touched directly.
  * Idempotent: a second run finds zero changes.

USAGE (on the gateway VM, inside the repo venv, as the hermes user):
    .venv/bin/python scripts/migrate_goal_refs.py                 # dry-run census
    tar cf /tmp/goal-refs-backup.tar <files from the census>
    .venv/bin/python scripts/migrate_goal_refs.py --execute \\
        --backup-tar /tmp/goal-refs-backup.tar
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# Run-from-anywhere: ensure the repo root (not scripts/) is importable so
# `grove.*` resolves when invoked as `python scripts/<this>.py`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SESSION_CLASS = "session_compacted"
_MEMORY_LOG_NAME = "memory_records.jsonl"
# R3 — the dangling-later exemplar, deliberately left in place. The (c)
# matcher (== "None") cannot reach it; this belt guard makes the exclusion
# structural and testable.
_R3_EXCLUDED_REF = "career-transition"


# ── census ──────────────────────────────────────────────────────────────


@dataclass
class Census:
    """Everything the sweep WOULD change (dry-run) or DID change (execute)."""

    session_pages: List[Tuple[Path, List[str]]] = field(default_factory=list)
    other_pages: List[Tuple[Path, List[str], List[str]]] = field(
        default_factory=list
    )
    none_records: List[Tuple[int, str]] = field(default_factory=list)
    r3_skipped: int = 0
    pages_scanned: int = 0
    events_scanned: int = 0

    @property
    def changed_files(self) -> List[Path]:
        return [p for p, _ in self.session_pages] + [
            p for p, _, _ in self.other_pages
        ]


# ── frontmatter surgery (byte-preserving) ───────────────────────────────


def _split_page(text: str) -> Optional[Tuple[List[str], List[str], List[str]]]:
    """Split a page into (opening-fence line, frontmatter lines, rest lines).

    Returns None when the page has no terminated leading ``---`` block.
    Operates on raw lines so the eventual rewrite preserves every byte
    outside the ``dock_goal_refs`` entry.
    """
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            return [lines[0]], lines[1:i], lines[i:]
    return None


def _refs_span(fm_lines: List[str]) -> Optional[Tuple[int, int]]:
    """(start, end) line span of the ``dock_goal_refs`` entry within the
    frontmatter lines — the key line plus any ``- item`` block lines. None
    when the key is absent."""
    for i, line in enumerate(fm_lines):
        if line == "dock_goal_refs:" or line.startswith("dock_goal_refs: "):
            end = i + 1
            if line == "dock_goal_refs:":  # block-sequence form
                while end < len(fm_lines) and fm_lines[end].startswith("- "):
                    end += 1
            return i, end
    return None


def _render_refs(refs: List[str]) -> List[str]:
    """Render ``refs`` in the same shape ``yaml.safe_dump(sort_keys=False)``
    produces inside ``_render`` — inline ``[]`` when empty, block items
    otherwise."""
    if not refs:
        return ["dock_goal_refs: []"]
    return ["dock_goal_refs:"] + [f"- {r}" for r in refs]


def _page_refs(fm_lines: List[str]) -> Optional[List[str]]:
    """Parse the current ``dock_goal_refs`` value (YAML, read-only). None when
    the key is absent or the frontmatter does not parse."""
    try:
        meta = yaml.safe_load("\n".join(fm_lines))
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict) or "dock_goal_refs" not in meta:
        return None
    value = meta["dock_goal_refs"]
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return None


def _rewrite_page(text: str, new_refs: List[str]) -> str:
    """Replace ONLY the ``dock_goal_refs`` entry; every other byte survives."""
    split = _split_page(text)
    assert split is not None  # caller verified via _page_refs
    fence, fm_lines, rest = split
    span = _refs_span(fm_lines)
    assert span is not None
    start, end = span
    new_fm = fm_lines[:start] + _render_refs(new_refs) + fm_lines[end:]
    return "\n".join(fence + new_fm + rest)


# ── target scans ────────────────────────────────────────────────────────


def _scan_pages(pages_dir: Path, census: Census) -> None:
    from grove.wiki.adapters import _validated_dock_goal_refs

    for path in sorted(pages_dir.rglob("*.md")):
        census.pages_scanned += 1
        text = path.read_text(encoding="utf-8")
        split = _split_page(text)
        if split is None:
            print(f"  [skip] {path}: no terminated frontmatter block")
            continue
        refs = _page_refs(split[1])
        if refs is None:
            print(f"  [skip] {path}: no parseable dock_goal_refs entry")
            continue
        is_session = path.parent.name == _SESSION_CLASS
        if is_session:
            # (a) honest-empty, R1 — session refs are category strings.
            if refs:
                census.session_pages.append((path, refs))
        else:
            # (b) Phase 1 adapter-door logic, applied statically.
            new = _validated_dock_goal_refs(
                refs, source_type="migration", name=path.name
            )
            if new != refs:
                census.other_pages.append((path, refs, new))


def _scan_memory_log(log_path: Path, census: Census) -> List[Optional[str]]:
    """Return per-line replacement JSON (None = line unchanged) for target
    (c); populates the census as it goes."""
    replacements: List[Optional[str]] = []
    if not log_path.exists():
        print(f"  [skip] memory log absent at {log_path}")
        return replacements
    for line_no, line in enumerate(
        log_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        replacements.append(None)
        if not line.strip():
            continue
        census.events_scanned += 1
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue  # replay-tolerant, like MemoryStore.read_events
        ref = data.get("dock_goal_ref")
        if ref == _R3_EXCLUDED_REF:
            # R3 belt guard — structurally unreachable by the == "None"
            # matcher, but the exclusion must not depend on that.
            census.r3_skipped += 1
            continue
        if ref == "None":
            data["dock_goal_ref"] = None
            census.none_records.append(
                (line_no, data.get("record_id", "<no record_id>"))
            )
            replacements[-1] = json.dumps(data, sort_keys=True, default=str)
    return replacements


# ── execute-mode guards ─────────────────────────────────────────────────


def _verify_backup(tar_path: Path, targets: List[Path]) -> List[str]:
    """Refusal reasons (empty = backup acceptable). The tar must exist, list
    every target (two-component suffix match — page hashes and the memory
    log name are unique at that depth), and be fresher than every target."""
    problems: List[str] = []
    if not tar_path.is_file():
        return [f"backup tar does not exist: {tar_path}"]
    try:
        with tarfile.open(tar_path) as tf:
            members = [m.lstrip("./").lstrip("/") for m in tf.getnames()]
    except tarfile.TarError as exc:
        return [f"backup tar unreadable: {exc}"]
    tar_mtime = tar_path.stat().st_mtime
    for target in targets:
        suffix = f"{target.parent.name}/{target.name}"
        if not any(m.endswith(suffix) or m.endswith(target.name) for m in members):
            problems.append(f"backup tar does not list target: {target}")
        if target.stat().st_mtime > tar_mtime:
            problems.append(
                f"backup tar is OLDER than target (stale backup): {target}"
            )
    return problems


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".migrate-tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


# ── main ────────────────────────────────────────────────────────────────


def _resolve_paths(args: argparse.Namespace) -> Tuple[Path, Path]:
    from hermes_constants import get_hermes_home, get_wiki_path

    wiki_root = Path(args.wiki_root) if args.wiki_root else get_wiki_path()
    home = Path(args.grove_home) if args.grove_home else Path(get_hermes_home())
    pages_dir = wiki_root / "pages"
    if not pages_dir.is_dir():
        sys.exit(f"ANDON: pages dir not found at {pages_dir} — wrong wiki root?")
    return pages_dir, home / _MEMORY_LOG_NAME


def _print_census(census: Census) -> None:
    print(f"\n(a) session_compacted pages -> refs []  "
          f"[{len(census.session_pages)} to change]")
    for path, old in census.session_pages:
        print(f"  {path}\n    {old} -> []")
    print(f"\n(b) non-session pages, name-map-or-drop  "
          f"[{len(census.other_pages)} to change]")
    for path, old, new in census.other_pages:
        print(f"  {path}\n    {old} -> {new}")
    print(f"\n(c) memory events 'None' -> null  "
          f"[{len(census.none_records)} to change]")
    for line_no, record_id in census.none_records:
        print(f"  line {line_no}  record_id={record_id}  'None' -> null")
    print(
        f"\nScanned {census.pages_scanned} pages, {census.events_scanned} "
        f"memory events. R3 career-transition events left in place: "
        f"{census.r3_skipped}."
    )
    print(
        f"TOTALS: (a)={len(census.session_pages)} "
        f"(b)={len(census.other_pages)} (c)={len(census.none_records)}"
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="dock-goal-ref-integrity-v1 M5 — remediate poisoned goal refs."
    )
    ap.add_argument(
        "--execute", action="store_true",
        help="Apply the changes (default: dry-run, writes nothing).",
    )
    ap.add_argument(
        "--backup-tar", type=Path, default=None,
        help="REQUIRED with --execute: existing tar listing every target file, "
             "created after their last modification.",
    )
    ap.add_argument("--wiki-root", type=Path, default=None,
                    help="Override the wiki root (default: get_wiki_path()).")
    ap.add_argument("--grove-home", type=Path, default=None,
                    help="Override GROVE_HOME (default: get_hermes_home()).")
    args = ap.parse_args(argv)

    pages_dir, memory_log = _resolve_paths(args)
    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"migrate_goal_refs [{mode}]  pages={pages_dir}  memory={memory_log}")

    census = Census()
    _scan_pages(pages_dir, census)
    replacements = _scan_memory_log(memory_log, census)
    _print_census(census)

    total = (
        len(census.session_pages)
        + len(census.other_pages)
        + len(census.none_records)
    )
    if not args.execute:
        print("\nDRY-RUN: nothing written.")
        return 0
    if total == 0:
        print("\nNothing to change — already migrated (idempotent no-op).")
        return 0

    # --execute refusal guards.
    targets = census.changed_files + (
        [memory_log] if census.none_records else []
    )
    if args.backup_tar is None:
        sys.exit("REFUSED: --execute requires --backup-tar <path>.")
    problems = _verify_backup(args.backup_tar, targets)
    if problems:
        for p in problems:
            print(f"REFUSED: {p}", file=sys.stderr)
        return 2

    # (a) + (b) — byte-preserving frontmatter rewrites; mtime bump is the
    # FTS re-index trigger.
    for path, _old in census.session_pages:
        _atomic_write(path, _rewrite_page(path.read_text(encoding="utf-8"), []))
    for path, _old, new in census.other_pages:
        _atomic_write(path, _rewrite_page(path.read_text(encoding="utf-8"), new))

    # (c) — line-level event-log rewrite (unchanged lines byte-identical),
    # then rebuild the projected index from the corrected log.
    if census.none_records:
        lines = memory_log.read_text(encoding="utf-8").splitlines()
        out = [
            repl if repl is not None else line
            for line, repl in zip(lines, replacements)
        ]
        _atomic_write(memory_log, "\n".join(out) + "\n")
        from grove.memory.store import MemoryStore

        MemoryStore(base_dir=memory_log.parent)  # __init__ rebuilds the index
        print("memory_index.json rebuilt from the corrected event log.")

    print(f"\nEXECUTED: {total} change(s) applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
upstream-merge-tool-registration.py
===================================

Convert upstream hermes-agent tool-registration pattern to the grove
Sprint 53 Dispatcher-driven pattern.

Run this after merging upstream changes into ``tools/`` to absorb new or
re-touched tool modules without hand-editing each one. The script is
idempotent — running it twice produces the same result as running it once.

Upstream pattern (top-level side effects against a module-level singleton)::

    from tools.registry import registry, tool_error

    def my_tool(...): ...

    registry.register(
        name="my_tool",
        toolset="utility",
        schema=MY_SCHEMA,
        handler=my_tool,
        emoji="🔧",
    )
    registry.register_toolset_check("utility", _check_utility)

Grove pattern (router-resident; the Dispatcher owns the registry and
calls each module's ``register(reg)`` at construction time)::

    from tools.registry import tool_error

    def my_tool(...): ...

    def register(reg):
        \"\"\"Sprint 53 — Dispatcher-driven registration entrypoint.\"\"\"
        reg.register(
            name="my_tool",
            toolset="utility",
            schema=MY_SCHEMA,
            handler=my_tool,
            emoji="🔧",
        )
        reg.register_toolset_check("utility", _check_utility)

Reports for every file it touches: ``converted``, ``skipped`` (already
grove or no registry calls), or ``manual`` (mixed/unexpected layout that
the script declines to rewrite blindly).

Usage:

    python scripts/upstream-merge-tool-registration.py            # in-place
    python scripts/upstream-merge-tool-registration.py --dry-run  # report only
    python scripts/upstream-merge-tool-registration.py path/to/dir
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import List, Tuple

DOCSTRING = '"""Sprint 53 — Dispatcher-driven registration entrypoint."""'


def _is_top_level_registry_call(stmt: ast.stmt) -> bool:
    """``stmt`` is a top-level ``registry.<attr>(...)`` expression statement."""
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return False
    fn = stmt.value.func
    return (
        isinstance(fn, ast.Attribute)
        and isinstance(fn.value, ast.Name)
        and fn.value.id == "registry"
    )


def _exposes_grove_register(tree: ast.Module) -> bool:
    """True iff the module already exposes ``def register(reg):`` at top level."""
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.FunctionDef)
            and stmt.name == "register"
            and len(stmt.args.args) == 1
            and stmt.args.args[0].arg == "reg"
        ):
            return True
    return False


def _strip_registry_from_single_line_import(line: str) -> str | None:
    """Rewrite ``from tools.registry import ...`` to drop the bare ``registry``.

    Returns the rewritten line, or ``None`` if the line should be removed
    entirely (the import had only ``registry`` in its name list).
    Returns the original line unchanged when the pattern doesn't match.
    """
    # Skip parenthesized / continued imports — the caller flags those for
    # manual review.
    if "(" in line or line.rstrip().endswith("\\"):
        return line
    m = re.match(
        r'^(?P<indent>\s*)from\s+tools\.registry\s+import\s+(?P<names>[^#\n]+?)'
        r'(?P<trailer>\s*(?:#.*)?)$',
        line.rstrip("\n"),
    )
    if not m:
        return line
    names = [n.strip() for n in m["names"].split(",") if n.strip()]
    kept = [n for n in names if n != "registry"]
    if not kept:
        return None
    newline = "\n" if line.endswith("\n") else ""
    return f'{m["indent"]}from tools.registry import {", ".join(kept)}{m["trailer"]}{newline}'


def _has_multiline_registry_import(src: str) -> bool:
    """Detect ``from tools.registry import (\\n ... )`` or backslash continuation."""
    for m in re.finditer(r'from\s+tools\.registry\s+import\s+', src):
        tail = src[m.end():m.end() + 200]
        first_line = tail.split("\n", 1)[0]
        if "(" in first_line or first_line.rstrip().endswith("\\"):
            return True
    return False


def convert_file(path: Path, dry_run: bool = False) -> Tuple[str, str]:
    """Convert one module. Returns (status, message).

    Statuses:
      - converted : rewrote the file (or would have, if dry-run).
      - skipped   : already-grove or no registry calls — left untouched.
      - manual    : layout the script declines to rewrite; human review.
    """
    try:
        src = path.read_text()
    except UnicodeDecodeError:
        return ("manual", "binary or non-utf8 source")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        return ("manual", f"SyntaxError: {exc.msg}")

    calls = [s for s in tree.body if _is_top_level_registry_call(s)]
    is_grove = _exposes_grove_register(tree)

    if is_grove and not calls:
        return ("skipped", "already grove pattern")
    if not calls:
        return ("skipped", "no registry calls")
    if is_grove and calls:
        return ("manual", "mixed: def register(reg) + stray module-level calls")
    if _has_multiline_registry_import(src):
        return ("manual", "multi-line ``from tools.registry import (...)`` — rewrite by hand")

    src_lines = src.splitlines(keepends=True)
    calls = sorted(calls, key=lambda s: s.lineno)
    first = calls[0].lineno - 1
    last = calls[-1].end_lineno - 1

    call_lines: set = set()
    for c in calls:
        call_lines.update(range(c.lineno - 1, c.end_lineno))

    for idx in range(first, len(src_lines)):
        if idx in call_lines:
            continue
        stripped = src_lines[idx].strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        return (
            "manual",
            f"non-call code interleaved with registry block at line {idx + 1}: "
            f"{stripped[:60]}",
        )

    body_chunks: List[str] = []
    for c in calls:
        chunk = "".join(src_lines[c.lineno - 1: c.end_lineno])
        chunk = re.sub(r'^registry\.', 'reg.', chunk, count=1, flags=re.MULTILINE)
        chunk_indented = "\n".join(
            ("    " + ln if ln.strip() else ln) for ln in chunk.splitlines()
        )
        if chunk.endswith("\n"):
            chunk_indented += "\n"
        body_chunks.append(chunk_indented)

    body = "\n".join(body_chunks).rstrip() + "\n"

    new_block = f"def register(reg):\n    {DOCSTRING}\n{body}"

    head_lines = src_lines[:first]
    rewritten_head: List[str] = []
    for ln in head_lines:
        if "from tools.registry import" in ln and "registry" in ln:
            new_ln = _strip_registry_from_single_line_import(ln)
            if new_ln is None:
                continue
            rewritten_head.append(new_ln)
        else:
            rewritten_head.append(ln)

    head = "".join(rewritten_head).rstrip("\n") + "\n\n"
    new_src = head + new_block

    try:
        ast.parse(new_src)
    except SyntaxError as exc:
        return ("manual", f"post-rewrite SyntaxError: {exc.msg}")

    if dry_run:
        return ("converted", "would rewrite (dry-run)")
    path.write_text(new_src)
    return ("converted", "rewritten")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "tools_dir",
        nargs="?",
        default="tools",
        help="Directory of tool modules (default: tools/).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report changes without writing files.",
    )
    args = parser.parse_args()

    root = Path(args.tools_dir)
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    counts = {"converted": 0, "skipped": 0, "manual": 0}
    converted: List[Tuple[Path, str]] = []
    manual: List[Tuple[Path, str]] = []

    for path in sorted(root.glob("*.py")):
        if path.name == "registry.py":
            continue
        status, msg = convert_file(path, dry_run=args.dry_run)
        counts[status] += 1
        if status == "converted":
            converted.append((path, msg))
        elif status == "manual":
            manual.append((path, msg))

    print(
        f"converted: {counts['converted']}  "
        f"skipped: {counts['skipped']}  "
        f"manual: {counts['manual']}"
    )
    if converted:
        print("\nconverted:")
        for path, msg in converted:
            print(f"  {path}  ({msg})")
    if manual:
        print("\nmanual review:")
        for path, msg in manual:
            print(f"  {path}  ({msg})")
    return 0 if counts["manual"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

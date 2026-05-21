"""``hermes index`` CLI subcommand — operator control of the cellar
retrieval index (Sprint 13, rag-substrate-v1).

The cellar index normally builds lazily on first query and refreshes
incrementally by file mtime. ``hermes index rebuild`` forces a full
rebuild — the operator's escape hatch when the index drifts or after a
bulk change to ~/.grove/ (D4).
"""

from __future__ import annotations

import argparse


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Wire subcommands onto the ``hermes index`` parser."""
    parser.set_defaults(func=cmd_rebuild)  # bare `hermes index` → rebuild
    subs = parser.add_subparsers(dest="index_command", metavar="COMMAND")

    p_rebuild = subs.add_parser(
        "rebuild",
        help="Drop and fully rebuild the cellar retrieval index",
    )
    p_rebuild.set_defaults(func=cmd_rebuild)


def cmd_rebuild(_args: argparse.Namespace) -> int:
    """Force a full rebuild of the cellar retrieval index."""
    from grove.cellar import CellarIndex

    index = CellarIndex()
    count = index.build_index()
    print(f"Cellar index rebuilt — {count} file(s) indexed at {index.index_path}")
    return 0

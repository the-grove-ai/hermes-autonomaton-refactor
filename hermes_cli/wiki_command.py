"""``hermes wiki`` CLI subcommand — operator control of the living cellar
(Sprint K1, living-cellar-v1).

Mirrors ``hermes_cli/index_command.py``. Verbs:

* ``ingest <path>`` — a FILE matching a fleet glob compacts through that fleet
  adapter; a FILE with no glob match compacts as operator_curated; a DIR runs
  :func:`grove.wiki.watcher.scan_and_ingest` over the fleet sinks beneath it.
  With no path, scans the default fleet sinks.
* ``search <query> [--source-type T] [--dock-goal G] [-k N]`` — ranked
  retrieval over the cellar.
* ``rebuild`` — drop and fully rebuild the wiki FTS index.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Wire subcommands onto the ``hermes wiki`` parser."""
    parser.set_defaults(func=cmd_rebuild)  # bare `hermes wiki` → rebuild (safe, idempotent)
    subs = parser.add_subparsers(dest="wiki_command", metavar="COMMAND")

    p_ingest = subs.add_parser(
        "ingest",
        help="Compact a file (or scan a directory of fleet sinks) into the cellar",
    )
    p_ingest.add_argument(
        "path",
        nargs="?",
        default=None,
        help="A file to compact, or a directory to scan; default: the fleet sinks",
    )
    p_ingest.set_defaults(func=cmd_ingest)

    p_search = subs.add_parser("search", help="Search the cellar")
    p_search.add_argument("query", help="Free-text query")
    p_search.add_argument("--source-type", default=None, help="Filter by source_type")
    p_search.add_argument("--dock-goal", default=None, help="Boost pages for this Dock goal")
    p_search.add_argument("-k", type=int, default=5, help="Max results (default 5)")
    p_search.set_defaults(func=cmd_search)

    p_rebuild = subs.add_parser(
        "rebuild", help="Drop and fully rebuild the wiki retrieval index"
    )
    p_rebuild.set_defaults(func=cmd_rebuild)


def cmd_ingest(args: argparse.Namespace) -> int:
    """Compact a file, scan a directory, or scan the default fleet sinks."""
    from grove.wiki.watcher import ingest_file, scan_and_ingest

    raw = getattr(args, "path", None)
    if raw is None:
        pages = scan_and_ingest()
    else:
        path = Path(raw)
        if path.is_dir():
            pages = scan_and_ingest(hermes_home=path)
        elif path.is_file():
            # R1 (compaction-ingest-contract-v1): the file-branch now funnels
            # through the shared ingest_file gatekeeper, so it inherits the
            # mtime-ledger idempotency the directory scan already had. INTENDED
            # behavior change — a re-ingest of an unchanged file is now a no-op
            # (was: always recompacted, re-running the LLM every invocation).
            page = ingest_file(path)
            pages = [page] if page is not None else []
        else:
            print(f"error: no such file or directory: {path}")
            return 1

    if not pages:
        print("No new or changed documents to ingest.")
        return 0
    print(f"Ingested {len(pages)} page(s):")
    for page in pages:
        print(f"  [{page.source_type}] {page.path}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Ranked retrieval over the cellar."""
    from grove.wiki.index import WikiIndex

    index = WikiIndex()
    results = index.query(
        args.query,
        k=args.k,
        source_type=args.source_type,
        dock_goal=args.dock_goal,
    )
    if not results:
        print("No results.")
        return 0
    for r in results:
        print(
            f"[{r.relevance_score:.3f}] {r.source_type}  {r.title}  "
            f"(conf {r.confidence})  {r.source_path}"
        )
    return 0


def cmd_rebuild(_args: argparse.Namespace) -> int:
    """Force a full rebuild of the wiki retrieval index."""
    from grove.wiki.index import WikiIndex

    index = WikiIndex()
    count = index.build_index()
    print(f"Wiki index rebuilt — {count} page(s) indexed at {index.index_path}")
    return 0

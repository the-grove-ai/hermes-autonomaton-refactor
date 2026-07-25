#!/usr/bin/env python3
"""Operator authoring tool for config/rubrics.yaml (artifact-review-v1 R-14 /
GRV-001 §IV.I — a domain expert alters behavior by editing declarative config).

R-14 pins each published rubric version with a content_hash; the loader fails
loud on mismatch, so a version's MEANING is immutable. That immutability would
otherwise make hand-authoring impossible (you cannot compute a valid hash by
hand). This tool restores the operator authoring path:

  mint <source-key> <new-key>   copy an existing version to a NEW class@version
                                key with its content_hash cleared — the starting
                                point for a new version. Edit the new entry's
                                criteria/default_threshold, then run `stamp`.

  stamp                         fill content_hash for every entry MISSING one.
                                REFUSES to overwrite an existing hash — changing a
                                published version's meaning requires minting a new
                                version, never re-stamping. That refusal IS the
                                immutability guard.

Sovereignty note (GRV-001 §V): an operator who deletes a content_hash by hand and
re-stamps CAN change a published version's meaning. That is deliberate — R-14
prevents SILENT drift and machine mutation, not a sovereign edit in a reviewed
diff on a RED surface. No guard prevents it, by ruling.

This writes config/rubrics.yaml — a scope-defining RED surface. It is an OPERATOR
authoring action, landed by git commit, never an agent runtime write.
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

from ruamel.yaml import YAML

# The tool lives in scripts/; the registry is the repo config twin the loader
# resolves (grove/fleet/rubric_registry.py), so hash computation matches exactly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grove.fleet.rubric_registry import Criterion, compute_content_hash  # noqa: E402

_RUBRICS_PATH = Path(__file__).resolve().parents[1] / "config" / "rubrics.yaml"


def _yaml() -> YAML:
    y = YAML()  # round-trip: preserves comments, key order, quoting
    y.preserve_quotes = True
    return y


def _entry_hash(entry) -> str:
    criteria = [
        Criterion(id=c["id"], type=c["type"], definition=c["definition"])
        for c in entry["criteria"]
    ]
    return compute_content_hash(entry["default_threshold"], criteria)


def stamp(doc) -> list:
    """Fill content_hash for entries missing one; NEVER overwrite an existing
    hash (immutability guard). Returns the list of newly-stamped keys. Warns on
    an existing hash that does not match its criteria (a hand-edited published
    version) but leaves it untouched — the operator must mint to change meaning."""
    stamped = []
    for key, entry in doc["rubrics"].items():
        existing = entry.get("content_hash")
        expected = _entry_hash(entry)
        if existing:
            if existing != expected:
                print(
                    f"WARN {key}: content_hash does not match its criteria "
                    f"(declared {existing}, criteria hash {expected}). Refusing to "
                    f"overwrite — mint a new version to change a published meaning.",
                    file=sys.stderr,
                )
            continue
        entry["content_hash"] = expected
        stamped.append(key)
    return stamped


def mint(doc, source_key: str, new_key: str) -> None:
    """Copy an existing version to a new class@version key with the hash cleared.
    The operator edits the new entry's criteria, then runs `stamp`."""
    rubrics = doc["rubrics"]
    if source_key not in rubrics:
        raise KeyError(f"source key {source_key!r} not in config/rubrics.yaml")
    if new_key in rubrics:
        raise KeyError(f"new key {new_key!r} already exists — pick an unused version")
    entry = copy.deepcopy(rubrics[source_key])
    entry.pop("content_hash", None)  # cleared: edit criteria, then stamp
    rubrics[new_key] = entry


def _load(path: Path):
    y = _yaml()
    return y, y.load(path.read_text(encoding="utf-8"))


def _save(y, doc, path: Path) -> None:
    import io

    buf = io.StringIO()
    y.dump(doc, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stamp", help="fill missing content_hashes (never overwrites)")
    m = sub.add_parser("mint", help="copy a version to a new class@version key")
    m.add_argument("source_key")
    m.add_argument("new_key")
    parser.add_argument(
        "--path", type=Path, default=_RUBRICS_PATH, help="rubrics.yaml path"
    )
    args = parser.parse_args(argv)

    y, doc = _load(args.path)
    if args.cmd == "stamp":
        stamped = stamp(doc)
        _save(y, doc, args.path)
        print("stamped: " + (", ".join(stamped) if stamped else "(nothing missing)"))
    elif args.cmd == "mint":
        mint(doc, args.source_key, args.new_key)
        _save(y, doc, args.path)
        print(
            f"minted {args.new_key} from {args.source_key} (hash cleared); "
            f"edit its criteria, then run: rubric_tool.py stamp"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

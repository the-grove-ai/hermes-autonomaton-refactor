"""Atomic draft staging + terminal-state events (background-worker-runtime-v1).

Two write primitives, both atomic by the same discipline — write a ``.tmp``
sibling, fsync, then ``os.rename`` into place (atomic within a filesystem). A
reader (the operator's portal, the ticker's reap) never sees a torn file.

``stage_draft`` additionally enforces a GENERIC PATH-JAIL: the filename is
reduced to ``os.path.basename`` (rejecting ``..`` / absolute / separator
traversal) and the destination directory is fixed by the caller to the record's
declared sink — a skill can name a file but can never escape its sink.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from grove.fleet.errors import FleetWorkerAndon


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    """Write *data* to *dest* atomically via ``<name>.tmp`` -> ``os.rename``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.rename(tmp, dest)


def stage_draft(sink_dir: Path, filename: str, content: str) -> Path:
    """Atomically stage *content* as a draft in *sink_dir*.

    GENERIC PATH-JAIL: *filename* is reduced to its basename and validated, so a
    skill-supplied ``../../etc/passwd`` or ``/abs/path`` collapses to a bare name
    written inside *sink_dir*. The destination is *sink_dir* and nothing else.
    """
    base = os.path.basename(filename.strip())
    if not base or base in (".", "..") or os.sep in base or (os.altsep and os.altsep in base):
        raise FleetWorkerAndon(
            f"draft filename {filename!r} does not reduce to a safe basename — "
            f"refusing to stage (path-jail)",
            check="path_escape",
        )
    dest = Path(sink_dir) / base
    _atomic_write_bytes(dest, content.encode("utf-8"))
    return dest


def write_terminal_event(dest: Path, event: Dict[str, Any]) -> Path:
    """Atomically write a worker's terminal-state event to the bus sink.

    Called BEFORE the worker exits so the ticker's reap always finds either a
    valid terminal event (done) or none at all (catastrophic) — never a partial.
    """
    _atomic_write_bytes(
        Path(dest), json.dumps(event, ensure_ascii=False, indent=2).encode("utf-8")
    )
    return Path(dest)

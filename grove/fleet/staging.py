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
import re
from pathlib import Path
from typing import Any, Dict, List

from grove.fleet.errors import FleetWorkerAndon

# A package slug is a model-influenced path component; constrain it to a safe
# filesystem slug so it cannot introduce separators or traversal.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


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


def stage_package(sink_dir: Path, slug: str, files: Dict[str, str]) -> List[Path]:
    """Atomically stage a multi-file package into ``sink_dir/<slug>/`` (Option 2).

    The runtime — never the skill — owns staging into pending_review, so the
    skill's output is written atomically (tmp -> os.rename) and cannot corrupt a
    half-written file the portal reads. TWO-LEVEL PATH-JAIL: the ``slug`` is a
    model-influenced path component, so it is basename-reduced, validated as a
    safe slug, and its resolved directory is asserted ``is_relative_to`` the sink
    (write-side jail, parity with the portal read-side jail). Each filename is
    basename-jailed too. Returns the staged file paths.
    """
    sink = Path(sink_dir).resolve()
    raw_slug = os.path.basename((slug or "").strip())
    if not raw_slug or not _SLUG_RE.match(raw_slug):
        raise FleetWorkerAndon(
            f"package slug {slug!r} is not a safe slug (^[a-z0-9][a-z0-9._-]*$) — "
            f"refusing to stage",
            check="path_escape",
        )
    slug_dir = (sink / raw_slug).resolve()
    if not slug_dir.is_relative_to(sink):
        raise FleetWorkerAndon(
            f"package slug {slug!r} escapes the declared sink {sink} — refusing",
            check="path_escape",
        )
    if not isinstance(files, dict) or not files:
        raise FleetWorkerAndon(
            "fleet_package carries no files — nothing to stage", check="empty_package"
        )

    staged: List[Path] = []
    for fname, content in files.items():
        base = os.path.basename(str(fname).strip())
        if not base or base in (".", "..") or os.sep in base or (os.altsep and os.altsep in base):
            raise FleetWorkerAndon(
                f"package filename {fname!r} does not reduce to a safe basename",
                check="path_escape",
            )
        dest = (slug_dir / base).resolve()
        if not dest.is_relative_to(sink):
            raise FleetWorkerAndon(
                f"package file {fname!r} escapes the declared sink — refusing",
                check="path_escape",
            )
        _atomic_write_bytes(dest, str(content).encode("utf-8"))
        staged.append(dest)
    return staged


def write_terminal_event(dest: Path, event: Dict[str, Any]) -> Path:
    """Atomically write a worker's terminal-state event to the bus sink.

    Called BEFORE the worker exits so the ticker's reap always finds either a
    valid terminal event (done) or none at all (catastrophic) — never a partial.
    """
    _atomic_write_bytes(
        Path(dest), json.dumps(event, ensure_ascii=False, indent=2).encode("utf-8")
    )
    return Path(dest)

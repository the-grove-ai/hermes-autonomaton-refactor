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
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from grove.fleet.errors import FleetWorkerAndon

logger = logging.getLogger(__name__)

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

    # researcher-fleet-worker-v1 P2 (F7/A-4) — CLEAN-ROOM: a slug dir is owned
    # by exactly ONE run's package. Wipe any prior contents (a killed run's
    # partial files) so a later package can never absorb a stray from an
    # earlier run.
    if slug_dir.exists():
        shutil.rmtree(slug_dir)

    # meta.json is written LAST: the staged-unit index keys on meta.json
    # presence, so a package killed mid-loop is INVISIBLE to skip_already_staged
    # and simply re-staged by the next run into a wiped clean room. Stable sort:
    # non-meta files keep their emission order.
    ordered = sorted(
        files.items(),
        key=lambda kv: os.path.basename(str(kv[0]).strip()) == "meta.json",
    )
    staged: List[Path] = []
    for fname, content in ordered:
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


def _resolve_dispatched_unit_id(worker_id: str, run_id: str) -> Optional[str]:
    """The host-minted unit identity for a run, for a synthetic receipt.

    Primary: the C1 genesis dispatch record. Fallback: the inbox — needed ONLY
    for a pre-C1 orphan on the first boot after deploy (a pidfile whose run
    predates the dispatch record). Distinguishable cleanly: ``read_dispatch_record``
    returns None when the record is absent. None when neither is readable — the
    receipt is still written, keyed by run, identity null.
    """
    from grove.fleet.runner import read_dispatch_record

    rec = read_dispatch_record(worker_id, run_id)
    if rec is not None:
        return rec.unit_id
    from grove.fleet.worker_entry import _dispatched_unit_id, _read_inbox_payload

    try:
        payload = _read_inbox_payload(worker_id, run_id)
    except Exception:  # noqa: BLE001 — no inbox either -> identity simply null
        return None
    return _dispatched_unit_id(payload)


def write_synthetic_receipt(
    worker_id: str,
    run_id: str,
    *,
    check: str,
    detail: str,
    loop: Optional[Any] = None,
) -> Optional[Path]:
    """The ONE synthetic-receipt writer, shared by the runner, the manager poll,
    and the boot sweep (fleet-receipt-custody-v1 C2b).

    Writes a terminal ``failed`` receipt carrying the dispatched ``unit_id``,
    keyed by ``run_id`` at the same event path a worker would use — so a
    kill/crash that left no receipt becomes unit-attributable and countable.

    NO-CLOBBER: the receipt shares the event path with a worker-written one, so
    if a receipt already exists this is a no-op (returns None). A worker that
    wrote its own richer record (with P1.2C identity) wins; the atomic
    ``write_terminal_event`` guarantees any existing file is whole, never torn.

    The check STRING is the fact (what happened). Whether a class counts against
    the retry cap is config the operator rules in YAML, NEVER baked into the
    record — a receipt names the fact, not the policy.
    """
    from grove.fleet.paths import event_path

    dest = event_path(worker_id, run_id)
    if dest.exists():
        logger.info(
            "[fleet.staging] receipt already exists for %s/%s — skipping synthetic %s",
            worker_id,
            run_id,
            check,
        )
        return None

    unit_id = _resolve_dispatched_unit_id(worker_id, run_id)
    from grove.fleet.worker_entry import _event

    event = _event(
        worker_id,
        run_id,
        worker_id,  # skill_id: the worker id is the honest identifier in scope
        "failed",
        detail=detail,
        check=check,
        unit_id=unit_id,
    )
    return write_terminal_event(dest, event)

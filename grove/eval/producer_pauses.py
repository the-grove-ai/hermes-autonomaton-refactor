"""Producer pause state — detector-sweep-resilience-v1 P2 (R-3a write side, R-6).

The operator-mutable pause set the Dispatcher's shared producer guard
(:func:`grove.dispatcher._run_guarded_producer`) consults once per producer
per dormancy sweep. ``set_producer_pause`` is the SOLE sanctioned writer;
``read_producer_pauses`` the reader the P1 ``_paused_producers()`` seam
delegates to. P3's recurrence-card approve flow is the writer's caller.

On-disk: ``~/.grove/flywheel/producer_pauses.yaml`` —

    producers:
      <name>:
        paused: bool
        proposal_id: str | null
        reason: str | null
        updated_at: ISO-8601

R-6: this is a NEW sovereign state file keyed on PRODUCER names, not
capability record ids — the capability-state allowlist
(``grove.capability_registry``) is untouched. The writer copies
``set_publication_state``'s discipline verbatim (fcntl LOCK_EX|LOCK_NB →
read prior → ``.bak`` → tempfile+fsync+os.replace → defer-on-contention);
the reader is fresh-per-call and read-resilient (the writer cannot produce
a malformed file; a hand-edit that does logs WARNING and reads empty —
producers keep running, a broken pause file must never pause the fleet
of detectors by accident or exception).
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

import yaml

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

logger = logging.getLogger(__name__)

__all__ = ["default_pauses_path", "read_producer_pauses", "set_producer_pause"]


def default_pauses_path() -> Path:
    """Resolve ``~/.grove/flywheel/producer_pauses.yaml`` via hermes_home."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "flywheel" / "producer_pauses.yaml"


def _atomic_write_yaml(path: Path, text: str) -> None:
    """tempfile + fsync + os.replace — the capability_registry discipline."""
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".pause_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def set_producer_pause(
    producer: str,
    paused: bool,
    *,
    proposal_id: Optional[str] = None,
    reason: Optional[str] = None,
    path: Optional[Path] = None,
) -> str:
    """The SOLE sanctioned writer for the producer pause set.

    Sets ``producers.<producer>`` to ``{paused, proposal_id, reason,
    updated_at}``, preserving every other producer's entry. Lock + ``.bak``
    + atomic-replace discipline copied verbatim from
    :func:`grove.capability_registry.set_publication_state`.

    WRITE-STRICT (fail loud): rejects a blank *producer* and a non-bool
    *paused* (explicit ``isinstance`` — bool is an int subclass; a pause is
    set, never inferred from a truthy).

    Files a ``producer_paused`` audit event AFTER the file mutation lands.
    Audit-filing failure floors to ``logger.error`` WITHOUT re-raise: this
    is a FILE-BACKED writer (the mutation already landed atomically), so
    the set_model_binding precedent applies — the H2-ratified fail-loud
    inversion is for ledger-IS-the-mutation writers only.

    Returns ``"applied"`` or ``"deferred"`` (lock contended — caller retries).
    """
    if not isinstance(producer, str) or not producer.strip():
        raise ValueError(
            "set_producer_pause: producer must be a non-empty string"
        )
    if not isinstance(paused, bool):
        raise ValueError(
            "set_producer_pause: paused must be a real bool (True/False), "
            f"got {type(paused).__name__}"
        )
    producer = producer.strip()
    pauses_path = path or default_pauses_path()

    def _apply() -> str:
        prior: Dict[str, Any] = {}
        if pauses_path.exists():
            try:
                loaded = yaml.safe_load(pauses_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    prior = loaded
            except yaml.YAMLError:
                prior = {}  # torn prior; .bak below retains the bytes
        producers = prior.get("producers")
        if not isinstance(producers, dict):
            producers = {}
        producers = dict(producers)
        producers[producer] = {
            "paused": paused,
            "proposal_id": proposal_id,
            "reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        merged = dict(prior)
        merged["producers"] = producers
        pauses_path.parent.mkdir(parents=True, exist_ok=True)
        prior_bytes = pauses_path.read_bytes() if pauses_path.exists() else b""
        if prior_bytes:
            pauses_path.with_suffix(
                pauses_path.suffix + ".bak"
            ).write_bytes(prior_bytes)
        _atomic_write_yaml(
            pauses_path,
            yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
        )
        _file_pause_audit(producer, paused, proposal_id, reason)
        return "applied"

    if fcntl is None:  # pragma: no cover - non-POSIX best-effort
        return _apply()

    pauses_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = pauses_path.with_suffix(".yaml.lock")
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return "deferred"
        try:
            return _apply()
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


def _file_pause_audit(
    producer: str,
    paused: bool,
    proposal_id: Optional[str],
    reason: Optional[str],
) -> None:
    """File the ``producer_paused`` audit event (error-log floor — the file
    mutation already landed; a filing failure must not unwind or misreport it)."""
    try:
        from grove.kaizen_ledger import KaizenLedger

        session = "pause-" + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        KaizenLedger(session_id=session).record(
            "producer_paused",
            producer=producer,
            paused=paused,
            proposal_id=proposal_id,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 — audit leg, log floor stands
        logger.error(
            "[producer_pauses] producer_paused audit filing failed (the pause "
            "file mutation above stands): %r", exc,
        )


def read_producer_pauses(path: Optional[Path] = None) -> FrozenSet[str]:
    """The paused producer names, read FRESH on every call (no cache).

    Missing file → empty set (P2 ships inert). Malformed file → WARNING +
    empty set (read-resilient: the sanctioned writer cannot produce one, so
    malformed means a hand-edit — producers keep running while the operator
    repairs it; a broken pause file must never halt the sweep).
    """
    pauses_path = path or default_pauses_path()
    if not pauses_path.exists():
        return frozenset()
    try:
        loaded = yaml.safe_load(pauses_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        logger.warning(
            "[producer_pauses] pause file unreadable at %s (%r) — treating "
            "as empty; repair or rewrite via set_producer_pause",
            pauses_path, exc,
        )
        return frozenset()
    producers = loaded.get("producers") if isinstance(loaded, dict) else None
    if not isinstance(producers, dict):
        if loaded is not None:
            logger.warning(
                "[producer_pauses] pause file at %s has no 'producers' "
                "mapping — treating as empty", pauses_path,
            )
        return frozenset()
    paused = set()
    for name, entry in producers.items():
        if isinstance(entry, dict) and entry.get("paused") is True:
            paused.add(str(name))
    return frozenset(paused)

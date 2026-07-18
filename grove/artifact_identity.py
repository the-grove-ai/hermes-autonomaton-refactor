"""artifact-identity-v1 C1 — the artifact identity derivation primitive.

One artifact, one ID: ``sha256(canonical path)[:16]``. The cellar already
derives its page filename hash as ``sha256(page.source)[:8]`` over the SAME
input (grove/wiki/pipeline.py::_write_page), so an artifact_id's first 8 hex
chars prefix-join to the cellar's short hash for the same source file — the
two derivations can be joined without a mapping table.

Canonicalization contract (byte-identical to the cellar's derivation input):
the cellar hashes ``str(path)`` AS PASSED — the watcher feeds it
``get_hermes_home()/sink_dir/filename``, an absolute UNRESOLVED path. No
``realpath`` is applied there, and none may be applied here: on the VM,
``~/.grove`` is a symlink (→ /mnt/grove-data), so realpath would produce a
different byte string and silently break the prefix join. This is the
deliberate divergence from the ENFORCEMENT canonicalizer
(grove.utils.fs_utils._canonical_write_target), which realpaths to defeat
symlink escapes. Enforcement resolves symlinks; identity preserves them.

Generic by construction: no skill names, no surface names — path in, ID out.
"""

from __future__ import annotations

import hashlib
import os

# 16 hex chars — a deliberate superset of the cellar's 8 (wiki/pipeline.py
# _HASH_LEN): artifact_id[:8] == the cellar short hash for the same path.
_ARTIFACT_ID_LEN = 16


def canonical_artifact_path(path: str) -> str:
    """The canonical-path form artifact identity hashes over.

    ``expanduser`` + ``abspath`` ONLY — never ``realpath`` (see module
    docstring: the cellar hashes the unresolved form; resolving symlinks
    here would break the prefix join on any symlinked home).
    """
    return os.path.abspath(os.path.expanduser(str(path)))


def artifact_id(canonical_path: str) -> str:
    """Derive the 16-hex artifact ID for an already-canonical path string.

    Callers canonicalize first via :func:`canonical_artifact_path`; this
    function hashes the given bytes verbatim so the derivation stays
    byte-identical to the cellar's ``sha256(page.source)`` input.
    """
    return hashlib.sha256(
        canonical_path.encode("utf-8")
    ).hexdigest()[:_ARTIFACT_ID_LEN]


def emit_approved_artifact_written(
    tool_name: str, write_targets: list, payload: dict,
) -> list:
    """artifact-continuation-v1 P2 (1e/1f ruling) — file ``artifact_written``
    for an APPROVED stored write at confirm time.

    The approved re-dispatch path (``approve_red_proposal`` →
    ``registry.dispatch``) never enters the Dispatcher, so the identity seam
    never sees the write; this core helper closes that gap. The surface
    (portal confirm handler) only invokes — capability stays here.

    ``write_targets`` come from the seam's own extraction machinery
    (``extract_write_targets``, returned by ``approve_red_proposal``);
    ``payload`` is the queue-row carrier holding the ORIGINAL minting turn's
    identity context. Rows without the carrier keys (pre-existing shape) emit
    with honest defaults (null / []). Write-strict/read-resilient: a
    per-target emission failure loud-logs and continues — it never fails the
    confirm that already executed.

    Returns the list of persisted event dicts (empty on no targets / all
    failures).
    """
    import logging

    from grove.kaizen_ledger import KaizenLedger

    _logger = logging.getLogger(__name__)
    payload = payload or {}
    turn_id = payload.get("turn_id")
    # File under the minting turn's session ledger when derivable from the
    # standard "{session_id}#{n}" turn-id shape, else a dedicated stream.
    session = (
        turn_id.split("#", 1)[0]
        if isinstance(turn_id, str) and "#" in turn_id
        else None
    ) or "approved_writes"
    events: list = []
    for target in list(write_targets or []):
        try:
            canonical = canonical_artifact_path(target)
            events.append(
                KaizenLedger(session).record(
                    "artifact_written",
                    path=canonical,
                    artifact_id=artifact_id(canonical),
                    turn_id=turn_id,
                    active_primary_skill_slug=payload.get(
                        "active_primary_skill_slug"
                    ),
                    intent_class=payload.get("intent_class"),
                    tool=tool_name,
                    parent_artifact_ids=list(
                        payload.get("parent_artifact_ids") or []
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 — telemetry-only
            _logger.warning(
                "[artifact-identity] approved-write EMISSION failed (identity "
                "telemetry only — the approved write itself stands): "
                "target=%r error=%r", target, exc,
            )
    return events

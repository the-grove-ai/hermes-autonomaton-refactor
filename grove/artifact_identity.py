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

"""Standing grant storage — mtime-cached loader for ~/.grove/grants.yaml.

GrantStore is the runtime interface for persisted operator standing grants.
Implicit (T0) grants are ephemeral and live only in the per-turn kaizen
handler closure; they never touch this module. GrantStore handles:

  * load()              — mtime-cached YAML load; fail-closed on any error
  * get_grant()         — exact (scope, write_class) lookup; revoked grants excluded
  * add_standing_grant()— write a new standing grant (operator-initiated only)
  * revoke_grant()      — set revoked=True and rewrite file
  * list_grants()       — active (non-revoked) standing grants

Scope protection invariant: get_grant() requires EXACT string equality on
both scope and write_class. No wildcard, no prefix, no substring match.
A grant for "grove-site-fetch / andon_promote" does NOT cover any other
skill or any other verb.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from grove.grant_recognition import GrantToken

_GRANTS_FILENAME = "grants.yaml"


class GrantStore:
    """Mtime-cached reader/writer for the operator standing-grant manifest.

    The live manifest is expected at ``~/.grove/grants.yaml`` (GROVE_HOME).
    If the file does not exist, all operations are fail-closed (empty list,
    no grants found) — the operator must provision the file before standing
    grants can be used.
    """

    def __init__(self, grants_path: Optional[Path] = None) -> None:
        if grants_path is None:
            grants_path = Path.home() / ".grove" / _GRANTS_FILENAME
        self._path = Path(grants_path)
        self._mtime_ns: Optional[int] = None
        self._grants: list[GrantToken] = []

    # ── read ──────────────────────────────────────────────────────────────────

    def load(self) -> list[GrantToken]:
        """Load grants from disk using mtime cache. Fail-closed."""
        try:
            mtime = os.stat(self._path).st_mtime_ns
        except OSError:
            return []
        if mtime == self._mtime_ns:
            return self._grants
        try:
            import yaml

            with open(self._path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            grants: list[GrantToken] = []
            for entry in data.get("grants", []):
                if not isinstance(entry, dict):
                    continue
                grants.append(
                    GrantToken(
                        id=str(entry.get("id") or f"grant-{uuid4().hex[:8]}"),
                        source=str(entry.get("source") or "standing"),
                        scope=str(entry.get("scope") or ""),
                        write_class=str(entry.get("write_class") or ""),
                        disposition=str(entry.get("disposition") or "standing"),
                        issued_at=str(entry.get("issued_at") or ""),
                        authorized_by=str(entry.get("authorized_by") or ""),
                        revoked=bool(entry.get("revoked", False)),
                    )
                )
            self._grants = grants
            self._mtime_ns = mtime
        except Exception:
            self._grants = []
        return self._grants

    def get_grant(self, scope: str, write_class: str) -> Optional[GrantToken]:
        """Return the first active standing grant matching the EXACT (scope, write_class) pair.

        Exact string equality is required on both fields — no wildcard, no prefix
        match, no substring. A grant for "grove-site-fetch / andon_promote" does
        not authorize any other skill or verb.
        """
        for g in self.load():
            if not g.revoked and g.scope == scope and g.write_class == write_class:
                return g
        return None

    def list_grants(self) -> list[GrantToken]:
        """Return all active (non-revoked) standing grants."""
        return [g for g in self.load() if not g.revoked]

    # ── write ─────────────────────────────────────────────────────────────────
    # These methods write to ~/.grove/grants.yaml, which is a scope-defining
    # surface. They are called ONLY from operator-authenticated code paths
    # (sovereignty prompt "Always" selection or grant management CLI). The
    # agent cannot call them on the autonomous loop — that path never reaches
    # add_standing_grant() without an operator disposition choice.

    def add_standing_grant(self, grant: GrantToken) -> None:
        """Append a new standing grant to the manifest and rewrite the file.

        Deduplicates on (scope, write_class): if an active grant with the same
        pair already exists, the existing grant is returned unchanged (idempotent).
        """
        existing = self.get_grant(grant.scope, grant.write_class)
        if existing is not None:
            return
        if not grant.issued_at:
            grant.issued_at = datetime.now(timezone.utc).isoformat()
        if grant.source != "standing":
            grant = GrantToken(
                id=grant.id,
                source="standing",
                scope=grant.scope,
                write_class=grant.write_class,
                timestamp=grant.timestamp,
                disposition="standing",
                issued_at=grant.issued_at,
                authorized_by=grant.authorized_by,
                revoked=False,
            )
        current = self.load()
        self._rewrite(current + [grant])

    def revoke_grant(self, grant_id: str) -> bool:
        """Mark a grant revoked by ID and rewrite the file. Returns True if found."""
        grants = self.load()
        found = False
        updated: list[GrantToken] = []
        for g in grants:
            if g.id == grant_id and not g.revoked:
                updated.append(
                    GrantToken(
                        id=g.id,
                        source=g.source,
                        scope=g.scope,
                        write_class=g.write_class,
                        timestamp=g.timestamp,
                        disposition=g.disposition,
                        issued_at=g.issued_at,
                        authorized_by=g.authorized_by,
                        revoked=True,
                    )
                )
                found = True
            else:
                updated.append(g)
        if found:
            self._rewrite(updated)
        return found

    def _rewrite(self, grants: list[GrantToken]) -> None:
        """Serialize grants list to YAML and write atomically."""
        import yaml

        data = {
            "schema_version": "1.0",
            "grants": [
                {
                    "id": g.id,
                    "source": g.source,
                    "scope": g.scope,
                    "write_class": g.write_class,
                    "disposition": g.disposition,
                    "issued_at": g.issued_at,
                    "authorized_by": g.authorized_by,
                    "revoked": g.revoked,
                }
                for g in grants
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".yaml.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
        tmp.replace(self._path)
        # Invalidate cache so next load() reads fresh data.
        self._mtime_ns = None
        self._grants = []


# Module-level singleton — resolved on first use, lazy import of grants path.
_store: Optional[GrantStore] = None


def get_grant_store(grants_path: Optional[Path] = None) -> GrantStore:
    """Return the module-level GrantStore singleton (creates on first call)."""
    global _store
    if _store is None or grants_path is not None:
        _store = GrantStore(grants_path)
    return _store

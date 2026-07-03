"""Deterministic, agentless publish of an approved application package.

``publish_application_package`` is the sibling of
``grove.utils.fs_utils.promote_artifact`` (cellar-only): the SAME shape —
surface-agnostic (no session / platform / surface identifier), deterministic, a
single non-model door, unreachable by the agent. Where ``promote_artifact`` moves
an approved artifact into the cellar, this one publishes the approved resume +
cover letter into a row-keyed Drive folder as native Google Docs and appends an
audit line.

SCOPE (forge-jobsearch-v1 Phase 2): Drive mechanics only. The Notion state update
— write the folder link to the row's "Application Package" URL and flip Status
"To Apply" -> "Drafted" — is a post-execution lifecycle event owned by the Phase-4
portal action handler via the LIVE Notion MCP substrate. It is NOT done here. The
legacy ``ntn_`` "Hermes" integration token is deprecated and intentionally
untouched. This function returns the folder link + per-doc links + audit record
the handler needs; it never speaks to Notion.

FAIL-LOUD: any Drive step failure raises :class:`PublishError` carrying the exact
partial state (which folder / docs were created) — no swallow, no silent
workaround, no partial success reported as success. Step order is chosen so the
only external effects are Drive creations; there is no cross-service sub-partial.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from hermes_constants import get_hermes_home

_DOC_MIME = "application/vnd.google-apps.document"
_FOLDER_MIME = "application/vnd.google-apps.folder"

# Adapter signature: (service, action, positional, flags) -> parsed JSON dict/list.
GapiFn = Callable[[str, str, list, dict], Any]


class PublishError(RuntimeError):
    """A Drive step failed. Carries ``partial_state`` so the caller can see
    exactly how far the publish got (folder created? which docs uploaded?)."""

    def __init__(self, message: str, partial_state: Dict[str, Any]):
        super().__init__(message)
        self.partial_state = dict(partial_state)

    def to_dict(self) -> Dict[str, Any]:
        return {"error": str(self), "partial_state": self.partial_state}


def _q(value: str) -> str:
    """Escape a value into a single-quoted Drive-query literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _short(row_id: str) -> str:
    return row_id.replace("-", "")[:8]


def folder_name(company: str, role: str, row_id: str) -> str:
    """Deterministic, human-readable, row-keyed folder name. The ``[short]``
    suffix makes it unique per row, so the idempotency guard is exact."""
    return f"JobApp — {company} — {role} [{_short(row_id)}]"


def _default_gapi(service: str, action: str, positional: list, flags: dict) -> Any:
    """Production adapter: the deterministic non-model door to google_api.py.

    Reuses ``tools.google_workspace_tool._run_gapi`` (subprocess into the gateway
    venv with the live ``GROVE_HOME`` OAuth token) and parses its JSON. On the
    deployed gateway the deploy-provisioned ~/.grove script carries the
    ``--convert-to-doc`` flag added in Phase 1.
    """
    from tools.google_workspace_tool import _run_gapi

    raw = _run_gapi(service, action, list(positional), dict(flags))
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:  # non-JSON is itself an Andon, surfaced
        raise PublishError(
            f"google_api.py {service} {action} returned non-JSON: {raw[:200]!r}", {}
        ) from exc


def _require_file(result: Any, step: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """A google_api.py verb prints an object carrying an ``id`` on success;
    ``_run_gapi`` wraps any failure as ``{"error": ...}``. Anything else is an
    Andon that names the failing step and the partial state."""
    if not isinstance(result, dict) or result.get("error") or "id" not in result:
        raise PublishError(f"{step} failed: {json.dumps(result)[:300]}", state)
    return result


def _audit_path() -> Path:
    return get_hermes_home() / "forge" / "published.jsonl"


def publish_application_package(
    row_id: str,
    company: str,
    role: str,
    resume_path: str,
    cover_letter_path: str,
    *,
    gapi: Optional[GapiFn] = None,
    audit_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Publish resume + cover letter into a row-keyed Drive folder as native Docs.

    Args mirror the locked contract; ``gapi`` and ``audit_path`` are keyword-only
    testability seams with production defaults (the real ``_run_gapi`` adapter and
    ``$GROVE_HOME/forge/published.jsonl``) — they do not change the single-door
    shape.

    Returns one of:
      * ``{"status": "exists", "created": False, "row_id", "folder_id",
        "folder_link"}`` — idempotency guard hit; nothing was created.
      * ``{"status": "published", "created": True, "row_id", "folder_id",
        "folder_link", "docs": {"resume": {"id","link"},
        "cover_letter": {"id","link"}}, "audit": {...}}`` — full success.

    Raises :class:`PublishError` (carrying ``.partial_state``) on any Drive
    failure. The Notion update is handler-owned and not attempted here.
    """
    gapi = gapi or _default_gapi
    audit_file = Path(audit_path) if audit_path else _audit_path()

    state: Dict[str, Any] = {
        "folder_id": None,
        "folder_link": None,
        "docs": {},
        "notion": "handler-owned (Phase 4, MCP)",
    }

    assets = (("resume", resume_path), ("cover_letter", cover_letter_path))

    # Validate inputs BEFORE any external effect — fail loud, create nothing.
    for label, path in assets:
        if not Path(path).expanduser().is_file():
            raise PublishError(f"{label} file not found: {path}", state)

    name = folder_name(company, role, row_id)

    # 1. IDEMPOTENCY GUARD — exact row-keyed folder, not trashed. No destructive
    #    verb on the happy path: an existing package is reported, never replaced.
    raw_q = f"name = {_q(name)} and mimeType = '{_FOLDER_MIME}' and trashed = false"
    found = gapi("drive", "search", [raw_q], {"--raw-query": True, "--max": 5})
    if isinstance(found, dict) and found.get("error"):
        raise PublishError(f"idempotency search failed: {found['error']}", state)
    for hit in found if isinstance(found, list) else []:
        if hit.get("name") == name:
            return {
                "status": "exists",
                "created": False,
                "row_id": row_id,
                "folder_id": hit.get("id"),
                "folder_link": hit.get("webViewLink"),
            }

    # 2. Create the row-keyed folder.
    folder = _require_file(
        gapi("drive", "create-folder", [name], {}), "create_folder", state
    )
    state["folder_id"] = folder["id"]
    state["folder_link"] = folder.get("webViewLink")

    # 3. Each asset -> native Google Doc inside the folder (Phase-1 mechanism).
    titles = {
        "resume": f"{company} — {role} — Resume",
        "cover_letter": f"{company} — {role} — Cover Letter",
    }
    for label, path in assets:
        result = _require_file(
            gapi(
                "drive",
                "upload",
                [str(Path(path).expanduser())],
                {
                    "--parent": state["folder_id"],
                    "--name": titles[label],
                    "--convert-to-doc": True,
                    "--mime-type": "text/markdown",
                },
            ),
            f"upload_{label}",
            state,
        )
        if result.get("mimeType") != _DOC_MIME:
            raise PublishError(
                f"upload_{label} did not convert to a native Doc "
                f"(mimeType={result.get('mimeType')!r})",
                state,
            )
        state["docs"][label] = {"id": result["id"], "link": result.get("webViewLink")}

    # 4. Notion state update is handler-owned (Phase 4, live MCP) — not here.

    # 5. AUDIT — append-only structured record. This is the audit trail, NOT a
    #    Kaizen proposal-ledger entry.
    audit = {
        "operator_initiated": True,
        "row_id": row_id,
        "company": company,
        "role": role,
        "folder_link": state["folder_link"],
        "docs": state["docs"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "disposition": "published",
    }
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    with audit_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(audit, ensure_ascii=False) + "\n")

    return {
        "status": "published",
        "created": True,
        "row_id": row_id,
        "folder_id": state["folder_id"],
        "folder_link": state["folder_link"],
        "docs": state["docs"],
        "audit": audit,
    }

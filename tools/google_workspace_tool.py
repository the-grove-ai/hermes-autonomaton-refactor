"""Google Workspace native tools — first-class verbs over scripts/google_api.py.

workspace-first-class-capability-v1 (Option B, GATE-B locked: native tools, not
MCP). 24 per-verb tools, callable at ALL tiers — native tools are not MCP, so
routing.config ``exclude_mcp`` never drops them (the whole point: Workspace must
be a callable verb on T1 Haiku on up, not an improvised ``terminal`` recon).

Each verb is a THIN subprocess adapter over the existing google_api.py CLI
grammar (service -> action -> flags). It reuses the gateway interpreter
(``sys.executable`` = the venv) and the live ~/.grove OAuth token (GROVE_HOME).
google_api.py is consumed as-is (its funcs print JSON to stdout) — no
restructuring.

ZONING is declarative in ``config/zones.schema.yaml::tool_zones``, keyed by the
bare tool_name and enforced at the Dispatcher intent gate. The in-process call
bypasses the terminal-command classifier, so the per-verb tool_zones entry IS
the enforcement: reads -> green, mutations -> yellow. An omitted entry defaults
to YELLOW in the classifier (fail-safe — a verb is never silently auto-green).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from hermes_constants import get_hermes_home

_SCRIPT_REL = "skills/productivity/google-workspace/scripts/google_api.py"
_TOOLSET = "google-workspace"


def _script_path() -> Path:
    return Path(get_hermes_home()) / _SCRIPT_REL


def _workspace_check() -> bool:
    """Available iff the Workspace OAuth token + the wrapped script both exist.

    Self-gating: the verbs only appear in the schema when Workspace is actually
    set up (mirrors how send_message/ha_* gate on their prerequisites).
    """
    try:
        home = Path(get_hermes_home())
        return (home / "google_token.json").exists() and (home / _SCRIPT_REL).exists()
    except Exception:
        return False


def _run_gapi(
    service: str,
    action: str,
    positional: List[Any],
    flags: Dict[str, Any],
    timeout: int = 60,
) -> str:
    """Run ``google_api.py <service> <action> ...`` and return its JSON stdout.

    Thin adapter: builds argv from the verb's positional args + flag map, runs
    the script with the gateway's venv interpreter and an inherited env carrying
    GROVE_HOME (so the live token is used), and returns stdout verbatim. Errors
    are surfaced loudly to the agent as a JSON ``{"error": ...}`` payload (no
    silent swallow) so a failure routes up rather than improvising.
    """
    cmd: List[str] = [sys.executable, str(_script_path()), service, action]
    cmd += [str(v) for v in positional if v is not None and v != ""]
    for flag, val in flags.items():
        if val is None or val == "" or val is False:
            continue
        if val is True:
            cmd.append(flag)
        else:
            cmd.extend([flag, str(val)])

    env = os.environ.copy()
    env["GROVE_HOME"] = str(get_hermes_home())
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"google_api.py {service} {action} timed out after {timeout}s"})
    except Exception as exc:  # pragma: no cover — defensive, surfaced not swallowed
        return json.dumps({"error": f"google_api.py {service} {action} could not run", "detail": str(exc)})

    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or out or f"exit {proc.returncode}"
        return json.dumps({
            "error": f"google_api.py {service} {action} failed",
            "detail": detail[-1000:],
        })
    return out or json.dumps({"ok": True})


# Verb spec rows:
#   (name, service, action, emoji, description,
#    positional = [(param, json_type, desc), ...]   # required, order matters
#    flags      = [(param, cli_flag, json_type, required, desc), ...])
# READS are green / MUTATIONS are yellow in zones.schema.yaml::tool_zones.
_VERBS: List[Tuple[str, str, str, str, str, list, list]] = [
    # ── READS (Green) ──────────────────────────────────────────────────
    ("gmail_search", "gmail", "search", "📧", "Search Gmail; returns matching messages.",
     [("query", "string", "Gmail search query, e.g. 'is:unread from:alice'.")],
     [("max", "--max", "integer", False, "Max results (default 10).")]),
    ("gmail_get", "gmail", "get", "📧", "Get one Gmail message (full content) by id.",
     [("message_id", "string", "Gmail message id.")], []),
    ("gmail_labels", "gmail", "labels", "🏷️", "List Gmail labels (id + name).", [], []),
    ("calendar_list", "calendar", "list", "📅", "List Google Calendar events.", [],
     [("start", "--start", "string", False, "Start time, ISO 8601."),
      ("end", "--end", "string", False, "End time, ISO 8601."),
      ("max", "--max", "integer", False, "Max events (default 25)."),
      ("calendar", "--calendar", "string", False, "Calendar id (default 'primary').")]),
    ("drive_search", "drive", "search", "🗂️", "Search Google Drive files.",
     [("query", "string", "Search text (or a raw Drive query if raw_query=true).")],
     [("max", "--max", "integer", False, "Max results (default 10)."),
      ("raw_query", "--raw-query", "boolean", False, "Treat query as a raw Drive API query.")]),
    ("drive_get", "drive", "get", "🗂️", "Get Google Drive file metadata by id.",
     [("file_id", "string", "Drive file id.")], []),
    ("drive_download", "drive", "download", "⬇️", "Download a Drive file to a local path (reads Drive; writes locally).",
     [("file_id", "string", "Drive file id.")],
     [("output", "--output", "string", False, "Local output path (default ./<name>)."),
      ("export_mime", "--export-mime", "string", False, "Export MIME for Google-native files.")]),
    ("contacts_list", "contacts", "list", "👤", "List Google Contacts.", [],
     [("max", "--max", "integer", False, "Max contacts (default 50).")]),
    ("sheets_get", "sheets", "get", "📊", "Read a range from a Google Sheet.",
     [("sheet_id", "string", "Spreadsheet id."),
      ("range", "string", "A1 range, e.g. 'Sheet1!A1:C10'.")], []),
    ("docs_get", "docs", "get", "📄", "Read a Google Doc's text by id.",
     [("doc_id", "string", "Document id (the string between /d/ and /edit in the URL).")], []),

    # ── MUTATIONS (Yellow — gated for approval) ────────────────────────
    ("gmail_send", "gmail", "send", "📧", "Send a Gmail message (outbound — requires approval).", [],
     [("to", "--to", "string", True, "Recipient email address(es)."),
      ("subject", "--subject", "string", True, "Subject line."),
      ("body", "--body", "string", True, "Body text (HTML if html=true)."),
      ("cc", "--cc", "string", False, "CC email address(es)."),
      ("from_header", "--from", "string", False, "Custom From header."),
      ("html", "--html", "boolean", False, "Send body as HTML."),
      ("thread_id", "--thread-id", "string", False, "Thread id for threading.")]),
    ("gmail_reply", "gmail", "reply", "📧", "Reply to a Gmail message (outbound — requires approval).",
     [("message_id", "string", "Message id to reply to.")],
     [("body", "--body", "string", True, "Reply body."),
      ("from_header", "--from", "string", False, "Custom From header.")]),
    ("gmail_modify", "gmail", "modify", "🏷️", "Add/remove labels on a Gmail message (requires approval).",
     [("message_id", "string", "Message id.")],
     [("add_labels", "--add-labels", "string", False, "Comma-separated label ids to add."),
      ("remove_labels", "--remove-labels", "string", False, "Comma-separated label ids to remove.")]),
    ("calendar_create", "calendar", "create", "📅", "Create a Google Calendar event (requires approval).", [],
     [("summary", "--summary", "string", True, "Event title."),
      ("start", "--start", "string", True, "Start, ISO 8601 with timezone."),
      ("end", "--end", "string", True, "End, ISO 8601 with timezone."),
      ("location", "--location", "string", False, "Location."),
      ("description", "--description", "string", False, "Description."),
      ("attendees", "--attendees", "string", False, "Comma-separated attendee emails."),
      ("calendar", "--calendar", "string", False, "Calendar id (default 'primary').")]),
    ("calendar_delete", "calendar", "delete", "📅", "Delete a Google Calendar event (requires approval).",
     [("event_id", "string", "Event id.")],
     [("calendar", "--calendar", "string", False, "Calendar id (default 'primary').")]),
    ("drive_upload", "drive", "upload", "⬆️", "Upload a local file to Google Drive (requires approval).",
     [("path", "string", "Local file path to upload.")],
     [("name", "--name", "string", False, "Override Drive file name."),
      ("parent", "--parent", "string", False, "Parent folder id."),
      ("mime_type", "--mime-type", "string", False, "Override MIME type.")]),
    ("drive_create_folder", "drive", "create-folder", "🗂️", "Create a Google Drive folder (requires approval).",
     [("name", "string", "Folder name.")],
     [("parent", "--parent", "string", False, "Parent folder id (default root).")]),
    ("drive_share", "drive", "share", "🔗", "Change Drive file sharing/permissions (outbound — requires approval).",
     [("file_id", "string", "Drive file id.")],
     [("role", "--role", "string", False, "reader|commenter|writer|fileOrganizer|organizer|owner (default reader)."),
      ("type", "--type", "string", False, "user|group|domain|anyone (default user)."),
      ("email", "--email", "string", False, "Email (required for type=user/group)."),
      ("domain", "--domain", "string", False, "Domain (required for type=domain)."),
      ("notify", "--notify", "boolean", False, "Send a notification email.")]),
    ("drive_delete", "drive", "delete", "🗑️", "Delete a Drive file — trash by default (requires approval).",
     [("file_id", "string", "Drive file id.")],
     [("permanent", "--permanent", "boolean", False, "Permanently delete (default trash, reversible).")]),
    ("sheets_update", "sheets", "update", "📊", "Overwrite a range in a Google Sheet (requires approval).",
     [("sheet_id", "string", "Spreadsheet id."),
      ("range", "string", "A1 range to overwrite.")],
     [("values", "--values", "string", True, "JSON array of arrays of cell values.")]),
    ("sheets_append", "sheets", "append", "📊", "Append rows to a Google Sheet (requires approval).",
     [("sheet_id", "string", "Spreadsheet id."),
      ("range", "string", "A1 range to append after.")],
     [("values", "--values", "string", True, "JSON array of arrays of cell values.")]),
    ("sheets_create", "sheets", "create", "📊", "Create a new Google Sheet (requires approval).", [],
     [("title", "--title", "string", True, "Spreadsheet title."),
      ("sheet_name", "--sheet-name", "string", False, "First tab name (default 'Sheet1').")]),
    ("docs_create", "docs", "create", "📄", "Create a new Google Doc (requires approval).", [],
     [("title", "--title", "string", True, "Document title."),
      ("body", "--body", "string", False, "Initial body text.")]),
    ("docs_append", "docs", "append", "📄", "Append text to a Google Doc (requires approval).",
     [("doc_id", "string", "Document id.")],
     [("text", "--text", "string", True, "Text to append at the end of the document.")]),
]


def _make_schema(name: str, desc: str, pos: list, flagspec: list) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    required: List[str] = []
    for (param, typ, d) in pos:
        props[param] = {"type": typ, "description": d}
        required.append(param)
    for (param, _flag, typ, req, d) in flagspec:
        props[param] = {"type": typ, "description": d}
        if req:
            required.append(param)
    return {
        "name": name,
        "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required},
    }


def _make_handler(service: str, action: str, pos: list, flagspec: list):
    pos_names = [p[0] for p in pos]
    flag_map = [(f[0], f[1]) for f in flagspec]

    def handler(args, **_kw):
        args = args or {}
        positional = [args.get(n) for n in pos_names]
        flags = {flag: args.get(param) for (param, flag) in flag_map}
        return _run_gapi(service, action, positional, flags)

    return handler


def register(reg):
    """Sprint workspace-first-class-capability-v1 — register the 24 native verbs."""
    for (name, service, action, emoji, desc, pos, flagspec) in _VERBS:
        reg.register(
            name=name,
            toolset=_TOOLSET,
            schema=_make_schema(name, desc, pos, flagspec),
            handler=_make_handler(service, action, pos, flagspec),
            check_fn=_workspace_check,
            emoji=emoji,
        )

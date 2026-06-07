#!/usr/bin/env python3
"""Governed Notion primitive access layer for the Grove Autonomaton.

Replaces the retired ``@notionhq/notion-mcp-server`` (Sprint 69). The agent
calls this script through the terminal; the zone classifier reads the
SUBCOMMAND off the command line and governs accordingly:

    reads  (Green)  : search · get · query
    writes (Yellow) : create-page · update-page

The subcommand — not the HTTP verb — carries read/write intent. That matters:
Notion's ``search`` and database ``query`` are both POST requests yet are
reads, so an HTTP-verb heuristic would misclassify them. Explicit subcommands
make the intent unambiguous (Sprint 69 GATE-B, Decision 1).

Design commitments (Grove Operating Principles):
  * Fail Fast, Fail Loud — no silent fallbacks. A missing token, an HTTP
    error, or a malformed argument stops the command and prints what failed,
    where, and what to check.
  * The agent never fumbles property schemas — this script owns the
    ``properties.title`` wrapper, URL→ID parsing, and the API version header.

Auth: ``NOTION_TOKEN`` from the environment (sourced from ``~/.grove/.env``).
Stdlib only — no third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

API_ROOT = "https://api.notion.com/v1"
API_VERSION = "2025-09-03"

# 32 hex chars, optionally dash-grouped 8-4-4-4-12 — a Notion page/database id.
_ID_RE = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
_BARE_ID_RE = re.compile(r"[0-9a-fA-F]{32}")


class NotionError(RuntimeError):
    """A Notion access failure surfaced loudly to the operator."""


def _die(message: str) -> "NoReturn":  # type: ignore[name-defined]
    """Fail loud: print a diagnostic to stderr and exit non-zero."""
    print(f"notion: {message}", file=sys.stderr)
    raise SystemExit(1)


def _token() -> str:
    tok = os.environ.get("NOTION_TOKEN", "").strip()
    if not tok:
        _die(
            "NOTION_TOKEN is not set. Add it to ~/.grove/.env "
            "(NOTION_TOKEN=ntn_...) and ensure the gateway loads that file."
        )
    return tok


def _dash(raw: str) -> str:
    """Return a 32-hex id in canonical 8-4-4-4-12 dashed form."""
    h = raw.replace("-", "").lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def extract_id(value: str) -> str:
    """Parse a Notion URL or UUID (dashed or bare) into a dashed id.

    Accepts the forms the operator actually pastes: a full
    ``https://www.notion.so/Workspace/Title-<id>?...`` URL, a dashed UUID,
    or a bare 32-hex id. Fails loud if no id is present.
    """
    value = value.strip()
    # Strip query/fragment so an id-looking ``?v=...`` view id can't shadow it.
    core = value.split("?", 1)[0].split("#", 1)[0]
    m = _ID_RE.search(core) or _BARE_ID_RE.search(core)
    if not m:
        _die(f"could not find a Notion page/database id in {value!r}")
    return _dash(m.group(0))


def _request(method: str, path: str, body: dict | None = None) -> dict:
    """Issue one Notion API call. Raises NotionError loudly on failure."""
    url = f"{API_ROOT}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Notion-Version", API_VERSION)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(detail)
            detail = parsed.get("message", detail)
        except json.JSONDecodeError:
            pass
        raise NotionError(
            f"{method} {path} → HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise NotionError(f"{method} {path} → connection failed: {exc.reason}") from exc


# ── rich-text / block rendering ──────────────────────────────────────────

def _plain(rich: list | None) -> str:
    if not rich:
        return ""
    return "".join(seg.get("plain_text", "") for seg in rich)


def _title_of(obj: dict) -> str:
    """Pull a human title from a page/database object regardless of schema."""
    # Database / data_source objects carry a top-level ``title`` array.
    if isinstance(obj.get("title"), list):
        t = _plain(obj["title"])
        if t:
            return t
    props = obj.get("properties") or {}
    for prop in props.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return _plain(prop.get("title")) or "(untitled)"
    return "(untitled)"


def _render_block(block: dict) -> str:
    btype = block.get("type", "")
    payload = block.get(btype, {}) if isinstance(block.get(btype), dict) else {}
    text = _plain(payload.get("rich_text"))
    if btype == "heading_1":
        return f"# {text}"
    if btype == "heading_2":
        return f"## {text}"
    if btype == "heading_3":
        return f"### {text}"
    if btype == "bulleted_list_item":
        return f"- {text}"
    if btype == "numbered_list_item":
        return f"1. {text}"
    if btype == "to_do":
        mark = "x" if payload.get("checked") else " "
        return f"- [{mark}] {text}"
    if btype == "quote":
        return f"> {text}"
    if btype == "code":
        lang = payload.get("language", "")
        return f"```{lang}\n{text}\n```"
    if btype == "callout":
        return f"💬 {text}"
    if btype == "divider":
        return "---"
    if btype == "child_page":
        return f"[page] {payload.get('title', '(untitled)')}"
    return text  # paragraph and everything else


# ── subcommands ──────────────────────────────────────────────────────────

def cmd_search(args: argparse.Namespace) -> None:
    res = _request("POST", "/search", {"query": args.query})
    results = res.get("results", [])
    if not results:
        print(f"No Notion results for {args.query!r}.")
        return
    print(f"Notion results for {args.query!r}:")
    for i, obj in enumerate(results, 1):
        kind = obj.get("object", "?")
        if kind == "data_source":
            kind = "database"
        title = _title_of(obj)
        url = obj.get("url", "")
        print(f"  {i}. {title}  [{kind}]")
        print(f"     {url or _dash(obj.get('id', ''))}")


def cmd_get(args: argparse.Namespace) -> None:
    page_id = extract_id(args.target)
    page = _request("GET", f"/pages/{page_id}")
    print(f"# {_title_of(page)}")
    print(f"({page.get('url', page_id)})\n")
    children = _request("GET", f"/blocks/{page_id}/children?page_size=100")
    blocks = children.get("results", [])
    if not blocks:
        print("(no content blocks)")
        return
    for block in blocks:
        line = _render_block(block)
        if line:
            print(line)


def _build_filter(expr: str) -> dict:
    """Parse ``--filter`` into a Notion filter object.

    Accepts raw JSON (escape hatch for complex filters) or the shorthand
    ``Property=Value`` — assumed a ``select`` equals, the most common case.
    Other property types: pass JSON, or drop to the REST examples in SKILL.md.
    """
    expr = expr.strip()
    if expr.startswith("{"):
        try:
            return json.loads(expr)
        except json.JSONDecodeError as exc:
            _die(f"--filter looked like JSON but did not parse: {exc}")
    if "=" not in expr:
        _die(f"--filter {expr!r} must be JSON or 'Property=Value'")
    prop, value = (p.strip() for p in expr.split("=", 1))
    return {"property": prop, "select": {"equals": value}}


def cmd_query(args: argparse.Namespace) -> None:
    ds_id = extract_id(args.database)
    body: dict = {}
    if args.filter:
        body["filter"] = _build_filter(args.filter)
    res = _request("POST", f"/data_sources/{ds_id}/query", body or None)
    rows = res.get("results", [])
    if not rows:
        print("No matching rows.")
        return
    print(f"{len(rows)} row(s):")
    for i, row in enumerate(rows, 1):
        print(f"  {i}. {_title_of(row)}  →  {row.get('url', '')}")


def _paragraphs(text: str) -> list[dict]:
    """Split text on newlines into Notion paragraph blocks."""
    blocks = []
    for line in text.split("\n"):
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]},
        })
    return blocks


def cmd_create_page(args: argparse.Namespace) -> None:
    parent_id = extract_id(args.parent)
    # Two-step: create with title only, then append content. Robust regardless
    # of whether the parent is a page or a database (and avoids the inline-
    # children validation quirk documented in the skill references).
    page = _request("POST", "/pages", {
        "parent": {"page_id": parent_id},
        "properties": {"title": [{"text": {"content": args.title}}]},
    })
    page_id = page["id"]
    if args.content:
        _request("PATCH", f"/blocks/{page_id}/children", {"children": _paragraphs(args.content)})
    print(f"Created page {args.title!r}: {page.get('url', page_id)}")


def _title_prop_key(page: dict) -> str:
    """Find the title property's key (it is literally 'title' for pages, but
    a database row's title property can have a custom name)."""
    for key, prop in (page.get("properties") or {}).items():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return key
    return "title"


def cmd_update_page(args: argparse.Namespace) -> None:
    if not args.title and not args.append:
        _die("update-page needs --title and/or --append")
    page_id = extract_id(args.url)
    did = []
    if args.title:
        page = _request("GET", f"/pages/{page_id}")
        key = _title_prop_key(page)
        _request("PATCH", f"/pages/{page_id}", {
            "properties": {key: {"title": [{"text": {"content": args.title}}]}},
        })
        did.append(f"title → {args.title!r}")
    if args.append:
        _request("PATCH", f"/blocks/{page_id}/children", {"children": _paragraphs(args.append)})
        did.append("appended content")
    print(f"Updated page {page_id}: {', '.join(did)}.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="notion",
        description="Governed Notion access for the Grove Autonomaton.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="(read) find pages/databases by title")
    s.add_argument("query")
    s.set_defaults(func=cmd_search)

    g = sub.add_parser("get", help="(read) page content as readable markdown")
    g.add_argument("target", help="page URL or UUID")
    g.set_defaults(func=cmd_get)

    q = sub.add_parser("query", help="(read) query a database / data source")
    q.add_argument("database", help="database URL or data-source UUID")
    q.add_argument("--filter", help="'Property=Value' or raw JSON filter")
    q.set_defaults(func=cmd_query)

    c = sub.add_parser("create-page", help="(write) create a page under a parent")
    c.add_argument("parent", help="parent page URL or UUID")
    c.add_argument("title")
    c.add_argument("--content", help="body text (newline-separated paragraphs)")
    c.set_defaults(func=cmd_create_page)

    u = sub.add_parser("update-page", help="(write) rename or append to a page")
    u.add_argument("url", help="page URL or UUID")
    u.add_argument("--title", help="new title")
    u.add_argument("--append", help="text to append as paragraphs")
    u.set_defaults(func=cmd_update_page)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except NotionError as exc:
        _die(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

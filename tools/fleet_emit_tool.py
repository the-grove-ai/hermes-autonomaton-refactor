"""tools/fleet_emit_tool.py — the emit_package tool (wiki-writer-structured-output-v1 P1).

Schema-bound terminal emission for fleet workers: the model emits its finished
file package as ONE structured tool call ``emit_package({files: {name: body},
meta?: {...}})`` instead of free-text sentinel blocks (the continuation-split
loss class byte-confirmed on run bb8b79bcc; transport proven byte-intact at
4KB/12KB by the P0 spike).

RUN-SCOPED BY CONSTRUCTION: a fleet worker is a short-lived one-run subprocess
(grove/fleet/worker_entry.py), so this module's process-global state IS per-run
state. ``worker_entry`` calls :func:`configure` with the RECORD-DERIVED spec
(GATE-B F5: the capability record's ``terminal_artifact.emit`` block is the
source of truth; the harness derives) BEFORE constructing the Dispatcher, so
the registered schema the model sees equals the declaration (parity pin).

Availability: ``check_fn`` is True only in a configured process — on the
gateway / CLI / test processes the tool is never offered (belt); it also has
no capability record, so the interactive admission pipeline would drop it
anyway (suspenders). NOTE for tests: ``tools.registry._check_fn_cached`` holds
check results for ~30 s — call ``tools.registry.invalidate_check_fn_cache()``
after :func:`configure`/:func:`reset` when cycling within one process.

Lock-on-emit (GATE-B F4): the FIRST valid call validates, stages atomically
into the declared sink via the same :func:`grove.fleet.staging.stage_package`
jail the sentinel path uses, and locks; every later call returns a loud
tool-result error the model sees. Validation failures are loud tool-result
errors too — the model can correct and re-call (the lock only engages on a
SUCCESSFUL emit). The basename jail (``worker_entry._is_safe_basename``) is
applied to arg filenames verbatim, BEFORE staging — parity with the sentinel
parser's jail; ``stage_package`` re-jails underneath (two independent layers,
unchanged discipline).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.registry import tool_error


class _EmitState:
    """Process-global (= run-scoped in a worker subprocess) emit state."""

    __slots__ = (
        "configured", "schema", "expected_files", "meta_required_keys",
        "sink", "slug", "synth_meta", "bound_row_id", "emitted",
    )

    def __init__(self) -> None:
        self.configured = False
        self.schema: Optional[dict] = None
        self.expected_files: List[str] = []
        self.meta_required_keys: Optional[List[str]] = None
        self.sink: Optional[Path] = None
        self.slug: Optional[str] = None
        self.synth_meta: Optional[str] = None
        self.bound_row_id: Optional[str] = None
        self.emitted: Optional[Dict[str, Any]] = None


_STATE = _EmitState()

# Placeholder schema for the unconfigured state. check_fn is False then, so no
# model ever sees this; it exists so registration is well-formed at Dispatcher
# bootstrap time (register_builtin_tools walks tools/*.py unconditionally).
_BASE_SCHEMA = {
    "name": "emit_package",
    "description": "Emit the run's finished file package (fleet worker only).",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


def build_schema(
    expected_files: List[str], meta_required_keys: Optional[List[str]]
) -> dict:
    """Derive the emit_package tool schema from the record-declared contract.

    SHARED by :func:`configure` (what gets registered) and the parity pin
    (what the record declares) — one derivation, no drift surface. ``files``
    is closed (additionalProperties: false) over exactly the expected names:
    schema-bound emission means the contract IS the schema, not prose the
    model may drift from. ``meta`` appears only for a self-authored producer
    (forge) whose record declares ``meta.required_keys``.
    """
    file_props = {
        name: {
            "type": "string",
            "description": (
                "the complete raw file body, verbatim — newlines and quotes "
                "are ordinary JSON string content"
            ),
        }
        for name in expected_files
    }
    params: dict = {
        "type": "object",
        "properties": {
            "files": {
                "type": "object",
                "description": "map of filename -> complete file body",
                "properties": file_props,
                "required": list(expected_files),
                "additionalProperties": False,
            },
        },
        "required": ["files"],
        "additionalProperties": False,
    }
    if meta_required_keys is not None:
        params["properties"]["meta"] = {
            "type": "object",
            "description": (
                "package identity + routing metadata; the runtime stages it "
                "as meta.json and files the package under meta.slug"
            ),
            "properties": {k: {"type": "string"} for k in meta_required_keys},
            "required": list(meta_required_keys),
            "additionalProperties": True,
        }
        params["required"].append("meta")
    return {
        "name": "emit_package",
        "description": (
            "Emit your finished file package EXACTLY ONCE. The runtime "
            "validates and atomically stages it to the declared sink; a "
            "second call is refused (the package locks on first successful "
            "emit). Emit ALL required files in this ONE call."
        ),
        "parameters": params,
    }


def configure(
    *,
    expected_files: List[str],
    meta_required_keys: Optional[List[str]],
    sink: Path,
    slug: Optional[str],
    synth_meta: Optional[str],
    bound_row_id: Optional[str] = None,
) -> None:
    """Arm the tool for this worker run with the record-derived spec.

    ``slug``/``synth_meta`` set for a declarative producer (runtime identity);
    both ``None`` for a self-authored producer (descriptive metadata arrives
    in the meta arg). ``bound_row_id`` (fleet-receipt-custody-v1 P1.1) is the
    HOST-dispatched unit identity for a self-authored producer — a narrow
    identity-only slot, NOT a document override: the runtime writes it into
    the staged meta.json at emit, overriding anything the model authored.
    Called by ``worker_entry`` BEFORE Dispatcher construction so the
    registered surface and check_fn see the configured state.
    """
    _STATE.configured = True
    _STATE.schema = build_schema(expected_files, meta_required_keys)
    _STATE.expected_files = list(expected_files)
    _STATE.meta_required_keys = (
        list(meta_required_keys) if meta_required_keys is not None else None
    )
    _STATE.sink = Path(sink)
    _STATE.slug = slug
    _STATE.synth_meta = synth_meta
    _STATE.bound_row_id = bound_row_id
    _STATE.emitted = None


def reset() -> None:
    """Disarm (test hygiene; a worker process never needs to)."""
    _STATE.__init__()


def emitted() -> Optional[Dict[str, Any]]:
    """The locked package — ``{"slug", "files", "staged"}`` — or None."""
    return _STATE.emitted


def _handle_emit_package(args: dict, **kwargs) -> str:
    """Validate → jail → stage → lock. Every rejection is a loud tool-result
    error the model sees and can correct; the lock engages ONLY on success."""
    from grove.fleet.staging import _SLUG_RE, stage_package
    from grove.fleet.worker_entry import _is_safe_basename

    if not _STATE.configured or _STATE.sink is None:
        return tool_error(
            "emit_package is not available outside a configured fleet worker run"
        )
    if _STATE.emitted is not None:
        return tool_error(
            "package already emitted and locked — emit_package accepts exactly "
            "one successful call per run; the first emission was staged and "
            "this call changed nothing. Do not call emit_package again."
        )

    files = args.get("files")
    if not isinstance(files, dict) or not files:
        return tool_error(
            "'files' must be a non-empty object mapping filename -> complete "
            "file body"
        )
    # Basename jail RETAINED VERBATIM on arg filenames (the sentinel parser
    # applied it at :func:`_parse_delimited_blocks`; arg validation inherits it).
    for name in files:
        if not isinstance(name, str) or not _is_safe_basename(name, _STATE.sink):
            return tool_error(
                f"unsafe filename {name!r} — one safe path component required "
                f"(no separators, no traversal, no '.'/'..')"
            )
    for name, body in files.items():
        if not isinstance(body, str) or not body.strip():
            return tool_error(
                f"file {name!r} has an empty or non-string body — emit the "
                f"complete raw content"
            )
    expected = set(_STATE.expected_files)
    missing = expected - set(files)
    if missing:
        return tool_error(
            f"missing required file(s): {sorted(missing)} — emit the complete "
            f"package ({sorted(expected)}) in ONE emit_package call"
        )
    extra = set(files) - expected
    if extra:
        return tool_error(
            f"unexpected file(s): {sorted(extra)} — emit exactly "
            f"{sorted(expected)}"
            + (
                "; identity travels in the 'meta' argument, not as a file"
                if "meta.json" in extra
                else ""
            )
        )

    if _STATE.meta_required_keys is not None:
        # Self-authored producer (forge): identity arrives structured.
        meta = args.get("meta")
        if not isinstance(meta, dict):
            return tool_error(
                f"'meta' object is required, carrying at least: "
                f"{_STATE.meta_required_keys}"
            )
        missing_keys = [
            k
            for k in _STATE.meta_required_keys
            if not (isinstance(meta.get(k), str) and meta[k].strip())
        ]
        if missing_keys:
            return tool_error(
                f"meta is missing required key(s) {missing_keys} (non-empty "
                f"strings required)"
            )
        slug = meta["slug"].strip()
        if not _SLUG_RE.match(slug):
            return tool_error(
                f"meta.slug {slug!r} is not a safe slug "
                f"(^[a-z0-9][a-z0-9._-]*$) — choose a lowercase filesystem slug"
            )
        # fleet-receipt-custody-v1 P1.1 (A6 RULED) — extra meta keys (beyond
        # the record-declared floor) are STRIPPED and RECORDED on the terminal
        # receipt (stripped_meta_keys): telemetry only, no Andon, no operator
        # surface. The file-level no-extras check above stays LOUD — this
        # softening is scoped to meta-key validation exclusively. Identity is
        # then runtime-bound: the DISPATCHED row id overwrites anything the
        # model authored (the model is no longer asked for row_id; a
        # habit-emitted one lands in the stripped list, never in custody).
        allowed = set(_STATE.meta_required_keys)
        stripped_meta_keys = sorted(k for k in meta if k not in allowed)
        meta = {k: v for k, v in meta.items() if k in allowed}
        if _STATE.bound_row_id:
            meta["row_id"] = _STATE.bound_row_id
        staged_files = dict(files)
        staged_files["meta.json"] = json.dumps(meta, ensure_ascii=False, indent=2)
    else:
        # Declarative producer: identity is the RUNTIME's (synthesized from
        # the resolver payload at configure time) — the skill authors content only.
        slug = _STATE.slug or ""
        staged_files = dict(files)
        stripped_meta_keys = None  # no model-facing meta arg on this branch
        if _STATE.synth_meta is not None:
            staged_files["meta.json"] = _STATE.synth_meta

    # Stage NOW, atomically, through the SAME jailed primitive the sentinel
    # path uses — then lock. A staging failure propagates as a loud tool-result
    # error (registry.dispatch wraps it); the lock does NOT engage, so the
    # model may correct (e.g. a slug rejection) and re-call.
    staged = stage_package(_STATE.sink, slug, staged_files)

    _STATE.emitted = {
        "slug": slug,
        "files": staged_files,
        "staged": [str(p) for p in staged],
        # P1.1 A6 telemetry rider — the names stripped from the meta arg (self-
        # authored branch), None where no model-facing meta exists (declarative).
        "stripped_meta_keys": stripped_meta_keys,
    }
    return json.dumps(
        {
            "staged": True,
            "locked": True,
            "slug": slug,
            "files": sorted(staged_files),
            "note": (
                "package staged and locked; do NOT call emit_package again — "
                "your job is complete"
            ),
        },
        ensure_ascii=False,
    )


def register(reg):
    """Dispatcher-driven registration entrypoint (Sprint 53 convention)."""
    reg.register(
        name="emit_package",
        toolset="fleet_emit",
        schema=_BASE_SCHEMA,
        handler=_handle_emit_package,
        # Offered ONLY in a configured fleet worker process; False everywhere
        # else (gateway/CLI/tests) so the tool never leaks onto an interactive
        # surface. (It also has no capability record — the admission pipeline
        # is an independent second wall on non-fleet platforms.)
        check_fn=lambda: _STATE.configured,
        # The registered schema IS the record-derived one (GATE-B F5 parity):
        # get_definitions() merges these overrides at offer time.
        dynamic_schema_overrides=lambda: (_STATE.schema or {}),
        description="Emit the run's finished file package (fleet worker terminal artifact).",
        emoji="📦",
    )

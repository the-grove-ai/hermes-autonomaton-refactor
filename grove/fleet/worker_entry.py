"""The fleet worker process (background-worker-runtime-v1).

Run as ``python -m grove.fleet.worker_entry --worker-id <id> --run-id <rid>``.
A short-lived, grant-less subprocess that runs ONE pinned skill against ONE
ticker-brokered payload, stages a Yellow draft to the record's declared sink,
writes a terminal-state event, and exits. It is skill-agnostic — the skill is
read from the capability record named by the worker's ``skill`` field.

Structural safety invariant (per SPEC):
  * builds its OWN empty GrantStore (grant-less principal);
  * installs ``non_interactive_deny_handler`` — ungranted Yellow/Red fail closed;
  * writes to an ISOLATED session DB under ``$GROVE_HOME/fleet/<id>/``, never the
    gateway session DB;
  * stages its draft to the declared pending_review sink via an atomic,
    path-jailed write;
  * an external write happens only later, at the operator publish tap.

The process ALWAYS writes a terminal-state event before exit (success | no_work
| failed) unless hard-killed; the ticker distinguishes those from an absent
event (catastrophic).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from dataclasses import replace as dc_replace
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

WORKER_MAX_ITERATIONS = 50

# The self-authored (Option 2, forge-style) sentinel triad — the required set
# the package parser enforces. Hoisted (drafter-quality-checks-v1 P3) so the
# first-pass extraction and the redraft re-extraction share one declaration.
_SELF_AUTHORED_REQUIRED = {"resume.md", "cover-letter.md", "meta.json"}

# forge-publish-meta-hotfix-v1 P1 — the forge meta COMPLETENESS contract. The
# publish endpoint (grove/api/actions.py:1209) rejects any package whose
# meta.json lacks these three keys; historically the worker validated only the
# `slug` (worker_entry.py:_extract_fleet_package) and staged an incomplete meta
# clean, so the operator only met the defect hours later at the Publish tap. This
# is the emit-time half of that contract: the same three keys, checked the moment
# the package stages, so a stub meta surfaces LOUD (Andon + forensics) AND still
# stages behind a visible defect marker — inform disposition, never withhold work.
_FORGE_META_REQUIRED = ("company", "role", "row_id")


def _forge_meta_defects(meta_raw: Optional[str]) -> List[str]:
    """Return the _FORGE_META_REQUIRED keys a staged forge meta.json is missing.

    A field counts as present only when it parses to a truthy value (the exact
    predicate the publish endpoint applies: ``all(meta.get(k) for k in ...)``).
    An unparseable / non-dict meta reports ALL three missing — the loudest honest
    signal, never a silent pass. Empty list == complete meta (no defect).
    """
    try:
        meta = json.loads(meta_raw) if isinstance(meta_raw, str) else None
    except (json.JSONDecodeError, TypeError):
        meta = None
    if not isinstance(meta, dict):
        return list(_FORGE_META_REQUIRED)
    return [k for k in _FORGE_META_REQUIRED if not meta.get(k)]


def _now_iso() -> str:
    # Runtime process (not a resumable workflow script) — wall clock is fine.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _load_capability(skill_id: str, worker_id: str):
    """Load the worker's capability record by id, or fail loud."""
    from grove.capability_registry import load_capabilities
    from grove.fleet.errors import FleetWorkerAndon

    records = load_capabilities()
    cap = records.get(skill_id)
    if cap is None:
        raise FleetWorkerAndon(
            f"capability record {skill_id!r} not found — the worker's 'skill' must "
            f"name a loaded capability record",
            worker_id=worker_id,
            check="record_not_found",
        )
    from grove.capability import CapabilityKind

    if cap.kind is not CapabilityKind.SKILL:
        raise FleetWorkerAndon(
            f"capability {skill_id!r} is kind={cap.kind.value}, not skill — a "
            f"fleet worker runs a pinned SKILL",
            worker_id=worker_id,
            check="record_not_skill",
        )
    return cap


def _derive_skill_name(cap, worker_id: str) -> str:
    """The invoke_skill name from the record id.

    Skills live category-nested at ``~/.grove/skills/<category>/<name>/`` and
    ``invoke_skill`` resolves ``active_path(name) = skills_dir()/name`` — so the
    invoke name is the CATEGORY-QUALIFIED path ``<category>/<name>``, NOT the bare
    name (which resolves to a nonexistent flat dir). id ``skill.<category>.<name>``
    -> ``<category>/<name>``.
    """
    from grove.fleet.errors import FleetWorkerAndon

    parts = cap.id.split(".")
    if len(parts) < 3 or parts[0] != "skill":
        raise FleetWorkerAndon(
            f"capability id {cap.id!r} is not of the form skill.<category>.<name> "
            f"— cannot derive the invoke_skill name",
            worker_id=worker_id,
            check="bad_skill_id",
        )
    category, name = parts[1], ".".join(parts[2:])
    return f"{category}/{name}"


def _resolve_declared_sink(cap, worker_id: str) -> Path:
    """Resolve governance.write_zone.staging_dir to an absolute sink path."""
    from grove.fleet.errors import FleetWorkerAndon
    from grove.utils.fs_utils import _grove_home_realpath, _grove_subdir_realpath

    gov = cap.governance or {}
    staging = ((gov.get("write_zone") or {}) if isinstance(gov, dict) else {}).get(
        "staging_dir"
    )
    if not staging:
        raise FleetWorkerAndon(
            f"capability {cap.id!r} declares no governance.write_zone.staging_dir "
            f"— a fleet worker must have a declared pending_review sink",
            worker_id=worker_id,
            check="no_declared_sink",
        )
    grove = _grove_home_realpath()
    if grove is None:
        raise FleetWorkerAndon(
            "GROVE_HOME could not be resolved — cannot locate the declared sink",
            worker_id=worker_id,
            check="no_grove_home",
        )
    return Path(_grove_subdir_realpath(staging, grove))


def _resolve_worker_runtime(cap, worker_id: str):
    """Resolve (model, max_tokens, runtime) from the record's preferred tier.

    Pins the tier explicitly (no LLM classification, classify=False) and reuses
    the sanctioned route -> runtime chain. No routing config = a worker cannot
    resolve a model = fail loud (never a blind default).

    aux-model-bindings-v1 — a record-declared exact-model pin
    (``model_binding: {type: model, model: <slug>}``) bypasses the tier's
    MODEL while preserving the tier envelope (provider, max_tokens, and the
    credential runtime resolved against the PINNED slug). A malformed slug
    fails the spawn loud — never a quiet fallback to the tier model (GATE-B
    F3): a dead pin must halt the worker, not silently inherit the tier.
    """
    from grove.fleet.errors import FleetWorkerAndon
    from grove.providers import resolve_tier_to_runtime, route_for_agent

    tier = f"T{cap.tier_rule.preferred}"
    mb = getattr(cap, "model_binding", None)
    pinned: Optional[str] = None
    if mb is not None and mb.type == "model":
        slug = (mb.model or "").strip()
        halves = slug.split("/")
        if not slug or len(halves) != 2 or not halves[0] or not halves[1]:
            raise FleetWorkerAndon(
                f"capability {cap.id!r} declares model_binding.type=model with a "
                f"malformed slug {mb.model!r} — expected '<provider-org>/<model>' "
                f"with non-empty halves; refusing to spawn (no tier fallback)",
                worker_id=worker_id,
                check="model_binding_malformed_slug",
            )
        pinned = slug
        logger.info(
            "model_binding: pinned=%s bypassing tier=T%d",
            pinned,
            cap.tier_rule.preferred,
        )
    routed = route_for_agent(explicit_tier=tier, classify=False)
    if routed is None:
        raise FleetWorkerAndon(
            "no routing.config.yaml present — a fleet worker cannot resolve a "
            "model/runtime for its tier",
            worker_id=worker_id,
            check="no_routing_config",
        )
    tier_config = routed.tier_config
    if pinned is not None:
        # Frozen dataclass — replace, never mutate (the router caches its
        # TierConfig instances). Credential resolution below then sees the
        # pinned slug, so api_key/base_url/api_mode match the model actually
        # called; provider + max_tokens carry from the tier envelope.
        tier_config = dc_replace(tier_config, model=pinned)
    runtime = resolve_tier_to_runtime(tier_config)
    return tier_config.model, tier_config.max_tokens, runtime


def _build_worker_prompt(
    skill_name: str, payload: Any, tag: str, content_files: Optional[List[str]] = None
) -> str:
    # suggest-revision-verb-v1 P3 (B1 attention fix) — a host-side revision_directive
    # is surfaced as an EXPLICIT turn instruction (its OWN segment, before RESOLVED
    # INPUT) and LIFTED OUT of the json.dumps blob: a passive json key is ambient
    # metadata the corpus-only worker ignores. Absent directive -> byte-identical.
    directive = payload.get("revision_directive") if isinstance(payload, dict) else None
    if directive:
        directive_block = (
            "REVISION DIRECTIVE (authoritative — the new draft MUST satisfy this):\n"
            f"{directive}\n\n"
        )
        json_payload = {k: v for k, v in payload.items() if k != "revision_directive"}
    else:
        directive_block = ""
        json_payload = payload
    # forge-fleet-package-emission-v1 P2 — the emit contract is the DELIMITED protocol
    # the P1 parser consumes. *tag* is the per-run short-hex (run_id[:8]) that frames the
    # sentinels; the SAME tag reaches _extract_fleet_package, so the markers the model is
    # told to write are exactly the ones the parser accepts (a drift = every run
    # no-files). The block is SKILL-AGNOSTIC: it never names resume.md/cover-letter.md —
    # the skill (skill_view) names the specific files. Delimited-only: no JSON envelope,
    # so a body full of literal quotes/newlines transports verbatim (this kills the
    # unescaped-quote no_package class byte-confirmed on run f157eb558b).
    if content_files is None:
        # Self-authored producer (forge) — byte-identical to the pre-C1b-2 prompt.
        return (
            f"You are an autonomous, non-interactive fleet background worker. You are "
            f"EXECUTING a job, not describing one. Your FIRST step is to call "
            f"skill_view('{skill_name}'): what it returns is your OPERATING PROCEDURE "
            f"to carry out, NOT reference material to summarize or report on. Then "
            f"perform that procedure to completion against the resolved input below.\n\n"
            f"No operator is present — do NOT ask clarifying questions. You have NO "
            f"write tool and you do NOT publish; the RUNTIME stages your output. Read "
            f"only your declared read surfaces.\n\n"
            f"Your job is COMPLETE ONLY when you emit EACH file your procedure produces, "
            f"each inside its OWN delimited block, using this EXACT protocol:\n"
            f"@@@FILE_START: <filename> [{tag}]@@@\n"
            f"<full raw file content — no JSON escaping; quotes and newlines are literal>\n"
            f"@@@FILE_END: <filename> [{tag}]@@@\n"
            f"One file MUST be meta.json — valid JSON carrying a \"slug\" key plus your "
            f"routing metadata; the runtime stages your output under that slug. Do NOT wrap "
            f"bodies in markdown fences. Prose outside blocks is ignored, but a run that "
            f"omits a required file, leaves a block unterminated, or emits an empty body is "
            f"an INCOMPLETE run.\n\n"
            f"{directive_block}"
            f"RESOLVED INPUT:\n"
            f"{json.dumps(json_payload, ensure_ascii=False, indent=2)}"
        )
    # Declarative producer (drafter/cultivator, C1b-2) — the skill authors CONTENT
    # only; the runtime synthesizes the identity envelope from the resolver payload,
    # so the worker is told exactly which content file to emit and NOT to author a
    # meta.json / slug. Names come from the record's terminal_artifact + the runtime.
    emit_lines = "\n".join(
        f"@@@FILE_START: {name} [{tag}]@@@\n"
        f"<full raw file content — no JSON escaping; quotes and newlines are literal>\n"
        f"@@@FILE_END: {name} [{tag}]@@@"
        for name in content_files
    )
    files_phrase = ", ".join(content_files)
    return (
        f"You are an autonomous, non-interactive fleet background worker. You are "
        f"EXECUTING a job, not describing one. Your FIRST step is to call "
        f"skill_view('{skill_name}'): what it returns is your OPERATING PROCEDURE "
        f"to carry out, NOT reference material to summarize or report on. Then "
        f"perform that procedure to completion against the resolved input below.\n\n"
        f"No operator is present — do NOT ask clarifying questions. You have NO "
        f"write tool and you do NOT publish; the RUNTIME stages your output. Read "
        f"only your declared read surfaces.\n\n"
        f"Your job is COMPLETE ONLY when you emit your finished content as EXACTLY "
        f"these file(s) — {files_phrase} — each inside its OWN delimited block, using "
        f"this EXACT protocol:\n"
        f"{emit_lines}\n"
        f"Do NOT author a meta.json or a slug — the runtime records identity from the "
        f"resolved input. Do NOT wrap bodies in markdown fences. Prose outside blocks "
        f"is ignored, but a run that omits the required file, leaves a block "
        f"unterminated, or emits an empty body is an INCOMPLETE run.\n\n"
        f"{directive_block}"
        f"RESOLVED INPUT:\n"
        f"{json.dumps(json_payload, ensure_ascii=False, indent=2)}"
    )


# ── emit_package transport (wiki-writer-structured-output-v1 P1) ────────────
# Schema-bound JSON tool emission replaces the sentinel free-text protocol,
# per producer, behind the record-declared flag (GATE-B F6 dual-read). The
# record's emission_preconditions.terminal_artifact.emit block is the source
# of truth; the harness DERIVES the registered schema from it (GATE-B F5).


def _emit_declaration(cap) -> Optional[Dict[str, Any]]:
    """The record's validated emit declaration, or None.

    None (absent, non-mapping, or loader-flagged ``emit_error``) resolves to
    the sentinel transport — the migration default. The loader already
    validated shape at load (grove/capability.py:_validate_emit, C1 pattern);
    the emit_error check here keeps this seam fail-closed to sentinel rather
    than trusting a block the loader flagged. getattr (not attribute access):
    a record with no governance at all is the ABSENT case, not an error.
    """
    gov = getattr(cap, "governance", None) or {}
    ta = (
        ((gov.get("emission_preconditions") or {}) if isinstance(gov, dict) else {})
        .get("terminal_artifact")
        or {}
    )
    emit = ta.get("emit") if isinstance(ta, dict) else None
    if not isinstance(emit, dict) or (isinstance(ta, dict) and ta.get("emit_error")):
        return None
    return emit


def _derive_emit_spec(
    emit_decl: Dict[str, Any],
    *,
    declarative: bool,
    content_files: Optional[List[str]],
    payload: Any,
    worker_id: str,
):
    """Resolve the emit_package contract for this run from the declaration.

    Returns ``(expected_files, meta_required_keys, slug, synth_meta)``.
    Declarative producer: files derive from terminal_artifact.path_pattern +
    unit_id (the existing :func:`_declarative_content_files` derivation — no
    duplicate declaration to drift against); identity is runtime-synthesized.
    Self-authored producer (forge): the record names its file set and its
    required meta keys (slug mandatory — it is the staging directory). A
    tool-transport declaration too thin to derive a contract is a LOUD Andon.
    """
    from grove.fleet.errors import FleetWorkerAndon

    if declarative:
        if not content_files:
            raise FleetWorkerAndon(
                f"worker {worker_id!r}: tool-transport declarative producer "
                f"resolved no content files — cannot derive an emit contract",
                worker_id=worker_id,
                check="emit_spec_missing",
            )
        unit_id = payload.get("unit_id") if isinstance(payload, dict) else None
        return (
            list(content_files),
            None,
            unit_id,
            _synthesize_meta(payload, worker_id, unit_id),
        )
    files_decl = (emit_decl.get("files") or {}).get("required")
    meta_keys = (emit_decl.get("meta") or {}).get("required_keys")
    if not files_decl or not meta_keys or "slug" not in meta_keys:
        raise FleetWorkerAndon(
            f"worker {worker_id!r}: self-authored tool-transport producer needs "
            f"emit.files.required AND emit.meta.required_keys including 'slug' "
            f"(got files={files_decl!r}, meta_keys={meta_keys!r}) — fix the "
            f"capability record's emit block",
            worker_id=worker_id,
            check="emit_spec_missing",
        )
    return list(files_decl), list(meta_keys), None, None


def _build_worker_prompt_tool(
    skill_name: str,
    payload: Any,
    expected_files: List[str],
    meta_required_keys: Optional[List[str]],
) -> str:
    """The tool-transport worker prompt: advertises emit_package and DROPS the
    sentinel protocol text entirely (GATE-B F6 — the flag flips the contract
    the model is given; the harness still dual-reads both this phase). The
    opening discipline paragraphs match the sentinel variant verbatim."""
    directive = payload.get("revision_directive") if isinstance(payload, dict) else None
    if directive:
        directive_block = (
            "REVISION DIRECTIVE (authoritative — the new draft MUST satisfy this):\n"
            f"{directive}\n\n"
        )
        json_payload = {k: v for k, v in payload.items() if k != "revision_directive"}
    else:
        directive_block = ""
        json_payload = payload
    files_phrase = ", ".join(expected_files)
    if meta_required_keys is None:
        emit_para = (
            f"Your job is COMPLETE ONLY when you call the emit_package tool "
            f"EXACTLY ONCE with your finished content: 'files' must map EXACTLY "
            f"these file name(s) — {files_phrase} — each to its complete raw "
            f"body. Do NOT author a meta.json or a slug — the runtime records "
            f"identity from the resolved input. Do NOT wrap bodies in markdown "
            f"fences. A run that never calls emit_package produces NO output "
            f"and is an INCOMPLETE run."
        )
    else:
        meta_phrase = ", ".join(meta_required_keys)
        emit_para = (
            f"Your job is COMPLETE ONLY when you call the emit_package tool "
            f"EXACTLY ONCE with your finished package: 'files' must map EXACTLY "
            f"these file names — {files_phrase} — each to its complete raw "
            f"body, and 'meta' must carry your routing metadata including: "
            f"{meta_phrase} (the runtime stages the package under meta.slug). "
            f"Do NOT wrap bodies in markdown fences. A run that never calls "
            f"emit_package produces NO output and is an INCOMPLETE run."
        )
    return (
        f"You are an autonomous, non-interactive fleet background worker. You are "
        f"EXECUTING a job, not describing one. Your FIRST step is to call "
        f"skill_view('{skill_name}'): what it returns is your OPERATING PROCEDURE "
        f"to carry out, NOT reference material to summarize or report on. Then "
        f"perform that procedure to completion against the resolved input below.\n\n"
        f"No operator is present — do NOT ask clarifying questions. You have NO "
        f"write tool and you do NOT publish; the RUNTIME stages your output. Read "
        f"only your declared read surfaces.\n\n"
        f"{emit_para}\n\n"
        f"{directive_block}"
        f"RESOLVED INPUT:\n"
        f"{json.dumps(json_payload, ensure_ascii=False, indent=2)}"
    )


# The agent loop's two truncation terminals (run_agent.py: truncated tool-call
# retry exhaustion, and text-continuation exhaustion). Worker-level guard keys
# on these result shapes — plus finish_reason=='length' on the final assistant
# message, which now folds in OpenRouter's native_finish_reason (P0 finding 1:
# the top-level finish_reason lies on OpenRouter; the native field is truth).
_TRUNCATION_ERRORS = frozenset(
    {
        "Response truncated due to output length limit",
        "Response remained truncated after 3 continuation attempts",
    }
)


def _is_truncation_result(result: Any) -> bool:
    """True when a run_conversation result is truncation-shaped (cap-hit)."""
    if not isinstance(result, dict):
        return False
    if result.get("error") in _TRUNCATION_ERRORS:
        return True
    for msg in reversed(result.get("messages") or []):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return msg.get("finish_reason") == "length"
    return False


def _strip_fences(lines: List[str]) -> str:
    """Drop a single leading fence line and a single trailing fence line, then join.

    The producer contract forbids markdown fences, but a model may still wrap a body
    in ``` ```markdown ``` / ``` ```text ``` / ``` ```json ``` (or a bare ``` ``` ```).
    A leading line whose first non-space chars are ``` ``` ``` is dropped, and a
    trailing line that is a bare fence is dropped, BEFORE the body is recorded. The
    caller ``.strip()``s the result; an empty body then fails loud.
    """
    buf = list(lines)
    if buf and buf[0].lstrip().startswith("```"):
        buf = buf[1:]
    if buf and buf[-1].strip().startswith("```"):
        buf = buf[:-1]
    return "\n".join(buf)


def _is_safe_basename(name: str, sink: Any) -> bool:
    """True iff *name* is one safe path component that cannot escape *sink*.

    Basename jail (BEFORE any Path/write): reject empty, ``.``/``..``, or any OS
    separator. Then a resolved sink-prefix check (``realpath(join(sink, name))``
    within ``realpath(sink)``) as the ratified second layer — parity with the
    write-side ``is_relative_to`` jail in ``stage_package``.
    """
    if not name or name in (".", "..") or os.sep in name or (os.altsep and os.altsep in name):
        return False
    sink_real = os.path.realpath(str(sink))
    dest_real = os.path.realpath(os.path.join(sink_real, name))
    return dest_real == sink_real or dest_real.startswith(sink_real + os.sep)


def _parse_delimited_blocks(messages, tag: str, sink: Any):
    """Parse the worker's delimited, sentinel-framed per-file emit (Path B) into a
    ``{filename: body}`` map. Returns ``(files, None)`` on a clean parse else
    ``(None, reason)`` — every malformed transition fails LOUD (never a partial
    stage). Delimited-ONLY: no JSON transport fallback (it masked forensics and
    produced the unescaped-quote failure class byte-confirmed on run f157eb558b).

    The per-run *tag* (``run_id[:8]``) frames the sentinels so a body line spoofing a
    marker with a DIFFERENT tag is text, not a marker. Producer-agnostic — the CALLER
    applies the required-file / identity contract (forge names its triad + reads
    meta.json's slug; a declarative producer names its content file + the runtime
    synthesizes identity)."""
    text = _final_assistant_text(messages)
    if not text:
        return None, "no-files"

    start_prefix = "@@@FILE_START: "
    end_prefix = "@@@FILE_END: "
    suffix = f" [{tag}]@@@"

    def is_start(line: str) -> bool:
        s = line.rstrip()
        return s.startswith(start_prefix) and s.endswith(suffix)

    def is_end(line: str) -> bool:
        s = line.rstrip()
        return s.startswith(end_prefix) and s.endswith(suffix)

    def name_of(line: str, prefix: str) -> str:
        return line.rstrip()[len(prefix):-len(suffix)].strip()

    files: Dict[str, str] = {}
    cur: Optional[str] = None  # None == WAITING_FOR_START; else IN_FILE(cur)
    buf: List[str] = []

    for line in text.splitlines():
        if cur is None:  # WAITING_FOR_START
            if is_start(line):
                nm = name_of(line, start_prefix)
                if not nm:
                    return None, "empty-filename"
                if not _is_safe_basename(nm, sink):
                    return None, f"unsafe-filename:{nm}"
                if nm in files:
                    return None, f"duplicate-file:{nm}"
                cur, buf = nm, []
            elif is_end(line):
                return None, "end-without-start"
            # else: prose OUTSIDE any block (e.g. a preamble) → ignored
        else:  # IN_FILE(cur)
            if is_start(line):
                return None, f"missing-end:{cur}"
            elif is_end(line):
                if name_of(line, end_prefix) != cur:
                    return None, f"mismatched-end:{cur}"
                body = _strip_fences(buf).strip()
                if not body:
                    return None, f"empty-body:{cur}"
                files[cur] = body
                cur, buf = None, []
            else:
                buf.append(line)

    if cur is not None:
        return None, f"unterminated:{cur}"
    if not files:
        return None, "no-files"
    return files, None


def _dispatched_unit_id(payload: Any) -> Optional[str]:
    """The HOST-minted unit identity for this run (fleet-receipt-custody-v1
    P1.1): the resolver's ``unit_id`` (resolvers.py — ``rows[0]["id"]`` for a
    notion producer), or None on a payload that predates the unit_id seam.
    The single source every binding/receipt site reads — never the model."""
    return payload.get("unit_id") if isinstance(payload, dict) else None


def _is_declarative_payload(payload: Any) -> bool:
    """Pure payload-shape predicate (C1b-2): a file producer's resolver payload
    carries ``units``, never ``rows``. Shared by ``run_worker`` and ``main()``'s
    catch-all so the receipt's identity FIELD convention (unit_id vs row_id)
    never needs the capability record in scope (P1.2 Commit C)."""
    return isinstance(payload, dict) and "units" in payload and "rows" not in payload


def _bind_identity(files: Dict[str, str], bound_row_id: Optional[str]) -> Dict[str, str]:
    """fleet-receipt-custody-v1 P1.1 — runtime-bound identity on a SELF-AUTHORED
    package: overwrite meta.json's ``row_id`` with the dispatched unit identity
    before staging. Descriptive fields (slug, company, role) stay model-authored
    — this is the narrow identity slot, not a merge engine. No-op without a
    bound id (legacy payloads without ``unit_id``) so the sentinel byte-parity
    baseline is untouched. The extractor already validated meta.json parses."""
    if not bound_row_id:
        return files
    out = dict(files)
    meta = json.loads(out["meta.json"])
    meta["row_id"] = bound_row_id
    out["meta.json"] = json.dumps(meta, ensure_ascii=False, indent=2)
    return out


def _extract_fleet_package(messages, tag: str, sink: Any, required_files):
    """Self-authored package (forge): parse the emit, require *required_files*, and
    recover the slug from the skill's OWN ``meta.json`` body. ALL files, meta
    included, travel the SAME protocol; reading meta.json's JSON for the slug is
    reading one file, not a transport fallback. Returns ``({"slug", "files"}, None)``
    or ``(None, reason)``. (C1b-2: the block parse is shared with the declarative
    extractor; forge's identity contract — required set + meta/slug — is unchanged.)
    """
    from grove.fleet.staging import _SLUG_RE

    files, reason = _parse_delimited_blocks(messages, tag, sink)
    if files is None:
        return None, reason

    missing = set(required_files) - set(files)
    if missing:
        return None, f"missing-required-files:{sorted(missing)}"

    meta_raw = files.get("meta.json")
    if meta_raw is None:
        return None, "bad-meta"
    try:
        meta = json.loads(meta_raw)
    except (json.JSONDecodeError, TypeError):
        return None, "bad-meta"
    slug = meta.get("slug") if isinstance(meta, dict) else None
    if not (isinstance(slug, str) and _SLUG_RE.match(slug)):
        return None, "bad-meta"

    return {"slug": slug, "files": files}, None


def _extract_declarative_content(messages, tag: str, sink: Any, required_content_files):
    """Declarative producer (drafter/cultivator, C1b-2): parse the emit and take ONLY
    the runtime-declared content file(s) — the skill authors content, NOT identity.
    The runtime synthesizes ``meta.json`` from the resolver payload (the skill never
    authors its own slug), so a stray skill-emitted meta.json is DISCARDED here, not
    honored. Returns ``({"files": {content-only}}, None)`` or ``(None, reason)``."""
    files, reason = _parse_delimited_blocks(messages, tag, sink)
    if files is None:
        return None, reason
    missing = set(required_content_files) - set(files)
    if missing:
        return None, f"missing-required-files:{sorted(missing)}"
    # content-only: drop any extra emitted file (incl. a stray meta.json — identity
    # is the runtime's, synthesized from the resolver payload).
    content = {name: files[name] for name in required_content_files}
    return {"files": content}, None


def _final_assistant_text(messages) -> str:
    """Best-effort extraction of the run's final assistant text for staging."""
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            joined = "\n".join(t for t in texts if t)
            if joined:
                return joined
    return ""


def _persist_raw_output(worker_id: str, run_id: str, text: str) -> Optional[str]:
    """Sidecar a failed run's raw final assistant text next to its terminal event.

    fleet-failure-forensics-v1 — a ``no_package`` failure discards the model's
    actual output, leaving zero diagnostic. Persist that output verbatim to
    ``events/<run_id>.raw.txt`` (sibling of the event JSON) so the failure is
    inspectable. Best-effort BY CONTRACT: any write error is swallowed and None is
    returned — a forensic sidecar must NEVER mask the original failure with a
    second one. Returns the path on success, None on any write failure.
    """
    from grove.fleet import paths

    try:
        raw_path = paths.event_path(worker_id, run_id).with_suffix(".raw.txt")
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(text or "", encoding="utf-8")
        return str(raw_path)
    except Exception:  # noqa: BLE001 — sidecar never masks the real terminal failure
        return None


# ---------------------------------------------------------------------------
# drafter-quality-checks-v1 P3 — the post-staging quality gate.
#
# ONE transport-agnostic gate site (R-A2): run_worker normalizes every success
# path (tool-emitted, sentinel, and the F6 dual-read fallback) into a common
# staging outcome, then calls _apply_quality_gate exactly once, BEFORE the
# success event. The gate keys on record-block presence only (R-A11): a record
# without a valid governance.quality_gate passes through untouched except for
# the four always-present null rider fields.
#
# The gate informs disposition; it NEVER withholds staged work: pass,
# skipped_oversize, fail-after-redraft, and even a redraft that produces no
# package (draft #1 restored from the archive) all proceed to the success
# event with the final score attached.
# ---------------------------------------------------------------------------

_UNGATED_QUALITY_KW: Dict[str, Any] = {
    "quality_score": None,
    "rubric_version": None,
    "redraft_count": None,
    "evaluator_model": None,
}


def _quality_task_context(gate: Dict[str, Any], payload: Any) -> Optional[Dict[str, Any]]:
    """A1 (R-A12) — resolve the gate's declared ``context_inputs`` against the
    dispatch payload (the SAME payload the prompt renderer consumes). Returns
    the present subset; a missing declared key is noted by the evaluator in
    the verdict (``context_keys_missing``), never a run failure. None when the
    record declares no context_inputs (criteria-only evaluation)."""
    keys = list(gate.get("context_inputs") or [])
    if not keys:
        return None
    if not isinstance(payload, dict):
        return {}
    return {k: payload[k] for k in keys if k in payload}


def _evaluate_or_andon(cap, worker_id: str, staged_files: Dict[str, str], task_context):
    """Run the evaluator; convert ANY evaluator exception into the Andon the
    reap → ledger → triage chain consumes (R-A10: catch-and-log is a SPEC
    violation — an evaluator failure rides the failed-event loudly)."""
    from grove.fleet import quality as fleet_quality
    from grove.fleet.errors import FleetWorkerAndon

    # meta.json is the runtime identity envelope (every transport stages one),
    # not draft content — the rubric evaluates the draft, not the plumbing.
    draft_files = {k: v for k, v in staged_files.items() if k != "meta.json"}
    try:
        return fleet_quality.evaluate_draft(cap, draft_files, task_context)
    except Exception as exc:
        raise FleetWorkerAndon(
            f"quality-gate evaluator call failed for record {cap.id!r}: "
            f"{type(exc).__name__}: {exc} — the declared rubric could not be "
            f"applied; failing loud",
            worker_id=worker_id,
            check="evaluator_call_failed",
        ) from exc


def _archive_staged_draft(cap, worker_id: str, sink: Path, slug: str) -> str:
    """(a) Archive the below-threshold draft #1 OUT of the staging sink into
    the write_zone archive location — ``<canonical_dir>/<archive_dir>/
    <slug>-<utc-ts>/``, the shipped reject-archive naming (actions.py
    ``_archive_forge_slug``, canonized by the purge core). One atomic rename
    within the one ~/.grove mount; the timestamped destination never
    overwrites (R-A6 — and a same-instant collision makes the rename itself
    fail loud rather than merge)."""
    from datetime import datetime, timezone

    from grove.fleet.errors import FleetWorkerAndon
    from grove.utils.fs_utils import _grove_home_realpath, _grove_subdir_realpath

    gov = cap.governance if isinstance(getattr(cap, "governance", None), dict) else {}
    wz = gov.get("write_zone") or {}
    canonical = wz.get("canonical_dir")
    if not canonical:
        raise FleetWorkerAndon(
            f"capability {cap.id!r} declares a quality_gate but no "
            f"governance.write_zone.canonical_dir — the redraft cycle has no "
            f"declared archive location for draft #1",
            worker_id=worker_id,
            check="no_archive_location",
        )
    archive_rel = (wz.get("retention") or {}).get("archive_dir") or ".archive"
    grove = _grove_home_realpath()
    canonical_root = Path(_grove_subdir_realpath(canonical, grove))
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = canonical_root / archive_rel / f"{slug}-{ts}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    (sink / slug).rename(dest)
    return str(dest)


def _restore_archived_draft(sink: Path, slug: str, archive_path: str) -> None:
    """Put draft #1 back when the redraft produced no package — the gate never
    withholds work. A restore failure raises loudly (main() → failed event)."""
    Path(archive_path).rename(sink / slug)


def _apply_quality_gate(
    *,
    cap,
    worker_id: str,
    run_id: str,
    payload: Any,
    agent,
    sink: Path,
    transport: str,
    declarative: bool,
    content_files: Optional[List[str]],
    result: Dict[str, Any],
    staged_list: List[str],
    staged_files: Dict[str, str],
    pkg_slug: str,
    success_detail: str,
):
    """The ONE post-staging quality-gate site (R-A2, transport-agnostic).

    Returns ``(quality_kw, staged_list, pkg_slug, success_detail)`` — the four
    event rider fields plus the (possibly re-staged) paths/slug/detail. An
    ungated record returns the four null keys and everything else untouched
    (byte-identical event behavior for ungated workers).
    """
    from grove.fleet import quality as fleet_quality

    gate = fleet_quality.quality_gate_declaration(cap)
    if gate is None:
        return dict(_UNGATED_QUALITY_KW), staged_list, pkg_slug, success_detail

    task_context = _quality_task_context(gate, payload)
    verdict = _evaluate_or_andon(cap, worker_id, staged_files, task_context)
    redraft_count = 0

    # Fail + a redraft budget (schema validates redraft_limit == 1 in v1) →
    # one governed redraft cycle. Pass and skipped_oversize proceed as-is.
    if verdict["status"] == "fail" and int(gate["redraft_limit"]) > 0:
        redraft_count = 1
        staged_list, staged_files, pkg_slug, verdict, success_detail = _redraft_cycle(
            cap=cap,
            worker_id=worker_id,
            run_id=run_id,
            payload=payload,
            agent=agent,
            sink=sink,
            transport=transport,
            declarative=declarative,
            content_files=content_files,
            result=result,
            staged_list=staged_list,
            staged_files=staged_files,
            pkg_slug=pkg_slug,
            success_detail=success_detail,
            verdict=verdict,
            task_context=task_context,
        )

    quality_kw = {
        "quality_score": verdict["quality_score"],
        "rubric_version": verdict["rubric_version"],
        "redraft_count": redraft_count,
        "evaluator_model": verdict["evaluator_model"],
    }
    return quality_kw, staged_list, pkg_slug, success_detail


def _redraft_cycle(
    *,
    cap,
    worker_id: str,
    run_id: str,
    payload: Any,
    agent,
    sink: Path,
    transport: str,
    declarative: bool,
    content_files: Optional[List[str]],
    result: Dict[str, Any],
    staged_list: List[str],
    staged_files: Dict[str, str],
    pkg_slug: str,
    success_detail: str,
    verdict: Dict[str, Any],
    task_context,
):
    """One governed redraft cycle (v1: exactly one, schema-enforced).

    (a) archive draft #1 (never overwrite, R-A6) → (b) tool path: reset +
    re-arm the emit tool with the SAME record-derived spec (the lock
    re-engages on the redraft emit, R-B4) → (c) ONE fresh continuation
    re-prompt via ``run_conversation(next_prompt, conversation_history=
    history)`` — the emit-ladder pattern — carrying the verdict issues
    verbatim and authorizing exactly one further emission → (d) re-stage +
    re-evaluate ONCE → (e) proceed regardless: the final verdict rides the
    event whether it passed or not.

    A redraft that produces NO package (or hits a governed denial) restores
    draft #1 from the archive and proceeds on the ORIGINAL verdict — the gate
    informs disposition; it never withholds work.

    Returns ``(staged_list, staged_files, pkg_slug, verdict, success_detail)``.
    """
    from grove.governance_halt import TerminalGovernanceHalt

    archive_path = _archive_staged_draft(cap, worker_id, sink, pkg_slug)
    logger.info(
        "[fleet:%s] quality gate failed (score=%s < threshold=%s); draft #1 "
        "archived to %s; running one governed redraft.",
        worker_id,
        verdict["quality_score"],
        verdict["threshold"],
        archive_path,
    )

    if transport == "tool":
        from tools import fleet_emit_tool
        from tools.registry import invalidate_check_fn_cache

        # Re-derive the SAME record-declared spec (deterministic) and re-arm:
        # reset() disarms the run-scoped lock; configure() re-engages the
        # contract so the authorized redraft emit stages-and-locks exactly like
        # the first (R-B4: a third call hits the lock refusal).
        emit_decl = _emit_declaration(cap)
        expected_files, meta_keys, unit_slug, synth = _derive_emit_spec(
            emit_decl,
            declarative=declarative,
            content_files=content_files,
            payload=payload,
            worker_id=worker_id,
        )
        fleet_emit_tool.reset()
        fleet_emit_tool.configure(
            expected_files=expected_files,
            meta_required_keys=meta_keys,
            sink=sink,
            slug=unit_slug,
            synth_meta=synth,
            # P1.1 — the redraft emit re-binds the SAME dispatched identity.
            bound_row_id=None if declarative else _dispatched_unit_id(payload),
        )
        invalidate_check_fn_cache()

    bullets = "\n".join(f"- {i}" for i in verdict["issues"]) or (
        "- (no specific issues listed)"
    )
    channel = (
        "call emit_package EXACTLY ONE more time with the complete revised "
        "file(s)"
        if transport == "tool"
        else "re-emit the complete revised file(s) using the same delimited "
        "protocol as before"
    )
    redraft_prompt = (
        "Your draft was evaluated against the skill's quality rubric "
        f"(score {verdict['quality_score']:.2f}, threshold "
        f"{verdict['threshold']:.2f}) and did not pass.\n"
        f"Issues to fix:\n{bullets}\n\n"
        f"Produce a complete REVISED draft that addresses every issue, then "
        f"{channel}. Do not reply with prose."
    )
    history = result.get("messages")
    try:
        result2 = agent.run_conversation(
            redraft_prompt, conversation_history=history, task_id=run_id
        )
    except TerminalGovernanceHalt as tgh:
        logger.warning(
            "[fleet:%s] governed denial during the redraft cycle (%s); draft #1 "
            "restored and proceeding on the original verdict.",
            worker_id,
            tgh,
        )
        _restore_archived_draft(sink, pkg_slug, archive_path)
        return (
            staged_list,
            staged_files,
            pkg_slug,
            verdict,
            success_detail + "; redraft_denied draft1_restored",
        )

    # (d) re-stage — same recovery machinery as the first pass, one attempt.
    new_staged: Optional[List[str]] = None
    new_files: Optional[Dict[str, str]] = None
    new_slug = pkg_slug
    if transport == "tool":
        emitted2 = fleet_emit_tool.emitted()
        if emitted2 is not None:
            new_staged = list(emitted2["staged"])
            new_files = dict(emitted2["files"])
            new_slug = pkg_slug if declarative else emitted2["slug"]
    else:
        from grove.fleet.staging import stage_package

        if declarative:
            extracted2, _reason2 = _extract_declarative_content(
                result2.get("messages"), run_id[:8], sink, content_files
            )
            if extracted2 is not None:
                files2 = dict(extracted2["files"])
                files2["meta.json"] = _synthesize_meta(payload, worker_id, pkg_slug)
                new_staged = [str(p) for p in stage_package(sink, pkg_slug, files2)]
                new_files = files2
        else:
            extracted2, _reason2 = _extract_fleet_package(
                result2.get("messages"), run_id[:8], sink, _SELF_AUTHORED_REQUIRED
            )
            if extracted2 is not None:
                new_slug = extracted2["slug"]
                # P1.1 — the redraft re-stage binds the SAME dispatched identity.
                bound2 = _bind_identity(
                    extracted2["files"], _dispatched_unit_id(payload)
                )
                new_staged = [
                    str(p) for p in stage_package(sink, new_slug, bound2)
                ]
                new_files = dict(bound2)

    if new_staged is None or new_files is None:
        logger.warning(
            "[fleet:%s] redraft produced no package; draft #1 restored and "
            "proceeding on the original verdict.",
            worker_id,
        )
        _restore_archived_draft(sink, pkg_slug, archive_path)
        return (
            staged_list,
            staged_files,
            pkg_slug,
            verdict,
            success_detail + "; redraft_no_package draft1_restored",
        )

    # Re-evaluate ONCE; (e) proceed regardless — the final score rides.
    verdict2 = _evaluate_or_andon(cap, worker_id, new_files, task_context)
    return (
        new_staged,
        new_files,
        new_slug,
        verdict2,
        success_detail + f"; redrafted draft1_archived={archive_path}",
    )


def run_worker(worker_id: str, run_id: str, payload: Any) -> Dict[str, Any]:
    """Execute one worker run and return its terminal-state event dict.

    Raises FleetWorkerAndon / other exceptions on structural failure; ``main``
    converts an uncaught exception into a ``failed`` terminal event. A governed
    denial (TerminalGovernanceHalt) is caught here and reported as ``failed``.
    """
    from gateway.session_context import clear_session_vars, set_session_vars
    from grove.dispatcher import Dispatcher
    from grove.fleet import paths
    from grove.fleet.read_surfaces import enforce_declared_surfaces
    from grove.fleet.staging import stage_package
    from grove.grants import get_grant_store
    from grove.governance_halt import TerminalGovernanceHalt
    from grove.sovereign_prompt_handlers import non_interactive_deny_handler
    from hermes_state import SessionDB

    paths.validate_worker_id(worker_id)
    session_key = f"fleet:{worker_id}:{run_id}"

    # (a) session vars — cleared in the finally.
    tokens = set_session_vars(
        platform="fleet",
        session_key=session_key,
        user_id=f"system:fleet:{worker_id}",
    )
    try:
        # (b) grant-less principal: point the process-global GrantStore at the
        # worker's grants file, which is NEVER created -> GrantStore is
        # fail-closed on a missing file -> no standing grants exist.
        get_grant_store(grants_path=paths.grantless_grants_path(worker_id))

        # Load record + enforce read_surfaces BEFORE running anything (item 3).
        cap = _load_capability_for(worker_id)
        enforce_declared_surfaces(cap, worker_id)  # index surface -> loud Andon
        # fleet-corpus-only-offering-v1 P1/P2 — the corpus-only tool surface is
        # enforced by TWO independent controls with SEPARATE trust roots (no
        # common-mode SPOF):
        #   L2 (P1): a config-BLIND floor hardcoded in the Dispatcher, keyed on
        #            platform=='fleet' -> {read_file, skill_view} (the ceiling).
        #   L1 (P2): a per-spawn allow-list on the RuntimeContext CONFIG, read at the
        #            top of run_agent._maybe_apply_tool_filter, which REPLACES the
        #            whole per-turn offered surface with exactly these tools (the
        #            enforced offering). Its trust root is this config key, NOT the
        #            platform hardcode — deliberately decoupled from L2.
        from hermes_cli.config import load_config

        sink = _resolve_declared_sink(cap, worker_id)
        sink.mkdir(parents=True, exist_ok=True)

        # Legitimate empty work: the ticker normally only spawns on work, but a
        # None payload is an explicit no_work signal — do not run the skill.
        if payload is None:
            return _event(worker_id, run_id, cap.id, "no_work", detail="empty payload")

        # fleet-review-unification-v1 C1b-2 — emission style. A file producer
        # (file_source resolver → payload carries "units", never "rows") uses
        # DECLARATIVE emission: the skill authors content only, named by the record's
        # terminal_artifact, and the RUNTIME synthesizes the identity envelope from
        # the resolver payload. A notion producer (forge) keeps its self-authored
        # path. (P1: resolved BEFORE Dispatcher construction now — the tool
        # transport must arm the emit tool and widen the allow-list before the
        # agent's tool surface is built and cached.)
        declarative = _is_declarative_payload(payload)
        content_files = (
            _declarative_content_files(cap, payload, worker_id) if declarative else None
        )

        # wiki-writer-structured-output-v1 P1 — record-declared emit transport
        # (GATE-B F6 dual-read migration; default sentinel). transport=="tool":
        # derive the emit_package contract from the record (GATE-B F5), arm the
        # run-scoped tool module, and admit emit_package on BOTH offer gates
        # (the L2 floor already ceilings it; this L1 allow-list enables it).
        emit_decl = _emit_declaration(cap)
        transport = (emit_decl or {}).get("transport", "sentinel")
        expected_files: Optional[List[str]] = None
        meta_keys: Optional[List[str]] = None
        if transport == "tool":
            from tools import fleet_emit_tool

            expected_files, meta_keys, unit_slug, synth = _derive_emit_spec(
                emit_decl,
                declarative=declarative,
                content_files=content_files,
                payload=payload,
                worker_id=worker_id,
            )
            fleet_emit_tool.reset()
            fleet_emit_tool.configure(
                expected_files=expected_files,
                meta_required_keys=meta_keys,
                sink=sink,
                slug=unit_slug,
                synth_meta=synth,
                # P1.1 — the host-identity slot: the dispatched unit id binds
                # meta.row_id at emit for a self-authored producer (forge).
                bound_row_id=None if declarative else _dispatched_unit_id(payload),
            )
            allowlist = ["read_file", "skill_view", "emit_package"]
        else:
            allowlist = ["read_file", "skill_view"]

        worker_config = {
            **load_config(),
            "fleet_offered_allowlist": allowlist,
        }

        # (c)+(d) install the deny handler and an ISOLATED session DB, then
        # (e) run the pinned skill via the Dispatcher — reuse skill-invoke whole.
        session_db = SessionDB(db_path=paths.session_db_path(worker_id))
        skill_name = _derive_skill_name(cap, worker_id)
        model, max_tokens, runtime = _resolve_worker_runtime(cap, worker_id)
        # binding-governance-surfaces-v1 P4 — binding-identity telemetry rider
        # (GATE-A D9/FLAG-9: no intent record fires on the fleet plane, so the
        # terminal EVENT carries the effective model identity). Derived from
        # values in scope at spawn, on BOTH resolve branches: the same
        # mb.type=="model" predicate the resolver just honored (a malformed pin
        # already Andon'd inside _resolve_worker_runtime, so reaching here means
        # the predicate and the resolution agree).
        _mb = getattr(cap, "model_binding", None)
        binding_kw = dict(
            model=model,
            tier=f"T{cap.tier_rule.preferred}",
            binding_source=(
                "pinned" if (_mb is not None and _mb.type == "model")
                else "inherited"
            ),
        )

        # The per-spawn RuntimeContext carries the base config; the fleet L2 floor
        # (Dispatcher.get_authorized_tools, platform=='fleet') is config-blind, so no
        # deny-complement injection happens here. platform='fleet' is passed to the
        # DISPATCHER itself (not only agent_kwargs) so self._platform=='fleet' and the
        # L2 floor fires — the prior code set platform ONLY in agent_kwargs, leaving
        # the Dispatcher default 'cli', which is why P5's 'fleet'-keyed deny-complement
        # silently never applied (the leg-1 write_file escape). agent_kwargs keeps
        # platform='fleet' too, for AIAgent.platform.
        from grove.dispatcher import RuntimeContext

        dispatcher = Dispatcher(
            runtime_ctx=RuntimeContext(env=dict(os.environ), config=worker_config),
            session_db=session_db,
            sovereign_prompt_handler=non_interactive_deny_handler,
            platform="fleet",
            agent_kwargs=dict(
                model=model,
                max_tokens=max_tokens,
                api_key=runtime.get("api_key"),
                base_url=runtime.get("base_url"),
                provider=runtime.get("provider"),
                api_mode=runtime.get("api_mode"),
                max_iterations=WORKER_MAX_ITERATIONS,
                quiet_mode=True,
                verbose_logging=False,
                session_id=run_id,
                platform="fleet",
            ),
        )
        agent = dispatcher.agent

        # Prompt contract follows the transport flag (GATE-B F6): the tool
        # variant advertises emit_package and DROPS the sentinel protocol text;
        # the sentinel variant is byte-identical to the pre-P1 prompt. The
        # sentinel prompt-side tag and the parser-side tag MUST be the identical
        # run_id[:8] (a mismatch = markers the parser rejects = every run no-files).
        if transport == "tool":
            prompt = _build_worker_prompt_tool(
                skill_name, payload, expected_files, meta_keys
            )
        else:
            prompt = _build_worker_prompt(
                skill_name, payload, run_id[:8], content_files=content_files
            )
        try:
            result = agent.run_conversation(prompt, task_id=run_id)
        except TerminalGovernanceHalt as tgh:
            # A grant-less worker hit an ungranted Yellow/Red action; the deny
            # handler fired. This is a completed-with-denial run: failed state,
            # diagnostics preserved. P1.2 — the receipt carries the dispatched
            # identity: a governed denial recurs deterministically (the worker
            # is blocked), so an identity-less receipt is the purest
            # uncountable poison pill.
            _unit = _dispatched_unit_id(payload)
            return _event(
                worker_id,
                run_id,
                cap.id,
                "failed",
                detail=f"governed denial: {tgh}",
                check="governed_denial",
                **({"unit_id": _unit} if declarative else {"row_id": _unit}),
                **binding_kw,
            )

        # drafter-quality-checks-v1 P3 — normalized staging outcome. Each
        # branch (tool-emitted, sentinel, F6 dual-read fallback) fills these,
        # then falls into the SINGLE quality-gate site + success emission at
        # the bottom (R-A2). None = not yet staged.
        staged_list: Optional[List[str]] = None
        staged_files: Dict[str, str] = {}
        pkg_slug = ""
        event_kw: Dict[str, Any] = {}
        success_detail = ""

        # ── wiki-writer-structured-output-v1 P1 — emit lifecycle ladder ──
        # Tool transport only. Lock-on-emit means a locked package was ALREADY
        # validated + atomically staged by the handler; here the run recovers
        # from the two known no-emit shapes, each bounded to ONE attempt:
        #   * truncation-shaped result → raised-cap FRESH re-run (P0 findings:
        #     identical-at-cap retry is deterministic 0/6; raised-cap 2/2);
        #   * clean end, no emit → ONE re-prompt continuing the conversation.
        # Exhausted → fall through to the dual-read sentinel extraction (F6),
        # then the loud failure event (emit_truncation | no_package).
        if transport == "tool":
            from tools import fleet_emit_tool

            raise_used = False
            reprompt_used = False
            while fleet_emit_tool.emitted() is None:
                if _is_truncation_result(result) and not raise_used:
                    raise_used = True
                    agent.max_tokens = 2 * (agent.max_tokens or max_tokens or 8192)
                    next_prompt, history = prompt, None
                elif not reprompt_used:
                    reprompt_used = True
                    next_prompt = (
                        "You have NOT called emit_package, so this run has "
                        "produced NO output. Call emit_package NOW with your "
                        "complete finished file(s). Do not reply with prose."
                    )
                    history = result.get("messages")
                else:
                    break
                try:
                    result = agent.run_conversation(
                        next_prompt, conversation_history=history, task_id=run_id
                    )
                except TerminalGovernanceHalt as tgh:
                    # P1.2 — same dispatched-identity rider as the first-run
                    # governed_denial site above.
                    _unit = _dispatched_unit_id(payload)
                    return _event(
                        worker_id,
                        run_id,
                        cap.id,
                        "failed",
                        detail=f"governed denial: {tgh}",
                        check="governed_denial",
                        **({"unit_id": _unit} if declarative else {"row_id": _unit}),
                        **binding_kw,
                    )

            emitted = fleet_emit_tool.emitted()
            if emitted is not None:
                # Locked = validated + staged (handler staged atomically via
                # the same jailed stage_package the sentinel path uses).
                # drafter-quality-checks-v1 P3 — the success emission moved to
                # the single post-staging gate site below (R-A2); this branch
                # now only NORMALIZES the staging outcome. Detail strings are
                # byte-identical to the pre-P3 events.
                staged_list = list(emitted["staged"])
                staged_files = dict(emitted["files"])
                if declarative:
                    unit_id = payload["unit_id"]
                    pkg_slug = unit_id
                    event_kw = {"unit_id": unit_id}
                    success_detail = (
                        f"completed={result.get('completed')}; "
                        f"unit={unit_id}; transport=tool"
                    )
                else:
                    row_id, fit_score = _row_identity(emitted, payload)
                    pkg_slug = emitted["slug"]
                    event_kw = {"row_id": row_id, "fit_score": fit_score}
                    success_detail = (
                        f"completed={result.get('completed')}; "
                        f"slug={emitted['slug']}; transport=tool"
                    )
            # No lock — fall through to the sentinel extraction (dual-read):
            # a tool-flagged producer that emitted sentinel blocks anyway is
            # still accepted this migration phase (F6).

        # (f) Option 2: the RUNTIME stages the skill's delimited per-file emit. The
        # skill emits each file inside sentinel-framed blocks (forge-fleet-package-
        # emission-v1, Path B); the parser's state machine recovers {slug, files},
        # and the runtime writes each file atomically into the declared sink under the
        # slug dir, jailed by is_relative_to(sink). The skill never self-writes — so a
        # wall-clock kill cannot leave a half-written file the portal reads. The
        # per-run tag (run_id[:8]) frames the sentinels; forge names its required set.
        # (P3: skipped entirely when the tool path already locked + normalized.)
        if staged_list is None:
            if declarative:
                # Declarative: parse the content file(s) only; the runtime synthesizes
                # meta.json (identity) and stages the package under the resolver's unit_id.
                extracted, reason = _extract_declarative_content(
                    result.get("messages"), run_id[:8], sink, content_files
                )
            else:
                # (f) Option 2: self-authored forge triad — the skill names its files and
                # authors meta.json; the runtime stages under the slug meta declares.
                extracted, reason = _extract_fleet_package(
                    result.get("messages"),
                    run_id[:8],
                    sink,
                    _SELF_AUTHORED_REQUIRED,
                )
            if extracted is None:
                # fleet-failure-forensics-v1 — the model produced output but it did not
                # parse to a valid package; that output is discarded, leaving zero
                # diagnostic without this. Enrich detail with the fail-loud reason + a
                # bounded preview and persist the FULL raw text to an events/<run_id>.raw.txt
                # sidecar. status + check are preserved EXACTLY (reap keys on them); only
                # detail is enriched and the additive raw_text_path is added.
                # P1: a tool-transport run whose FINAL turn is still truncation-shaped
                # (after the bounded raised-cap retry) fails as its own Andon class,
                # emit_truncation — distinct from no_package so the reap/portal can
                # tell "model never emitted" from "the cap ate the emission".
                final_text = _final_assistant_text(result.get("messages") or [])
                preview = (
                    (final_text[:800] + "…") if len(final_text) > 800 else final_text
                ).strip()
                if transport == "tool" and _is_truncation_result(result):
                    fail_check = "emit_truncation"
                    fail_detail = (
                        "emit_package was never locked and the final turn was "
                        "truncation-shaped even after the bounded raised-cap retry "
                        "(P1 truncation guard); sentinel dual-read also found no "
                        f"package (reason: {reason}); final assistant message was: "
                        f"{preview!r}"
                    )
                elif transport == "tool":
                    fail_check = "no_package"
                    fail_detail = (
                        "emit_package was never called (after one bounded re-prompt) "
                        "and the sentinel dual-read found no package "
                        f"(reason: {reason}); final assistant message was: {preview!r}"
                    )
                else:
                    fail_check = "no_package"
                    fail_detail = (
                        "delimited emit did not parse to a valid fleet_package "
                        f"(reason: {reason}); final assistant message was: {preview!r}"
                    )
                # P1.1 — every receipt is attributable: the failure receipt
                # carries the HOST-dispatched unit identity (the model's meta
                # never existed here, and was never the source anyway). Field
                # follows the producer's identity convention: unit_id for a
                # declarative file producer, row_id for forge.
                _unit = _dispatched_unit_id(payload)
                return _event(
                    worker_id,
                    run_id,
                    cap.id,
                    "failed",
                    detail=fail_detail,
                    check=fail_check,
                    raw_text_path=_persist_raw_output(worker_id, run_id, final_text),
                    **({"unit_id": _unit} if declarative else {"row_id": _unit}),
                    **binding_kw,
                )
            if declarative:
                unit_id = payload["unit_id"]
                files = dict(extracted["files"])
                files["meta.json"] = _synthesize_meta(payload, worker_id, unit_id)
                staged = stage_package(sink, unit_id, files)
                staged_list = [str(p) for p in staged]
                staged_files = files
                pkg_slug = unit_id
                event_kw = {"unit_id": unit_id}
                success_detail = f"completed={result.get('completed')}; unit={unit_id}"
            else:
                # P1.1 — sentinel-path identity binding (this site previously
                # had NO injection point for self-authored packages): the
                # dispatched unit id overwrites meta.row_id before staging.
                bound_files = _bind_identity(
                    extracted["files"], _dispatched_unit_id(payload)
                )
                extracted = {**extracted, "files": bound_files}
                staged = stage_package(sink, extracted["slug"], bound_files)
                row_id, fit_score = _row_identity(extracted, payload)
                staged_list = [str(p) for p in staged]
                staged_files = dict(bound_files)
                pkg_slug = extracted["slug"]
                event_kw = {"row_id": row_id, "fit_score": fit_score}
                success_detail = (
                    f"completed={result.get('completed')}; slug={extracted['slug']}"
                )

        # ── drafter-quality-checks-v1 P3 — the ONE quality-gate site (R-A2),
        # after the staging outcome is known on every transport (tool,
        # sentinel, and the F6 dual-read fallback), BEFORE the success event.
        # Ungated records pass through byte-identical except the four
        # always-present null rider fields.
        quality_kw, staged_list, pkg_slug, success_detail = _apply_quality_gate(
            cap=cap,
            worker_id=worker_id,
            run_id=run_id,
            payload=payload,
            agent=agent,
            sink=sink,
            transport=transport,
            declarative=declarative,
            content_files=content_files,
            result=result,
            staged_list=staged_list,
            staged_files=staged_files,
            pkg_slug=pkg_slug,
            success_detail=success_detail,
        )
        # P1.1 A6 telemetry — the stripped-meta-keys rider rides the LATEST
        # locked emit (a redraft's re-emit supersedes the first). None on the
        # sentinel transport and the F6 sentinel-fallback (no meta arg exists).
        stripped_meta: Optional[list] = None
        if transport == "tool":
            from tools import fleet_emit_tool as _fet

            _em = _fet.emitted()
            if _em is not None:
                stripped_meta = _em.get("stripped_meta_keys")
        # ── forge-publish-meta-hotfix-v1 P1 — emit-time meta-completeness check,
        # AFTER the staging outcome is known on every transport (tool + sentinel
        # dual-read), scoped to the self-authored forge triad (declarative
        # producers synthesize their own complete meta at :1305 and cannot hit
        # this). The package is ALREADY staged — this NEVER un-stages it (surface-
        # regardless). A stub meta persists the raw output to the same forensic
        # sidecar a no_package failure uses, and rides a `meta_defect` token on the
        # success event; the manager reads it to fire the loud operator Andon and
        # stamp the promote card. Publish stays endpoint-blocked (actions.py
        # untouched) — this only moves the DISCOVERY of the defect from the
        # operator's Publish tap to emit time.
        meta_defect = None
        raw_forensics_path = None
        if not declarative:
            defects = _forge_meta_defects((staged_files or {}).get("meta.json"))
            if defects:
                meta_defect = "missing:" + ",".join(defects)
                success_detail += f"; meta_defect={meta_defect}"
                raw_forensics_path = _persist_raw_output(
                    worker_id,
                    run_id,
                    _final_assistant_text(result.get("messages") or []),
                )
        return _event(
            worker_id,
            run_id,
            cap.id,
            "success",
            detail=success_detail,
            staged=staged_list,
            slug=pkg_slug,
            meta_defect=meta_defect,
            raw_text_path=raw_forensics_path,
            stripped_meta_keys=stripped_meta,
            **event_kw,
            **quality_kw,
            **binding_kw,
        )
    finally:
        clear_session_vars(tokens)


# Bound at call time so run_worker can be unit-tested with a monkeypatched
# loader; the default resolves the record from the registry.
def _load_capability_for(worker_id: str):
    from grove.fleet.config import load_fleet_workers

    workers = load_fleet_workers()
    cfg = workers.get(worker_id)
    if cfg is None:
        from grove.fleet.errors import FleetWorkerAndon

        raise FleetWorkerAndon(
            f"worker id {worker_id!r} is not declared in fleet_workers.yaml",
            worker_id=worker_id,
            check="worker_not_registered",
        )
    return _load_capability(cfg.skill, worker_id)


def _event(
    worker_id: str,
    run_id: str,
    skill_id: str,
    status: str,
    *,
    detail: str = "",
    staged: Optional[list] = None,
    check: Optional[str] = None,
    slug: Optional[str] = None,
    row_id: Optional[str] = None,
    fit_score: Optional[Any] = None,
    raw_text_path: Optional[str] = None,
    unit_id: Optional[str] = None,
    model: Optional[str] = None,
    tier: Optional[str] = None,
    binding_source: Optional[str] = None,
    quality_score: Optional[float] = None,
    rubric_version: Optional[str] = None,
    redraft_count: Optional[int] = None,
    evaluator_model: Optional[str] = None,
    meta_defect: Optional[str] = None,
    stripped_meta_keys: Optional[list] = None,
) -> Dict[str, Any]:
    # fleet-pipeline-v1 P2 (A1) — additive fields the reap emitter reads OFF the
    # event (never parsed from detail/paths). None for workers that don't produce
    # them; the terminal-state reap keys on presence-of-status, not exact shape,
    # so these additions are tolerated (manager.py:98,109-110). raw_text_path
    # (fleet-failure-forensics-v1) follows the same additive precedent — the path
    # to a failed run's persisted raw output, or None.
    event = {
        "worker_id": worker_id,
        "run_id": run_id,
        "skill": skill_id,
        "status": status,  # success | no_work | failed
        "detail": detail,
        "staged": staged or [],
        "check": check,
        "slug": slug,
        "row_id": row_id,
        "fit_score": fit_score,
        "raw_text_path": raw_text_path,
        # binding-governance-surfaces-v1 P4 — binding-identity rider (GATE-A
        # D9: the fleet plane writes no intent record, so the terminal event
        # is where the effective model identity lands). Same additive-field
        # precedent as raw_text_path: always present, None when the run
        # terminated before spawn resolution (no_work, main()'s failed shape).
        "model": model,
        "tier": tier,
        "binding_source": binding_source,
        # drafter-quality-checks-v1 P3 — the quality-gate rider (same additive
        # always-present precedent as the binding rider above): null on every
        # UNGATED worker's events and on failed/no_work shapes; populated only
        # when a record-declared quality_gate evaluated this run's draft.
        "quality_score": quality_score,
        "rubric_version": rubric_version,
        "redraft_count": redraft_count,
        "evaluator_model": evaluator_model,
        # forge-publish-meta-hotfix-v1 P1 — the emit-time meta-completeness rider
        # (same additive always-present precedent as the quality rider above). None
        # on every complete package and on non-forge/failed/no_work shapes; a short
        # "missing:company,role" token when a forge package staged with an
        # incomplete meta.json. The manager reads it OFF the event to fire the loud
        # Andon and stamp the promote card's defect marker.
        "meta_defect": meta_defect,
        # fleet-receipt-custody-v1 P1.1 (A6 RULED) — meta keys the emit handler
        # stripped from the model's meta arg (telemetry only; no Andon). Same
        # additive always-present precedent: None on sentinel/declarative/
        # failed/no_work shapes; [] on a clean tool emit with no extras.
        "stripped_meta_keys": stripped_meta_keys,
        "ts": _now_iso(),
    }
    # fleet-review-unification-v1 C1b-2 — the stable unit_id, ADDED ONLY when set (a
    # file producer's identity for the generic-proposal emission). Omitted for a
    # notion producer (forge) so its event JSON stays byte-identical.
    if unit_id is not None:
        event["unit_id"] = unit_id
    return event


def _declarative_content_files(cap, payload: Any, worker_id: str) -> List[str]:
    """The content filename(s) a declarative producer must emit (C1b-2), derived from
    the record's ``terminal_artifact.path_pattern`` with ``*`` filled by the unit_id
    — so ``draft-*.md`` + unit ``moon-bot`` → ``draft-moon-bot.md`` (matching the flat
    canonical adapter glob the promote mv targets). One content file per file producer
    today. Missing pattern / unit_id is a LOUD Andon (never a silent no-file run)."""
    from grove.fleet.errors import FleetWorkerAndon

    gov = cap.governance or {}
    ta = (
        ((gov.get("emission_preconditions") or {}) if isinstance(gov, dict) else {})
        .get("terminal_artifact")
        or {}
    )
    pattern = ta.get("path_pattern")
    unit_id = payload.get("unit_id") if isinstance(payload, dict) else None
    if not pattern or "*" not in pattern or not unit_id:
        raise FleetWorkerAndon(
            f"worker {worker_id!r}: declarative producer needs a "
            f"terminal_artifact.path_pattern with '*' (got {pattern!r}) and a "
            f"resolver unit_id (got {unit_id!r})",
            worker_id=worker_id,
            check="declarative_config_missing",
        )
    return [pattern.replace("*", unit_id)]


def _synthesize_meta(payload: Any, worker_id: str, unit_id: str) -> str:
    """The runtime-authored identity envelope for a declarative producer (C1b-2). The
    skill authors content only; identity — unit_id, slug, worker, source ref — is the
    runtime's, from the resolver payload. ``slug == unit_id`` (the staged package dir
    and the stable fleet identity are one for a file producer)."""
    src = payload if isinstance(payload, dict) else {}
    return json.dumps(
        {
            "unit_id": unit_id,
            "slug": unit_id,
            "worker": worker_id,
            "source_path": src.get("source_path"),
            "source_name": src.get("source_name"),
        },
        ensure_ascii=False,
        indent=2,
    )


def _row_identity(package: Dict[str, Any], payload: Any) -> "tuple":
    """Best-effort (row_id, fit_score) for the P2 proposal payload.

    row_id is authoritative from the skill's own meta.json (what it published for
    the row it chose); fit_score comes from the matching input row. Both None when
    absent — additive event fields, never load-bearing for the run itself.
    """
    row_id = None
    meta_txt = (package.get("files") or {}).get("meta.json")
    if isinstance(meta_txt, str):
        try:
            row_id = json.loads(meta_txt).get("row_id")
        except (json.JSONDecodeError, TypeError, AttributeError):
            row_id = None
    fit_score = None
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if isinstance(rows, list):
        match = next(
            (r for r in rows if isinstance(r, dict) and r.get("id") == row_id), None
        )
        if match is None and len(rows) == 1 and isinstance(rows[0], dict):
            match = rows[0]
        if isinstance(match, dict):
            fit_score = match.get("Fit Score")
    return row_id, fit_score


def _read_inbox_payload(worker_id: str, run_id: str) -> Any:
    from grove.fleet import paths

    inbox = paths.inbox_path(worker_id, run_id)
    if not inbox.exists():
        # No inbox = the ticker never brokered a payload = catastrophic wiring.
        from grove.fleet.errors import FleetWorkerAndon

        raise FleetWorkerAndon(
            f"no inbox payload at {inbox} — the runner must broker the resolved "
            f"input before the worker starts",
            worker_id=worker_id,
            check="inbox_missing",
        )
    data = json.loads(inbox.read_text(encoding="utf-8"))
    return data.get("payload")


def _setup_worker_logging() -> None:
    """aux-model-bindings-v1 P4 (ruling i-b) — worker-process observability.

    The worker is Popen'd as its own process and never inherits the gateway's
    logging config, so without this its INFO records are dropped (unconfigured
    root logger; Python's lastResort handler is WARNING+). Two legs:

    * ``setup_logging(mode="cron")`` — the rotating-file stack (idempotent by
      its own ``_logging_initialized`` latch). ``cron`` because the worker is
      a headless scheduled process: not an operator terminal (``cli``), and
      ``gateway`` would misdirect component records into gateway.log.
    * one stderr ``StreamHandler`` at INFO — journald sees only the process's
      inherited stdout/stderr (the runner's Popen does not redirect), and
      setup_logging attaches file handlers ONLY, so this leg is what makes
      worker INFO journald-visible. Tagged + checked for idempotence.

    Best-effort with a stderr floor: a worker must never fail its run over
    logging setup.
    """
    try:
        from hermes_logging import setup_logging

        setup_logging(mode="cron")
    except Exception as exc:  # noqa: BLE001 — observability leg, never fatal
        print(f"[fleet.worker] setup_logging failed: {exc!r}", file=sys.stderr)
    root = logging.getLogger()
    for h in root.handlers:
        if getattr(h, "_fleet_worker_stderr", False):
            return  # already attached (idempotence guard)
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    handler._fleet_worker_stderr = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


# binding-telemetry-v1 P3 — dead_pinned_slug classification at the ONE
# uncaught-exception chokepoint. A model slug that was cataloged at pin time
# and later dies at the provider spawns fine (spawn validates SHAPE only,
# GATE-B F3) and fails at call time as a provider 4xx. Classify ONLY the
# UNAMBIGUOUS model-not-found signature; ALL ambiguity keeps the generic
# check — "No endpoints found" (an OpenRouter routing/retention-filter
# artifact, not proof of nonexistence), 5xx, timeouts, and generic 400s all
# stay "uncaught". When in doubt, generic.
_DEAD_SLUG_PHRASES = (
    "not a valid model",
    "model not found",
    "no such model",
    "unknown model",
)


def _is_dead_pinned_slug(exc: BaseException) -> bool:
    """True ONLY for an unambiguous provider model-does-not-exist failure:
    an HTTP-status-bearing error (the SDK's APIStatusError family exposes
    ``status_code``) at 400/404 whose message names model nonexistence."""
    if getattr(exc, "status_code", None) not in (400, 404):
        return False
    msg = str(exc).lower()
    if any(p in msg for p in _DEAD_SLUG_PHRASES):
        return True
    return "model" in msg and "does not exist" in msg


def _uncaught_check(exc: BaseException) -> str:
    """The failed-event ``check`` for an exception reaching main()'s
    chokepoint: an upstream-stamped check wins; the dead-pin signature is the
    one classified class; everything else is the structural default."""
    stamped = getattr(exc, "check", None)
    if stamped:
        return stamped
    if _is_dead_pinned_slug(exc):
        return "dead_pinned_slug"
    return "uncaught"


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="grove.fleet.worker_entry")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    worker_id, run_id = args.worker_id, args.run_id

    _setup_worker_logging()

    from grove.fleet import paths
    from grove.fleet.staging import write_terminal_event

    payload: Any = None
    try:
        payload = _read_inbox_payload(worker_id, run_id)
        event = run_worker(worker_id, run_id, payload)
    except BaseException as exc:  # noqa: BLE001 — ALWAYS surface a terminal event
        # Includes FleetWorkerAndon and any unexpected error. TerminalGovernanceHalt
        # subclasses BaseException, but run_worker already catches it; anything
        # reaching here is an unhandled structural failure -> failed + diagnostics.
        # P1.2 Commit C — the invariant: every terminal receipt carries the
        # identity of the unit it was dispatched for. Any FleetWorkerAndon
        # raised INSIDE run_worker (emit_spec_missing, path_escape,
        # evaluator_call_failed, dead_pinned_slug, uncaught, ...) has the
        # payload in scope here. The two NAMED exceptions fail BEFORE a
        # payload exists (inbox_missing, worker_not_registered — payload is
        # None at the raise) and carry a null identity value, never a missing
        # mechanism.
        _unit = _dispatched_unit_id(payload)
        event = _event(
            worker_id,
            run_id,
            skill_id="",
            status="failed",
            detail=f"{type(exc).__name__}: {exc}",
            check=_uncaught_check(exc),
            **({"unit_id": _unit} if _is_declarative_payload(payload) else {"row_id": _unit}),
        )
        event["traceback"] = traceback.format_exc()

    # (g) write the terminal-state event BEFORE exit. exit 0 for a clean terminal
    # state (success | no_work); nonzero for failed so the ticker Andons and
    # reads the event for the WHY.
    try:
        write_terminal_event(paths.event_path(worker_id, run_id), event)
    except Exception as exc:  # a truly unwritable sink — last-resort stderr
        print(
            f"[fleet:{worker_id}] FATAL: could not write terminal event: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 2
    return 0 if event["status"] in ("success", "no_work") else 1


if __name__ == "__main__":
    raise SystemExit(main())

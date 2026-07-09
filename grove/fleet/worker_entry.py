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
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

WORKER_MAX_ITERATIONS = 50


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
    """
    from grove.fleet.errors import FleetWorkerAndon
    from grove.providers import resolve_tier_to_runtime, route_for_agent

    tier = f"T{cap.tier_rule.preferred}"
    routed = route_for_agent(explicit_tier=tier, classify=False)
    if routed is None:
        raise FleetWorkerAndon(
            "no routing.config.yaml present — a fleet worker cannot resolve a "
            "model/runtime for its tier",
            worker_id=worker_id,
            check="no_routing_config",
        )
    runtime = resolve_tier_to_runtime(routed.tier_config)
    return routed.tier_config.model, routed.tier_config.max_tokens, runtime


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

        worker_config = {
            **load_config(),
            "fleet_offered_allowlist": ["read_file", "skill_view"],
        }
        sink = _resolve_declared_sink(cap, worker_id)
        sink.mkdir(parents=True, exist_ok=True)

        # Legitimate empty work: the ticker normally only spawns on work, but a
        # None payload is an explicit no_work signal — do not run the skill.
        if payload is None:
            return _event(worker_id, run_id, cap.id, "no_work", detail="empty payload")

        # (c)+(d) install the deny handler and an ISOLATED session DB, then
        # (e) run the pinned skill via the Dispatcher — reuse skill-invoke whole.
        session_db = SessionDB(db_path=paths.session_db_path(worker_id))
        skill_name = _derive_skill_name(cap, worker_id)
        model, max_tokens, runtime = _resolve_worker_runtime(cap, worker_id)

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

        # fleet-review-unification-v1 C1b-2 — emission style. A file producer
        # (file_source resolver → payload carries "units", never "rows") uses
        # DECLARATIVE emission: the skill authors content only, named by the record's
        # terminal_artifact, and the RUNTIME synthesizes the identity envelope from
        # the resolver payload. A notion producer (forge) keeps its self-authored
        # triad path, byte-identical.
        declarative = (
            isinstance(payload, dict) and "units" in payload and "rows" not in payload
        )
        content_files = (
            _declarative_content_files(cap, payload, worker_id) if declarative else None
        )
        # The prompt-side tag and the parser-side tag MUST be the identical run_id[:8]
        # (a mismatch = the model writes markers the parser rejects = every run no-files).
        prompt = _build_worker_prompt(
            skill_name, payload, run_id[:8], content_files=content_files
        )
        try:
            result = agent.run_conversation(prompt, task_id=run_id)
        except TerminalGovernanceHalt as tgh:
            # A grant-less worker hit an ungranted Yellow/Red action; the deny
            # handler fired. This is a completed-with-denial run: failed state,
            # diagnostics preserved.
            return _event(
                worker_id,
                run_id,
                cap.id,
                "failed",
                detail=f"governed denial: {tgh}",
                check="governed_denial",
            )

        # (f) Option 2: the RUNTIME stages the skill's delimited per-file emit. The
        # skill emits each file inside sentinel-framed blocks (forge-fleet-package-
        # emission-v1, Path B); the parser's state machine recovers {slug, files},
        # and the runtime writes each file atomically into the declared sink under the
        # slug dir, jailed by is_relative_to(sink). The skill never self-writes — so a
        # wall-clock kill cannot leave a half-written file the portal reads. The
        # per-run tag (run_id[:8]) frames the sentinels; forge names its required set.
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
                {"resume.md", "cover-letter.md", "meta.json"},
            )
        if extracted is None:
            # fleet-failure-forensics-v1 — the model produced output but it did not
            # parse to a valid package; that output is discarded, leaving zero
            # diagnostic without this. Enrich detail with the fail-loud reason + a
            # bounded preview and persist the FULL raw text to an events/<run_id>.raw.txt
            # sidecar. status + check are preserved EXACTLY (reap keys on them); only
            # detail is enriched and the additive raw_text_path is added.
            final_text = _final_assistant_text(result.get("messages") or [])
            preview = (
                (final_text[:800] + "…") if len(final_text) > 800 else final_text
            ).strip()
            return _event(
                worker_id,
                run_id,
                cap.id,
                "failed",
                detail=(
                    "delimited emit did not parse to a valid fleet_package "
                    f"(reason: {reason}); final assistant message was: {preview!r}"
                ),
                check="no_package",
                raw_text_path=_persist_raw_output(worker_id, run_id, final_text),
            )
        if declarative:
            unit_id = payload["unit_id"]
            files = dict(extracted["files"])
            files["meta.json"] = _synthesize_meta(payload, worker_id, unit_id)
            staged = stage_package(sink, unit_id, files)
            return _event(
                worker_id,
                run_id,
                cap.id,
                "success",
                detail=f"completed={result.get('completed')}; unit={unit_id}",
                staged=[str(p) for p in staged],
                slug=unit_id,
                unit_id=unit_id,
            )
        staged = stage_package(sink, extracted["slug"], extracted["files"])
        row_id, fit_score = _row_identity(extracted, payload)
        return _event(
            worker_id,
            run_id,
            cap.id,
            "success",
            detail=f"completed={result.get('completed')}; slug={extracted['slug']}",
            staged=[str(p) for p in staged],
            slug=extracted["slug"],
            row_id=row_id,
            fit_score=fit_score,
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


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="grove.fleet.worker_entry")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    worker_id, run_id = args.worker_id, args.run_id

    from grove.fleet import paths
    from grove.fleet.staging import write_terminal_event

    try:
        payload = _read_inbox_payload(worker_id, run_id)
        event = run_worker(worker_id, run_id, payload)
    except BaseException as exc:  # noqa: BLE001 — ALWAYS surface a terminal event
        # Includes FleetWorkerAndon and any unexpected error. TerminalGovernanceHalt
        # subclasses BaseException, but run_worker already catches it; anything
        # reaching here is an unhandled structural failure -> failed + diagnostics.
        event = _event(
            worker_id,
            run_id,
            skill_id="",
            status="failed",
            detail=f"{type(exc).__name__}: {exc}",
            check=getattr(exc, "check", None) or "uncaught",
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

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
from typing import Any, Dict, Optional

WORKER_MAX_ITERATIONS = 50


def _corpus_only_admission(cap, worker_id: str, config: dict) -> Optional[list]:
    """Corpus-only per-tool admission (fleet-pipeline-v1 P5 — the SOLE structural
    control). If the record declares required_tools:

      1. ASSERT they are all in the LIVE admitted set (declared ⊄ admitted is a
         config error -> loud Andon, refuse spawn). This is NON-VACUOUS: the
         DECLARED list and the RESOLVED admitted set come from DIFFERENT sources
         (the hand-authored record vs get_admitted_tools), so it is a real check,
         not a tautology.
      2. Return the deny-complement (admitted − required_tools) — the tools to
         block so the agent is offered ONLY required_tools.

    Returns None when no required_tools are declared (no per-tool restriction —
    full toolset, for non-corpus-only workers). Computed at SPAWN against the live
    config: a hot-reloaded green tool lands in the complement and is denied."""
    required = list(cap.required_tools or [])
    if not required:
        return None
    from grove.tool_admission import get_admitted_tools
    from tools.registry import ToolRegistry, register_builtin_tools

    reg = ToolRegistry()
    register_builtin_tools(reg)
    admitted = get_admitted_tools(reg, "fleet", config)
    missing = sorted(t for t in required if t not in admitted)
    if missing:
        from grove.fleet.errors import FleetWorkerAndon

        raise FleetWorkerAndon(
            f"worker {worker_id!r}: declared required_tools {missing} are NOT "
            f"admitted for platform 'fleet' (declared ⊄ admitted) — a config "
            f"error. Refusing to spawn; fix the record's required_tools or the "
            f"platform admission, never silently strip.",
            worker_id=worker_id,
            check="required_tool_unadmitted",
        )
    return sorted(admitted - set(required))


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


def _build_worker_prompt(skill_name: str, payload: Any) -> str:
    return (
        f"You are running as an autonomous, non-interactive fleet background "
        f"worker. Invoke the '{skill_name}' skill using the invoke_skill tool and "
        f"carry it out to completion against the resolved input below. Do NOT ask "
        f"clarifying questions — no operator is present this turn. Do NOT call "
        f"write_file, do NOT publish, do NOT read external sources beyond your "
        f"declared read surfaces — the RUNTIME stages your output.\n\n"
        f"When finished, your FINAL message MUST be a single JSON object and "
        f"nothing else:\n"
        f'{{"fleet_package": {{"slug": "<short-kebab-slug>", "files": '
        f'{{"<filename>": "<full file content>", ...}}}}}}\n'
        f"The runtime writes each file atomically into your pending_review sink "
        f"under the slug directory.\n\nRESOLVED INPUT:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _extract_fleet_package(messages) -> Optional[Dict[str, Any]]:
    """Parse the skill's returned ``fleet_package`` from its final message.

    Accepts either a bare JSON object or a ```json fenced block containing
    ``{"fleet_package": {"slug": ..., "files": {...}}}``. Returns
    ``{"slug", "files"}`` or None when no valid package is present.
    """
    import re

    text = _final_assistant_text(messages)
    if not text:
        return None
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates.append(text)
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("fleet_package"), dict):
            fp = obj["fleet_package"]
            if fp.get("slug") and isinstance(fp.get("files"), dict) and fp["files"]:
                return {"slug": fp["slug"], "files": fp["files"]}
    return None


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
        # (P5) corpus-only per-tool admission — PRE-DISPATCHER assertion (refuse
        # spawn if declared required_tools ⊄ live-admitted) + deny-complement to
        # inject below so the agent is offered ONLY required_tools.
        from hermes_cli.config import load_config

        worker_config = load_config()
        blocked_tools = _corpus_only_admission(cap, worker_id, worker_config)
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

        # (P5) inject the deny-complement via a PER-SPAWN RuntimeContext so the
        # Dispatcher's live admission (get_admitted_tools -> minus blocked_tools)
        # offers the agent ONLY the record's required_tools. The Dispatcher's
        # _tools_cache is per-instance, and this is a fresh subprocess per dispatch,
        # so the complement is never boot-cached.
        from grove.dispatcher import RuntimeContext

        if blocked_tools is not None:
            _bt = dict(worker_config.get("blocked_tools") or {})
            _bt["fleet"] = sorted(set(_bt.get("fleet") or []) | set(blocked_tools))
            worker_config = {**worker_config, "blocked_tools": _bt}
        dispatcher = Dispatcher(
            runtime_ctx=RuntimeContext(env=dict(os.environ), config=worker_config),
            session_db=session_db,
            sovereign_prompt_handler=non_interactive_deny_handler,
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

        prompt = _build_worker_prompt(skill_name, payload)
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

        # (f) Option 2: the RUNTIME stages the skill's returned package. The
        # skill returns a fleet_package (slug + files); the runtime writes each
        # file atomically into the declared sink under the slug dir, jailed by
        # is_relative_to(sink). The skill never self-writes — so a wall-clock kill
        # cannot leave a half-written file the portal reads.
        package = _extract_fleet_package(result.get("messages"))
        if package is None:
            return _event(
                worker_id,
                run_id,
                cap.id,
                "failed",
                detail=(
                    "skill returned no valid fleet_package (expected a final "
                    "JSON object {\"fleet_package\": {\"slug\", \"files\"}})"
                ),
                check="no_package",
            )
        staged = stage_package(sink, package["slug"], package["files"])
        row_id, fit_score = _row_identity(package, payload)
        return _event(
            worker_id,
            run_id,
            cap.id,
            "success",
            detail=f"completed={result.get('completed')}; slug={package['slug']}",
            staged=[str(p) for p in staged],
            slug=package["slug"],
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
) -> Dict[str, Any]:
    # fleet-pipeline-v1 P2 (A1) — additive fields the reap emitter reads OFF the
    # event (never parsed from detail/paths). None for workers that don't produce
    # them; the terminal-state reap keys on presence-of-status, not exact shape,
    # so these additions are tolerated (manager.py:98,109-110).
    return {
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
        "ts": _now_iso(),
    }


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

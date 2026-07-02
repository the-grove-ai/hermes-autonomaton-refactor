#!/usr/bin/env python3
"""One-off migration: legacy hermes MEMORY.md / USER.md -> Grove substrate proposals.

Phase 1 of ``legacy-memory-tool-retirement-v1``. The upstream-hermes file store
(``MEMORY.md`` + ``USER.md``) is orphaned: the agent still writes to it via the
legacy ``memory`` tool, but nothing injects it (``legacy-memory-retirement-v1``
disabled the composer sections; ``hydrate_memory_context`` has no caller). Before
Phase 2 severs the tool, this script recovers the ~20 orphaned entries into the
Grove Kaizen substrate WITHOUT data loss.

WHAT IT DOES (stage-only — never approves):
  1. Reads the legacy ``MEMORY.md`` and ``USER.md`` (split on the ``\\n§\\n`` delimiter).
  2. T1-classifies each entry into the closed substrate entity_type set
     (DomainFact | OperatorPreference | ProjectState | ArchitecturalRule) via the
     telemetry-tier classifier — Option A. USER.md entries bias to OperatorPreference
     but are still classified per entry.
  3. Stages each as a *pending* proposal in ``memory_proposals.jsonl`` at
     ``confidence: 0.9``, in the exact detector record shape.

It does NOT approve anything. The operator reviews/approves via the normal surface
(``flywheel memory list`` / ``show`` / ``approve``), which routes through
``grove.memory.digest.run_digest`` exactly like any auto-detected proposal — so the
operator's decide callback remains the sole authority before anything hits the
permanent index. A mis-classified entry is corrected by rejecting it (and re-staging
with a fixed type) or hand-editing the pending record's ``entity_type`` before approve.

SAFETY:
  * Dry-run by default: classify + print a table, write nothing. Pass ``--commit`` to append.
  * Idempotent: refuses to re-stage if migration proposals already exist
    (marked by ``session_id == "memory-md-migration"``) unless ``--force``.
  * Fail-loud: an entry T1 classifies outside the closed set is reported and SKIPPED
    (never silently defaulted — a wrong entity_type calcifies or mis-decays the record).

USAGE (on the gateway VM, inside the venv):
    .venv/bin/python scripts/migrate_legacy_memory_to_substrate.py            # dry-run
    .venv/bin/python scripts/migrate_legacy_memory_to_substrate.py --commit   # stage proposals
    # then, as operator:  flywheel memory list  ->  flywheel memory approve <id>
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Closed substrate entity_type set (grove/memory/record.py DECAY_RATES).
_ENTITY_TYPES = ("DomainFact", "OperatorPreference", "ProjectState", "ArchitecturalRule")
_MIGRATION_SESSION_ID = "memory-md-migration"
_CONFIDENCE = 0.9

_CLASSIFY_SYSTEM = """You classify a single operator-memory entry into exactly one Grove memory entity_type.

Definitions (choose the single best fit):
- DomainFact: stable factual knowledge about the world/domain. Zero decay.
- OperatorPreference: a durable preference, habit, or fact about the operator. Zero decay.
- ArchitecturalRule: a rule, constraint, or invariant about the system's architecture. Zero decay.
- ProjectState: time-bound status of ongoing work (a sprint, a task, a "currently" fact). Decays daily — use ONLY for genuinely transient state.

Bias: when an entry is a fact/preference about the operator, prefer OperatorPreference.
Return STRICT JSON, no prose: {"entity_type": "<one of the four>", "justification": "<one short clause>"}"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _legacy_entries() -> List[Tuple[str, str]]:
    """Return (source_label, entry_text) for every legacy MEMORY.md + USER.md entry."""
    from tools.memory_tool import get_memory_dir, ENTRY_DELIMITER

    mem_dir = get_memory_dir()
    out: List[Tuple[str, str]] = []
    for fname, label in (("MEMORY.md", "MEMORY.md"), ("USER.md", "USER.md")):
        path = mem_dir / fname
        if not path.exists():
            continue
        raw = path.read_text(encoding="utf-8")
        for chunk in raw.split(ENTRY_DELIMITER):
            entry = chunk.strip()
            if entry:
                out.append((label, entry))
    return out


def _classify(entry: str) -> Dict[str, str]:
    """T1-classify one entry. Fail loud on an out-of-set type (caller skips + reports)."""
    from grove.classify import _telemetry_tier_runtime
    from agent.anthropic_adapter import build_anthropic_client

    runtime, _tier = _telemetry_tier_runtime()
    client = build_anthropic_client(
        api_key=runtime.get("api_key") or "",
        base_url=runtime.get("base_url") or None,
    )
    resp = client.messages.create(
        model=runtime["model"],
        max_tokens=200,
        system=_CLASSIFY_SYSTEM,
        messages=[{"role": "user", "content": entry}],
    )
    text = "".join(getattr(b, "text", "") for b in resp.content).strip()
    # Tolerate a ```json fence.
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    parsed = json.loads(text[text.find("{"): text.rfind("}") + 1])
    etype = parsed.get("entity_type")
    if etype not in _ENTITY_TYPES:
        raise ValueError(f"T1 returned out-of-set entity_type {etype!r} for entry: {entry[:60]!r}")
    return {"entity_type": etype, "justification": str(parsed.get("justification", "")).strip()}


def _staged_record(entry: str, source_label: str, classified: Dict[str, str]) -> Dict[str, Any]:
    ts = _now_iso()
    return {
        "session_id": _MIGRATION_SESSION_ID,
        "status": "pending",
        "timestamp": ts,
        "proposal": {
            "action": "create",
            "proposed_record": {
                "entity_type": classified["entity_type"],
                "content": entry,
                "confidence": _CONFIDENCE,
                "justification": classified["justification"] or f"migrated from legacy {source_label}",
            },
            "dock_goal_ref": None,
            "sources": [{"origin": f"legacy {source_label} migration", "migrated_at": ts}],
        },
    }


def _already_migrated(proposals_path: Path) -> int:
    if not proposals_path.exists():
        return 0
    n = 0
    for line in proposals_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            if json.loads(line).get("session_id") == _MIGRATION_SESSION_ID:
                n += 1
        except json.JSONDecodeError:
            continue
    return n


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Stage legacy MEMORY.md/USER.md entries as Grove substrate proposals.")
    ap.add_argument("--commit", action="store_true", help="Append staged proposals (default: dry-run, no writes).")
    ap.add_argument("--force", action="store_true", help="Stage even if migration proposals already exist.")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N entries (0 = all).")
    args = ap.parse_args(argv)

    from hermes_constants import get_hermes_home
    proposals_path = Path(get_hermes_home()) / "memory_proposals.jsonl"

    entries = _legacy_entries()
    if args.limit:
        entries = entries[: args.limit]
    if not entries:
        print("No legacy entries found — nothing to migrate.")
        return 0

    prior = _already_migrated(proposals_path)
    if prior and not args.force:
        print(f"REFUSING: {prior} migration proposal(s) already staged (session_id={_MIGRATION_SESSION_ID}). "
              f"Use --force to stage again.", file=sys.stderr)
        return 1

    print(f"Legacy entries: {len(entries)}  |  target: {proposals_path}  |  "
          f"mode: {'COMMIT' if args.commit else 'DRY-RUN'}\n")

    staged: List[Dict[str, Any]] = []
    skipped: List[Tuple[str, str]] = []
    by_type: Dict[str, int] = {}
    for i, (label, entry) in enumerate(entries, 1):
        try:
            classified = _classify(entry)
        except Exception as exc:  # fail loud per entry; skip, report, keep going
            skipped.append((entry[:70], str(exc)))
            print(f"  [{i:>2}] SKIP  ({label})  {entry[:60]!r}  -- {exc}")
            continue
        et = classified["entity_type"]
        by_type[et] = by_type.get(et, 0) + 1
        staged.append(_staged_record(entry, label, classified))
        print(f"  [{i:>2}] {et:<18} ({label})  {entry[:56]!r}")

    print(f"\nClassified: {len(staged)}   Skipped: {len(skipped)}   By type: {by_type}")

    if not args.commit:
        print("\nDRY-RUN — nothing written. Re-run with --commit to stage these proposals,\n"
              "then approve via:  flywheel memory list  ->  flywheel memory approve <id>")
        return 0

    with open(proposals_path, "a", encoding="utf-8") as fh:
        for rec in staged:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    print(f"\nSTAGED {len(staged)} pending proposal(s) -> {proposals_path}")
    print("Next (operator): flywheel memory list  ->  flywheel memory approve <id>  (review each; reject mis-classified).")
    if skipped:
        print(f"\n{len(skipped)} entr(y/ies) SKIPPED (not staged) — re-run or hand-stage after review:")
        for preview, why in skipped:
            print(f"  - {preview!r}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

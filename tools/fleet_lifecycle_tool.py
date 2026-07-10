"""fleet_lifecycle_tool — operator lifecycle verbs over canonical fleet
artifacts (promoted-artifact-persistence-v1 P5).

  fleet_purge — RED (GRV-001 grantable): archive-first purge of a PROMOTED
  unit's canonical artifacts. Semantic revocation of operator approval: the
  bytes SURVIVE in the sink's declared archive; they leave the canonical
  (ambient) plane, the derived wiki pages are tombstoned, and the unit is
  marked terminal (never re-selected).

Registered following the andon_tool pattern — one registration, inherited by
every surface (Telegram, CLI, API) through the shared agent/dispatcher loop.
Red + valid Grant Token (implicit from the operator's message, or standing
``(fleet_purge, fleet_purge)``) → execute; Red + agent-synthesized → the
sovereignty ceremony (interactive prompt, or the durable pending store's
portal approve→confirm).

PRODUCER-BLIND (generality pin): the target skill and unit arrive as tool
ARGUMENTS; nothing here names a producer. The handler is the ACTION layer —
it resolves the capability record, the worker id, and the wiki surfaces; the
filesystem act itself is :func:`grove.utils.fs_utils.purge_artifacts`
(orchestrator core, storage_transfer-routed).

Handler ordering (P5-S4 ruling): purge core (moves + manifest) →
feedback-store terminal_skip marker → wiki tombstone + ingest-ledger drop.
A partial failure after the moves raises LOUD (never swallowed); a re-tap
completes every remaining step idempotently (the core resumes the
manifest-less archive dir; the marker write and tombstones are idempotent).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import yaml

# Module-level: the feedback store is the (worker, unit_id)-keyed guidance
# store, generalized to every fleet worker since C1b — its module PATH is
# historical, its API producer-blind.
from grove.forge import feedback_store

logger = logging.getLogger(__name__)

# ── Tool schema ───────────────────────────────────────────────────────────────

FLEET_PURGE_SCHEMA = {
    "name": "fleet_purge",
    "description": (
        "Purge a PROMOTED fleet unit's canonical artifacts into the sink's "
        "declared archive (archive-first: bytes survive, operator-recoverable). "
        "This is semantic revocation of a prior approval — the unit's derived "
        "wiki pages are tombstoned and the unit is marked terminal so it is "
        "never re-selected. Irreversible on the ambient plane. Requires "
        "operator authority (RED)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": (
                    "The fleet skill owning the unit — trailing name or full "
                    "capability id (as shown in the fleet review surface)."
                ),
            },
            "unit": {
                "type": "string",
                "description": (
                    "The unit to purge: the canonical package dir name (slug) "
                    "or the canonical file's unit key, as shown in the review "
                    "surface."
                ),
            },
            "unit_id": {
                "type": "string",
                "description": (
                    "Optional distinct feedback identity (e.g. a remote row id) "
                    "when it differs from the unit key. Defaults to unit."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Optional reason, recorded in the purge manifest.",
            },
        },
        "required": ["skill", "unit"],
    },
}


# ── action-layer resolvers (producer-blind: everything arrives as data) ──────


def _capability_for(skill: str):
    """The governance-bearing capability record for *skill* (trailing segment
    or full id). Fail loud on unknown / non-fleet targets."""
    from grove.capability_registry import load_capabilities

    caps = load_capabilities()
    cap = caps.get(skill)
    if cap is None:
        matches = [c for cid, c in caps.items()
                   if cid.rsplit(".", 1)[-1] == skill]
        cap = matches[0] if len(matches) == 1 else None
    if cap is None or not getattr(cap, "governance", None) \
            or "write_zone" not in (cap.governance or {}):
        raise ValueError(
            f"fleet_purge: no governance-bearing fleet capability matches "
            f"{skill!r} — nothing to purge against"
        )
    return cap


def _worker_for(skill_id: str) -> Optional[str]:
    """The fleet worker id whose capability is *skill_id* (the action-layer
    worker resolution — same source of truth as the portal's resolver)."""
    from grove.fleet.config import load_fleet_workers

    for wid, cfg in load_fleet_workers().items():
        if getattr(cfg, "skill", None) == skill_id:
            return wid
    return None


def _strip_pattern(filename: str, pattern: str) -> str:
    """Recover a unit key from a flat canonical filename by stripping the
    terminal_artifact pattern's fixed prefix/suffix (the read-side rule)."""
    if "*" not in pattern:
        return Path(filename).stem
    pre, suf = pattern.split("*", 1)
    s = filename
    if pre and s.startswith(pre):
        s = s[len(pre):]
    if suf and s.endswith(suf):
        s = s[: -len(suf)]
    return s


def _completed_purge(gov: dict, unit: str) -> Optional[dict]:
    """The newest MANIFESTED archive dir for *unit*, as a purge-result dict —
    the P5-S4.3 re-tap seam: a completed purge's moves + manifest already
    happened, so the handler proceeds idempotently to the remaining
    post-steps (marker / tombstone / ledger-drop) from the manifest's own
    record. None when no completed purge exists (the caller re-raises)."""
    import json as _json
    import re as _re

    from grove.utils.fs_utils import _grove_home_realpath, _grove_subdir_realpath

    wz = (gov or {}).get("write_zone") or {}
    grove = _grove_home_realpath()
    if grove is None or not wz.get("canonical_dir"):
        return None
    canonical = Path(_grove_subdir_realpath(wz["canonical_dir"], grove))
    archive_rel = ((wz.get("retention") or {}).get("archive_dir") or ".archive")
    unit_safe = _re.sub(r"[^A-Za-z0-9._-]", "_", str(unit)).strip(".") or "unit"
    root = canonical / archive_rel
    if not root.is_dir():
        return None
    for d in sorted(root.glob(f"{unit_safe}-*"), reverse=True):
        man = d / "purge-manifest.json"
        if d.is_dir() and man.is_file():
            try:
                record = _json.loads(man.read_text(encoding="utf-8"))
            except (_json.JSONDecodeError, OSError):
                continue  # unreadable manifest — not a usable completion
            return {"archive_dir": str(d),
                    "files": list(record.get("archived_files") or []),
                    "manifest": str(man), "resumed": True,
                    # the ORIGINAL resolved paths at purge time — the re-tap's
                    # tombstone/ledger basis (S4.3)
                    "sources_record": list(record.get("sources") or [])}
    return None


def _page_source(page: Path) -> Optional[str]:
    """The ``source:`` frontmatter field of a wiki page, or None."""
    from grove.wiki.index import MalformedWikiPage, _split_frontmatter

    try:
        fm_str, _body = _split_frontmatter(page.read_text(encoding="utf-8"))
        meta = yaml.safe_load(fm_str)
    except (MalformedWikiPage, yaml.YAMLError, OSError):
        return None  # non-canonical neighbour — not a tombstone candidate
    return meta.get("source") if isinstance(meta, dict) else None


# ── the verb ─────────────────────────────────────────────────────────────────


def fleet_purge(skill: str, unit: str, unit_id: Optional[str] = None,
                reason: Optional[str] = None) -> str:
    """Archive-first purge of one promoted unit. See module docstring for the
    ordering contract. Returns an operator-facing summary; raises LOUD on any
    refusal or post-move partial failure."""
    from hermes_constants import get_wiki_path
    from grove.effect_signature import canonical_effect_signature
    from grove.utils.fs_utils import (
        _grove_home_realpath,
        _grove_subdir_realpath,
        purge_artifacts,
    )
    from grove.wiki.index import WikiIndex
    from grove.wiki.watcher import _LEDGER_REL, _load_ledger, _save_ledger

    if not skill or not unit:
        raise ValueError("fleet_purge: skill and unit are both required")
    cap = _capability_for(skill)
    gov = cap.governance
    wz = gov["write_zone"]

    grove = _grove_home_realpath()
    if grove is None:
        raise ValueError("fleet_purge: could not resolve GROVE_HOME")
    canonical = Path(_grove_subdir_realpath(wz["canonical_dir"], grove))

    # ── locate the unit's canonical artifacts (dir layout, else flat key) ───
    sources: List[str] = []
    unit_dir = canonical / unit
    if unit_dir.is_dir():
        sources = [str(unit_dir)]
    else:
        pattern = ((gov.get("emission_preconditions") or {})
                   .get("terminal_artifact", {}).get("path_pattern", "*"))
        for f in sorted(canonical.glob(pattern)):
            if f.is_file() and (f.name == unit
                                or _strip_pattern(f.name, pattern) == unit):
                sources.append(str(f))
    # No sources found is NOT an immediate refusal: the core resumes an
    # interrupted purge (manifest-less archive dir) and fail-louds otherwise.

    sig = canonical_effect_signature("fleet_purge", {
        "skill": cap.id, "unit": unit, "unit_id": unit_id, "reason": reason,
    })
    try:
        res = purge_artifacts(
            sources,  # [] on a re-tap: the core resumes an interrupted purge,
            gov,      # and fail-louds when there is truly nothing to purge.
            unit=unit,
            reason=reason or "operator purge",
            initiated_by="operator",
            effect_signature=sig,
        )
    except ValueError as exc:
        if "nothing to purge" not in str(exc):
            raise
        # P5-S4.3 (ruled re-tap semantics): a COMPLETED purge — moves +
        # manifest done — still owes any post-steps a crash or defect left
        # behind; complete them idempotently from the manifest's record.
        completed = _completed_purge(gov, unit)
        if completed is None:
            raise
        res = completed

    # ── terminal_skip marker: the selection pass never resurrects this unit.
    # set_terminal_skip is a deliberate no-op on an ABSENT entry (its
    # N-breaker caller always follows accumulated feedback), so a
    # never-revised unit needs the entry created first — the write doubles
    # as the audit note recording WHY the unit went terminal. Pure reuse of
    # the shipped API; idempotent on re-tap (skip already set → no-op). ────
    worker = _worker_for(cap.id)
    fb_key = unit_id or unit
    if worker:
        existing = feedback_store.read(worker, fb_key)
        if not (existing and existing.get("terminal_skip")):
            feedback_store.write(
                worker, fb_key,
                f"purged by operator: {reason or 'operator purge'}",
            )
            feedback_store.set_terminal_skip(worker, fb_key)
    else:
        logger.warning(
            "fleet_purge: no fleet worker declares %s — terminal_skip marker "
            "not written (selection-side resurrection guard absent).", cap.id,
        )

    # ── wiki tombstone (R1): derived pages leave the ambient plane ──────────
    # Original (pre-move) file paths: reconstruct from the dir sources + the
    # archived basenames; flat sources are their own originals.
    #
    # REALPATH-CANONICAL matching (P5-S4.3): the purge core realpath-resolves
    # its sources (on the VM, ~/.grove is a symlink into /mnt/grove-data), but
    # page ``source:`` frontmatter and the ingest ledger store the SYMLINK
    # spelling the poller saw. String equality misses across the symlink — the
    # merchants bake proved it live — so BOTH comparands are realpath'd.
    # (realpath normalizes existing symlink components even when the leaf file
    # is already gone, which post-purge it always is.)
    import os as _os

    originals = set()
    archived_names = [Path(f).name for f in res["files"]]
    # Basis: this tap's live sources, else the manifest's recorded sources
    # (a completed-purge re-tap — S4.3), else the unit dir. A basis entry
    # whose basename is itself an archived name is a FLAT-file original;
    # anything else is a package dir and expands with the archived names.
    basis = sources or res.get("sources_record") or [str(unit_dir)]
    for s in basis:
        sp = Path(s)
        if sp.name in archived_names:
            originals.add(s)
        else:
            originals.update(str(sp / n) for n in archived_names
                             if n != "purge-manifest.json")
    originals_real = {_os.path.realpath(p) for p in originals}
    tombstoned: List[str] = []
    pages_root = Path(get_wiki_path()) / "pages"
    if pages_root.is_dir() and originals_real:
        idx = WikiIndex()
        for page in sorted(pages_root.glob("*/*.md")):
            src_field = _page_source(page)
            if src_field and _os.path.realpath(src_field) in originals_real:
                rel = str(page.relative_to(pages_root))
                idx.tombstone(rel)  # unlink-first-then-purge-rows (fail-safe)
                tombstoned.append(rel)

    # ── ingest-ledger drop: the purged sources' mtime entries leave too ─────
    ledger_path = Path(get_wiki_path()) / _LEDGER_REL
    ledger = _load_ledger(ledger_path)
    dropped = [k for k in list(ledger)
               if _os.path.realpath(k) in originals_real]
    if dropped:
        for k in dropped:
            ledger.pop(k, None)
        _save_ledger(ledger_path, ledger)

    return (
        f"Purged {unit!r} ({cap.id}): {len(res['files'])} file(s) archived to "
        f"{res['archive_dir']}"
        f"{' (resumed an interrupted purge)' if res.get('resumed') else ''}; "
        f"terminal_skip marked for ({worker or 'no-worker'}, {fb_key}); "
        f"{len(tombstoned)} wiki page(s) tombstoned; "
        f"{len(dropped)} ingest-ledger entr(y/ies) dropped. "
        f"Bytes survive in the archive; manifest: {res['manifest']}."
    )


# ── registration (auto-discovered by tools.registry.register_builtin_tools) ──


def register(reg) -> None:
    """One registration, inherited by every surface. RED zone classification
    (config/zones.schema.yaml) is the guard — GRV-001 Grant Token model."""
    reg.register(
        name="fleet_purge",
        toolset="fleet_lifecycle",
        schema=FLEET_PURGE_SCHEMA,
        handler=lambda args, **kw: fleet_purge(
            args.get("skill", ""),
            args.get("unit", ""),
            unit_id=args.get("unit_id"),
            reason=args.get("reason"),
        ),
        emoji="🗄️",
    )

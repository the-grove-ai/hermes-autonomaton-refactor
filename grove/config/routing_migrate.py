"""GRV-001 v2.0 migration — split the v1 ``routing.config.yaml`` into
``routing.operational.yaml`` + ``routing.authority.yaml``
(routing-v2-migration-v1, Phase 1).

Callable utility (:func:`migrate_v1_to_v2`) + a thin argparse CLI. NOT wired
into any command surface this phase.

Census (per SPEC / GATE-A §2 ruling):

    authority     escalation.threshold  → escalation_threshold (flattened)
                  tier_budgets          (top-level)
                  escalation_policy      (from routing.escalation_policy)
    operational   default_tier, tier_preferences, model_facts, routing_rules,
                  telemetry, provider_routing   (from routing.*)
                  pattern_cache, goal_attachment (top-level)
    dropped       zone_overrides         (GRV-001 v2.0 §VII removes it)

Each output file gets ``schema_version: "2.0"`` plus its surface markers
(``surface_class`` / ``writable_on``; authority additionally ``default_zone:
red``). ``approval_requirements`` and ``zone_assignment`` are deliberately NOT
written — R3: no reader enforces them yet, and a config claim with no enforcing
reader is a detection-honesty defect.

Comments survive via ruamel round-trip (the ``routing_writer.py`` precedent).
Block-internal comments travel with their moved value nodes; a parent banner
that precedes a moved key does not follow it across the split.

Atomicity: write both ``.tmp``, fsync both, ``os.replace`` both, and only after
BOTH replaces succeed rename ``v1 → <name>.bak_<timestamp>``. The v1 file is the
recovery anchor — it is never removed before both v2 files are live, so any
partial failure re-runs cleanly from the intact v1.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from grove.router_merge import _AUTHORITY_RESERVED_KEYS

# ── census tables ────────────────────────────────────────────────────────────
# Keys taken from the v1 ``routing:`` mapping into the operational surface.
_OPERATIONAL_FROM_ROUTING = (
    "default_tier",
    "tier_preferences",
    "model_facts",
    "routing_rules",
    "telemetry",
    "provider_routing",
)
# Keys taken from the v1 top level into the operational surface.
_OPERATIONAL_TOPLEVEL = ("pattern_cache", "goal_attachment")
# Keys taken from the v1 top level into the authority surface.
_AUTHORITY_TOPLEVEL = ("tier_budgets",)
# Keys dropped entirely (reported, never written).
_DROPPED_FROM_ROUTING = ("zone_overrides",)

_V2 = "2.0"


class MigrationError(RuntimeError):
    """The migration cannot proceed or recover — e.g. the v1 anchor is gone and
    the v2 pair is incomplete (an unrecoverable partial state)."""


def _ruamel() -> YAML:
    """Round-trip parser tuned to the routing file layout (routing_writer.py
    precedent), so the comment-dense config survives the split without reflow."""
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 100
    return y


def _detect_state(v1_path: Path, operational_out: Path, authority_out: Path) -> str:
    """``both-v2-present`` | ``v1-only`` | ``partial`` | (raises) unrecoverable."""
    op_exists = operational_out.exists()
    auth_exists = authority_out.exists()
    if op_exists and auth_exists:
        return "both-v2-present"
    if v1_path.exists():
        return "v1-only" if not (op_exists or auth_exists) else "partial"
    raise MigrationError(
        f"unrecoverable state: v1 file {v1_path} is absent and the v2 pair is "
        f"incomplete (operational={op_exists}, authority={auth_exists}). The v1 "
        f"anchor is only renamed after BOTH v2 files land, so this should not "
        f"occur from a normal run — restore {v1_path} to re-migrate."
    )


def _build_split(v1_doc: Any) -> Tuple[CommentedMap, CommentedMap, Dict[str, str]]:
    """Return (operational_doc, authority_doc, disposition) from a loaded v1 doc.

    ``disposition`` maps each censused v1 key to ``"operational"`` /
    ``"authority"`` / ``"dropped"`` — only keys actually present in v1 appear.
    """
    if not isinstance(v1_doc, dict):
        raise MigrationError("v1 routing config did not parse to a mapping")
    routing = v1_doc.get("routing")
    if not isinstance(routing, dict):
        raise MigrationError(
            "v1 routing config has no top-level 'routing:' mapping — not a v1 "
            "file (already migrated, or malformed)"
        )

    operational: CommentedMap = CommentedMap()
    operational["schema_version"] = _V2
    operational["surface_class"] = "in_scope"
    operational["writable_on"] = "autonomous_loop"

    authority: CommentedMap = CommentedMap()
    authority["schema_version"] = _V2
    authority["surface_class"] = "scope_defining"
    authority["writable_on"] = "operator_authenticated"
    authority["default_zone"] = "red"

    disposition: Dict[str, str] = {}

    for key in _OPERATIONAL_FROM_ROUTING:
        if key in routing:
            operational[key] = routing[key]  # moves the value node + its comments
            disposition[key] = "operational"
    for key in _OPERATIONAL_TOPLEVEL:
        if key in v1_doc:
            operational[key] = v1_doc[key]
            disposition[key] = "operational"

    # escalation.threshold → authority.escalation_threshold (flatten). The rest
    # of the v1 ``escalation`` block (prose ``description``) is not load-bearing
    # and is not carried.
    esc = routing.get("escalation")
    if isinstance(esc, dict) and "threshold" in esc:
        authority["escalation_threshold"] = esc["threshold"]
        disposition["escalation_threshold"] = "authority"
    for key in _AUTHORITY_TOPLEVEL:
        if key in v1_doc:
            authority[key] = v1_doc[key]
            disposition[key] = "authority"
    if "escalation_policy" in routing:
        authority["escalation_policy"] = routing["escalation_policy"]
        disposition["escalation_policy"] = "authority"

    for key in _DROPPED_FROM_ROUTING:
        if key in routing:
            disposition[key] = "dropped"

    return operational, authority, disposition


def _dump_to_str(doc: Any, yaml_rt: YAML) -> str:
    """Serialize a split doc to a YAML string via the same ruamel writer the
    migrator uses on disk — so fixtures and the CLI emit byte-comparable v2."""
    buf = StringIO()
    yaml_rt.dump(doc, buf)
    return buf.getvalue()


def to_v2_split(v1: Union[str, Dict[str, Any]]) -> Tuple[str, str]:
    """The single reusable v1→v2 split transform (routing-v2-migration-v1, 2b).

    Shared by the migration CLI and by test fixtures so NO caller hand-sorts
    keys — the census in :func:`_build_split` is the sole source of the split.
    Accepts a loaded v1 mapping (a top-level ``routing:`` block) or a v1 YAML
    string, and returns the ``(operational_yaml, authority_yaml)`` text pair,
    each serialized through the migrator's ruamel writer. Only keys actually
    present in the input are carried; ``zone_overrides`` is dropped (§VII).
    """
    yaml_rt = _ruamel()
    doc = yaml_rt.load(v1) if isinstance(v1, str) else v1
    operational, authority, _disposition = _build_split(doc)
    return _dump_to_str(operational, yaml_rt), _dump_to_str(authority, yaml_rt)


def _write_tmp_fsync(doc: Any, out_path: Path, yaml_rt: YAML) -> Path:
    """Write ``doc`` to ``out_path``'s ``.tmp`` sibling, flush + fsync, return it."""
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml_rt.dump(doc, fh)
        fh.flush()
        os.fsync(fh.fileno())
    return tmp


def migrate_v1_to_v2(
    v1_path: Path,
    operational_out: Path,
    authority_out: Path,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Split a v1 routing config into the v2 operational + authority pair.

    Idempotent and crash-safe. First act is state detection: ``both-v2-present``
    → no-op; ``v1-only`` / ``partial`` → (re-)migrate from the intact v1;
    unrecoverable (v1 gone, pair incomplete) → :class:`MigrationError`.

    Returns a report: ``state``, ``disposition`` (key → destination), ``wrote``,
    ``operational_out``, ``authority_out``, and ``v1_backup`` (the ``.bak_*``
    path, or ``None`` for dry-run / no-op).
    """
    v1_path = Path(v1_path)
    operational_out = Path(operational_out)
    authority_out = Path(authority_out)

    state = _detect_state(v1_path, operational_out, authority_out)

    report: Dict[str, Any] = {
        "state": state,
        "disposition": {},
        "wrote": False,
        "operational_out": str(operational_out),
        "authority_out": str(authority_out),
        "v1_backup": None,
    }

    if state == "both-v2-present":
        # Already migrated — no-op. Recompute disposition only if v1 lingers.
        if v1_path.exists():
            try:
                yaml_rt = _ruamel()
                with open(v1_path, encoding="utf-8") as fh:
                    v1_doc = yaml_rt.load(fh)
                _, _, disposition = _build_split(v1_doc)
                report["disposition"] = disposition
            except Exception:  # noqa: BLE001 — reporting leg only
                pass
        return report

    # v1-only or partial: (re-)migrate from the intact v1.
    yaml_rt = _ruamel()
    with open(v1_path, encoding="utf-8") as fh:
        v1_doc = yaml_rt.load(fh)
    operational_doc, authority_doc, disposition = _build_split(v1_doc)
    report["disposition"] = disposition

    if dry_run:
        return report

    # write both .tmp + fsync, THEN replace both, THEN move v1 → .bak
    op_tmp = _write_tmp_fsync(operational_doc, operational_out, yaml_rt)
    auth_tmp = _write_tmp_fsync(authority_doc, authority_out, yaml_rt)

    os.replace(op_tmp, operational_out)
    os.replace(auth_tmp, authority_out)  # partial-crash boundary: v1 still intact

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = v1_path.with_name(v1_path.name + f".bak_{stamp}")
    os.replace(v1_path, backup)

    report["wrote"] = True
    report["v1_backup"] = str(backup)
    return report


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m grove.config.routing_migrate",
        description="Migrate a v1 routing.config.yaml to the GRV-001 v2.0 "
        "operational + authority split.",
    )
    p.add_argument("v1_path", help="path to the v1 routing.config.yaml")
    p.add_argument(
        "--operational-out",
        default=None,
        help="output path for routing.operational.yaml "
        "(default: sibling of v1_path)",
    )
    p.add_argument(
        "--authority-out",
        default=None,
        help="output path for routing.authority.yaml "
        "(default: sibling of v1_path)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the disposition without writing anything",
    )
    return p


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    v1_path = Path(args.v1_path)
    operational_out = (
        Path(args.operational_out)
        if args.operational_out
        else v1_path.with_name("routing.operational.yaml")
    )
    authority_out = (
        Path(args.authority_out)
        if args.authority_out
        else v1_path.with_name("routing.authority.yaml")
    )

    report = migrate_v1_to_v2(
        v1_path, operational_out, authority_out, dry_run=args.dry_run
    )

    print(f"state: {report['state']}")
    for key in sorted(report["disposition"]):
        print(f"  {key:24s} -> {report['disposition'][key]}")
    if args.dry_run:
        print("dry-run: nothing written")
    elif report["wrote"]:
        print(f"wrote: {report['operational_out']}")
        print(f"wrote: {report['authority_out']}")
        print(f"v1 backed up: {report['v1_backup']}")
    else:
        print("no-op (already migrated)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

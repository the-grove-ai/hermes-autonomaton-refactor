"""zones-v2-scope-keying P2 — operator overlay migration.

Prunes the retired category-keyed content from the operator overlay
(``~/.grove/zones.autonomaton.yaml``) so the v2 loader accepts it. Under v2 the
overlay's only valid content is ``red_denied_by_policy`` (+ ``schema_version``);
the entire ``tool_zones`` block (bare ``default_zone`` promotions + the
``terminal.rules`` list) is retired — its writer (``grove.zone_rules``) and
reader (``classify_command_string``) are deleted, so every entry is inert.

One operator-facing command (``python -m grove.config.zones_overlay_migrate``).
Steps, in order (atomic where possible):

    (a) ``.bak`` the overlay (timestamped; the recovery anchor).
    (b) prune the entire ``tool_zones`` block.
    (c) stamp ``schema_version: 2``.
    (d) dismiss pending ``zone_promotion`` proposals (receipted reason:
        "writer retired: zones-v2-scope-keying P2").
    (e) emit the PRUNE REPORT — every pruned entry with its status.

Refusals: a malformed overlay (unparseable YAML / non-mapping) raises loud and
performs ZERO writes (absent ≠ malformed — an absent overlay is nothing-to-do).
Idempotent: a re-run over an already-migrated overlay (no ``tool_zones`` key)
writes nothing and reports nothing-to-do; the file is byte-identical.

admin.py precedent: tmp + ``os.replace`` atomic write; the ``.bak`` is written
before the replace so a partial failure re-runs cleanly from the backup.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# The v2 loader's valid overlay key surface (kept in sync with grove.zones).
_VALID_OVERLAY_KEYS = frozenset({"schema_version", "red_denied_by_policy"})
_DISMISS_REASON = "writer retired: zones-v2-scope-keying P2"
_DEAD_READER_CITE = (
    "terminal.rules reader classify_command_string deleted (zones-v2 P2); inert"
)


class OverlayMigrationError(Exception):
    """A malformed overlay was encountered — refuse loud, zero writes."""


@dataclass
class PruneReport:
    """The receipt of a migration run — evidence, not estimate."""

    overlay_path: str
    status: str  # "migrated" | "nothing-to-do" | "absent"
    backup_path: Optional[str] = None
    pruned_terminal_rules: List[Dict[str, str]] = field(default_factory=list)
    pruned_default_zone_promotions: List[Dict[str, str]] = field(default_factory=list)
    dismissed_proposals: List[Dict[str, str]] = field(default_factory=list)
    pre_hash: Optional[str] = None
    post_hash: Optional[str] = None

    def render(self) -> str:
        lines = [
            f"zones-v2 overlay migration — {self.status}",
            f"  overlay: {self.overlay_path}",
        ]
        if self.backup_path:
            lines.append(f"  backup:  {self.backup_path}")
        if self.status == "nothing-to-do":
            lines.append("  (no tool_zones block present — already v2-conformant)")
            return "\n".join(lines)
        if self.status == "absent":
            lines.append("  (no overlay file — repo policy only)")
            return "\n".join(lines)
        lines.append(
            f"  pruned {len(self.pruned_default_zone_promotions)} default_zone "
            f"promotion(s) + {len(self.pruned_terminal_rules)} terminal.rules"
        )
        if self.pruned_default_zone_promotions:
            lines.append("  ── default_zone promotions (tool · overlay-zone · v2-derived) ──")
            for p in self.pruned_default_zone_promotions:
                lines.append(
                    f"    {p['tool']}: overlay={p['overlay_zone']} "
                    f"→ v2-derived={p['v2_derived_zone']}"
                )
        if self.pruned_terminal_rules:
            lines.append(f"  ── terminal.rules ({len(self.pruned_terminal_rules)}) — {_DEAD_READER_CITE} ──")
            for r in self.pruned_terminal_rules:
                lines.append(f"    {r['zone']}: {r['match_pattern']}")
        if self.dismissed_proposals:
            lines.append(f"  ── dismissed {len(self.dismissed_proposals)} pending zone_promotion proposal(s) ──")
            for d in self.dismissed_proposals:
                lines.append(f"    {d['proposal_id']} — {d['reason']}")
        lines.append(f"  pre_hash={self.pre_hash} post_hash={self.post_hash}")
        return "\n".join(lines)


def _overlay_default_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "zones.autonomaton.yaml"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


_repo_classifier: Any = None


def _v2_derived_zone(tool: str) -> str:
    """The tool's zone under the v2 repo schema (or the declared default if the
    tool has no v2 tool_effects entry).

    Classifies against a COPY of the repo schema placed at a non-repo path so the
    ZoneClassifier does NOT load the operator overlay — the overlay being migrated
    is the legacy (retired-key) one, which the v2 loader would refuse (A3). This
    keeps the migration's report generation independent of the overlay it prunes.
    Built once and cached."""
    global _repo_classifier
    if _repo_classifier is None:
        import shutil
        import tempfile

        from grove.zones import ZoneClassifier, _resolve_schema_path

        tmpdir = tempfile.mkdtemp(prefix="zones_migrate_")
        repo_copy = Path(tmpdir) / "zones.schema.yaml"
        shutil.copy(_resolve_schema_path(None), repo_copy)
        _repo_classifier = ZoneClassifier(repo_copy)
    return _repo_classifier.classify(tool).zone


def _dismiss_zone_promotion_proposals(
    queue_path: Optional[Path], *, dry_run: bool,
) -> List[Dict[str, str]]:
    """Dismiss every pending ``zone_promotion`` proposal; return the receipts."""
    try:
        from grove.eval import proposal_queue as pq
    except Exception:  # noqa: BLE001 — queue subsystem absent → nothing to dismiss
        return []
    dismissed: List[Dict[str, str]] = []
    try:
        pending = pq.read_all(path=queue_path)
    except Exception:  # noqa: BLE001 — an unreadable queue is not this tool's failure
        return []
    for prop in pending:
        if getattr(prop, "type", None) == "zone_promotion":
            pid = getattr(prop, "proposal_id", None) or getattr(prop, "id", "?")
            if not dry_run:
                try:
                    pq.remove(pid, path=queue_path)
                except Exception:  # noqa: BLE001
                    continue
            dismissed.append({"proposal_id": str(pid), "reason": _DISMISS_REASON})
    return dismissed


def migrate_overlay(
    overlay_path: Optional[Path] = None,
    *,
    queue_path: Optional[Path] = None,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> PruneReport:
    """Migrate the operator overlay to the v2 valid-key surface. See module docstring."""
    path = Path(overlay_path) if overlay_path is not None else _overlay_default_path()
    report = PruneReport(overlay_path=str(path), status="absent")

    if not path.exists():
        return report  # absent ≠ malformed — nothing to do

    raw_bytes = path.read_bytes()
    report.pre_hash = _sha256(raw_bytes)
    try:
        data = yaml.safe_load(raw_bytes.decode("utf-8"))
    except yaml.YAMLError as exc:
        raise OverlayMigrationError(
            f"overlay at {path} is not valid YAML ({exc}) — REFUSING to migrate "
            f"(malformed ≠ absent; zero writes). Repair the YAML by hand first."
        ) from exc
    if data is None:
        report.status = "nothing-to-do"
        report.post_hash = report.pre_hash
        return report
    if not isinstance(data, dict):
        raise OverlayMigrationError(
            f"overlay at {path} did not parse to a mapping — REFUSING to migrate "
            f"(zero writes). Repair the file by hand first."
        )

    tool_zones = data.get("tool_zones")
    if not tool_zones:
        # Already v2-conformant (no retired block). Idempotent no-op — dismiss any
        # lingering pending proposals, but write nothing to the overlay.
        report.status = "nothing-to-do"
        report.post_hash = report.pre_hash
        report.dismissed_proposals = _dismiss_zone_promotion_proposals(
            queue_path, dry_run=dry_run,
        )
        return report

    # (e-pre) Enumerate the prune set for the report BEFORE mutating.
    if isinstance(tool_zones, dict):
        for tool, entry in tool_zones.items():
            if tool == "terminal" and isinstance(entry, dict):
                for rule in entry.get("rules", []) or []:
                    if isinstance(rule, dict):
                        report.pruned_terminal_rules.append({
                            "match_pattern": str(rule.get("match_pattern", "?")),
                            "zone": str(rule.get("zone", "?")),
                        })
                continue
            overlay_zone = (
                entry.get("default_zone") if isinstance(entry, dict) else str(entry)
            )
            report.pruned_default_zone_promotions.append({
                "tool": str(tool),
                "overlay_zone": str(overlay_zone),
                "v2_derived_zone": _v2_derived_zone(str(tool)),
            })

    # (a-c) Prune tool_zones, stamp v2, preserve red_denied_by_policy.
    migrated = {"schema_version": 2}
    if "red_denied_by_policy" in data:
        migrated["red_denied_by_policy"] = data["red_denied_by_policy"]
    # Refuse to silently drop any OTHER unexpected key (a surprise key is not
    # something to guess about) — surface it loud.
    unexpected = set(data) - _VALID_OVERLAY_KEYS - {"tool_zones"}
    if unexpected:
        raise OverlayMigrationError(
            f"overlay at {path} carries unexpected key(s) {sorted(unexpected)} the "
            f"migration will not silently drop — resolve by hand (valid keys: "
            f"{sorted(_VALID_OVERLAY_KEYS)})."
        )

    new_bytes = yaml.safe_dump(migrated, sort_keys=True).encode("utf-8")
    report.post_hash = _sha256(new_bytes)

    if not dry_run:
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
        backup = path.with_suffix(f".yaml.bak_{stamp}")
        backup.write_bytes(raw_bytes)  # recovery anchor, written BEFORE replace
        report.backup_path = str(backup)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_bytes(new_bytes)
        os.replace(tmp, path)

    # (d) Dismiss pending zone_promotion proposals.
    report.dismissed_proposals = _dismiss_zone_promotion_proposals(
        queue_path, dry_run=dry_run,
    )
    report.status = "migrated"
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m grove.config.zones_overlay_migrate",
        description="Migrate the operator zones overlay to the v2 valid-key surface.",
    )
    parser.add_argument(
        "--overlay", type=Path, default=None,
        help="Overlay path (default: $GROVE_HOME/zones.autonomaton.yaml).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report the prune set without writing (no .bak, no dismissal).",
    )
    args = parser.parse_args(argv)
    try:
        report = migrate_overlay(args.overlay, dry_run=args.dry_run)
    except OverlayMigrationError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(report.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
